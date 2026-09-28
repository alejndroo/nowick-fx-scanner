"""Fake MetaTrader5 and firebase_admin modules so the REAL production code
(mt5_broker.py, firebase_push.py) can be imported and tested on a machine
that has neither (MetaTrader5 is Windows-only; firebase_admin isn't
installed here). Install these into sys.modules BEFORE importing the
modules under test.
"""
import sys
import types
from dataclasses import dataclass, field


class FakeMT5(types.ModuleType):
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 60
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1

    def __init__(self):
        super().__init__("MetaTrader5")
        self.reset()

    def reset(self):
        self._symbol_info = {}
        self._symbol_tick = {}
        self._positions = []
        self._account = None
        self._terminal_alive = True
        self._deals_by_position = {}
        self._last_order_request = None
        self._order_send_result = None
        self._last_error = (1, "no error")

    def initialize(self):
        return True

    def login(self, *a, **k):
        return True

    def shutdown(self):
        return True

    def terminal_info(self):
        return object() if self._terminal_alive else None

    def account_info(self):
        return self._account

    def symbol_info(self, symbol):
        return self._symbol_info.get(symbol)

    def symbol_info_tick(self, symbol):
        return self._symbol_tick.get(symbol)

    def symbol_select(self, symbol, enable):
        return True

    def positions_get(self, ticket=None):
        if ticket is not None:
            return [p for p in self._positions if p.ticket == ticket]
        return list(self._positions)

    def order_send(self, request):
        self._last_order_request = request
        return self._order_send_result

    def history_deals_get(self, *args, position=None, **kwargs):
        if position is not None:
            return self._deals_by_position.get(position, [])
        return [d for deals in self._deals_by_position.values() for d in deals]

    def last_error(self):
        return self._last_error


@dataclass
class FakeSymbolInfo:
    trade_tick_value: float = 1.0
    trade_tick_size: float = 0.0001
    volume_step: float = 0.01
    volume_min: float = 0.01
    volume_max: float = 100.0
    filling_mode: int = 2  # bit 1 = IOC by default in tests
    trade_stops_level: int = 0
    point: float = 0.0001


@dataclass
class FakeTick:
    ask: float = 1.1000
    bid: float = 1.0999


@dataclass
class FakePosition:
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    magic: int
    time: float = 0.0


@dataclass
class FakeAccount:
    balance: float = 100.0
    equity: float = 100.0
    currency: str = "USD"
    margin_mode: int = 2
    login: int = 12345


@dataclass
class FakeDeal:
    profit: float
    swap: float = 0.0
    commission: float = 0.0
    entry: int = 1  # DEAL_ENTRY_OUT by default
    price: float = 0.0
    position_id: int = 0
    time: float = 0.0
    magic: int = 990033


@dataclass
class FakeOrderResult:
    retcode: int = 10009
    order: int = 1
    price: float = 1.1000


def install_fake_mt5() -> FakeMT5:
    fake = FakeMT5()
    sys.modules["MetaTrader5"] = fake
    return fake


class FakeFirebaseDB:
    """In-memory stand-in for firebase_admin.db — enough surface for
    firebase_push.py's db.reference(...).get()/.set()/.update()/.delete()
    calls to work against a plain nested dict."""

    def __init__(self):
        self.data = {}

    def reference(self, path):
        return FakeRef(self, path)


class FakeRef:
    def __init__(self, db_obj, path):
        self.db = db_obj
        self.path = [p for p in path.strip("/").split("/") if p]

    def _walk(self, create=False):
        node = self.db.data
        for part in self.path[:-1]:
            if part not in node:
                if not create:
                    return None
                node[part] = {}
            node = node[part]
        return node

    def get(self):
        node = self.db.data
        for part in self.path:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def set(self, value):
        if not self.path:
            self.db.data = value
            return
        parent = self._walk(create=True)
        parent[self.path[-1]] = value

    def update(self, values: dict):
        if not self.path:
            self.db.data.update(values)
            return
        parent = self._walk(create=True)
        existing = parent.get(self.path[-1])
        if not isinstance(existing, dict):
            existing = {}
        existing.update(values)
        parent[self.path[-1]] = existing

    def delete(self):
        parent = self._walk(create=False)
        if parent is not None and self.path and self.path[-1] in parent:
            del parent[self.path[-1]]

    def child(self, sub):
        return FakeRef(self.db, "/".join(self.path + [sub]))


def install_fake_firebase_admin():
    fake_db_instance = FakeFirebaseDB()

    admin_mod = types.ModuleType("firebase_admin")
    admin_mod.initialize_app = lambda cred, options=None: None
    admin_mod.get_app = lambda: object()

    credentials_mod = types.ModuleType("firebase_admin.credentials")
    credentials_mod.Certificate = lambda path: object()

    db_mod = types.ModuleType("firebase_admin.db")
    db_mod.reference = fake_db_instance.reference

    admin_mod.credentials = credentials_mod
    admin_mod.db = db_mod

    sys.modules["firebase_admin"] = admin_mod
    sys.modules["firebase_admin.credentials"] = credentials_mod
    sys.modules["firebase_admin.db"] = db_mod
    return fake_db_instance
