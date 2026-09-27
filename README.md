# Nowick FX Scanner

A free, 24/7 background scanner that replicates the **Bard.FX Nowick Signal
Engine** TradingView indicator's exact logic for all 27 forex pairs, and
pushes BUY/SELL/SL/TP alerts straight to Telegram — no TradingView window,
no always-on Mac, no paid hosting.

## How it works

- **GitHub Actions** runs `main.py` every 15 minutes, 24/7, on GitHub's own
  servers (this repo is public, so Actions minutes are unlimited and free).
- Each run fetches fresh 15-minute and 1-hour candles from **OANDA's free
  practice API** — the same feed TradingView uses for `OANDA:` symbols — for
  all 27 pairs.
- `engine.py` replays the identical trend/Nowick/retest/SL/TP math as the
  Pine indicator, from scratch, every run (no fragile state between runs).
- Any brand-new signal gets sent to your Telegram immediately. A small
  `state.json` (committed back to the repo automatically) remembers what's
  already been sent, so you never get duplicates.
- The same run also checks for any Telegram commands you've sent
  (`/status`, `/pause`, etc.) and replies — see below. Since it only checks
  every 15 minutes, command replies can take up to that long.

## One-time setup (the only manual step left)

The bot needs a free **OANDA practice account** for market data. This takes
about 3 minutes and needs no credit card:

1. Go to https://www.oanda.com/register/#/sign-up/demo and create a free
   **practice/demo** account (any country, no card required).
2. Log in, then go to **"Manage API Access"** in your account settings
   (or https://www.oanda.com/demo-account/tpa/personal_token) and generate
   a **Personal Access Token**.
3. Add it to this repo as a secret:
   ```bash
   gh secret set OANDA_API_KEY --body "PASTE_YOUR_TOKEN_HERE" --repo alejndroo/nowick-fx-scanner
   ```
   (or via GitHub.com → this repo → Settings → Secrets and variables →
   Actions → New repository secret)

That's it. The Telegram bot token, chat ID, and OANDA environment are
already configured as repo secrets. Once `OANDA_API_KEY` is added, the next
scheduled run (within 15 minutes) will start working — or trigger it
immediately:

```bash
gh workflow run scan.yml --repo alejndroo/nowick-fx-scanner
```

## Telegram commands

Message your bot (`@fxsignlsbot`) any of these any time:

| Command | What it does |
|---|---|
| `/status` | Current trend (bullish/bearish) and any pending retest setups, across all 27 pairs |
| `/pairs` | List the 27 monitored pairs |
| `/pause` | Stop sending new alerts (scanning still runs, just silent) |
| `/resume` | Resume sending alerts |
| `/history` | Last 10 signals sent |
| `/ping` | Confirms the bot is alive and when it last ran |
| `/help` | Show this list |

## Files

- `engine.py` — the exact ported strategy logic (trend, Nowick detection, retest, SL/TP)
- `oanda.py` — OANDA candle-fetching client
- `telegram.py` — sending alerts + handling commands
- `pairs.py` — the 27 monitored pairs
- `main.py` — orchestrates everything, run every 15 min by the workflow
- `.github/workflows/scan.yml` — the schedule
- `state.json` — dedupe memory, pause flag, last snapshot (auto-updated)

## Keeping parameters in sync

If you ever change an input on the TradingView indicator (EMA length, wick
tolerance, retest bars, etc.), update the matching value in `PARAMS` at the
top of `engine.py` so the two stay identical.

## Honest limitation

OANDA's feed is the same source TradingView uses for `OANDA:` symbols, so
this should track the indicator closely. It is still a separate computation
against a separately-fetched candle stream — treat this as the same
strategy, not a mirror of TradingView's exact internal repaint/tick
behavior. Cross-check occasionally against the live indicator.
