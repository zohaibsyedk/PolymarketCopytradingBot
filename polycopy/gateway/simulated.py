"""A self-contained simulated Polymarket used by the demo mode and the tests.

It produces wallets with known skill, a month of resolved trading history, live
markets that move and resolve, synthetic order books and a realtime trade
stream. Nothing here touches the network. The web terminal labels every screen
"DEMO" while this world is in use, so it can never be mistaken for real data.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from polycopy.models import (
    AccountInfo,
    BookLevel,
    FeeInfo,
    Fill,
    GeoStatus,
    LeaderboardRow,
    MarketInfo,
    OrderBook,
    PositionInfo,
    TradeEvent,
)

HOUR = 3600.0
DAY = 86400.0

_TEAMS = [
    "Lakers", "Celtics", "Warriors", "Knicks", "Heat", "Bucks", "Suns", "Nuggets", "Arsenal",
    "Chelsea", "Liverpool", "Real Madrid", "Barcelona", "Bayern", "Yankees", "Dodgers", "Chiefs",
    "Eagles", "Cowboys", "49ers", "Rangers", "Bruins", "Maple Leafs", "Oilers",
]
_CRYPTO = ["Bitcoin", "Ethereum", "Solana", "XRP"]
_FIRST = ["swift", "quiet", "lucky", "sharp", "bold", "calm", "steady", "wild", "clever", "grim"]
_SECOND = ["fox", "otter", "whale", "hawk", "tiger", "sloth", "bear", "lynx", "raven", "shark"]


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


@dataclass
class SimWallet:
    address: str
    name: str
    edge: float  # extra win probability over the price paid
    trades_per_day: float
    avg_usdc: float
    long_dated_share: float
    sell_share: float


@dataclass
class SimMarket:
    condition_id: str
    question: str
    category: str
    yes_token: str
    no_token: str
    yes_price: float
    end_ts: float
    start_ts: float
    outcome_yes: bool
    game_start_ts: float | None = None
    closed: bool = False
    closed_ts: float | None = None
    fees: FeeInfo = field(default_factory=FeeInfo)
    liquidity: float = 20000.0

    def info(self) -> MarketInfo:
        resolved = self.closed
        yes_price = (1.0 if self.outcome_yes else 0.0) if resolved else self.yes_price
        slug = self.question.lower().replace(" ", "-").replace("?", "")[:60]
        return MarketInfo(
            condition_id=self.condition_id,
            question=self.question,
            yes_token=self.yes_token,
            no_token=self.no_token,
            yes_label="Yes",
            no_label="No",
            yes_price=round(yes_price, 4),
            no_price=round(1 - yes_price, 4),
            slug=slug,
            event_slug=slug,
            category=self.category,
            tags=[self.category],
            active=not resolved,
            closed=resolved,
            accepting_orders=not resolved,
            end_ts=self.end_ts,
            start_ts=self.start_ts,
            closed_ts=self.closed_ts,
            game_start_ts=self.game_start_ts,
            liquidity=self.liquidity,
            best_bid=round(max(0.01, yes_price - 0.005), 4),
            best_ask=round(min(0.99, yes_price + 0.005), 4),
            spread=0.01,
            tick_size=0.001,
            min_order_size=1.0,
            fees=self.fees,
            fetched_at=time.time(),
        )


class SimWorld:
    def __init__(self, seed: int = 7, n_wallets: int = 70, live_rate: float = 1.0,
                 market_minutes: tuple[float, float] = (25.0, 180.0),
                 history_days: int = 30) -> None:
        self.rng = random.Random(seed)
        self.now0 = time.time()
        self.live_rate = live_rate
        self.market_minutes = market_minutes
        self.history_days = history_days
        self.wallets: list[SimWallet] = []
        self.markets: dict[str, SimMarket] = {}
        self.token_to_market: dict[str, str] = {}
        self.trades: dict[str, list[TradeEvent]] = {}
        self.holdings: dict[str, dict[str, float]] = {}
        self.live_ids: set[str] = set()
        self._subscribers: list[asyncio.Queue] = []
        self._task: asyncio.Task | None = None
        self._make_wallets(n_wallets)
        self._make_history()
        for _ in range(28):
            self._new_live_market()

    # --------------------------------------------------------------- creation
    def _make_wallets(self, n: int) -> None:
        for i in range(n):
            roll = self.rng.random()
            if roll < 0.12:
                edge = self.rng.uniform(0.08, 0.16)
            elif roll < 0.35:
                edge = self.rng.uniform(0.0, 0.06)
            else:
                edge = self.rng.uniform(-0.08, 0.01)
            bot_like = self.rng.random() < 0.06
            self.wallets.append(SimWallet(
                address="0x" + hashlib.sha1(f"w{i}-{self.rng.random()}".encode()).hexdigest()[:40],
                name=f"{self.rng.choice(_FIRST)}-{self.rng.choice(_SECOND)}-{i}",
                edge=edge,
                trades_per_day=self.rng.uniform(250, 400) if bot_like else self.rng.uniform(1.5, 9),
                avg_usdc=self.rng.choice([60, 120, 250, 500, 1200, 3000]),
                long_dated_share=self.rng.choice([0.05, 0.1, 0.2, 0.8]),
                sell_share=self.rng.uniform(0.05, 0.35),
            ))

    def _market(self, *, start: float, end: float, p_true: float, outcome_yes: bool,
                category: str | None = None, closed: bool = False) -> SimMarket:
        category = category or self.rng.choice(["sports", "sports", "crypto", "politics"])
        if category == "sports":
            a, b = self.rng.sample(_TEAMS, 2)
            question = f"Will the {a} beat the {b}?"
            game_start = end - 2.5 * HOUR
        elif category == "crypto":
            coin = self.rng.choice(_CRYPTO)
            question = f"{coin} above ${self.rng.randint(1, 200) * 500:,} at close?"
            game_start = None
        else:
            question = f"Will poll #{self.rng.randint(100, 999)} show the incumbent ahead?"
            game_start = None
        cid = "0x" + _hex(self.rng, 64)
        market = SimMarket(
            condition_id=cid,
            question=question,
            category=category,
            yes_token=str(self.rng.getrandbits(128)),
            no_token=str(self.rng.getrandbits(128)),
            yes_price=p_true,
            end_ts=end,
            start_ts=start,
            outcome_yes=outcome_yes,
            game_start_ts=game_start,
            closed=closed,
            closed_ts=end + 600 if closed else None,
            fees=FeeInfo(True, {"sports": 0.05, "crypto": 0.07}.get(category, 0.04), 1.0, True),
            liquidity=self.rng.uniform(800, 60000),
        )
        self.markets[cid] = market
        self.token_to_market[market.yes_token] = cid
        self.token_to_market[market.no_token] = cid
        return market

    def _make_history(self) -> None:
        now = self.now0
        for wallet in self.wallets:
            events: list[TradeEvent] = []
            n = int(wallet.trades_per_day * self.history_days)
            for _ in range(n):
                t = now - self.rng.uniform(0.5 * DAY, self.history_days * DAY)
                long_dated = self.rng.random() < wallet.long_dated_share
                hours = self.rng.uniform(72, 900) if long_dated else self.rng.uniform(1, 40)
                p_side = min(max(self.rng.betavariate(2.2, 2.2), 0.08), 0.92)
                win = self.rng.random() < min(0.97, max(0.03, p_side + wallet.edge))
                buy_yes = self.rng.random() < 0.5
                yes_price = p_side if buy_yes else 1 - p_side
                market = self._market(
                    start=t - DAY, end=t + hours * HOUR, p_true=yes_price,
                    outcome_yes=(win if buy_yes else not win),
                    closed=t + hours * HOUR < now,
                )
                token = market.yes_token if buy_yes else market.no_token
                usdc = wallet.avg_usdc * self.rng.uniform(0.4, 1.8)
                events.append(self._event(wallet, market, token, "BUY", p_side, usdc / p_side, t))
                if self.rng.random() < wallet.sell_share and not long_dated:
                    exit_t = t + self.rng.uniform(0.1, 0.8) * hours * HOUR
                    if exit_t < now:
                        drift = (0.25 if win else -0.25) * self.rng.random()
                        exit_px = min(0.98, max(0.02, p_side + drift))
                        events.append(self._event(wallet, market, token, "SELL", exit_px,
                                                  usdc / p_side, exit_t))
                if not market.closed:
                    market.yes_price = yes_price
            events.sort(key=lambda e: e.ts)
            self.trades[wallet.address] = events

    def _event(self, wallet: SimWallet, market: SimMarket, token: str, side: str, price: float,
               size: float, ts: float, source: str = "history") -> TradeEvent:
        return TradeEvent(
            wallet=wallet.address,
            asset_id=token,
            condition_id=market.condition_id,
            side=side,  # type: ignore[arg-type]
            price=round(price, 3),
            size=round(size, 2),
            ts=ts,
            tx_hash="0x" + _hex(self.rng, 64),
            outcome="Yes" if token == market.yes_token else "No",
            title=market.question,
            slug=market.info().slug,
            event_slug=market.info().slug,
            name=wallet.name,
            source=source,
        )

    def _new_live_market(self) -> SimMarket:
        now = time.time()
        lo, hi = self.market_minutes
        end = now + self.rng.uniform(lo, hi) * 60
        p_true = min(max(self.rng.betavariate(2.2, 2.2), 0.1), 0.9)
        market = self._market(start=now - HOUR, end=end, p_true=p_true,
                              outcome_yes=self.rng.random() < p_true)
        market.game_start_ts = end + HOUR if market.category == "sports" else None
        self.live_ids.add(market.condition_id)
        return market

    # ------------------------------------------------------------------ clock
    def step(self) -> list[TradeEvent]:
        """Advance the live world by one tick and return new trades."""
        now = time.time()
        new: list[TradeEvent] = []
        live = [self.markets[c] for c in self.live_ids if not self.markets[c].closed]
        for market in live:
            if market.end_ts <= now:
                market.closed = True
                market.closed_ts = now
                continue
            remaining = max(market.end_ts - now, 1.0)
            pull = (1.0 if market.outcome_yes else 0.0) - market.yes_price
            market.yes_price += pull * min(0.02, 30.0 / remaining) + self.rng.gauss(0, 0.004)
            market.yes_price = min(0.97, max(0.03, market.yes_price))
        open_count = sum(1 for c in self.live_ids if not self.markets[c].closed)
        while open_count < 28:
            self._new_live_market()
            open_count += 1
        trades_this_tick = int(self.live_rate) + (1 if self.rng.random() < self.live_rate % 1 else 0)
        for _ in range(trades_this_tick):
            live = [self.markets[c] for c in self.live_ids
                    if not self.markets[c].closed and self.markets[c].end_ts - now > 20 * 60]
            if live:
                wallet = self.rng.choices(
                    self.wallets, weights=[min(w.trades_per_day, 12.0) for w in self.wallets]
                )[0]
                market = self.rng.choice(live)
                held = self.holdings.setdefault(wallet.address, {})
                held_tokens = [t for t in (market.yes_token, market.no_token) if held.get(t, 0) > 0]
                if held_tokens and self.rng.random() < 0.35:
                    token = held_tokens[0]
                    price = market.yes_price if token == market.yes_token else 1 - market.yes_price
                    size = held[token] * self.rng.choice([0.5, 1.0])
                    held[token] -= size
                    new.append(self._event(wallet, market, token, "SELL", price - 0.003, size, now,
                                           "stream"))
                else:
                    yes_wins = market.outcome_yes
                    p_yes = market.yes_price
                    # Skilled wallets pick the eventual winner more often than the price implies.
                    pick_winner = self.rng.random() < min(0.95, max(0.05, 0.5 + wallet.edge * 2.5))
                    buy_yes = yes_wins if pick_winner else not yes_wins
                    token = market.yes_token if buy_yes else market.no_token
                    price = p_yes if buy_yes else 1 - p_yes
                    usdc = wallet.avg_usdc * self.rng.uniform(0.4, 1.8)
                    size = usdc / price
                    held[token] = held.get(token, 0.0) + size
                    impact = 0.004 if buy_yes else -0.004
                    market.yes_price = min(0.97, max(0.03, market.yes_price + impact))
                    new.append(self._event(wallet, market, token, "BUY", price, size, now, "stream"))
        for event in new:
            self.trades.setdefault(event.wallet, []).append(event)
        return new

    async def run(self, interval: float = 2.0) -> None:
        while True:
            for event in self.step():
                for queue in list(self._subscribers):
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait(event)
            await asyncio.sleep(interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="sim-world")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._subscribers.append(queue)
        return queue

    # ------------------------------------------------------------------ books
    def token_price(self, token: str) -> float:
        market = self.markets[self.token_to_market[token]]
        if market.closed:
            won = market.outcome_yes == (token == market.yes_token)
            return 1.0 if won else 0.0
        return market.yes_price if token == market.yes_token else 1 - market.yes_price

    def book(self, token: str) -> OrderBook:
        price = self.token_price(token)
        market = self.markets[self.token_to_market[token]]
        depth = market.liquidity / 40
        asks = [BookLevel(round(min(0.99, price + 0.005 + 0.01 * i), 3), round(depth * (1 + i), 2))
                for i in range(6) if price + 0.005 + 0.01 * i < 0.995]
        bids = [BookLevel(round(max(0.01, price - 0.005 - 0.01 * i), 3), round(depth * (1 + i), 2))
                for i in range(6) if price - 0.005 - 0.01 * i > 0.005]
        return OrderBook(asset_id=token, bids=bids, asks=asks, tick_size=0.001,
                         min_order_size=1.0, ts=time.time())


class SimData:
    """MarketDataGateway backed by :class:`SimWorld`."""

    def __init__(self, world: SimWorld) -> None:
        self.world = world

    async def close(self) -> None:
        return None

    async def leaderboard(self, *, window: str, category: str | None, limit: int) -> list[LeaderboardRow]:
        span = {"day": DAY, "week": 7 * DAY, "month": 30 * DAY, "all": 365 * DAY}.get(window, 7 * DAY)
        since = time.time() - span
        rows = []
        for wallet in self.world.wallets:
            pnl = 0.0
            volume = 0.0
            for event in self.world.trades.get(wallet.address, []):
                if event.ts < since:
                    continue
                market = self.world.markets[event.condition_id]
                if category and market.category != category:
                    continue
                volume += event.usdc
                if market.closed and event.side == "BUY":
                    won = market.outcome_yes == (event.asset_id == market.yes_token)
                    pnl += (event.size if won else 0.0) - event.usdc
            rows.append(LeaderboardRow(wallet=wallet.address, rank=0, pnl=round(pnl, 2),
                                       volume=round(volume, 2), name=wallet.name, window=window,
                                       category=category))
        rows.sort(key=lambda r: -r.pnl)
        for i, row in enumerate(rows):
            row.rank = i + 1
        return rows[:limit]

    async def user_trades(self, wallet: str, *, since_ts: float, max_items: int) -> list[TradeEvent]:
        events = [e for e in self.world.trades.get(wallet.lower(), []) if e.ts >= since_ts]
        return events[-max_items:]

    async def user_activity(self, wallet: str, *, since_ts: float, limit: int = 50) -> list[TradeEvent]:
        events = [e for e in self.world.trades.get(wallet.lower(), []) if e.ts >= since_ts]
        return [replace(e) for e in events[-limit:]]

    async def user_positions(self, wallet: str, *, condition_ids: Iterable[str] | None = None) -> list[PositionInfo]:
        wanted = set(condition_ids or [])
        out = []
        for token, size in self.world.holdings.get(wallet.lower(), {}).items():
            if size <= 0.01:
                continue
            cid = self.world.token_to_market[token]
            if wanted and cid not in wanted:
                continue
            price = self.world.token_price(token)
            out.append(PositionInfo(wallet=wallet.lower(), asset_id=token, condition_id=cid,
                                    size=size, avg_price=price, current_price=price,
                                    current_value=size * price))
        return out

    async def markets_by_condition(
        self, condition_ids: Iterable[str], *, prefer_closed: bool = False
    ) -> dict[str, MarketInfo]:
        return {c: self.world.markets[c].info() for c in condition_ids if c in self.world.markets}

    async def market_for_token(self, asset_id: str) -> MarketInfo | None:
        cid = self.world.token_to_market.get(asset_id)
        return self.world.markets[cid].info() if cid else None

    async def order_book(self, asset_id: str) -> OrderBook:
        return self.world.book(asset_id)

    async def midpoints(self, asset_ids: Iterable[str]) -> dict[str, float]:
        out = {}
        for token in asset_ids:
            if token in self.world.token_to_market:
                book = self.world.book(token)
                if book.mid is not None:
                    out[token] = round(book.mid, 4)
        return out

    async def geoblock(self) -> GeoStatus:
        return GeoStatus(blocked=False, country="DEMO", checked=True)


class SimStream:
    """StreamSource backed by :class:`SimWorld`."""

    def __init__(self, world: SimWorld) -> None:
        self.world = world
        self._stop = asyncio.Event()
        self._connected = False
        self.messages = 0
        self.last_message_at: float | None = None
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def stop(self) -> None:
        self._stop.set()

    async def run(self, on_trade) -> None:
        self._stop.clear()
        queue = self.world.subscribe()
        self._connected = True
        try:
            while not self._stop.is_set():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                self.messages += 1
                self.last_message_at = time.time()
                await on_trade(event)
        finally:
            self._connected = False
            with contextlib.suppress(ValueError):
                self.world._subscribers.remove(queue)


class SimAccount:
    """TradingAccount backed by :class:`SimWorld` (used for demo 'live' mode)."""

    def __init__(self, world: SimWorld, cash: float = 2500.0) -> None:
        self.world = world
        self.info = AccountInfo(wallet="0xdemo000000000000000000000000000000000001",
                                signer="0xdemo000000000000000000000000000000000002",
                                wallet_type="DEMO", cash=cash, has_relayer_key=True)
        self.positions: dict[str, float] = {}

    async def close(self) -> None:
        return None

    async def cash_balance(self) -> float:
        return self.info.cash

    async def closed_only(self) -> bool:
        return False

    async def ensure_approvals(self) -> str:
        return "approved (demo)"

    async def market_buy(self, asset_id: str, *, usdc: float, max_price: float, book=None) -> Fill:
        from polycopy.engine.executor import walk_asks

        book = self.world.book(asset_id)
        usdc = min(usdc, self.info.cash)
        shares, spent = walk_asks(book.asks, usdc=usdc, max_price=max_price)
        if shares <= 0:
            return Fill(ok=False, error="no liquidity under cap", error_code="fak_not_filled")
        self.info.cash -= spent
        self.positions[asset_id] = self.positions.get(asset_id, 0.0) + shares
        return Fill(ok=True, shares=shares, usdc=spent, order_id="demo-" + _hex(self.world.rng, 8),
                    status="matched")

    async def market_sell(self, asset_id: str, *, shares: float, min_price: float, book=None) -> Fill:
        from polycopy.engine.executor import walk_bids

        book = self.world.book(asset_id)
        shares = min(shares, self.positions.get(asset_id, 0.0))
        sold, received = walk_bids(book.bids, shares=shares, min_price=min_price)
        if sold <= 0:
            return Fill(ok=False, error="no bids above floor", error_code="fak_not_filled")
        self.info.cash += received
        self.positions[asset_id] -= sold
        return Fill(ok=True, shares=sold, usdc=received, order_id="demo-" + _hex(self.world.rng, 8),
                    status="matched")

    async def redeem(self, condition_id: str) -> str:
        market = self.world.markets.get(condition_id)
        if market is None or not market.closed:
            return "failed: not resolved"
        for token in (market.yes_token, market.no_token):
            shares = self.positions.pop(token, 0.0)
            won = market.outcome_yes == (token == market.yes_token)
            if won:
                self.info.cash += shares
        return "redeemed"
