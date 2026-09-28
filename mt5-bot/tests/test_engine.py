"""Real, executable unit tests for engine.py — the actual production signal
logic, imported directly (pure Python, no MT5/Firebase needed). Each test
targets one specific documented behavior and checks it precisely, not just
"doesn't crash".

Run: python3 tests/test_engine.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import engine  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print(f"FAIL: {name}  {detail}")


def iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def flat_h1(n, price, start):
    """n H1 candles, all identical -> ATR-neutral, htf_ema == price exactly."""
    return [{"time": iso(start + timedelta(hours=i)), "open": price, "high": price, "low": price, "close": price} for i in range(n)]


def flat_m15(n, price, start, hour_start=10):
    """n M15 candles, all identical, starting at a fixed UTC hour (inside
    the trading session) so session_ok is true unless a test overrides it."""
    base = start.replace(hour=hour_start, minute=0, second=0, microsecond=0)
    return [{"time": iso(base + timedelta(minutes=15 * i)), "open": price, "high": price, "low": price, "close": price} for i in range(n)]


PIP = 0.0001
BASE_TIME = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)  # a Monday

# engine.py returns an early no-op result ({"trend":0,"pending":None,...}) if
# there aren't at least struct_lookback + pivot_len*2 + 5 bars — a test using
# fewer bars would "pass" by coincidence (the early-return also happens to
# look like "no signal"), not because the logic under test actually ran.
# Confirmed this happened for real: two tests below initially used 30 bars
# against a 35-bar minimum and silently tested nothing.
MIN_BARS = engine.PARAMS["struct_lookback"] + engine.PARAMS["pivot_len"] * 2 + 5


def assert_enough_bars(n):
    assert n >= MIN_BARS, f"test uses {n} bars but engine.py needs >= {MIN_BARS} to do anything — this test would silently test nothing"


# ---------------------------------------------------------------------------
# TEST 1: trend flips bullish on a body-close break above HBC AND above HTF EMA
# ---------------------------------------------------------------------------
def test_trend_flip_bullish():
    n = 40
    assert_enough_bars(n)
    m15 = flat_m15(n, 1.1000, BASE_TIME)
    h1 = flat_h1(60, 1.1000, BASE_TIME)  # htf_ema == 1.1000 everywhere
    # Break bar: close well above both HBC (=1.1000, the flat prior closes)
    # and htf (=1.1000).
    m15[-1]["close"] = 1.1050
    m15[-1]["open"] = 1.1010
    m15[-1]["high"] = 1.1055
    m15[-1]["low"] = 1.1005
    result = engine.run_engine("EUR_USD", m15, h1, PIP, seed=None)
    check("trend_flip_bullish", result["trend"] == 1, f"got trend={result['trend']}")


def test_trend_flip_bearish():
    n = 40
    assert_enough_bars(n)
    m15 = flat_m15(n, 1.1000, BASE_TIME)
    h1 = flat_h1(60, 1.1000, BASE_TIME)
    m15[-1]["close"] = 1.0950
    m15[-1]["open"] = 1.0990
    m15[-1]["high"] = 1.0995
    m15[-1]["low"] = 1.0945
    result = engine.run_engine("EUR_USD", m15, h1, PIP, seed=None)
    check("trend_flip_bearish", result["trend"] == -1, f"got trend={result['trend']}")


# ---------------------------------------------------------------------------
# TEST 2: Nowick is trend-matched only — a perfect bullish wickless candle
# must NOT arm a watch when the seeded trend is bearish.
# ---------------------------------------------------------------------------
def make_seed(trend, swing_low, swing_high, through_time):
    return {
        "trend": trend, "swing_low": swing_low, "swing_high": swing_high,
        "watch_dir": 0, "watch_level": None, "watch_bars_left": 0,
        "seeded_through": iso(through_time),
    }


def test_nowick_trend_matched_only():
    n = 40
    assert_enough_bars(n)
    start = BASE_TIME
    m15 = flat_m15(n, 1.1000, start)
    h1 = flat_h1(60, 1.1000, start)
    seed_through = datetime.fromisoformat(m15[-2]["time"].replace("Z", "+00:00"))
    # With a PERFECTLY flat baseline, hbc == lbc exactly, so ANY deviation in
    # the test candle's close triggers a trend recompute one way or the
    # other — there's no room to place a close that leaves the seeded trend
    # alone. Give the struct_lookback window (the 14 bars before the test
    # candle) slight oscillation so hbc/lbc form a real band, then place the
    # candle's close strictly inside that band.
    pl = engine.PARAMS["struct_lookback"]
    for j in range(n - 1 - pl, n - 1):
        m15[j]["close"] = 1.1005 if j % 2 == 0 else 1.0995
        m15[j]["open"] = m15[j]["high"] = m15[j]["low"] = m15[j]["close"]
    # A clean bullish wickless candle with close(1.1000) strictly between
    # lbc(1.0995) and hbc(1.1005), so it does NOT itself trigger a trend flip.
    m15[-1]["open"] = 1.0985
    m15[-1]["close"] = 1.1000
    m15[-1]["high"] = 1.1002
    m15[-1]["low"] = 1.0984  # lower wick = 0.0001, range = 0.0018 -> ratio ~5.6% < 6% tol

    seed_bearish = make_seed(-1, 1.0900, 1.1100, seed_through)
    result_bearish = engine.run_engine("EUR_USD", m15, h1, PIP, seed=seed_bearish)
    check("nowick_suppressed_when_trend_mismatched", result_bearish["pending"] is None,
          f"expected no pending watch with trend=-1, got {result_bearish['pending']}")

    seed_bullish = make_seed(1, 1.0900, 1.1100, seed_through)
    result_bullish = engine.run_engine("EUR_USD", m15, h1, PIP, seed=seed_bullish)
    check("nowick_arms_when_trend_matched", result_bullish["pending"] is not None and result_bullish["pending"]["dir"] == "BUY",
          f"expected an armed BUY watch with trend=1, got {result_bullish['pending']}")


# ---------------------------------------------------------------------------
# TEST 3: session filter — the same perfect nowick candle must NOT arm a
# watch outside 07:00-20:45 UTC, even with the trend matched.
# ---------------------------------------------------------------------------
def test_session_filter_blocks_arming():
    n = 40
    assert_enough_bars(n)
    start = BASE_TIME
    m15 = flat_m15(n, 1.1000, start, hour_start=10)  # baseline safely inside the session
    h1 = flat_h1(60, 1.1000, start)
    seed_through = datetime.fromisoformat(m15[-2]["time"].replace("Z", "+00:00"))
    # Explicitly override just the LAST bar's timestamp to a fixed,
    # unambiguous out-of-session hour (23:00 UTC, well past the 20:45
    # cutoff) rather than relying on wraparound arithmetic from hour_start.
    out_of_session_time = start.replace(hour=23, minute=0, second=0, microsecond=0)
    m15[-1]["time"] = iso(out_of_session_time)
    m15[-1]["open"] = 1.1000
    m15[-1]["close"] = 1.1020
    m15[-1]["high"] = 1.1021
    m15[-1]["low"] = 1.0999
    last_hour = 23
    seed = make_seed(1, 1.0900, 1.1100, seed_through)
    result = engine.run_engine("EUR_USD", m15, h1, PIP, seed=seed)
    check("session_filter_blocks_outside_window", result["pending"] is None,
          f"bar hour={last_hour} (outside 07:00-20:45), expected no pending, got {result['pending']}")


# ---------------------------------------------------------------------------
# TEST 4: retest touch fires a signal with correct 1:1 R:R SL/TP math.
# ---------------------------------------------------------------------------
def test_retest_touch_signal_math():
    n = 40
    assert_enough_bars(n)
    start = BASE_TIME
    m15 = flat_m15(n, 1.1000, start)
    h1 = flat_h1(60, 1.1000, start)
    # Arm a BUY watch directly via seed (isolates the retest math from the
    # arming logic tested above). seeded_through points at m15[-2] so only
    # the LAST bar gets processed — this flat synthetic baseline is
    # degenerate (every bar ties as both min AND max in the pivot window),
    # so processing more than one bar here would let the pivot-recompute
    # overwrite the seeded swing_low with the flat baseline value before
    # the touch is evaluated (confirmed this happens; not a production bug,
    # just means this test must isolate exactly one bar).
    seed_through = datetime.fromisoformat(m15[-2]["time"].replace("Z", "+00:00"))
    watch_level = 1.1000
    swing_low = 1.0950
    seed = {
        "trend": 1, "swing_low": swing_low, "swing_high": 1.1100,
        "watch_dir": 1, "watch_level": watch_level, "watch_bars_left": 5,
        "seeded_through": iso(seed_through),
    }
    m15[-1]["low"] = 1.0995  # touches (low <= watch_level)
    result = engine.run_engine("EUR_USD", m15, h1, PIP, seed=seed)
    check("retest_touch_emits_one_signal", len(result["signals"]) == 1, f"got {len(result['signals'])} signals")
    if result["signals"]:
        sig = result["signals"][0]
        check("retest_signal_direction", sig["dir"] == "BUY", sig)
        check("retest_signal_entry_equals_watch_level", sig["entry"] == watch_level, sig)
        sl_buffer = engine.PARAMS["sl_buffer_pips"] * PIP
        expected_sl0 = swing_low - sl_buffer
        expected_risk_from_pivot = watch_level - expected_sl0
        # min_risk floor uses ATR — in this flat series ATR==0, so the pivot-based risk wins.
        check("retest_sl_matches_pivot_formula", abs(sig["sl"] - (watch_level - expected_risk_from_pivot)) < 1e-9,
              f"sl={sig['sl']} expected={watch_level - expected_risk_from_pivot}")
        risk = watch_level - sig["sl"]
        check("retest_tp_is_exactly_1to1_RR", abs((sig["tp"] - watch_level) - risk) < 1e-9,
              f"tp-entry={sig['tp']-watch_level} risk={risk}")


# ---------------------------------------------------------------------------
# TEST 5: retest expires after max_retest_bars without a touch.
# ---------------------------------------------------------------------------
def test_retest_expires():
    n = MIN_BARS + engine.PARAMS["max_retest_bars"] + 5
    assert_enough_bars(n)
    start = BASE_TIME
    m15 = flat_m15(n, 1.1000, start)
    h1 = flat_h1(80, 1.1000, start)
    # Price never comes close to watch_level (far away), across more than
    # max_retest_bars bars from the seed point.
    watch_level = 0.5000
    seed_through = datetime.fromisoformat(m15[0]["time"].replace("Z", "+00:00"))
    seed = {
        "trend": 1, "swing_low": 0.4000, "swing_high": 1.1100,
        "watch_dir": 1, "watch_level": watch_level, "watch_bars_left": engine.PARAMS["max_retest_bars"],
        "seeded_through": iso(seed_through),
    }
    result = engine.run_engine("EUR_USD", m15, h1, PIP, seed=seed)
    check("retest_expires_with_no_signal", len(result["signals"]) == 0, result["signals"])
    check("retest_expires_clears_pending", result["pending"] is None, result["pending"])


# ---------------------------------------------------------------------------
# TEST 6: doji / zero-range candle never counts as a nowick (guards div-by-zero).
# ---------------------------------------------------------------------------
def test_zero_range_candle_is_safe():
    n = 40
    assert_enough_bars(n)
    start = BASE_TIME
    m15 = flat_m15(n, 1.1000, start)
    h1 = flat_h1(60, 1.1000, start)
    seed_through = datetime.fromisoformat(m15[-2]["time"].replace("Z", "+00:00"))
    m15[-1]["open"] = m15[-1]["close"] = m15[-1]["high"] = m15[-1]["low"] = 1.1000  # zero range
    seed = make_seed(1, 1.0900, 1.1100, seed_through)
    try:
        result = engine.run_engine("EUR_USD", m15, h1, PIP, seed=seed)
        check("zero_range_no_crash_no_arm", result["pending"] is None, result["pending"])
    except ZeroDivisionError:
        check("zero_range_no_crash_no_arm", False, "raised ZeroDivisionError")


# ---------------------------------------------------------------------------
# TEST 7: ATR floor rejects a technically-wickless but too-small-range candle.
# ---------------------------------------------------------------------------
def test_atr_floor_rejects_tiny_candle():
    n = 40
    assert_enough_bars(n)
    start = BASE_TIME
    # Give the series real volatility so ATR is meaningfully nonzero.
    m15 = flat_m15(n, 1.1000, start)
    for i in range(1, n - 1):
        wobble = 0.0020 if i % 2 == 0 else -0.0020
        m15[i]["high"] = 1.1000 + abs(wobble) + 0.0005
        m15[i]["low"] = 1.1000 - abs(wobble) - 0.0005
        m15[i]["open"] = 1.1000
        m15[i]["close"] = 1.1000 + (wobble / 4)
    h1 = flat_h1(60, 1.1000, start)
    seed_through = datetime.fromisoformat(m15[-2]["time"].replace("Z", "+00:00"))
    # A perfectly wickless candle, but with a TINY range relative to the
    # real ATR built up above.
    m15[-1]["open"] = 1.1000
    m15[-1]["close"] = 1.1001
    m15[-1]["high"] = 1.10011
    m15[-1]["low"] = 1.09999
    seed = make_seed(1, 1.0900, 1.1100, seed_through)
    result = engine.run_engine("EUR_USD", m15, h1, PIP, seed=seed)
    check("atr_floor_rejects_tiny_range_candle", result["pending"] is None,
          f"expected the volatility floor to reject a tiny-range candle, got {result['pending']}")


# ---------------------------------------------------------------------------
# TEST 8: seeded_through never rewinds on stale/truncated data.
# ---------------------------------------------------------------------------
def test_seeded_through_monotonic():
    n = 40
    assert_enough_bars(n)
    start = BASE_TIME
    m15 = flat_m15(n, 1.1000, start)
    h1 = flat_h1(60, 1.1000, start)
    # Seed claims to already be caught up to a time AFTER this whole fetched window.
    future_through = datetime.fromisoformat(m15[-1]["time"].replace("Z", "+00:00")) + timedelta(days=1)
    seed = make_seed(1, 1.0900, 1.1100, future_through)
    result = engine.run_engine("EUR_USD", m15, h1, PIP, seed=seed)
    check("seeded_through_never_rewinds", result["state"]["seeded_through"] == iso(future_through),
          f"got {result['state']['seeded_through']}, expected it to stay at {iso(future_through)}")


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
    print("ALL ENGINE TESTS PASSED")
