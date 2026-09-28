# Nowick MT5 Auto-Trader — Quick Setup (Windows PC)

Same signal logic as the live Telegram bot (`engine.py`/`pairs.py` are
copied over unchanged), but this one places real trades on your MT5
account instead of just alerting.

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

Open `config.py` and fill in:
- `MT5_LOGIN` — your account number
- `MT5_PASSWORD` — your account's **trade password**
- `MT5_SERVER` — exact server name (right-click your account in the
  Navigator panel in MT5 -> it's shown there, e.g. `ICMarketsSC-Live01`)
- `SYMBOL_SUFFIX` — leave blank unless your broker's Market Watch shows
  pairs like `EURUSD.m` instead of plain `EURUSD` — check there first
- `RISK_PCT` — currently `0.15` (15% of equity per trade)
- `MAX_OPEN_TRADES` — already set to `3`
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional, paste the same
  values used for the signal bot if you want trade-execution confirmations
  in the same Telegram chat

## 2b. Connect it to the iPhone dashboard (live, no manual steps)

This bot pushes every trade, your balance, and P&L straight to Firebase —
the same database the dashboard app reads — every ~10 seconds. No more
"I took this trade" button; the dashboard just mirrors your real account.

1. Firebase console -> gear icon -> **Project settings** -> **Service accounts**
2. Click **Generate new private key** -> confirm -> a `.json` file downloads
3. Rename it to `firebase-service-account.json` and put it in this same
   folder (`C:\NowickBot\`), next to `bot.py`
4. `config.py`'s `FIREBASE_DB_URL` is already set correctly — leave it

**Never share that JSON file or commit it anywhere** — it's a real
credential for your Firebase project (it is NOT included in this zip/repo).

## 3. Run it

```
python bot.py
```

Leave that window open. It:
- Scans all 27 pairs every 60 seconds, using the identical Nowick logic
- On a brand-new signal, sizes the position so a full stop-loss loss
  equals exactly `RISK_PCT` of your current equity, then opens the trade
  with SL/TP at the same risk distance and 1:1 reward the indicator uses
- Refuses new trades once `MAX_OPEN_TRADES` of its own positions are open
- Force-closes everything it opened at `FORCE_CLOSE_HOUR_UTC` (default
  21:00 UTC) so nothing carries overnight
- On first launch, it silently learns each pair's current trend/structure
  from history — it will NOT fire trades from that backfill, only from
  genuinely new signals going forward

## Notes

- This must stay running on this PC — closing the window stops the bot.
  If the PC restarts, just re-run `python bot.py`.
- Every order this bot places is tagged with a magic number (990033), so
  it will never touch, count, or close trades you open manually.
- **20-30% risk per trade with up to 3 at once means a bad run can lose
  most of the account fast.** That's exactly what you asked for — just
  flagging it's live.
