"""One-off debug: enumerate every historical signal the engine finds for
USD_JPY, to compare entry/SL/TP against what the live indicator printed."""
import os
from oanda import fetch_candles
from engine import run_engine

pair = "USD_JPY"
m15 = fetch_candles(pair, "M15", count=5000)
h1 = fetch_candles(pair, "H1", count=5000)
result = run_engine(pair, m15, h1, 0.01)  # JPY pip size

print(f"Total signals found: {len(result['signals'])}")
for s in result["signals"]:
    print(f"{s['time']}  {s['dir']}  entry={s['entry']:.3f}  sl={s['sl']:.3f}  tp={s['tp']:.3f}")
