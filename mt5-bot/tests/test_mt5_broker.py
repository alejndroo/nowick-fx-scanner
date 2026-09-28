"""Real, executable unit tests for mt5_broker.py's money-critical logic,
using a fake MetaTrader5 module (see mocks.py) since the real one is
Windows-only. Imports and exercises the ACTUAL production mt5_broker.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mocks import install_fake_mt5, FakeSymbolInfo, FakeTick, FakePosition, FakeAccount, FakeOrderResult  # noqa: E402

fake_mt5 = install_fake_mt5()
import mt5_broker as broker  # noqa: E402  (must import AFTER installing the fake module)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print(f"FAIL: {name}  {detail}")


# ---------------------------------------------------------------------------
# lots_for_risk: basic sizing math is correct.
# ---------------------------------------------------------------------------
def test_lots_for_risk_basic_math():
    fake_mt5.reset()
    fake_mt5._symbol_info["EURUSD"] = FakeSymbolInfo(trade_tick_value=1.0, trade_tick_size=0.0001, volume_step=0.01, volume_min=0.01, volume_max=100.0)
    # risk_amount=$20, sl_distance=0.0020 -> loss_per_lot = 0.0020 * (1.0/0.0001) = $20/lot -> 1.0 lot
    lots = broker.lots_for_risk("EURUSD", risk_amount=20.0, sl_distance_price=0.0020)
    check("lots_for_risk_basic_math", abs(lots - 1.0) < 1e-9, lots)


def test_lots_for_risk_rounds_to_volume_step():
    fake_mt5.reset()
    fake_mt5._symbol_info["EURUSD"] = FakeSymbolInfo(trade_tick_value=1.0, trade_tick_size=0.0001, volume_step=0.1, volume_min=0.1, volume_max=100.0)
    lots = broker.lots_for_risk("EURUSD", risk_amount=23.0, sl_distance_price=0.0020)
    # raw = 23/20 = 1.15 -> rounds to nearest 0.1 -> 1.2 (or 1.1, either way must be a clean multiple of 0.1)
    check("lots_rounds_to_step", abs(round(lots / 0.1) * 0.1 - lots) < 1e-9, lots)


def test_lots_for_risk_clamps_to_volume_min():
    fake_mt5.reset()
    fake_mt5._symbol_info["EURUSD"] = FakeSymbolInfo(trade_tick_value=1.0, trade_tick_size=0.0001, volume_step=0.01, volume_min=0.01, volume_max=100.0)
    # tiny risk_amount -> raw lots far below volume_min
    lots = broker.lots_for_risk("EURUSD", risk_amount=0.01, sl_distance_price=0.0020)
    check("lots_clamped_to_volume_min", lots == 0.01, lots)


def test_lots_for_risk_safety_ceiling_refuses_oversized_trade():
    fake_mt5.reset()
    fake_mt5._symbol_info["EURUSD"] = FakeSymbolInfo(trade_tick_value=1.0, trade_tick_size=0.0001, volume_step=0.01, volume_min=0.01, volume_max=100.0)
    # This sizing would predict a $20 worst-case loss (1.0 lot * $20/lot),
    # but the ceiling only allows $10 -> must raise, not silently oversize.
    try:
        broker.lots_for_risk("EURUSD", risk_amount=20.0, sl_distance_price=0.0020, max_risk_amount=10.0)
        check("safety_ceiling_refuses_oversized_trade", False, "did not raise")
    except RuntimeError as e:
        check("safety_ceiling_refuses_oversized_trade", "safety ceiling" in str(e), str(e))


def test_lots_for_risk_within_ceiling_is_allowed():
    fake_mt5.reset()
    fake_mt5._symbol_info["EURUSD"] = FakeSymbolInfo(trade_tick_value=1.0, trade_tick_size=0.0001, volume_step=0.01, volume_min=0.01, volume_max=100.0)
    try:
        lots = broker.lots_for_risk("EURUSD", risk_amount=20.0, sl_distance_price=0.0020, max_risk_amount=25.0)
        check("within_ceiling_allowed", abs(lots - 1.0) < 1e-9, lots)
    except RuntimeError as e:
        check("within_ceiling_allowed", False, f"raised unexpectedly: {e}")


# ---------------------------------------------------------------------------
# _filling_type: picks IOC/FOK/RETURN based on the symbol's actual bitmask,
# never a hardcoded value (this exact bug caused every real order to fail
# live earlier — SYMBOL_FILLING_IOC/FOK aren't valid mt5 attributes).
# ---------------------------------------------------------------------------
def test_filling_type_prefers_ioc():
    info = FakeSymbolInfo(filling_mode=2)  # bit 1 (IOC) set
    check("filling_type_ioc", broker._filling_type(info) == fake_mt5.ORDER_FILLING_IOC, broker._filling_type(info))


def test_filling_type_falls_back_to_fok():
    info = FakeSymbolInfo(filling_mode=1)  # bit 0 (FOK) set, IOC not set
    check("filling_type_fok", broker._filling_type(info) == fake_mt5.ORDER_FILLING_FOK, broker._filling_type(info))


def test_filling_type_falls_back_to_return():
    info = FakeSymbolInfo(filling_mode=0)  # neither bit set
    check("filling_type_return", broker._filling_type(info) == fake_mt5.ORDER_FILLING_RETURN, broker._filling_type(info))


# ---------------------------------------------------------------------------
# enforce_min_stop_distance: widens a too-tight SL/TP, leaves a fine one alone.
# ---------------------------------------------------------------------------
def test_enforce_min_stop_distance_widens_tight_sl():
    fake_mt5.reset()
    fake_mt5._symbol_info["EURUSD"] = FakeSymbolInfo(trade_stops_level=50, point=0.0001)  # min dist = 50*0.0001 = 0.0050
    price = 1.1000
    sl_too_tight = 1.0999  # only 0.0001 away, below the 0.0050 minimum
    tp_too_tight = 1.1001
    sl, tp = broker.enforce_min_stop_distance("EURUSD", price, sl_too_tight, tp_too_tight)
    check("sl_widened_to_min_distance", abs((price - sl) - 0.0050) < 1e-9, sl)
    check("tp_widened_to_min_distance", abs((tp - price) - 0.0050) < 1e-9, tp)


def test_enforce_min_stop_distance_leaves_fine_sl_alone():
    fake_mt5.reset()
    fake_mt5._symbol_info["EURUSD"] = FakeSymbolInfo(trade_stops_level=50, point=0.0001)
    price = 1.1000
    sl_fine = 1.0900  # 0.0100 away, well above the 0.0050 minimum
    tp_fine = 1.1100
    sl, tp = broker.enforce_min_stop_distance("EURUSD", price, sl_fine, tp_fine)
    check("fine_sl_untouched", sl == sl_fine, sl)
    check("fine_tp_untouched", tp == tp_fine, tp)


# ---------------------------------------------------------------------------
# total_open_risk: sums worst-case loss across positions, skips ones with no SL.
# ---------------------------------------------------------------------------
def test_total_open_risk_sums_correctly():
    fake_mt5.reset()
    fake_mt5._symbol_info["EURUSD"] = FakeSymbolInfo(trade_tick_value=1.0, trade_tick_size=0.0001)
    fake_mt5._symbol_info["GBPUSD"] = FakeSymbolInfo(trade_tick_value=1.0, trade_tick_size=0.0001)
    fake_mt5._positions = [
        FakePosition(ticket=1, symbol="EURUSD", type=0, volume=1.0, price_open=1.1000, sl=1.0980, tp=1.1040, profit=0, magic=990033),  # risk = 0.0020*10000*1.0 = $20
        FakePosition(ticket=2, symbol="GBPUSD", type=0, volume=0.5, price_open=1.3000, sl=1.2950, tp=1.3050, profit=0, magic=990033),  # risk = 0.0050*10000*0.5 = $25
        FakePosition(ticket=3, symbol="EURUSD", type=0, volume=5.0, price_open=1.1000, sl=1.0980, tp=1.1040, profit=0, magic=111111),  # different magic -> excluded
        FakePosition(ticket=4, symbol="EURUSD", type=0, volume=1.0, price_open=1.1000, sl=0.0, tp=0.0, profit=0, magic=990033),  # no SL -> excluded (can't compute)
    ]
    total = broker.total_open_risk()
    check("total_open_risk_sums_only_our_magic_with_sl", abs(total - 45.0) < 1e-6, total)


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
    print("ALL MT5_BROKER TESTS PASSED")
