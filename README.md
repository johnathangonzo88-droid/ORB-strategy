# Cronus Signals — ORB 15m Live Signal Bot

Live Telegram signal bot for the 15-minute Opening Range Breakout (ORB)
strategy, running the best **pooled** configuration found in a 864-combo
optimization study over CME_MINI ES1!/NQ1! futures:

```
Session anchor : 08:30 ET   (US macro-data print time, not the 9:30 cash open)
Opening range  : 60 minutes
Entry trigger  : wick        (stop-order fill the instant price touches the OR level)
Stop           : opposite side of the opening range (no buffer)
Target         : 3.0R
Direction      : short_only
Symbols        : NQ=F, ES=F
```

Backtested performance (1 contract, no commissions/slippage, ~5-6 months of data):

| Symbol | Trades | Win Rate | R:R | PF | Expectancy | Net $ |
|---|---|---|---|---|---|---|
| ES | 44 | 36.4% | 2.38 | 1.36 | +0.219R | **-$3,913** |
| NQ | 35 | 45.7% | 2.32 | 1.96 | +0.474R | +$36,005 |
| Pooled | 79 | 40.5% | 2.34 | 1.59 | +0.332R | **+$32,093** |

## ⚠️ Read before running this live

- **This is the best pooled (ES+NQ combined) config, not the safest per-symbol one.** ES alone loses money in dollar terms over the backtest despite a positive R-expectancy — the pooled profit is carried by NQ. Only run both legs if you're treating them as one book with shared risk capital. If you want each symbol to stand on its own, swap in the "Tier 1" config instead: `09:30 ET / OR60m / close-trigger / opposite stop / TP1.0R / short_only` (smaller edge, ~+$8-13k/symbol, but profitable on each leg independently).
- **Thin backtest sample.** 35-44 trades per symbol over ~5-6 months. Re-validate as more data accumulates before sizing up.
- **Short-only bias found during a rally** (ES +19%, NQ +12% over the sample). Re-test if market character changes.
- **No slippage/commissions modeled.** The wick/stop-order entry assumes a perfect fill at the breakout level.
- **ES and NQ are correlated (~0.9)** — this isn't true diversification.

## Setup

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and note the bot token.
2. Get your chat/channel ID (send a message to the bot, then hit `https://api.telegram.org/bot<TOKEN>/getUpdates`).
3. In your GitHub repo: **Settings → Secrets and variables → Actions**, add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Push this repo. The `ORB Live Signals` workflow runs automatically every 15 minutes on weekdays (`.github/workflows/orb_signals.yml`).
5. To test without sending real Telegram messages, run the workflow manually (**Actions → ORB Live Signals → Run workflow**) with `dry_run: true`, or locally with `DRY_RUN=1 python orb_signal_bot.py`.

## Running locally

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=xxxx
export TELEGRAM_CHAT_ID=xxxx
python orb_signal_bot.py
```

## Files

| File | Purpose |
|---|---|
| `orb_signal_bot.py` | Main bot — fetches 15m candles, checks for a qualifying breakout, sends a Telegram alert, tracks state so each symbol fires once per session. |
| `orb_state.json` | Auto-generated/committed by the workflow — tracks which symbol already fired today. Don't edit by hand. |
| `.github/workflows/orb_signals.yml` | Scheduled GitHub Actions workflow — polls every 15 min, commits state back to the repo. |
| `requirements.txt` | Python dependencies. |

## Changing the configuration

Edit the `SYMBOLS` list at the top of `orb_signal_bot.py`. Each symbol carries
its own independent copy of `session_open`, `or_minutes`, `entry_trigger`,
`stop_mode`, `tp_R`, and `direction`, so you can run ES on a different
(e.g. Tier 1) config than NQ if you want each leg to be independently
profitable rather than relying on the pooled result.

## Next steps recommended by the optimization study

- Extend the take-profit grid past 3R for this family — expectancy was still
  climbing at 3R in the backtest.
- Add a trend/regime filter (e.g. yesterday's range expansion, or a
  longer-timeframe moving-average slope) — the single highest-value addition
  found in the study, since ranging days had ~2-4x worse expectancy than
  trending days across every configuration tested.
- Implement real partial exits (bank at 1R, move stop to breakeven, let the
  rest run) instead of a single fixed target.
- Validate on an uncorrelated third market before increasing size — ES and
  NQ alone are not a true robustness test.
