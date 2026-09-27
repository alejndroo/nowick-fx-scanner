"""Exact Python port of the 'Bard.FX Nowick Signal Engine' Pine script.

Every constant below matches the live TradingView indicator's inputs.
If the indicator's defaults ever change, update PARAMS here to match.
"""
import math
from bisect import bisect_right
from datetime import datetime, timedelta, timezone

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
    session_start_utc=7,
    session_end_utc=13,
)


def _parse_time(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _ema_series(closes: list[float], length: int) -> list[float]:
    """EMA matching Pine's ta.ema: seeded with the SMA of the first `length`
    bars (not the first close), NaN before that seed point.
    """
    n = len(closes)
    out = [float("nan")] * n
    if n < length:
        return out
    seed = sum(closes[:length]) / length
    out[length - 1] = seed
    k = 2 / (length + 1)
    for i in range(length, n):
        out[i] = closes[i] * k + out[i - 1] * (1 - k)
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
    """For each M15 bar, the most recently CONFIRMED H1 EMA value at/just before it.

    OANDA (like Pine) timestamps a candle by its OPEN time, so an H1 bar isn't
    actually closed/confirmed until open_time + 1h. Mirrors Pine's
    request.security(..., lookahead_off), which only ever exposes an H1 bar's
    value from its close onward — never while it's still forming.
    """
    h1_close_times = [_parse_time(c["time"]) + timedelta(hours=1) for c in h1]
    out = []
    for bar in m15:
        t = _parse_time(bar["time"])
        idx = bisect_right(h1_close_times, t) - 1
        out.append(htf_ema[idx] if idx >= 0 else float("nan"))
    return out


def run_engine(pair: str, m15: list[dict], h1: list[dict], pip_size: float) -> dict:
    """Replays the full Pine state machine over historical bars.

    Returns {"trend": 1|-1|0, "pending": {...}|None, "signals": [ {...}, ... ]}
    signals is every BUY/SELL trigger found in the fetched window, oldest first.
    """
    p = PARAMS
    n = len(m15)
    if n < p["struct_lookback"] + p["pivot_len"] * 2 + 5:
        return {"trend": 0, "pending": None, "signals": []}

    closes = [b["close"] for b in m15]
    opens = [b["open"] for b in m15]
    highs = [b["high"] for b in m15]
    lows = [b["low"] for b in m15]

    htf_ema_full = _ema_series([b["close"] for b in h1], p["htf_ema_len"])
    htf_bias = _htf_bias_lookup(m15, h1, htf_ema_full)
    atr = _atr14(m15)

    trend = 0
    swing_low = None
    swing_high = None
    watch_dir = 0
    watch_level = None
    watch_bars_left = 0

    signals = []
    pl = p["struct_lookback"]
    pv = p["pivot_len"]

    for i in range(n):
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

        # ---- session filter ----
        hour_utc = _parse_time(m15[i]["time"]).hour
        s, e = p["session_start_utc"], p["session_end_utc"]
        session_ok = (s <= hour_utc < e) if s <= e else (hour_utc >= s or hour_utc < e)

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
                    signals.append({"time": m15[i]["time"], "dir": "BUY", "entry": entry, "sl": sl, "tp": tp})
                elif watch_dir == -1 and swing_high is not None:
                    sl0 = swing_high + p["sl_buffer_pips"] * pip_size
                    risk = max(sl0 - entry, min_risk)
                    sl = entry + risk
                    tp = entry - risk * p["risk_reward"]
                    signals.append({"time": m15[i]["time"], "dir": "SELL", "entry": entry, "sl": sl, "tp": tp})
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

    return {"trend": trend, "pending": pending, "signals": signals}
