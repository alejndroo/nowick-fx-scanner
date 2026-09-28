# Nowick MT5 Auto-Trader — Quick Setup (Windows PC)

Scans all 27 pairs with the Nowick signal engine (`engine.py`, kept
byte-identical to the TradingView indicator's logic) and executes real
trades directly on your connected MT5 account.

## 1. One-time setup (5 minutes)

1. Install **MetaTrader 5** on this Windows PC and log into your **real
   account** (the one you want traded). Leave it open and logged in.
2. In MT5: click **Tools -> Options -> Expert Advisors** and check
   **"Allow automated trading"**. Also click the **AutoTrading** button in
   the toolbar so it's green/on.
3. Install Python 3.10+ from python.org if you don't have it (check "Add
   to PATH" during install).
4. Put this whole folder somewhere on the PC, e.g. `C:\NowickBot\`.
5. Open Command Prompt in that folder and run:
   ```
   pip install -r requirements.txt
   ```

## 2. Configure

Copy `config.example.py` to `config.py` (that copy is gitignored — never
committed, never shared, since it holds your real MT5 login) and fill in:
- `MT5_LOGIN` — your account number
- `MT5_PASSWORD` — your account's **trade password**
- `MT5_SERVER` — exact server name (right-click your account in the
  Navigator panel in MT5 -> it's shown there, e.g. `ICMarketsSC-Live01`)
- `SYMBOL_SUFFIX` — leave blank unless your broker's Market Watch shows
  pairs like `EURUSD.m` instead of plain `EURUSD` — check there first
- `RISK_PCT_MIN` / `RISK_PCT_MAX` — the band the planner picks a risk %
  from (currently 15-20%), based on the recent win/loss streak — never a
  random draw, and can be overridden live from the dashboard
- `MAX_OPEN_TRADES` — cap on simultaneous positions (currently 6)
- `AGGREGATE_RISK_CAP_PCT` — hard cap on the TOTAL worst-case loss across
  every open position at once (currently 50% of equity) — this exists
  because per-trade risk alone doesn't stop several correlated pairs from
  each risking the max at the same time
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional, execution
  confirmations sent to Telegram

## 2b. Connect it to the iPhone dashboard (live, no manual steps)

This bot pushes every trade, your balance, and P&L straight to Firebase —
the same database the dashboard app reads — every ~1 second. No manual
confirmation step; the dashboard just mirrors your real account, including
a live "AI Insights" card showing the planner's current risk %, any
paused pairs, and whether the daily loss limit has triggered.

1. Firebase console -> gear icon -> **Project settings** -> **Service accounts**
2. Click **Generate new private key** -> confirm -> a `.json` file downloads
3. Rename it to `firebase-service-account.json` and put it in this same
   folder (`C:\NowickBot\`), next to `bot.py`
4. `config.py`'s `FIREBASE_DB_URL` is already set correctly — leave it

**Never share that JSON file or commit it anywhere.**

## 3. Run it

```
python bot.py
```

Leave that window open — it prints a heartbeat line every ~60 seconds
(pairs scanned, open positions, connection status, current risk %) as
proof it's genuinely running, not just idling silently. It:
- Scans all 27 pairs every 60 seconds, using the identical Nowick logic
- On a brand-new signal, sizes the position so a full stop-loss loss
  equals the planner's current risk % of equity, then opens the trade
  with SL/TP at the same risk distance and 1:1 reward the indicator uses
- Refuses new trades once `MAX_OPEN_TRADES` are open, or once the
  aggregate risk cap would be exceeded, or once the daily loss circuit
  breaker has tripped for the day
- **Does NOT force-close positions overnight** — open trades run to their
  own SL/TP regardless of time. Only NEW entries are time-gated (07:00-
  20:45 UTC, the engine's own session filter).
- Logs every trade to a permanent local journal (`mt5_journal.json`) that
  a planner (`planner.py`) reads every cycle to adjust risk and pause
  underperforming pairs — see `mt5_journal.json` and the dashboard's AI
  Insights card for what it's actually doing and why
- On first launch, it silently learns each pair's current trend/structure
  from history — it will NOT fire trades from that backfill, only from
  genuinely new signals going forward

## Testing before you trust it

- `python test_bot.py signals` — free, read-only: shows real historical
  signals across all 27 pairs
- `python test_bot.py trade EUR_USD BUY` — places one real minimum-lot
  trade and closes ONLY that ticket, proving the execution path works
- `python verify_all_pairs.py scan` — free: proves candle-fetch access
  works for every pair individually
- `python verify_all_pairs.py execute` — real money, tiny: proves the
  execution path per-pair, not just for whichever pair you spot-checked
- `python tests/test_engine.py` — a real, executable unit test suite for
  the signal-detection logic itself (trend flips, Nowick detection,
  retest math, session filtering, seed monotonicity), run directly against
  the production `engine.py`, no MT5 connection needed

## Notes

- This must stay running on this PC — closing the window stops the bot.
  Open positions' SL/TP are broker-side orders and stay protected even if
  this process isn't running; only new entries and the dashboard sync
  depend on it being up.
- Every order this bot places is tagged with a magic number (990033), so
  it will never touch, count, or close trades you open manually.
- `mt5_broker.close_all()` force-flattens every position this bot opened —
  it has no callers in the normal run loop anymore (no-overnight force-
  close was removed). It still exists as a manual emergency tool; be
  deliberate before calling it, since running it while real trades are
  open closes ALL of them, not just a specific one (use
  `mt5_broker.close_ticket(ticket)` for anything that should only touch
  one position).
