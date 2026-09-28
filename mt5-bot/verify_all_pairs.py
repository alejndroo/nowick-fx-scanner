"""Per-pair proof, not extrapolation.

Every earlier "it works" claim in this project was based on testing ONE
pair (usually EURUSD) and assuming the rest behave the same. Two real bugs
slipped through that way (a filling-mode AttributeError, a close_all() that
touched unrelated positions) before this script existed. This tests EVERY
pair individually and prints a hard pass/fail table — check the table
yourself, don't take a summary sentence's word for it.

Usage:
  python verify_all_pairs.py scan            # free: candle-fetch only, all 27 pairs
  python verify_all_pairs.py execute         # REAL MONEY: places + immediately closes
                                              # one minimum-lot order per pair (27 round
                                              # trips, real spread cost, but tiny)
  python verify_all_pairs.py execute EUR_USD GBP_JPY   # just these pairs
"""
import sys
import time

import MetaTrader5 as mt5

import config
import mt5_broker as broker
from pairs import PAIRS, display_symbol


def verify_scan(pairs: list[str]) -> None:
    broker.connect(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER)
    print(f"{'PAIR':10s} {'SYMBOL EXISTS':15s} {'M15 FETCH':12s} {'H1 FETCH':12s} RESULT")
    print("-" * 70)
    fail_count = 0
    for pair in pairs:
        symbol = broker.mt5_symbol(pair, config.SYMBOL_SUFFIX)
        exists = mt5.symbol_info(symbol) is not None
        m15_ok = h1_ok = False
        if exists:
            mt5.symbol_select(symbol, True)
            try:
                m15 = broker.fetch_candles(symbol, "M15", 50)
                m15_ok = len(m15) > 0
            except Exception:
                m15_ok = False
            try:
                h1 = broker.fetch_candles(symbol, "H1", 50)
                h1_ok = len(h1) > 0
            except Exception:
                h1_ok = False
        result = "PASS" if (exists and m15_ok and h1_ok) else "FAIL"
        if result == "FAIL":
            fail_count += 1
        print(f"{display_symbol(pair):10s} {str(exists):15s} {str(m15_ok):12s} {str(h1_ok):12s} {result}")

    print("-" * 70)
    print(f"{len(pairs) - fail_count}/{len(pairs)} pairs PASS scan verification.")
    if fail_count:
        print(f"{fail_count} pair(s) FAILED — these will NEVER produce a signal until fixed (check SYMBOL_SUFFIX / broker offering).")


def verify_execute(pairs: list[str]) -> None:
    broker.connect(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER)
    account = mt5.account_info()
    print(f"Account {account.login} — balance {account.balance} {account.currency}")
    print(f"About to place + immediately close {len(pairs)} minimum-lot orders (real spread cost, tiny).")
    print(f"{'PAIR':10s} {'TICKET':12s} {'FILL PRICE':12s} {'CLOSED':8s} RESULT")
    print("-" * 70)
    fail_count = 0
    for pair in pairs:
        symbol = broker.mt5_symbol(pair, config.SYMBOL_SUFFIX)
        if mt5.symbol_info(symbol) is None:
            print(f"{display_symbol(pair):10s} {'—':12s} {'—':12s} {'—':8s} FAIL (symbol not found)")
            fail_count += 1
            continue
        mt5.symbol_select(symbol, True)
        try:
            tick = mt5.symbol_info_tick(symbol)
            info = mt5.symbol_info(symbol)
            fill_price = tick.ask
            dist = 50 * info.point * 10
            sl = fill_price - dist
            tp = fill_price + dist
            lots = info.volume_min
            res = broker.place_order(symbol, "BUY", lots, sl, tp)
            closed = broker.close_ticket(res["ticket"])
            result = "PASS" if closed else "FAIL (order OK, close failed)"
            if "FAIL" in result:
                fail_count += 1
            print(f"{display_symbol(pair):10s} {res['ticket']:<12} {res['price']:<12} {str(closed):8s} {result}")
        except Exception as e:
            print(f"{display_symbol(pair):10s} {'—':12s} {'—':12s} {'—':8s} FAIL: {e}")
            fail_count += 1
        time.sleep(0.5)  # avoid hammering the broker's rate limits

    print("-" * 70)
    print(f"{len(pairs) - fail_count}/{len(pairs)} pairs PASS live execution verification.")
    if fail_count:
        print(f"{fail_count} pair(s) FAILED — trading on these specific pairs will NOT work until fixed.")
    else:
        print("Every pair independently confirmed: real order placed, real ticket returned, cleanly closed.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("scan", "execute"):
        print(__doc__)
        sys.exit(1)

    target_pairs = sys.argv[2:] if len(sys.argv) > 2 else PAIRS
    if sys.argv[1] == "scan":
        verify_scan(target_pairs)
    else:
        verify_execute(target_pairs)
