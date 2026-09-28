# Nowick FX Scanner — MT5 Auto-Trader

A live-trading bot that replicates the **Bard.FX Nowick Signal Engine**
TradingView indicator's exact logic for all 27 forex pairs, and executes
real trades directly on a MetaTrader5 account — no manual confirmation, no
Telegram-only alerts, fully automated.

> The earlier GitHub-Actions/Telegram-only version of this bot has been
> retired. Everything now lives under [`mt5-bot/`](mt5-bot/) and runs on a
> Windows machine/VPS with MT5 installed.

## How it works

- `mt5-bot/bot.py` runs 24/7 on a Windows PC/VPS with the MT5 terminal open
  and logged into your real account.
- Every ~60 seconds it fetches fresh 15-minute and 1-hour candles directly
  from MT5 for all 27 pairs, and `mt5-bot/engine.py` replays the identical
  trend/Nowick/retest/SL/TP math as the Pine indicator.
- On a genuinely new signal, it sizes the position (risk % of equity,
  live-adjustable from the dashboard) and places a real order via
  `mt5-bot/mt5_broker.py`, with SL/TP attached directly to the order —
  those are broker-side, so they stay protected even if the bot process
  restarts.
- Every ~1 second, `mt5-bot/firebase_push.py` pushes your real account
  balance, equity, open positions, and trade history straight to Firebase —
  the iPhone dashboard (`docs/index.html`) just mirrors that, live, with no
  manual "I took this trade" step.
- All 27 positions can be open simultaneously (`MAX_OPEN_TRADES` in
  `mt5-bot/config.py`), any mix of pairs.
- A no-overnight safety net force-closes every position this bot opened at
  a configured UTC hour, even if SL/TP hasn't hit yet.

See [`mt5-bot/README.md`](mt5-bot/README.md) for the full setup guide.

## Files

- `mt5-bot/engine.py` — the exact ported strategy logic (trend, Nowick detection, retest, SL/TP)
- `mt5-bot/mt5_broker.py` — MT5 candle-fetching, position sizing, order execution
- `mt5-bot/firebase_push.py` — pushes live account/trade state to Firebase for the dashboard
- `mt5-bot/bot.py` — the main loop: scans, trades, syncs
- `mt5-bot/config.py` — account login, risk %, max trades, Firebase config
- `mt5-bot/pairs.py` — the 27 monitored pairs
- `mt5-bot/test_bot.py` — read-only signal check + minimal round-trip execution test
- `docs/index.html` — the iPhone dashboard (installable via Safari → Add to Home Screen)

## Keeping parameters in sync

If you ever change an input on the TradingView indicator (EMA length, wick
tolerance, retest bars, etc.), update the matching value in `PARAMS` at the
top of `mt5-bot/engine.py` so the two stay identical.

## Verified against the live indicator

This was checked directly against the live TradingView chart, not just
unit-tested: debug output was temporarily added to the Pine script to print
its real internal `HBC`/`LBC`/`htfEma`/`trend` values, and this engine's
computation from independently-fetched OANDA data was diffed against it.
Trend matched exactly on 5 different pairs, and three historical BUY
signals matched TradingView's printed Entry/SL/TP to the exact decimal.
(One implementation detail — how `_ema_series` seeds and how
`_htf_bias_lookup` selects the HTF bar — was initially "fixed" based on a
plausible-sounding assumption about Pine semantics, which this same live
comparison proved wrong; see the docstrings in `mt5-bot/engine.py` for the
empirical evidence and why the code is deliberately the "naive" version.)

The live MT5 bot's persisted trend state has separately been cross-checked
against an independent OANDA-based computation of the same engine and
agreed on 22/27 pairs, with the 5 differences fully explained by the
comparison snapshot's age (trend flips over that span are expected) — see
git history for details.
