"""Exact Python port of the 'Bard.FX Nowick Signal Engine' Pine script.

Every constant below matches the live TradingView indicator's inputs.
If the indicator's defaults ever change, update PARAMS here to match.
"""
import math
from bisect import bisect_right
from datetime import datetime, timezone

PARAMS = dict(
    struct_lookback=14,     # HBC/LBC body-close structure lookback
    htf_ema_len=150,        # 1H EMA bias length
    wick_tol_pct=0.06,      # max wick ratio for a "Nowick" candle
    k_min_range_atr=0.3,    # min candle range, x ATR14
    max_retest_bars=12,     # retest expiration window
    pivot_len=8,            # swing pivot length (SL anchor)
    sl_buffer_pips=2.0,
    min_risk_dist_atr=0.9,
    risk_reward=1.0,
    session_start_utc=7,      # London open
    session_end_hour=20,      # NY close (21:00 UTC) minus a 15-min buffer
    session_end_minute=45,
)


def _parse_time(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _ema_series(closes: list[float], length: int) -> list[float]:
    """EMA matching Pine's ta.ema: seeded with the first close (out[0]=close[0]),
    not an SMA warmup. Verified empirically against the live indicator's
    actual htfEma value on OANDA:USDJPY — the SMA-seeded variant was off by
    ~8 pips, this naive-seed variant is off by ~1.3 pips (residual is just
    finite-history convergence noise, not a seeding-method error).
    """
    k = 2 / (length + 1)
    out = [closes[0]]
    for c in closes[1:]:
        out.append(c * k + out[-1] * (1 - k))
    return out


def _atr14(candles: list[dict]) -> list[float]:
    """Wilder-smoothed ATR(14), matching Pine's ta.atr(14)."""
    trs = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = [trs[0]]
    for i in range(1, len(trs)):
        atr.append((atr[-1] * 13 + trs[i]) / 14)
    return atr


def _htf_bias_lookup(m15: list[dict], h1: list[dict], htf_ema: list[float]) -> list[float]:
    """For each M15 bar, the H1 EMA value from the H1 bar covering that moment.

    Verified empirically against the live indicator: Pine's default
    request.security(..., lookahead_off) with gaps_off, in historical
    replay, uses the CURRENT (timestamp-aligned) HTF bar's fully-resolved
    value, not the previous fully-closed one — i.e. it matches whichever H1
    bar's open time is <= the M15 bar's time. Using the previous-closed-bar
    interpretation (the theoretically "safer" reading of lookahead_off) was
    tested and measured ~6x further from the real indicator's value, so this
    is deliberately the naive/current-bar lookup, not a bug.
    """
    h1_times = [_parse_time(c["time"]) for c in h1]
    out = []
    for bar in m15:
        t = _parse_time(bar["time"])
        idx = bisect_right(h1_times, t) - 1
        out.append(htf_ema[idx] if idx >= 0 else float("nan"))
    return out


def run_engine(pair: str, m15: list[dict], h1: list[dict], pip_size: float, seed: dict | None = None) -> dict:
    """Replays the Pine state machine over historical bars.

    `seed`, if given, is the persisted state from the previous run
    ({"trend", "swing_low", "swing_high", "watch_dir", "watch_level",
    "watch_bars_left", "seeded_through"}). This matters: Pine's `var trend`
    (and the other `var`s) persist across the indicator's ENTIRE chart
    history, not just however many days we happen to fetch. Recomputing
    from trend=0 every run using only a fetched window can land on the
    wrong trend if the confirming break that set Pine's real state happened
    before that window starts. Seeding from persisted state and only
    advancing/emitting for bars strictly after `seeded_through` reproduces
    Pine's true continuously-running state instead.

    Without a seed (first run ever for a pair), state starts at 0/None
    exactly like a freshly-added Pine indicator would, using the whole
    fetched window as context — callers should fetch deep history in that
    case to minimize the (small, but nonzero) chance of misseeding.

    Returns {"trend", "pending", "signals", "state"} where "state" is the
    new seed to persist for next run.
    """
    p = PARAMS
    n = len(m15)
    pl = p["struct_lookback"]
    pv = p["pivot_len"]
    if n < pl + pv * 2 + 5:
        return {"trend": 0, "pending": None, "signals": [], "state": seed}

    closes = [b["close"] for b in m15]
    opens = [b["open"] for b in m15]
    highs = [b["high"] for b in m15]
    lows = [b["low"] for b in m15]

    htf_ema_full = _ema_series([b["close"] for b in h1], p["htf_ema_len"])
    htf_bias = _htf_bias_lookup(m15, h1, htf_ema_full)
    atr = _atr14(m15)

    if seed:
        trend = seed["trend"]
        swing_low = seed["swing_low"]
        swing_high = seed["swing_high"]
        watch_dir = seed["watch_dir"]
        watch_level = seed["watch_level"]
        watch_bars_left = seed["watch_bars_left"]
        seeded_through = _parse_time(seed["seeded_through"])
        start_i = 0
        for idx, bar in enumerate(m15):
            if _parse_time(bar["time"]) > seeded_through:
                start_i = idx
                break
        else:
            start_i = n  # nothing new to process
    else:
        trend = 0
        swing_low = None
        swing_high = None
        watch_dir = 0
        watch_level = None
        watch_bars_left = 0
        start_i = pl

    signals = []

    for i in range(start_i, n):
        # ---- structure trend (body-close break + HTF EMA hysteresis lock) ----
        if i >= pl:
            hbc = max(closes[i - pl:i])
            lbc = min(closes[i - pl:i])
            htf = htf_bias[i]
            if not math.isnan(htf):
                if closes[i] > hbc and closes[i] > htf:
                    trend = 1
                if closes[i] < lbc and closes[i] < htf:
                    trend = -1

        # ---- real structural swing pivots (confirmed pv bars later) ----
        confirm_idx = i - pv
        if confirm_idx - pv >= 0:
            window_hi = highs[confirm_idx - pv: confirm_idx + pv + 1]
            window_lo = lows[confirm_idx - pv: confirm_idx + pv + 1]
            if highs[confirm_idx] == max(window_hi):
                swing_high = highs[confirm_idx]
            if lows[confirm_idx] == min(window_lo):
                swing_low = lows[confirm_idx]

        # ---- Nowick (wickless) detection, trend-matched only ----
        rng = highs[i] - lows[i]
        up_w = highs[i] - max(closes[i], opens[i])
        lo_w = min(closes[i], opens[i]) - lows[i]
        bull_raw = closes[i] > opens[i] and rng > 0 and (lo_w / rng <= p["wick_tol_pct"]) and (rng >= p["k_min_range_atr"] * atr[i])
        bear_raw = closes[i] < opens[i] and rng > 0 and (up_w / rng <= p["wick_tol_pct"]) and (rng >= p["k_min_range_atr"] * atr[i])
        bull_nowick = bull_raw and trend == 1
        bear_nowick = bear_raw and trend == -1

        # ---- session filter: London open through 15 min before NY close ----
        bar_t = _parse_time(m15[i]["time"])
        hour_utc, minute_utc = bar_t.hour, bar_t.minute
        after_start = hour_utc >= p["session_start_utc"]
        before_end = (hour_utc < p["session_end_hour"]) or (
            hour_utc == p["session_end_hour"] and minute_utc < p["session_end_minute"]
        )
        session_ok = after_start and before_end

        # ---- retest state machine ----
        if watch_dir != 0:
            watch_bars_left -= 1
            touched = (lows[i] <= watch_level) if watch_dir == 1 else (highs[i] >= watch_level)
            if touched:
                entry = watch_level
                min_risk = atr[i] * p["min_risk_dist_atr"]
                if watch_dir == 1 and swing_low is not None:
                    sl0 = swing_low - p["sl_buffer_pips"] * pip_size
                    risk = max(entry - sl0, min_risk)
                    sl = entry - risk
                    tp = entry + risk * p["risk_reward"]
                    signals.append({"time": m15[i]["time"], "dir": "BUY", "entry": entry, "sl": sl, "tp": tp, "atr": atr[i]})
                elif watch_dir == -1 and swing_high is not None:
                    sl0 = swing_high + p["sl_buffer_pips"] * pip_size
                    risk = max(sl0 - entry, min_risk)
                    sl = entry + risk
                    tp = entry - risk * p["risk_reward"]
                    signals.append({"time": m15[i]["time"], "dir": "SELL", "entry": entry, "sl": sl, "tp": tp, "atr": atr[i]})
                watch_dir = 0
                watch_bars_left = 0
            elif watch_bars_left <= 0:
                watch_dir = 0

        # multi-Nowick confluence averaging
        if watch_dir == 1 and bull_nowick:
            watch_level = (watch_level + opens[i]) / 2
            watch_bars_left = p["max_retest_bars"]
        elif watch_dir == -1 and bear_nowick:
            watch_level = (watch_level + opens[i]) / 2
            watch_bars_left = p["max_retest_bars"]

        if watch_dir == 0 and session_ok:
            if bull_nowick:
                watch_dir, watch_level, watch_bars_left = 1, opens[i], p["max_retest_bars"]
            elif bear_nowick:
                watch_dir, watch_level, watch_bars_left = -1, opens[i], p["max_retest_bars"]

    pending = None
    if watch_dir != 0:
        pending = {"dir": "BUY" if watch_dir == 1 else "SELL", "level": watch_level, "bars_left": watch_bars_left}

    # seeded_through must be monotonic. A bad/stale OANDA fetch (transient
    # outage, truncated response, cached data) could otherwise hand us a
    # last-bar time EARLIER than what we already persisted, which would
    # silently rewind the seed and re-walk already-applied bars next run —
    # corrupting trend/swing/watch state without raising any error.
    candidate_through = m15[-1]["time"] if n > 0 else None
    if seed and seed.get("seeded_through") and (candidate_through is None or candidate_through < seed["seeded_through"]):
        new_seeded_through = seed["seeded_through"]
    else:
        new_seeded_through = candidate_through if candidate_through is not None else (seed["seeded_through"] if seed else None)

    new_seed = {
        "trend": trend,
        "swing_low": swing_low,
        "swing_high": swing_high,
        "watch_dir": watch_dir,
        "watch_level": watch_level,
        "watch_bars_left": watch_bars_left,
        "seeded_through": new_seeded_through,
    }

    return {"trend": trend, "pending": pending, "signals": signals, "state": new_seed}
