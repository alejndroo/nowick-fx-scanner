"""Sanity-check script for the MT5 auto-trader. Run this BEFORE trusting
bot.py to run unattended.

Usage (from inside mt5-bot's folder, e.g. C:\\NowickBot):

  python test_bot.py signals              # read-only: show recent real signals for all 27 pairs
  python test_bot.py signals EUR_USD      # just one pair
  python test_bot.py trade EUR_USD BUY    # places a REAL 0.01-lot trade, then closes it immediately

"signals" proves MT5 connectivity + the engine can see real market data.
"trade" proves order execution actually works end-to-end. It uses the
smallest possible lot size and closes the position within seconds, so the
market-risk window is tiny, but it is still a REAL trade with REAL spread
cost — don't run it more than you need to.
"""
import sys
from datetime import datetime, timezone

import MetaTrader5 as mt5

import config
import mt5_broker as broker
from engine import run_engine
from pairs import PAIRS, pip_size, display_symbol


def show_signals(pairs: list[str]) -> None:
    broker.connect(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER)
    account = mt5.account_info()
    print(f"Connected. Account {account.login} — balance {account.balance} {account.currency}, equity {account.equity}\n")

    any_found = False
    for pair in pairs:
        symbol = broker.mt5_symbol(pair, config.SYMBOL_SUFFIX)
        if mt5.symbol_info(symbol) is None:
            print(f"{display_symbol(pair):10s}  symbol '{symbol}' not found on this broker — check SYMBOL_SUFFIX")
            continue
        mt5.symbol_select(symbol, True)
        try:
            m15 = broker.fetch_candles(symbol, "M15", 5000)
            h1 = broker.fetch_candles(symbol, "H1", 5000)
        except Exception as e:
            print(f"{display_symbol(pair):10s}  fetch failed: {e}")
            continue
        if len(m15) < 100 or len(h1) < 160:
            print(f"{display_symbol(pair):10s}  not enough history yet")
            continue

        result = run_engine(pair, m15, h1, pip_size(pair), seed=None)
        trend_label = {1: "BULLISH", -1: "BEARISH", 0: "neutral"}[result["trend"]]
        sigs = result["signals"]
        print(f"{display_symbol(pair):10s}  trend={trend_label:8s}  {len(sigs)} signal(s) in fetched history")
        for s in sigs[-3:]:  # last 3 max, so output stays readable
            any_found = True
            d = 3 if pair.endswith("JPY") else 5
            print(f"    {s['time']}  {s['dir']:4s}  entry={s['entry']:.{d}f}  sl={s['sl']:.{d}f}  tp={s['tp']:.{d}f}")

    if not any_found:
        print("\nNo signals found in the fetched window for these pairs — that's normal, they're infrequent. "
              "Try more pairs, or trust that bot.py will catch the next one live.")


def test_trade(pair: str, direction: str) -> None:
    broker.connect(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER)
    symbol = broker.mt5_symbol(pair, config.SYMBOL_SUFFIX)
    if mt5.symbol_info(symbol) is None:
        print(f"Symbol '{symbol}' not found — check SYMBOL_SUFFIX in config.py")
        return
    mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    fill_price = tick.ask if direction == "BUY" else tick.bid
    dist = 50 * info.point * 10  # a wide, harmless SL/TP just so the order is valid; not risk-sized
    sl = fill_price - dist if direction == "BUY" else fill_price + dist
    tp = fill_price + dist if direction == "BUY" else fill_price - dist
    lots = info.volume_min  # smallest possible size

    print(f"Placing a REAL {lots} lot {direction} on {symbol} at ~{fill_price} ...")
    res = broker.place_order(symbol, direction, lots, sl, tp)
    print(f"Order filled: ticket {res['ticket']} at {res['price']}")

    print("Closing ONLY this test position (never touches any other open trade)...")
    ok = broker.close_ticket(res["ticket"])
    print(f"Closed: {ok}. Test complete — execution pipeline works." if ok
          else f"WARNING: could not auto-close ticket {res['ticket']} — close it manually in MT5.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("signals", "trade"):
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "signals":
        pairs = [sys.argv[2]] if len(sys.argv) > 2 else PAIRS
        show_signals(pairs)
    else:
        if len(sys.argv) != 4 or sys.argv[3] not in ("BUY", "SELL"):
            print("Usage: python test_bot.py trade EUR_USD BUY")
            sys.exit(1)
        test_trade(sys.argv[2], sys.argv[3])
