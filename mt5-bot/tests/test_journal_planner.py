"""Real, executable unit tests for journal.py and planner.py — imported
directly (pure Python, no MT5/Firebase needed)."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journal  # noqa: E402
import planner  # noqa: E402
import config  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print(f"FAIL: {name}  {detail}")


NOW = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)


def rec(pair, status, pnl, hours_ago, day_of_week="Monday"):
    closed_at = NOW - timedelta(hours=hours_ago)
    return {
        "pair": pair, "status": status, "pnl": pnl,
        "closed_at": closed_at.isoformat(timespec="seconds"),
        "day_of_week": day_of_week,
    }


# ---------------------------------------------------------------------------
# journal.record_close on an unknown ticket must no-op, never crash.
# ---------------------------------------------------------------------------
def test_record_close_unknown_ticket_noop():
    j = {}
    try:
        journal.record_close(j, 99999, "sl", -5.0, NOW)
        check("record_close_unknown_ticket_noop", j == {}, j)
    except Exception as e:
        check("record_close_unknown_ticket_noop", False, f"raised {e!r}")


# ---------------------------------------------------------------------------
# rolling_stats: hand-traced streak direction and magnitude.
# 3 losses then 2 wins, most recent first when read back -> streak should
# be +2 (two wins in a row at the tail), not -3, not some other sign/value.
# ---------------------------------------------------------------------------
def test_streak_sign_and_magnitude():
    j = {}
    t = 1
    # oldest -> newest: L L L W W  (5 hours ago down to 1 hour ago)
    for i, status in enumerate(["sl", "sl", "sl", "tp", "tp"]):
        hours_ago = 5 - i
        j[str(t)] = rec("EUR_USD", status, -3.0 if status == "sl" else 4.0, hours_ago)
        t += 1
    stats = journal.rolling_stats(j)
    check("streak_is_plus_two_wins", stats["current_streak"] == 2, stats["current_streak"])

    j2 = {}
    t = 1
    # oldest -> newest: W W L L L  (5 losses to 1... wait: W W L L L, tail is L L L -> -3)
    for i, status in enumerate(["tp", "tp", "sl", "sl", "sl"]):
        hours_ago = 5 - i
        j2[str(t)] = rec("EUR_USD", status, 4.0 if status == "tp" else -3.0, hours_ago)
        t += 1
    stats2 = journal.rolling_stats(j2)
    check("streak_is_minus_three_losses", stats2["current_streak"] == -3, stats2["current_streak"])


def test_rolling_stats_empty_journal_no_crash():
    try:
        stats = journal.rolling_stats({})
        check("empty_journal_no_crash", stats["overall_win_rate"] is None and stats["n_closed"] == 0, stats)
    except Exception as e:
        check("empty_journal_no_crash", False, f"raised {e!r}")


def test_today_realized_pnl_only_counts_today():
    j = {
        "1": rec("EUR_USD", "sl", -5.0, hours_ago=1),   # today
        "2": rec("EUR_USD", "tp", 10.0, hours_ago=2),   # today
        "3": rec("EUR_USD", "tp", 100.0, hours_ago=30),  # yesterday-ish, must NOT count
    }
    total = journal.today_realized_pnl(j, NOW)
    check("today_realized_pnl_excludes_older_days", abs(total - 5.0) < 1e-9, total)


def test_win_rate_by_pair():
    j = {}
    t = 1
    for _ in range(4):
        j[str(t)] = rec("EUR_USD", "sl", -1.0, hours_ago=1); t += 1
    j[str(t)] = rec("EUR_USD", "tp", 1.0, hours_ago=1); t += 1
    for _ in range(4):
        j[str(t)] = rec("GBP_JPY", "tp", 1.0, hours_ago=1); t += 1
    j[str(t)] = rec("GBP_JPY", "sl", -1.0, hours_ago=1); t += 1
    stats = journal.rolling_stats(j)
    check("eur_usd_win_rate_20pct", abs(stats["by_pair"]["EUR_USD"]["win_rate"] - 0.2) < 1e-9, stats["by_pair"]["EUR_USD"])
    check("gbp_jpy_win_rate_80pct", abs(stats["by_pair"]["GBP_JPY"]["win_rate"] - 0.8) < 1e-9, stats["by_pair"]["GBP_JPY"])


# ---------------------------------------------------------------------------
# planner: risk_pct is provably bounded to [RISK_PCT_MIN, RISK_PCT_MAX] for
# ANY streak, including extreme/unrealistic ones.
# ---------------------------------------------------------------------------
def test_planner_risk_never_escapes_band():
    j = {}
    t = 1
    for _ in range(50):  # an extreme, unrealistic win streak
        j[str(t)] = rec("EUR_USD", "tp", 1.0, hours_ago=1); t += 1
    plan = planner.compute_plan(j, day_start_balance=1000, now=NOW)
    check("risk_never_above_max", plan["risk_pct"] <= config.RISK_PCT_MAX + 1e-12, plan["risk_pct"])
    check("risk_never_below_min", plan["risk_pct"] >= config.RISK_PCT_MIN - 1e-12, plan["risk_pct"])

    j2 = {}
    t = 1
    for _ in range(50):  # an extreme losing streak
        j2[str(t)] = rec("EUR_USD", "sl", -1.0, hours_ago=1); t += 1
    plan2 = planner.compute_plan(j2, day_start_balance=1000, now=NOW)
    check("risk_never_above_max_losing", plan2["risk_pct"] <= config.RISK_PCT_MAX + 1e-12, plan2["risk_pct"])
    check("risk_never_below_min_losing", plan2["risk_pct"] >= config.RISK_PCT_MIN - 1e-12, plan2["risk_pct"])


def test_planner_pauses_bad_pair_not_good_one():
    j = {}
    t = 1
    for _ in range(4):
        j[str(t)] = rec("EUR_USD", "sl", -1.0, hours_ago=1); t += 1
    j[str(t)] = rec("EUR_USD", "tp", 1.0, hours_ago=1); t += 1
    for _ in range(4):
        j[str(t)] = rec("GBP_JPY", "tp", 1.0, hours_ago=1); t += 1
    j[str(t)] = rec("GBP_JPY", "sl", -1.0, hours_ago=1); t += 1
    plan = planner.compute_plan(j, day_start_balance=1000, now=NOW)
    check("bad_pair_paused", "EUR_USD" in plan["paused_pairs"], plan["paused_pairs"])
    check("good_pair_not_paused", "GBP_JPY" not in plan["paused_pairs"], plan["paused_pairs"])


def test_planner_no_pause_below_min_sample_size():
    # Only 2 trades on this pair, both losses -> must NOT pause (noise, not signal).
    j = {"1": rec("EUR_USD", "sl", -1.0, 1), "2": rec("EUR_USD", "sl", -1.0, 2),
         "3": rec("GBP_JPY", "tp", 1.0, 1), "4": rec("GBP_JPY", "tp", 1.0, 2)}
    plan = planner.compute_plan(j, day_start_balance=1000, now=NOW)
    check("no_pause_on_tiny_sample", plan["paused_pairs"] == [], plan["paused_pairs"])


def test_daily_circuit_breaker_math():
    j = {"1": rec("EUR_USD", "sl", -35.0, hours_ago=1)}  # 35 of 100 = 35% > 30% threshold
    plan = planner.compute_plan(j, day_start_balance=100.0, now=NOW)
    check("circuit_breaker_trips_above_threshold", plan["daily_loss_limit_hit"] is True, plan)

    j2 = {"1": rec("EUR_USD", "sl", -25.0, hours_ago=1)}  # 25% < 30% threshold
    plan2 = planner.compute_plan(j2, day_start_balance=100.0, now=NOW)
    check("circuit_breaker_does_not_trip_below_threshold", plan2["daily_loss_limit_hit"] is False, plan2)


def test_daily_circuit_breaker_zero_day_start_balance_no_crash():
    j = {"1": rec("EUR_USD", "sl", -25.0, hours_ago=1)}
    try:
        plan = planner.compute_plan(j, day_start_balance=0.0, now=NOW)
        check("zero_day_start_balance_no_crash", plan["daily_loss_limit_hit"] is False, plan)
    except ZeroDivisionError:
        check("zero_day_start_balance_no_crash", False, "raised ZeroDivisionError")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed (of {len(tests)} test functions, {len(PASS)+len(FAIL)} assertions)")
    if FAIL:
        print("FAILED:")
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        sys.exit(1)
    print("ALL JOURNAL/PLANNER TESTS PASSED")
