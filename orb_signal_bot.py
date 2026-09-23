#!/usr/bin/env python3
"""
Cronus Signals — ORB 15m Live Signal Bot
=========================================

Implements the "best pooled profit factor" configuration found in the
864-combo optimization study (see /docs/optimization-study.md in this repo,
or the companion project doc "orb-15m-optimization-study.md"):

    Session anchor : 08:30 ET  (US macro-data print time, NOT the 9:30 cash open)
    Opening range  : 60 minutes
    Entry trigger  : "wick"    (stop-order fill the instant price touches the
                                 OR level — not a close-confirmation)
    Stop           : opposite side of the opening range (no buffer)
    Target         : 3.0R (single fixed target)
    Direction      : short_only

Backtested result (CME_MINI ES1!/NQ1!, ~5-6 months of 15m data, 1 contract,
no commissions/slippage):

    Symbol   Trades  WinRate   R:R    PF     Expectancy   Net $
    ES       44      36.4%     2.38   1.36   +0.219R      -$3,913
    NQ       35      45.7%     2.32   1.96   +0.474R      +$36,005
    Pooled   79      40.5%     2.34   1.59   +0.332R      +$32,093

*** READ BEFORE RUNNING LIVE ***
  - This is the best POOLED (ES+NQ combined) configuration, not the safest
    per-symbol one. ES alone is a net LOSER in dollar terms on this exact
    config over the backtested sample (positive R-expectancy, negative $,
    because ES's opening-range width in points is small relative to its
    $50/point value). It only nets positive because NQ's edge more than
    covers it. Only run both legs if you're treating them as one combined
    book with shared risk capital — if you want ES and NQ to each stand on
    their own, use the "Tier 1" config from the optimization study instead
    (09:30 ET / OR60m / close-trigger / opposite stop / TP1.0R / short_only).
  - Backtested on ~5-6 months / 35-44 trades per symbol. That is a thin
    sample by professional standards (500+ trades is the usual bar before
    real capital). Treat this as a validated hypothesis, not a proven edge.
  - The short-only bias was discovered during a rally (ES +19%, NQ +12%
    over the backtest window). Re-validate if the market's character changes.
  - No slippage or commissions are modeled. The wick/stop-order entry trigger
    assumes a perfect fill at the OR breakout level; expect some slippage on
    fast breakouts in live trading.
  - ES and NQ are highly correlated (~0.9) — running both is not real
    diversification, it's one signal expressed twice.

Environment variables required:
    TELEGRAM_BOT_TOKEN   - Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID     - Telegram chat/channel ID to post signals to

Designed to run on a polling schedule (GitHub Actions cron, every 15 min)
against fresh 15-minute candles from yfinance. State is persisted to
orb_state.json (committed back to the repo by the workflow) so a symbol
only fires once per session.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("cronus-orb")

# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------
# One entry per symbol you want the bot to watch. Each symbol carries its own
# copy of the strategy parameters so you can tune them independently later
# (e.g. if you decide to run ES on the safer Tier-1 config instead).

SYMBOLS = [
    {
        "ticker": "NQ=F",
        "label": "NQ",
        "tz": "America/New_York",
        "session_open": time(8, 30),
        "or_minutes": 60,
        "entry_trigger": "wick",      # "wick" (stop-order) or "close" (confirmed close)
        "stop_mode": "opposite",      # "opposite" | "buffer10" | "buffer25"
        "tp_R": 3.0,
        "direction": "short_only",    # "both" | "long_only" | "short_only"
    },
    {
        "ticker": "ES=F",
        "label": "ES",
        "tz": "America/New_York",
        "session_open": time(8, 30),
        "or_minutes": 60,
        "entry_trigger": "wick",
        "stop_mode": "opposite",
        "tp_R": 3.0,
        "direction": "short_only",
    },
]

STATE_FILE = os.environ.get("ORB_STATE_FILE", "orb_state.json")
STOP_BUFFERS = {"opposite": 0.0, "buffer10": 0.10, "buffer25": 0.25}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    ticker: str
    label: str
    direction: str          # "LONG" or "SHORT"
    entry: float
    stop: float
    target: float
    tp_R: float
    or_high: float
    or_low: float
    session_date: str
    timestamp: str


# ---------------------------------------------------------------------------
# State (prevents duplicate signals within the same session)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state file (%s) — starting fresh.", exc)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def already_fired(state: dict, ticker: str, session_date: str) -> bool:
    return state.get(ticker) == session_date


def mark_fired(state: dict, ticker: str, session_date: str) -> None:
    state[ticker] = session_date


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_15m_candles(ticker: str, tz: str):
    """Pull the last 2 days of 15m candles, converted to the symbol's local tz."""
    try:
        df = yf.Ticker(ticker).history(period="2d", interval="15m")
    except Exception as exc:  # yfinance can raise a range of network/parse errors
        log.error("[%s] fetch failed: %s", ticker, exc)
        return None
    if df is None or df.empty:
        return df
    return df.tz_convert(ZoneInfo(tz))


# ---------------------------------------------------------------------------
# Core ORB logic
# ---------------------------------------------------------------------------

