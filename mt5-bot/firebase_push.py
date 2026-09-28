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


def get_risk_pct(default: float) -> float:
    """Reads the dashboard's Risk % field so it's an actual live control,
    not just a display. Firebase stores it as a percentage (e.g. 15), config
    stores a fraction (0.15) — converts and sanity-checks, falling back to
    `default` (config.RISK_PCT) if unset, unreadable, or out of range.

    init_firebase() is INSIDE the try block deliberately: a Firebase outage
    or bad credential must never block real trade execution — this always
    degrades to the safe config default instead of raising into the caller.
    """
    try:
        init_firebase()
        val = db.reference("/account/risk_pct").get()
        if val is None:
            return default
        pct = float(val)
        if not (0 < pct <= 100):
            return default
        return pct / 100.0
    except Exception:
        return default


def mark_force_closing(tickets: list[int]) -> None:
    """Call right before broker.close_all() for the no-overnight cutoff, so
    the closure handler below can label these as a scheduled close instead
    of a fake TP/SL hit.
    """
    init_firebase()
    for t in tickets:
        db.reference(f"/trades/{t}").update({"force_closed": True})


def sync_to_firebase(_unused: dict | None = None) -> dict:
    """Call this once per bot.py loop iteration.

    Firebase's own /trades node (not local process memory) is the source of
    truth for "which tickets are currently open" — this makes closure
    detection correct even across a bot restart, since in-memory state
    would otherwise forget a position that closed while the bot was down
    and leave it permanently stuck showing "open" on the dashboard.

    Return value is unused; kept only so existing call sites that do
    `known_tickets = sync_to_firebase(known_tickets)` don't need to change.
    """
    init_firebase()

    account = mt5.account_info()
    if account is None:
        # mt5.account_info() returns None (never raises) on a dropped
        # terminal/account session, so this is the ONE place that would
        # otherwise silently do nothing every ~1s with zero visible symptom.
        print("Firebase sync: mt5.account_info() returned None — MT5 session may be disconnected.")
        return {}

    positions = mt5.positions_get() or []
    our_positions = [p for p in positions if p.magic == broker.MAGIC]
    open_tickets = {p.ticket for p in our_positions}

    trades_ref = db.reference("/trades")
    all_trades = trades_ref.get() or {}
    fb_open_tickets = {int(k) for k, v in all_trades.items() if v.get("status") == "open"}

    for p in our_positions:
        pair_display = p.symbol[:-len(config.SYMBOL_SUFFIX)] if config.SYMBOL_SUFFIX and p.symbol.endswith(config.SYMBOL_SUFFIX) else p.symbol
        risk_dist = abs(p.price_open - p.sl) if p.sl else None
        r_multiple = round(p.profit / (risk_dist * p.volume * 100000), 3) if risk_dist else 0.0
        existing = all_trades.get(str(p.ticket), {})
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

    closed_now = fb_open_tickets - open_tickets
    if closed_now:
        deals = mt5.history_deals_get(datetime.now(timezone.utc) - timedelta(days=2), datetime.now(timezone.utc) + timedelta(minutes=5)) or []
        deals_by_position: dict[int, list] = {}
        for d in deals:
            deals_by_position.setdefault(d.position_id, []).append(d)

        for ticket in closed_now:
            info = all_trades.get(str(ticket), {})
            pos_deals = deals_by_position.get(ticket, [])
            close_deal = next((d for d in pos_deals if d.entry == mt5.DEAL_ENTRY_OUT), None)
            profit = sum(d.profit + d.swap + d.commission for d in pos_deals)

            if info.get("force_closed"):
                # Flattened by the no-overnight cutoff, not a real TP/SL
                # hit — label distinctly so Signals/win-rate aren't skewed.
                status = "manual"
            else:
                status = "tp" if profit >= 0 else "sl"
                if close_deal is not None and info.get("sl"):
                    close_price = close_deal.price
                    if info.get("dir") == "BUY":
                        status = "sl" if close_price <= info["sl"] + 1e-9 else "tp"
                    else:
                        status = "sl" if close_price >= info["sl"] - 1e-9 else "tp"

            trades_ref.child(str(ticket)).update({
                "status": status,
                "closed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "pnl_gbp": round(profit, 2),
            })

    floating = sum(p.profit for p in our_positions)
    withdrawals = db.reference("/withdrawals").get() or {}
    total_withdrawn = sum(float(w.get("amount", 0)) for w in withdrawals.values())
    db.reference("/account").update({
        "balance_gbp": round(account.balance, 2),
        "equity_usd": round(account.equity, 2),
        "floating_pnl_gbp": round(floating, 2),
        "total_withdrawn_gbp": round(total_withdrawn, 2),
        "currency": account.currency,
    })

    if closed_now:
        _rebuild_calendar_and_aggregates()

    rate = usd_gbp_rate()
    if rate:
        db.reference("/fx").update({"usd_gbp": rate, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    return {}


def _rebuild_calendar_and_aggregates() -> None:
    trades = db.reference("/trades").get() or {}
    # "manual" (no-overnight cutoff) closes are still real realized P&L and
    # must count toward totals/calendar — they're excluded from win-rate
    # client-side by checking status in ("tp","sl") there instead.
    closed = [t for t in trades.values() if t.get("status") in ("tp", "sl", "manual") and t.get("closed_at")]

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
