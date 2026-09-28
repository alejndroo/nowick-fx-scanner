"""Pushes live MT5 account/trade state straight to Firebase.

Replaces the GitHub-Actions-based dashboard_sync.py polling path for this
bot: since bot.py is already connected to the real MT5 account and knows
every fill/close in real time, there is no "I took this trade" step and no
replayed/estimated P&L — every number here comes straight from MT5's own
account and trade history.

Field names carried over from the original design (pnl_gbp, balance_gbp,
week_gbp, etc.) are a naming holdover — every value is actually in the
MT5 account's own currency (USD). Only /fx/usd_gbp is pushed for the app's
optional headline-only $ -> £ display toggle. Kept unrenamed to avoid
touching every read site in docs/index.html and dashboard_sync.py.
"""
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5
import firebase_admin
from firebase_admin import credentials, db

import config
import mt5_broker as broker

_initialized = False


def init_firebase() -> None:
    global _initialized
    if _initialized:
        return
    cred = credentials.Certificate(config.FIREBASE_SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred, {"databaseURL": config.FIREBASE_DB_URL})
    _initialized = True


def usd_gbp_rate() -> float | None:
    """Best-effort live rate via the broker's own GBPUSD quote (not traded
    by this strategy, just read for display conversion). None if unavailable
    — the app falls back to an approximate static rate in that case.
    """
    if mt5.symbol_info("GBPUSD") is None:
        mt5.symbol_select("GBPUSD", True)
    tick = mt5.symbol_info_tick("GBPUSD")
    if tick is None or tick.bid == 0:
        return None
    return round(1.0 / tick.bid, 6)


def sync_to_firebase(known_tickets: dict) -> dict:
    """Call this once per bot.py loop iteration.

    known_tickets: {ticket: {"sl", "tp", "dir", "pair", "opened_at"}},
    carried across calls (bot.py owns the dict and passes it back in each
    time) so a position that disappears between iterations can be looked up
    in MT5's trade history to record its real close price/profit.
    Returns the updated dict — always reassign the return value.
    """
    init_firebase()

    account = mt5.account_info()
    if account is None:
        return known_tickets

    positions = mt5.positions_get() or []
    our_positions = [p for p in positions if p.magic == broker.MAGIC]
    open_tickets = {p.ticket for p in our_positions}

    trades_ref = db.reference("/trades")

    for p in our_positions:
        pair_display = p.symbol[:-len(config.SYMBOL_SUFFIX)] if config.SYMBOL_SUFFIX and p.symbol.endswith(config.SYMBOL_SUFFIX) else p.symbol
        risk_dist = abs(p.price_open - p.sl) if p.sl else None
        r_multiple = round(p.profit / (risk_dist * p.volume * 100000), 3) if risk_dist else 0.0
        prior = known_tickets.get(p.ticket)
        if prior is not None:
            opened_at = prior.get("opened_at")
        else:
            # Not in memory — likely a restart. Firebase already has this
            # trade's real opened_at from before the restart (synced every
            # ~10s), so recover it from there instead of resetting the clock.
            existing = trades_ref.child(str(p.ticket)).get() or {}
            opened_at = existing.get("opened_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
        trades_ref.child(str(p.ticket)).update({
            "pair": pair_display,
            "dir": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
            "entry": p.price_open, "sl": p.sl, "tp": p.tp,
            "status": "open",
            "opened_at": opened_at,
            "pnl_gbp": round(p.profit, 2),
            "r_multiple": r_multiple,
        })
        known_tickets[p.ticket] = {
            "sl": p.sl, "tp": p.tp,
            "dir": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
            "pair": pair_display, "opened_at": opened_at,
        }

    closed_now = [t for t in known_tickets if t not in open_tickets]
    if closed_now:
        deals = mt5.history_deals_get(datetime.now(timezone.utc) - timedelta(days=2), datetime.now(timezone.utc) + timedelta(minutes=5)) or []
        deals_by_position: dict[int, list] = {}
        for d in deals:
            deals_by_position.setdefault(d.position_id, []).append(d)

        for ticket in closed_now:
            info = known_tickets.pop(ticket)
            pos_deals = deals_by_position.get(ticket, [])
            close_deal = next((d for d in pos_deals if d.entry == mt5.DEAL_ENTRY_OUT), None)
            profit = sum(d.profit for d in pos_deals)
            status = "tp" if profit >= 0 else "sl"
            if close_deal is not None and info.get("sl"):
                close_price = close_deal.price
                if info["dir"] == "BUY":
                    status = "sl" if close_price <= info["sl"] + 1e-9 else "tp"
                else:
                    status = "sl" if close_price >= info["sl"] - 1e-9 else "tp"
            trades_ref.child(str(ticket)).update({
                "status": status,
                "closed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "pnl_gbp": round(profit, 2),
            })

    floating = sum(p.profit for p in our_positions)
    db.reference("/account").update({
        "balance_gbp": round(account.balance, 2),
        "equity_usd": round(account.equity, 2),
        "floating_pnl_gbp": round(floating, 2),
        "currency": account.currency,
    })

    if closed_now:
        _rebuild_calendar_and_aggregates()

    rate = usd_gbp_rate()
    if rate:
        db.reference("/fx").update({"usd_gbp": rate, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    return known_tickets


def _rebuild_calendar_and_aggregates() -> None:
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
