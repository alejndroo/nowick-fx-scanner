"""Dashboard live-data sync — COMPLETELY SEPARATE from the signal bot.

Runs on its own schedule (sync_dashboard.yml), reads state.json (read-only)
for the latest signals, talks to Firebase for everything the phone app
needs, and tracks confirmed trades' live P&L. It never touches state.json,
never calls telegram.py, and is never imported by main.py — the signal
scanning and Telegram alerting are completely unaffected by anything here.

Money model (currency-agnostic, no FX conversion needed):
  Every trade risks a fixed £ amount = balance_gbp * risk_pct / 100,
  locked in at the moment the trade is confirmed. Since every signal is a
  fixed 1:1 R:R trade, a trade's P&L is always some multiple of that R
  amount: closed at TP = +1R, closed at SL = -1R, still open = whatever
  fraction of the SL-to-entry distance price has currently moved favorably.
  This is exactly how risking "1% per trade" works in practice, and needs
  no per-pair pip-value or currency-conversion lookups at all.
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, db

from oanda import fetch_candles
from pairs import display_symbol

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

DEFAULT_ACCOUNT = {"balance_gbp": 10000.0, "risk_pct": 1.0, "starting_balance_gbp": 10000.0}


def init_firebase():
    cred_json = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    cred = credentials.Certificate(cred_json)
    firebase_admin.initialize_app(cred, {"databaseURL": os.environ["FIREBASE_DB_URL"]})


def load_local_history() -> list[dict]:
    with open(STATE_PATH) as f:
        state = json.load(f)
    return state.get("history", [])


def mirror_signals(history: list[dict]) -> None:
    """Push the bot's recent signal history into Firebase so the app has
    something to show on the Signals tab without ever reading GitHub raw
    JSON. Pure mirror — read-only on the state.json side.
    """
    ref = db.reference("/signals")
    existing = ref.get() or {}
    existing_keys = {v.get("time", "") + v.get("pair", "") for v in existing.values()} if existing else set()
    for sig in history[-30:]:
        key = sig["time"] + sig["pair"]
        if key in existing_keys:
            continue
        ref.push({
            "pair": sig["pair"], "dir": sig["dir"], "time": sig["time"],
            "confirmed": False,
        })


def process_pending_confirmations() -> None:
    """The app writes here when the user taps 'I took this trade'."""
    pending_ref = db.reference("/pending_confirmations")
    pending = pending_ref.get() or {}
    if not pending:
        return

    account = db.reference("/account").get() or dict(DEFAULT_ACCOUNT)
    balance = float(account.get("balance_gbp", DEFAULT_ACCOUNT["balance_gbp"]))
    risk_pct = float(account.get("risk_pct", DEFAULT_ACCOUNT["risk_pct"]))
    risk_amount = round(balance * risk_pct / 100.0, 2)

    trades_ref = db.reference("/trades")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for key, item in pending.items():
        trade_id = str(uuid.uuid4())[:8]
        trades_ref.child(trade_id).set({
            "pair": item["pair"], "dir": item["dir"],
            "entry": item["entry"], "sl": item["sl"], "tp": item["tp"],
            "status": "open", "opened_at": now_iso,
            "risk_amount_gbp": risk_amount,
            "r_multiple": 0.0, "pnl_gbp": 0.0,
        })
        # `key` is the SAME push-key the app used for /signals/{key}, so we
        # can flip that signal to "confirmed" for the Signals tab UI too.
        signal_key = item.get("signal_key")
        if signal_key:
            db.reference(f"/signals/{signal_key}").update({"confirmed": True})
        pending_ref.child(key).delete()


def oanda_pair_code(display_pair: str) -> str:
    """EURUSD -> EUR_USD"""
    return display_pair[:3] + "_" + display_pair[3:]


def update_open_trades() -> None:
    trades_ref = db.reference("/trades")
    trades = trades_ref.get() or {}
    open_trades = {k: v for k, v in trades.items() if v.get("status") == "open"}
    if not open_trades:
        return

    for trade_id, t in open_trades.items():
        pair_code = oanda_pair_code(t["pair"])
        try:
            candles = fetch_candles(pair_code, "M15", count=20)
        except Exception:
            continue
        if not candles:
            continue

        entry, sl, tp, direction = t["entry"], t["sl"], t["tp"], t["dir"]
        risk_amount = t.get("risk_amount_gbp", 0.0)
        risk_dist = abs(entry - sl)
        if risk_dist == 0:
            continue

        closed = False
        close_r = 0.0
        # Walk candles since the trade opened, in order, checking for a
        # touch — mirrors how the price actually traded, not just the
        # latest snapshot (a fast SL/TP touch between sync runs is still caught).
        for c in candles:
            if direction == "BUY":
                if c["low"] <= sl:
                    closed, close_r = True, -1.0
                    break
                if c["high"] >= tp:
                    closed, close_r = True, 1.0
                    break
            else:
                if c["high"] >= sl:
                    closed, close_r = True, -1.0
                    break
                if c["low"] <= tp:
                    closed, close_r = True, 1.0
                    break

        last_close = candles[-1]["close"]
        if closed:
            trades_ref.child(trade_id).update({
                "status": "tp" if close_r > 0 else "sl",
                "closed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "r_multiple": close_r,
                "pnl_gbp": round(close_r * risk_amount, 2),
            })
        else:
            live_move = (last_close - entry) if direction == "BUY" else (entry - last_close)
            r = live_move / risk_dist
            trades_ref.child(trade_id).update({
                "r_multiple": round(r, 3),
                "pnl_gbp": round(r * risk_amount, 2),
            })


def process_withdrawals_and_balance() -> None:
    account = db.reference("/account").get()
    if not account:
        account = dict(DEFAULT_ACCOUNT)
        db.reference("/account").set(account)

    starting = float(account.get("starting_balance_gbp", DEFAULT_ACCOUNT["starting_balance_gbp"]))

    trades = db.reference("/trades").get() or {}
    realized_pnl = sum(float(t.get("pnl_gbp", 0)) for t in trades.values() if t.get("status") in ("tp", "sl"))
    floating_pnl = sum(float(t.get("pnl_gbp", 0)) for t in trades.values() if t.get("status") == "open")

    withdrawals = db.reference("/withdrawals").get() or {}
    total_withdrawn = sum(float(w.get("amount", 0)) for w in withdrawals.values())

    current_balance = round(starting + realized_pnl - total_withdrawn, 2)
    db.reference("/account").update({
        "balance_gbp": current_balance,
        "total_withdrawn_gbp": round(total_withdrawn, 2),
        "realized_pnl_all_time_gbp": round(realized_pnl, 2),
        "floating_pnl_gbp": round(floating_pnl, 2),
    })


def rebuild_calendar_and_aggregates() -> None:
    trades = db.reference("/trades").get() or {}
    closed = [t for t in trades.values() if t.get("status") in ("tp", "sl") and t.get("closed_at")]

    by_day: dict[str, float] = {}
    by_pair: dict[str, float] = {}
    for t in closed:
        day = t["closed_at"][:10]
        by_day[day] = round(by_day.get(day, 0) + t["pnl_gbp"], 2)
        by_pair[t["pair"]] = round(by_pair.get(t["pair"], 0) + t["pnl_gbp"], 2)

    db.reference("/calendar").set(by_day)

    top_pairs = sorted(by_pair.items(), key=lambda kv: kv[1], reverse=True)[:3]
    db.reference("/top_pairs").set([{"pair": p, "pnl_gbp": v} for p, v in top_pairs])

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    today_pnl = by_day.get(today_str, 0.0)

    from datetime import timedelta
    week_pnl = sum(v for d, v in by_day.items() if (now - datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days < 7)
    month_pnl = sum(v for d, v in by_day.items() if (now - datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days < 30)
    all_time_pnl = sum(by_day.values())

    db.reference("/live").set({
        "today_gbp": round(today_pnl, 2),
        "week_gbp": round(week_pnl, 2),
        "month_gbp": round(month_pnl, 2),
        "all_time_gbp": round(all_time_pnl, 2),
        "updated_at": now.isoformat(timespec="seconds"),
    })


def main() -> None:
    init_firebase()
    history = load_local_history()
    mirror_signals(history)
    process_pending_confirmations()
    update_open_trades()
    process_withdrawals_and_balance()
    rebuild_calendar_and_aggregates()


if __name__ == "__main__":
    main()
