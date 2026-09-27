"""Thin client for OANDA's free v20 REST API (practice account).

This is the same underlying feed TradingView uses for OANDA:EURUSD-style
symbols, so candles fetched here match what the Pine indicator sees.
"""
import os
import requests

OANDA_API_KEY = os.environ["OANDA_API_KEY"]
# "practice" (free demo, default) or "live" if the user ever upgrades.
OANDA_ENV = os.environ.get("OANDA_ENV", "practice")

BASE_URL = (
    "https://api-fxpractice.oanda.com/v3"
    if OANDA_ENV == "practice"
    else "https://api-fxtrade.oanda.com/v3"
)

HEADERS = {"Authorization": f"Bearer {OANDA_API_KEY}"}


def fetch_candles(instrument: str, granularity: str, count: int) -> list[dict]:
    """Fetch the last `count` candles for an instrument.

    granularity: "M15" or "H1" (OANDA granularity codes).
    Returns a list of dicts: {time, open, high, low, close, complete}.
    Only COMPLETE candles are returned (the still-forming candle is dropped),
    matching how the Pine script only ever acts on closed bars.
    """
    url = f"{BASE_URL}/instruments/{instrument}/candles"
    params = {
        "granularity": granularity,
        "count": count,
        "price": "M",  # midpoint price (matches TradingView's plotted price)
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    out = []
    for c in data.get("candles", []):
        if not c.get("complete"):
            continue
        mid = c["mid"]
        out.append({
            "time": c["time"],
            "open": float(mid["o"]),
            "high": float(mid["h"]),
            "low": float(mid["l"]),
            "close": float(mid["c"]),
        })
    return out
