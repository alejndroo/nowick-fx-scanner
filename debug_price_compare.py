"""One-off debug: compare OANDA bid/mid/ask for USD_JPY around a known
TradingView reference bar (time=1790175600, TradingView close=158.286)."""
import os
import requests

OANDA_API_KEY = os.environ["OANDA_API_KEY"]
BASE_URL = "https://api-fxpractice.oanda.com/v3"
HEADERS = {"Authorization": f"Bearer {OANDA_API_KEY}"}

r = requests.get(
    f"{BASE_URL}/instruments/USD_JPY/candles",
    headers=HEADERS,
    params={"granularity": "M15", "count": 5, "price": "BAM"},
    timeout=20,
)
r.raise_for_status()
for c in r.json()["candles"]:
    print(c["time"], "complete=", c["complete"])
    for k in ("bid", "mid", "ask"):
        if k in c:
            print(f"  {k}: O={c[k]['o']} H={c[k]['h']} L={c[k]['l']} C={c[k]['c']}")
