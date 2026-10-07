from __future__ import annotations

import time
from collections.abc import Iterable

import pytest

from polycopy.config import Settings, SettingsStore
from polycopy.db import Database
from polycopy.engine.bot import Bot
from polycopy.models import (
    AccountInfo,
    BookLevel,
    FeeInfo,
    Fill,
    GeoStatus,
    MarketInfo,
    OrderBook,
    PositionInfo,
    TradeEvent,
)
from polycopy.secrets_store import SecretStore

LEADER = "0x" + "aa" * 20
LEADER2 = "0x" + "bb" * 20
CID = "0x" + "11" * 32
YES = "1001"
NO = "1002"


def make_market(cid: str = CID, yes: str = YES, no: str = NO, *, hours: float = 6.0,
                yes_price: float = 0.5, closed: bool = False, **kw) -> MarketInfo:
    now = time.time()
    data = dict(
        condition_id=cid, question=f"Market {cid[:6]}", yes_token=yes, no_token=no,
        yes_price=yes_price, no_price=1 - yes_price, end_ts=now + hours * 3600,
        closed=closed, active=not closed, accepting_orders=not closed, liquidity=50_000.0,
        fees=FeeInfo(True, 0.05, 1.0, True), fetched_at=now, slug="m", event_slug="ev",
    )
    data.update(kw)
    return MarketInfo(**data)


def make_book(asset: str, price: float, depth: float = 10_000) -> OrderBook:
    return OrderBook(
        asset_id=asset,
        bids=[BookLevel(round(price - 0.01 - 0.01 * i, 3), depth) for i in range(5)],
        asks=[BookLevel(round(price + 0.01 * i, 3), depth) for i in range(5)],
        tick_size=0.01,
    )


class FakeData:
    def __init__(self) -> None:
        self.markets: dict[str, MarketInfo] = {}
        self.books: dict[str, OrderBook] = {}
        self.positions: dict[str, list[PositionInfo]] = {}
        self.activity: dict[str, list[TradeEvent]] = {}
        self.trades: dict[str, list[TradeEvent]] = {}
        self.geo = GeoStatus(blocked=False, country="XX")

    def add_market(self, market: MarketInfo, yes_ask: float | None = None) -> MarketInfo:
        self.markets[market.condition_id] = market
        price = market.yes_price if yes_ask is None else yes_ask
        self.books[market.yes_token] = make_book(market.yes_token, price)
        self.books[market.no_token] = make_book(market.no_token, 1 - price + 0.01)
        return market

    async def close(self): ...

    async def leaderboard(self, *, window, category, limit):
        return []

    async def user_trades(self, wallet, *, since_ts, max_items):
        return [t for t in self.trades.get(wallet, []) if t.ts >= since_ts][-max_items:]

    async def user_activity(self, wallet, *, since_ts, limit=50):
        return [t for t in self.activity.get(wallet, []) if t.ts >= since_ts]

    async def user_positions(self, wallet, *, condition_ids: Iterable[str] | None = None):
        return list(self.positions.get(wallet, []))

    async def markets_by_condition(self, condition_ids, *, prefer_closed=False):
        return {c: self.markets[c] for c in condition_ids if c in self.markets}

    async def market_for_token(self, asset_id):
        for m in self.markets.values():
            if asset_id in (m.yes_token, m.no_token):
                return m
        return None

    async def order_book(self, asset_id):
        return self.books[asset_id]

    async def midpoints(self, asset_ids):
        return {a: self.books[a].mid for a in asset_ids if a in self.books}

    async def geoblock(self):
        return self.geo


class FakeAccount:
    def __init__(self, cash: float = 1000.0) -> None:
        self.info = AccountInfo(wallet="0x" + "cc" * 20, signer="0x" + "dd" * 20,
                                wallet_type="POLY_PROXY", cash=cash, has_relayer_key=True)
        self.orders: list[tuple] = []
        self.redeemed: list[str] = []

    async def close(self): ...

    async def cash_balance(self):
        return self.info.cash

    async def closed_only(self):
        return False

    async def market_buy(self, asset_id, *, usdc, max_price, book=None):
        self.orders.append(("BUY", asset_id, usdc, max_price))
        price = book.best_ask
        shares = usdc / price
        self.info.cash -= usdc
        return Fill(True, shares=shares, usdc=usdc, order_id="o1", status="matched")

    async def market_sell(self, asset_id, *, shares, min_price, book=None):
        self.orders.append(("SELL", asset_id, shares, min_price))
        price = book.best_bid
        self.info.cash += shares * price
        return Fill(True, shares=shares, usdc=shares * price, order_id="o2", status="matched")

    async def redeem(self, condition_id):
        self.redeemed.append(condition_id)
        return "redeemed"


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYCOPY_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_data():
    return FakeData()


@pytest.fixture
def bot(tmp_home, fake_data):
    store = SettingsStore(tmp_home / "settings.json")
    store.save(Settings())
    db = Database(tmp_home / "test.db")
    secrets = SecretStore(fallback_path=tmp_home / "creds.json", use_keyring=False)
    account = FakeAccount()

    async def factory(**_kw):
        return account, {"credentials": None, "builder_key": None}

    b = Bot(store=store, db=db, secrets=secrets, data=fake_data, account_factory=factory)
    b.fake_account = account  # type: ignore[attr-defined]
    b.status = "running"  # handle_signal directly without background loops
    db.upsert_trader(LEADER, {"status": "followed", "name": "alice", "score": 80.0,
                              "metrics": {"median_trade_usdc": 200.0}})
    db.upsert_trader(LEADER2, {"status": "followed", "name": "bob", "score": 60.0,
                               "metrics": {"median_trade_usdc": 200.0}})
    yield b
    db.close()