def get_opening_range(df, session_open: time, or_minutes: int, tz: str):
    """Return (or_high, or_low, session_date) for today's session, or None if
    the opening-range window hasn't fully printed yet (or no data for it)."""
    now = datetime.now(ZoneInfo(tz))
    today = now.date()

    or_start = datetime.combine(today, session_open, tzinfo=ZoneInfo(tz))
    or_end = or_start + timedelta(minutes=or_minutes)

    if now < or_end:
        return None  # opening range window hasn't fully printed yet

    window = df[(df.index >= or_start) & (df.index < or_end)]
    if window.empty:
        return None

    return float(window["High"].max()), float(window["Low"].min()), str(today)


def check_entry(df, or_high: float, or_low: float, session_open: time,
                 or_minutes: int, tz: str, entry_trigger: str, stop_mode: str,
                 tp_R: float, direction: str):
    """Scan every completed 15m bar since the opening range closed for the
    first qualifying breakout, and compute its stop/target. Mirrors the
    backtest engine's simulate_combo() bar-by-bar logic exactly."""
    now = datetime.now(ZoneInfo(tz))
    today = now.date()
    or_end = datetime.combine(today, session_open, tzinfo=ZoneInfo(tz)) + timedelta(minutes=or_minutes)

    post_range = df[df.index >= or_end]
    if post_range.empty:
        return None

    risk0 = or_high - or_low
    if risk0 <= 0:
        return None
    buf = STOP_BUFFERS[stop_mode]

    for _, bar in post_range.iterrows():
        h, l, c = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        dirn, entry_price = None, None

        if entry_trigger == "close":
            if c > or_high:
                dirn, entry_price = "LONG", c
            elif c < or_low:
                dirn, entry_price = "SHORT", c
        else:  # "wick" — stop-order fill at the OR level itself
            if h > or_high:
                dirn, entry_price = "LONG", or_high
            elif l < or_low:
                dirn, entry_price = "SHORT", or_low

        if dirn is None:
            continue
        if direction == "long_only" and dirn != "LONG":
            continue
        if direction == "short_only" and dirn != "SHORT":
            continue

        stop = (or_low - buf * risk0) if dirn == "LONG" else (or_high + buf * risk0)
        risk = abs(entry_price - stop)
        if risk <= 0:
            continue
        target = entry_price + tp_R * risk if dirn == "LONG" else entry_price - tp_R * risk
        return dirn, entry_price, stop, target

    return None


def build_signal(cfg: dict, direction: str, entry: float, stop: float, target: float,
                  or_high: float, or_low: float, session_date: str) -> Signal:
    return Signal(
        ticker=cfg["ticker"],
        label=cfg["label"],
        direction=direction,
        entry=round(entry, 5),
        stop=round(stop, 5),
        target=round(target, 5),
        tp_R=cfg["tp_R"],
        or_high=round(or_high, 5),
        or_low=round(or_low, 5),
        session_date=session_date,
        timestamp=datetime.utcnow().isoformat(timespec="seconds") + "Z",
    )


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def format_message(sig: Signal) -> str:
    emoji = "\U0001F7E2" if sig.direction == "LONG" else "\U0001F534"
    risk_pts = abs(sig.entry - sig.stop)
    return (
        f"{emoji} *ORB 15m Signal — {sig.label}*\n"
        f"Direction: *{sig.direction}*\n"
        f"Entry: `{sig.entry}`\n"
        f"Stop: `{sig.stop}`\n"
        f"Target ({sig.tp_R:g}R): `{sig.target}`\n"
        f"Risk: `{round(risk_pts, 2)}` pts\n"
        f"Opening Range: `{sig.or_low} – {sig.or_high}`\n"
        f"Session: {sig.session_date}"
    )


def send_telegram(message: str) -> None:
    if DRY_RUN:
        log.info("[dry-run] would send:\n%s", message)
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead of sending.")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> int:
    state = load_state()
    signals_sent = 0

    for cfg in SYMBOLS:
        ticker, label = cfg["ticker"], cfg["label"]
        tz = cfg["tz"]

        df = fetch_15m_candles(ticker, tz)
        if df is None or df.empty:
            log.info("[skip] %s: no data returned", label)
            continue

        rng = get_opening_range(df, cfg["session_open"], cfg["or_minutes"], tz)
        if rng is None:
            log.info("[skip] %s: opening range not complete yet", label)
            continue
        or_high, or_low, session_date = rng

        if already_fired(state, ticker, session_date):
            log.info("[skip] %s: signal already sent for %s", label, session_date)
            continue

        result = check_entry(
            df, or_high, or_low, cfg["session_open"], cfg["or_minutes"], tz,
            cfg["entry_trigger"], cfg["stop_mode"], cfg["tp_R"], cfg["direction"],
        )
        if result is None:
            log.info("[skip] %s: no qualifying breakout yet (range %.2f-%.2f)", label, or_low, or_high)
            continue

        direction, entry, stop, target = result
        sig = build_signal(cfg, direction, entry, stop, target, or_high, or_low, session_date)

        try:
            send_telegram(format_message(sig))
        except Exception:
            log.error("[%s] failed to deliver signal — will retry next run (state not marked).", label)
            continue

        mark_fired(state, ticker, session_date)
        signals_sent += 1
        log.info("[signal] %s: %s @ %s (stop %s, target %s)", label, direction, entry, stop, target)

    save_state(state)
    log.info("Run complete. %d signal(s) sent.", signals_sent)
    return 0


if __name__ == "__main__":
    sys.exit(run())
