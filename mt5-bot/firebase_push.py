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
import traceback
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5
import firebase_admin
from firebase_admin import credentials, db

import config
import journal as journal_mod
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


def sync_to_firebase(journal_data: dict | None = None) -> dict:
    """Call this once per bot.py loop iteration.

    Firebase's own /trades node (not local process memory) is the source of
    truth for "which tickets are currently open" — this makes closure
    detection correct even across a bot restart, since in-memory state
    would otherwise forget a position that closed while the bot was down
    and leave it permanently stuck showing "open" on the dashboard.

    journal_data: bot.py's in-memory journal dict (same object it passes to
    journal.record_open()) — when a close is detected here, the journal
    gets the matching record_close() call so the planner's history stays
    complete even though closes are detected in this module, not bot.py.
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
            elif profit != 0:
                # Real dollar P&L is unambiguous ground truth — a trade that
                # lost real money is a loss, full stop. Previously this was
                # OVERRIDDEN by a price-vs-recorded-SL comparison, which
                # could flip a large real loss to "tp" whenever the recorded
                # SL didn't match the position's true state at close (e.g.
                # on a netting-mode account, where repeated entries into the
                # same symbol merge into one growing position and the SL
                # recorded from an earlier, smaller version of that position
                # no longer reflects it). Confirmed live: a real -$46.30
                # EURCAD loss was being logged as a win this way.
                status = "sl" if profit < 0 else "tp"
            else:
                # Exact breakeven close (rare) — no P&L sign to trust, fall
                # back to price-vs-recorded-SL only for this edge case.
                status = "tp"
                if close_deal is not None and info.get("sl"):
                    close_price = close_deal.price
                    if info.get("dir") == "BUY":
                        status = "sl" if close_price <= info["sl"] + 1e-9 else "tp"
                    else:
                        status = "sl" if close_price >= info["sl"] - 1e-9 else "tp"

            closed_at_dt = datetime.now(timezone.utc)
            trades_ref.child(str(ticket)).update({
                "status": status,
                "closed_at": closed_at_dt.isoformat(timespec="seconds"),
                "pnl_gbp": round(profit, 2),
            })
            if journal_data is not None:
                journal_mod.record_close(journal_data, ticket, status, profit, closed_at_dt)

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


def backfill_journal_from_firebase(journal_data: dict) -> int:
    """One-time-per-gap, idempotent catch-up: pulls any closed trade already
    sitting in Firebase's /trades (from before journal.py existed, or from
    any gap) into the local journal, so the planner learns from ALL real
    trade history today, not just trades placed after this system was
    added. Safe to call every startup — only adds tickets not already
    present. Returns the number of records added.

    Firebase's /trades stores "pair" in DISPLAY format ("EURUSD", no
    underscore — see sync_to_firebase's pair_display) while the live
    journal path (bot.py's process_pair -> journal.record_open) stores the
    underscore OANDA-style format ("EUR_USD", matching pairs.PAIRS) that
    the planner's pause-matching logic compares against. Converting here is
    mandatory: without it, "EURUSD" and "EUR_USD" would silently become two
    different pairs in the planner's stats — the exact kind of
    format-mismatch bug that was specifically checked for and cleared on
    the live path, so it must not get reintroduced here.
    """
    try:
        init_firebase()
        all_trades = db.reference("/trades").get() or {}
    except Exception:
        print("Journal backfill: could not reach Firebase, skipping this attempt.")
        return 0

    added = 0
    for ticket_str, t in all_trades.items():
        if ticket_str in journal_data:
            continue
        if t.get("status") not in ("tp", "sl", "manual"):
            continue  # only backfill trades with a known, final, real outcome

        pair_display = t.get("pair") or ""
        pair_underscore = f"{pair_display[:3]}_{pair_display[3:]}" if len(pair_display) == 6 else pair_display

        opened_at_str = t.get("opened_at")
        try:
            opened_at = datetime.fromisoformat(opened_at_str) if opened_at_str else None
        except ValueError:
            opened_at = None

        journal_data[ticket_str] = {
            "pair": pair_underscore, "dir": t.get("dir"),
            "entry": t.get("entry"), "sl": t.get("sl"), "tp": t.get("tp"),
            "risk_pct": None,   # unknown for trades placed before this system existed
            "atr_at_entry": None,
            "session_hour_utc": opened_at.hour if opened_at else None,
            "day_of_week": opened_at.strftime("%A") if opened_at else None,
            "opened_at": opened_at_str,
            "status": t.get("status"),
            "pnl": t.get("pnl_gbp") or 0,  # rolling_stats' r.get("pnl", 0) only defaults on a MISSING key, not an explicit None
            "closed_at": t.get("closed_at"),
            "backfilled": True,
        }
        added += 1

    if added:
        journal_mod.save_journal(journal_data)
    return added


def reconcile() -> list[str]:
    """Compares real MT5 state against what Firebase (and therefore the
    dashboard) currently shows, and returns a list of plain-text mismatch
    descriptions — empty list means they agree. This exists so drift gets
    caught automatically instead of only when someone happens to check by
    hand, per a direct request after a display/reality mismatch was found.
    """
    problems = []
    try:
        init_firebase()
        positions = mt5.positions_get() or []
        mt5_open_tickets = {p.ticket for p in positions if p.magic == broker.MAGIC}

        all_trades = db.reference("/trades").get() or {}
        fb_open_tickets = {int(k) for k, v in all_trades.items() if v.get("status") == "open"}

        missing_in_fb = mt5_open_tickets - fb_open_tickets
        stale_in_fb = fb_open_tickets - mt5_open_tickets
        if missing_in_fb:
            problems.append(f"{len(missing_in_fb)} position(s) open in MT5 but NOT shown as open in Firebase: {sorted(missing_in_fb)}")
        if stale_in_fb:
            problems.append(f"{len(stale_in_fb)} ticket(s) shown open in Firebase but NOT actually open in MT5: {sorted(stale_in_fb)}")

        account = mt5.account_info()
        fb_account = db.reference("/account").get() or {}
        if account is not None and fb_account.get("balance_gbp") is not None:
            balance_diff = abs(account.balance - fb_account["balance_gbp"])
            if balance_diff > 1.0:  # more than $1 apart is real drift, not just a mid-sync snapshot gap
                problems.append(f"Balance mismatch: MT5={account.balance:.2f} vs Firebase={fb_account['balance_gbp']:.2f} (diff {balance_diff:.2f})")
    except Exception:
        print("Reconciliation check failed:", traceback.format_exc())
        problems.append("Reconciliation check itself failed to run (see console traceback above) — treat this as unverified, not as 'all clear'.")

    return problems


def reclassify_existing(journal_data: dict) -> int:
    """One-time-per-startup correction: fixes any already-closed trade (in
    Firebase's /trades and/or the local journal) whose stored tp/sl status
    contradicts its own stored pnl sign.

    Exists because the classifier bug just fixed above (price-vs-recorded-SL
    could override real dollar profit) already wrote WRONG statuses for
    trades closed before that fix — including into the local journal, since
    backfill_journal_from_firebase() copied those same wrong statuses over
    before this correction existed. Real P&L sign is unambiguous ground
    truth, so this is safe to run every startup (idempotent — trades already
    correctly labeled are left untouched).
    """
    fixed = 0
    try:
        init_firebase()
        all_trades = db.reference("/trades").get() or {}
        for ticket_str, t in all_trades.items():
            if t.get("status") not in ("tp", "sl"):
                continue
            pnl = t.get("pnl_gbp")
            if pnl is None:
                continue
            correct = "sl" if pnl < 0 else "tp"
            if t.get("status") != correct:
                db.reference(f"/trades/{ticket_str}/status").set(correct)
                fixed += 1
    except Exception:
        print("Firebase reclassification pass failed:", traceback.format_exc())

    journal_changed = False
    for rec in journal_data.values():
        if rec.get("status") not in ("tp", "sl"):
            continue
        pnl = rec.get("pnl")
        if pnl is None:
            continue
        correct = "sl" if pnl < 0 else "tp"
        if rec.get("status") != correct:
            rec["status"] = correct
            fixed += 1
            journal_changed = True
    if journal_changed:
        journal_mod.save_journal(journal_data)

    return fixed


def push_insights(plan: dict) -> None:
    """Pushes the planner's current adaptive plan + reasoning to Firebase
    so the dashboard can show it — the point is that it's inspectable, not
    a black box."""
    try:
        init_firebase()
        db.reference("/insights").set(plan)
    except Exception:
        pass


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
