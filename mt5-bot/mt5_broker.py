"""MetaTrader5 broker adapter: candle fetching, symbol mapping, order execution.

Run this ONLY on Windows with the MT5 desktop terminal installed, running,
and logged into the account you want traded. MetaTrader5's Python package
talks to that local terminal process — there is no cloud/headless MT5 API,
which is why the signal-only bot uses OANDA's REST API instead.
"""
from datetime import datetime, timezone

import MetaTrader5 as mt5

TF_MAP = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}

MAGIC = 990033  # tags every order/position this bot opens, so force-close/count logic never touches manual trades


def mt5_symbol(oanda_pair: str, suffix: str = "") -> str:
    """'EUR_USD' -> 'EURUSD' + optional broker suffix (e.g. '.m', 'm', '-ECN').

    Check your broker's Market Watch for the exact spelling and set
    SYMBOL_SUFFIX in config.py if it doesn't match plain 'EURUSD'.
    """
    return oanda_pair.replace("_", "") + suffix


def connect(login: int, password: str, server: str) -> None:
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
    if not mt5.login(login, password=password, server=server):
        raise RuntimeError(f"MT5 login() failed: {mt5.last_error()}")


def is_connected() -> bool:
    """True only if the terminal IPC link AND the trade account session are
    both actually alive. mt5.* calls return None/empty on either kind of
    disconnect without raising, which otherwise looks identical to a normal
    "this symbol isn't offered" skip — this is the one place that tells the
    two apart.
    """
    return mt5.terminal_info() is not None and mt5.account_info() is not None


def reconnect(login: int, password: str, server: str) -> bool:
    """Best-effort recovery from a dropped terminal/account session."""
    try:
        mt5.shutdown()
    except Exception:
        pass
    try:
        connect(login, password, server)
        return True
    except Exception:
        return False


def fetch_candles(symbol: str, granularity: str, count: int) -> list[dict]:
    """Same shape as oanda.fetch_candles: only CLOSED candles, oldest -> newest.

    start_pos=1 skips index 0, which is the still-forming (incomplete) bar —
    matching how the OANDA feed only ever hands the engine complete candles.
    """
    rates = mt5.copy_rates_from_pos(symbol, TF_MAP[granularity], 1, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"copy_rates_from_pos failed for {symbol}: {mt5.last_error()}")
    out = []
    for r in rates:
        t = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        out.append({
            "time": t,
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        })
    return out


def lots_for_risk(symbol: str, risk_amount: float, sl_distance_price: float) -> float:
    """Position size so a full SL hit loses exactly `risk_amount` (account currency)."""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"symbol_info failed for {symbol}")
    tick_value = info.trade_tick_value
    tick_size = info.trade_tick_size
    if tick_size == 0 or tick_value == 0:
        raise RuntimeError(f"bad tick_value/tick_size for {symbol}")
    value_per_price_unit_per_lot = tick_value / tick_size
    loss_per_lot = sl_distance_price * value_per_price_unit_per_lot
    if loss_per_lot <= 0:
        raise RuntimeError(f"non-positive loss_per_lot for {symbol}")
    lots = risk_amount / loss_per_lot
    step = info.volume_step or 0.01
    lots = round(lots / step) * step
    lots = max(info.volume_min, min(info.volume_max, lots))
    return round(lots, 2)


def open_positions_count(magic: int = MAGIC) -> int:
    positions = mt5.positions_get()
    if not positions:
        return 0
    return sum(1 for p in positions if p.magic == magic)


# The MetaTrader5 Python package doesn't expose SYMBOL_FILLING_FOK/IOC as
# named attributes even though the bitmask values are stable/documented in
# the underlying MQL5 API — using mt5.SYMBOL_FILLING_IOC raises
# AttributeError (confirmed live: this crashed every single order until
# fixed). Using the raw bit values directly instead.
_SYMBOL_FILLING_FOK = 1
_SYMBOL_FILLING_IOC = 2


def _filling_type(info) -> int:
    """Brokers only accept specific fill modes per symbol (a bitmask on
    symbol_info.filling_mode) — hardcoding IOC silently fails every single
    order on any broker/symbol that doesn't support it. Prefer IOC, then
    FOK, then fall back to RETURN (always accepted)."""
    mode = info.filling_mode
    if mode & _SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    if mode & _SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def enforce_min_stop_distance(symbol: str, price: float, sl: float, tp: float) -> tuple[float, float]:
    """Widens sl/tp outward if they're inside the broker's minimum stop
    distance for this symbol — otherwise order_send rejects the order with
    no way to tell it apart from any other failure."""
    info = mt5.symbol_info(symbol)
    if info is None or not info.trade_stops_level:
        return sl, tp
    min_dist = info.trade_stops_level * info.point
    if abs(price - sl) < min_dist:
        sl = price - min_dist if sl < price else price + min_dist
    if abs(price - tp) < min_dist:
        tp = price + min_dist if tp > price else price - min_dist
    return sl, tp


def place_order(symbol: str, direction: str, lots: float, sl: float, tp: float, deviation: int = 20) -> dict:
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if tick is None or info is None:
        raise RuntimeError(f"symbol_info/tick failed for {symbol}")
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if direction == "BUY" else tick.bid
    sl, tp = enforce_min_stop_distance(symbol, price, sl, tp)
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": deviation,
        "magic": MAGIC,
        "comment": "NowickBot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _filling_type(info),
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result is not None else mt5.last_error()
        raise RuntimeError(f"order_send failed for {symbol}: retcode={code} full={result}")
    return {"ticket": result.order, "price": result.price}


def close_all(magic: int = MAGIC) -> int:
    """Force-flatten every open position this bot opened. Returns count closed."""
    positions = mt5.positions_get()
    if not positions:
        return 0
    closed = 0
    for pos in positions:
        if pos.magic != magic:
            continue
        tick = mt5.symbol_info_tick(pos.symbol)
        info = mt5.symbol_info(pos.symbol)
        if tick is None or info is None:
            continue
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": price,
            "deviation": 20,
            "magic": magic,
            "comment": "NowickBot close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling_type(info),
        }
        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            closed += 1
        else:
            print(f"close_all: failed to close {pos.symbol} ticket {pos.ticket}: {result}")
    return closed
