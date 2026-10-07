"""Interfaces between the trading engine and the outside world.

``MarketDataGateway`` covers public, read-only data (leaderboards, wallet
trades, markets, order books). ``TradingAccount`` covers the authenticated
wallet (balance, orders, redemption). The engine only depends on these
protocols, so tests and the demo mode can plug in simulated implementations.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from polycopy.models import (
    AccountInfo,
    Fill,
    GeoStatus,
    LeaderboardRow,
    MarketInfo,
    OrderBook,
    PositionInfo,
    TradeEvent,
)


class MarketDataGateway(Protocol):
    async def leaderboard(
        self, *, window: str, category: str | None, limit: int
    ) -> list[LeaderboardRow]: ...

    async def user_trades(
        self, wallet: str, *, since_ts: float, max_items: int
    ) -> list[TradeEvent]: ...

    async def user_activity(
        self, wallet: str, *, since_ts: float, limit: int = 50
    ) -> list[TradeEvent]: ...

    async def user_positions(
        self, wallet: str, *, condition_ids: Iterable[str] | None = None
    ) -> list[PositionInfo]: ...

    async def markets_by_condition(
        self, condition_ids: Iterable[str], *, prefer_closed: bool = False
    ) -> dict[str, MarketInfo]: ...

    async def market_for_token(self, asset_id: str) -> MarketInfo | None: ...

    async def order_book(self, asset_id: str) -> OrderBook: ...

    async def midpoints(self, asset_ids: Iterable[str]) -> dict[str, float]: ...

    async def geoblock(self) -> GeoStatus: ...

    async def close(self) -> None: ...


class TradingAccount(Protocol):
    info: AccountInfo

    async def cash_balance(self) -> float: ...

    async def market_buy(
        self, asset_id: str, *, usdc: float, max_price: float, book: OrderBook | None = None
    ) -> Fill: ...

    async def market_sell(
        self, asset_id: str, *, shares: float, min_price: float, book: OrderBook | None = None
    ) -> Fill: ...

    async def redeem(self, condition_id: str) -> str: ...

    async def closed_only(self) -> bool: ...

    async def close(self) -> None: ...


class StreamSource(Protocol):
    """A realtime source of trades by any wallet (filtered by the engine)."""

    async def run(self, on_trade) -> None: ...

    async def stop(self) -> None: ...

    @property
    def connected(self) -> bool: ...
