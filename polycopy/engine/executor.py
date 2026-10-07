"""Order execution for paper and live trading.

Both executors take the same arguments and return a :class:`Fill`, so the rest
of the engine does not care which mode is active. Paper fills walk the real,
live order book, so paper results include realistic slippage and fees.
"""

from __future__ import annotations

from typing import Protocol

from polycopy.engine.fees import taker_fee
from polycopy.models import BookLevel, FeeInfo, Fill, OrderBook


class Executor(Protocol):
    mode: str

    async def buy(
        self, asset_id: str, *, usdc: float, max_price: float, book: OrderBook, fees: FeeInfo
    ) -> Fill: ...

    async def sell(
        self, asset_id: str, *, shares: float, min_price: float, book: OrderBook, fees: FeeInfo
    ) -> Fill: ...


def walk_asks(asks: list[BookLevel], *, usdc: float, max_price: float) -> tuple[float, float]:
    """Shares bought and USDC spent sweeping asks up to ``max_price``."""
    remaining = usdc
    shares = 0.0
    spent = 0.0
    for level in asks:
        if level.price > max_price + 1e-9 or remaining <= 1e-9:
            break
        take = min(remaining, level.price * level.size)
        shares += take / level.price
        spent += take
        remaining -= take
    return shares, spent


def walk_bids(bids: list[BookLevel], *, shares: float, min_price: float) -> tuple[float, float]:
    """Shares sold and USDC received sweeping bids down to ``min_price``."""
    remaining = shares
    sold = 0.0
    received = 0.0
    for level in bids:
        if level.price < min_price - 1e-9 or remaining <= 1e-9:
            break
        take = min(remaining, level.size)
        sold += take
        received += take * level.price
        remaining -= take
    return sold, received


def liquidity_under(asks: list[BookLevel], max_price: float) -> float:
    return sum(level.price * level.size for level in asks if level.price <= max_price + 1e-9)


class PaperExecutor:
    mode = "paper"

    async def buy(
        self, asset_id: str, *, usdc: float, max_price: float, book: OrderBook, fees: FeeInfo
    ) -> Fill:
        shares, spent = walk_asks(book.asks, usdc=usdc, max_price=max_price)
        if shares <= 0:
            return Fill(ok=False, status="unfilled", error="no asks at or below the price cap",
                        error_code="fak_not_filled")
        fee = taker_fee(shares, spent / shares, fees)
        return Fill(ok=True, shares=round(shares, 6), usdc=round(spent, 6), fee=round(fee, 6),
                    order_id=None, status="matched")

    async def sell(
        self, asset_id: str, *, shares: float, min_price: float, book: OrderBook, fees: FeeInfo
    ) -> Fill:
        sold, received = walk_bids(book.bids, shares=shares, min_price=min_price)
        if sold <= 0:
            return Fill(ok=False, status="unfilled", error="no bids at or above the price floor",
                        error_code="fak_not_filled")
        fee = taker_fee(sold, received / sold, fees)
        return Fill(ok=True, shares=round(sold, 6), usdc=round(received, 6), fee=round(fee, 6),
                    status="matched")


class LiveExecutor:
    mode = "live"

    def __init__(self, account) -> None:
        self.account = account

    async def buy(
        self, asset_id: str, *, usdc: float, max_price: float, book: OrderBook, fees: FeeInfo
    ) -> Fill:
        fill = await self.account.market_buy(asset_id, usdc=usdc, max_price=max_price, book=book)
        if fill.ok and fill.shares > 0:
            fill.fee = round(taker_fee(fill.shares, fill.avg_price, fees), 6)
        return fill

    async def sell(
        self, asset_id: str, *, shares: float, min_price: float, book: OrderBook, fees: FeeInfo
    ) -> Fill:
        fill = await self.account.market_sell(
            asset_id, shares=shares, min_price=min_price, book=book
        )
        if fill.ok and fill.shares > 0:
            fill.fee = round(taker_fee(fill.shares, fill.avg_price, fees), 6)
        return fill
