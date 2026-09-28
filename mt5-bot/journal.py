"""Permanent structured trade journal — the memory the planner learns from.

Every trade this bot opens gets a full record here (not just pair/dir/P&L
like Firebase's /trades — this also keeps the risk % used, the session hour,
day of week, and market volatility (ATR) at entry, so the planner can find
real patterns: "this pair loses more on Fridays", "risk was too high during
the last losing streak", etc.

Stored as plain JSON, keyed by ticket (same key space as Firebase's
/trades, so the two are easy to cross-reference). No database, no ML
library — the data volume here (tens to low hundreds of trades) makes a
flat file both sufficient and, honestly, more inspectable than a database
would be.
"""
import json
import os
from datetime import datetime, timezone

JOURNAL_PATH = os.path.join(os.path.dirname(__file__), "mt5_journal.json")


def load_journal() -> dict:
    if os.path.exists(JOURNAL_PATH):
        try:
            with open(JOURNAL_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print("mt5_journal.json unreadable — starting fresh (existing Firebase /trades history is unaffected).")
    return {}


def save_journal(journal: dict) -> None:
    tmp = JOURNAL_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(journal, f, indent=2, default=str)
    os.replace(tmp, JOURNAL_PATH)


def record_open(journal: dict, ticket: int, pair: str, direction: str, entry: float, sl: float, tp: float,
                 risk_pct: float, atr: float | None, now: datetime) -> None:
    journal[str(ticket)] = {
        "pair": pair, "dir": direction, "entry": entry, "sl": sl, "tp": tp,
        "risk_pct": round(risk_pct, 4),
        "atr_at_entry": atr,
        "session_hour_utc": now.hour,
        "day_of_week": now.strftime("%A"),
        "opened_at": now.isoformat(timespec="seconds"),
        "status": "open",
    }
    save_journal(journal)


def record_close(journal: dict, ticket: int, status: str, pnl: float, closed_at: datetime) -> None:
    rec = journal.get(str(ticket))
    if rec is None:
        return  # opened before the journal existed, or a restart gap — nothing to update
    rec["status"] = status
    rec["pnl"] = round(pnl, 2)
    rec["closed_at"] = closed_at.isoformat(timespec="seconds")
    save_journal(journal)


def today_realized_pnl(journal: dict, now: datetime) -> float:
    """Sum of P&L for trades closed on today's UTC calendar date — the
    number the daily circuit breaker watches."""
    today_str = now.strftime("%Y-%m-%d")
    total = 0.0
    for rec in journal.values():
        if rec.get("status") not in ("tp", "sl", "manual"):
            continue
        closed_at = rec.get("closed_at", "")
        if closed_at[:10] == today_str:
            total += rec.get("pnl", 0)
    return round(total, 2)


def rolling_stats(journal: dict, lookback_days: int = 14) -> dict:
    """Aggregates the recent closed history into the numbers the planner
    actually reasons with. Returns overall win rate/streak plus per-pair,
    per-session-hour-bucket, and per-day-of-week breakdowns.
    """
    now = datetime.now(timezone.utc)
    closed = []
    for rec in journal.values():
        if rec.get("status") not in ("tp", "sl", "manual") or "closed_at" not in rec:
            continue
        try:
            closed_at = datetime.fromisoformat(rec["closed_at"])
        except ValueError:
            continue
        if (now - closed_at).days > lookback_days:
            continue
        closed.append(rec)

    closed.sort(key=lambda r: r["closed_at"])

    def win_rate(records: list[dict]) -> float | None:
        decided = [r for r in records if r["status"] in ("tp", "sl")]
        if not decided:
            return None
        wins = sum(1 for r in decided if r["status"] == "tp")
        return wins / len(decided)

    # Current streak: consecutive wins (positive) or losses (negative) at the tail end.
    streak = 0
    for rec in reversed(closed):
        if rec["status"] not in ("tp", "sl"):
            continue
        is_win = rec["status"] == "tp"
        if streak == 0:
            streak = 1 if is_win else -1
        elif (streak > 0) == is_win:
            streak += 1 if is_win else -1
        else:
            break

    by_pair: dict[str, list[dict]] = {}
    for r in closed:
        by_pair.setdefault(r["pair"], []).append(r)

    by_day: dict[str, list[dict]] = {}
    for r in closed:
        by_day.setdefault(r.get("day_of_week", "?"), []).append(r)

    return {
        "n_closed": len(closed),
        "overall_win_rate": win_rate(closed),
        "current_streak": streak,  # e.g. -3 = three losses in a row, +2 = two wins in a row
        "total_pnl": round(sum(r.get("pnl", 0) for r in closed), 2),
        "by_pair": {p: {"n": len(rs), "win_rate": win_rate(rs), "pnl": round(sum(r.get("pnl", 0) for r in rs), 2)} for p, rs in by_pair.items()},
        "by_day_of_week": {d: {"n": len(rs), "win_rate": win_rate(rs)} for d, rs in by_day.items()},
    }
