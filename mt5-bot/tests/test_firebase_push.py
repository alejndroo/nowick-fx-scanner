"""Real, executable unit tests for firebase_push.py's money-critical
classification logic, using fake MetaTrader5 + firebase_admin modules
(see mocks.py). Imports and exercises the ACTUAL production code."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mocks import install_fake_mt5, install_fake_firebase_admin, FakeSymbolInfo, FakePosition, FakeAccount, FakeDeal  # noqa: E402

fake_mt5 = install_fake_mt5()
fake_db = install_fake_firebase_admin()
import firebase_push  # noqa: E402  (must import AFTER installing fakes)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print(f"FAIL: {name}  {detail}")


def reset_all():
    fake_mt5.reset()
    fake_db.data = {}
    firebase_push._initialized = False


# ---------------------------------------------------------------------------
# get_risk_pct: dashboard override, clamping, and safe fallback.
# ---------------------------------------------------------------------------
def test_get_risk_pct_uses_default_when_unset():
    reset_all()
    result = firebase_push.get_risk_pct(0.175)
    check("risk_pct_default_when_unset", result == 0.175, result)


def test_get_risk_pct_uses_dashboard_override():
    reset_all()
    fake_db.data = {"account": {"risk_pct": 12}}
    result = firebase_push.get_risk_pct(0.175)
    check("risk_pct_dashboard_override_applied", abs(result - 0.12) < 1e-9, result)


def test_get_risk_pct_rejects_out_of_range_override():
    reset_all()
    fake_db.data = {"account": {"risk_pct": 150}}  # >100%, must fall back to default
    result = firebase_push.get_risk_pct(0.175)
    check("risk_pct_rejects_over_100", result == 0.175, result)

    fake_db.data = {"account": {"risk_pct": 0}}  # not > 0, must fall back
    result2 = firebase_push.get_risk_pct(0.175)
    check("risk_pct_rejects_zero", result2 == 0.175, result2)


def test_get_risk_pct_never_raises_on_firebase_failure():
    reset_all()

    def broken_reference(path):
        raise ConnectionError("simulated Firebase outage")
    fake_db.reference = broken_reference
    try:
        result = firebase_push.get_risk_pct(0.175)
        check("risk_pct_degrades_on_firebase_outage", result == 0.175, result)
    except Exception as e:
        check("risk_pct_degrades_on_firebase_outage", False, f"raised {e!r} instead of degrading")
    finally:
        fake_db.reference = fake_db.__class__.reference.__get__(fake_db)


# ---------------------------------------------------------------------------
# sync_to_firebase: the classification bug that was confirmed live — a real
# loss must NEVER be recorded as a win, and an empty deal lookup must NEVER
# record a fake $0 result (both were real, confirmed production bugs).
# ---------------------------------------------------------------------------
def setup_account(balance=100.0, equity=100.0):
    fake_mt5._account = FakeAccount(balance=balance, equity=equity)


def test_classification_real_loss_is_never_a_win():
    reset_all()
    setup_account()
    fake_db.data = {"trades": {"555": {"status": "open", "pair": "EURCAD", "dir": "BUY", "sl": 1.6089, "opened_at": "2026-09-28T13:00:00Z"}}}
    fake_mt5._positions = []  # position no longer open -> detected as closed
    fake_mt5._deals_by_position[555] = [
        FakeDeal(profit=-40.0, swap=-5.0, commission=-5.30, entry=1, price=1.6089, position_id=555),  # total -50.30, matches the real incident
    ]
    firebase_push.sync_to_firebase({})
    status = fake_db.data["trades"]["555"]["status"]
    pnl = fake_db.data["trades"]["555"]["pnl_gbp"]
    check("real_loss_recorded_as_loss", status == "sl", status)
    check("real_loss_pnl_correct", abs(pnl - (-50.30)) < 1e-6, pnl)


def test_classification_empty_deal_lookup_defers_not_fakes_zero():
    reset_all()
    setup_account()
    fake_db.data = {"trades": {"777": {"status": "open", "pair": "GBPCAD", "dir": "BUY", "sl": 1.87586, "opened_at": "2026-09-28T13:00:00Z"}}}
    fake_mt5._positions = []  # closed
    # No entry in _deals_by_position for 777 -> history_deals_get(position=777) returns []
    firebase_push.sync_to_firebase({})
    status = fake_db.data["trades"]["777"]["status"]
    check("empty_deal_lookup_leaves_status_open_not_fake_win", status == "open",
          f"expected 'open' (deferred), got {status!r} — this is the exact bug that recorded a real -$46 loss as a $0 win")


def test_classification_force_closed_is_manual_not_tp():
    reset_all()
    setup_account()
    fake_db.data = {"trades": {"888": {"status": "open", "pair": "EURUSD", "dir": "BUY", "sl": 1.1350, "force_closed": True, "opened_at": "2026-09-28T13:00:00Z"}}}
    fake_mt5._positions = []
    fake_mt5._deals_by_position[888] = [FakeDeal(profit=3.0, entry=1, price=1.1360, position_id=888)]
    firebase_push.sync_to_firebase({})
    status = fake_db.data["trades"]["888"]["status"]
    check("force_closed_is_manual_not_tp", status == "manual", status)


def test_classification_real_win_is_tp():
    reset_all()
    setup_account()
    fake_db.data = {"trades": {"999": {"status": "open", "pair": "GBP_JPY", "dir": "SELL", "sl": 210.104, "opened_at": "2026-09-28T13:00:00Z"}}}
    fake_mt5._positions = []
    fake_mt5._deals_by_position[999] = [FakeDeal(profit=14.77, entry=1, price=208.049, position_id=999)]
    firebase_push.sync_to_firebase({})
    status = fake_db.data["trades"]["999"]["status"]
    check("real_win_recorded_as_win", status == "tp", status)


# ---------------------------------------------------------------------------
# reclassify_existing: fixes a wrong status, repairs a corrupted zero-pnl
# record via re-fetch, leaves correct records untouched.
# ---------------------------------------------------------------------------
def test_reclassify_fixes_wrong_status():
    reset_all()
    fake_db.data = {"trades": {"1": {"status": "tp", "pnl_gbp": -5.0, "pair": "EURUSD"}}}
    fixed = firebase_push.reclassify_existing({})
    check("reclassify_fixes_sign_mismatch", fake_db.data["trades"]["1"]["status"] == "sl", fake_db.data["trades"]["1"])
    check("reclassify_reports_one_fix", fixed == 1, fixed)


def test_reclassify_repairs_corrupted_zero_pnl():
    reset_all()
    fake_db.data = {"trades": {"2": {"status": "tp", "pnl_gbp": 0, "pair": "EURCAD"}}}
    fake_mt5._deals_by_position[2] = [FakeDeal(profit=-46.30, entry=1, position_id=2)]
    firebase_push.reclassify_existing({})
    check("corrupted_zero_repaired_status", fake_db.data["trades"]["2"]["status"] == "sl", fake_db.data["trades"]["2"])
    check("corrupted_zero_repaired_pnl", abs(fake_db.data["trades"]["2"]["pnl_gbp"] - (-46.30)) < 1e-6, fake_db.data["trades"]["2"])


def test_reclassify_leaves_correct_records_alone():
    reset_all()
    fake_db.data = {"trades": {"3": {"status": "tp", "pnl_gbp": 14.94, "pair": "EURCAD"}}}
    fixed = firebase_push.reclassify_existing({})
    check("correct_record_untouched", fake_db.data["trades"]["3"]["status"] == "tp", fake_db.data["trades"]["3"])
    check("correct_record_not_counted_as_fixed", fixed == 0, fixed)


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
    print("ALL FIREBASE_PUSH TESTS PASSED")
