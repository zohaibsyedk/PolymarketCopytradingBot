"""Detects followed leaders' trades and turns them into copy signals.

Two sources feed the watcher:
  * the realtime RTDS trade stream (sub-second, but a websocket can drop), and
  * polling each leader's activity feed every few seconds (the safety net).

Fills are de-duplicated across sources, then fills of the same order are
grouped for ``aggregation_window_sec`` so one leader order becomes one signal.
Trades made before PolyCopy started following a wallet are never copied.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from polycopy.config import Settings
from polycopy.db import Database
from polycopy.gateway.base import MarketDataGateway
from polycopy.models import TradeEvent

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Signal:
    wallet: str
    asset_id: str
    condition_id: str
    side: str
    kind: str
    price: float
    size: float
    usdc: float
    ts: float
    tx_hash: str
    detected_at: float
    source: str
    title: str | None = None
    outcome: str | None = None
    slug: str | None = None
    event_slug: str | None = None
    icon: str | None = None
    fills: int = 1

    @property
    def latency(self) -> float:
        return max(0.0, self.detected_at - self.ts)


@dataclass(slots=True)
class _Group:
    signal: Signal
    first_seen: float
    keys: list[tuple] = field(default_factory=list)


class _LRU:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._data: OrderedDict[tuple, None] = OrderedDict()

    def add(self, key: tuple) -> bool:
        """Add key; return False if it was already present."""
        if key in self._data:
            self._data.move_to_end(key)
            return False
        self._data[key] = None
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)
        return True

    def __contains__(self, key: tuple) -> bool:
        return key in self._data


class LeaderWatcher:
    def __init__(
        self,
        data: MarketDataGateway,
        db: Database,
        settings: Callable[[], Settings],
        on_signal: Callable[[Signal], Awaitable[None]],
        stream=None,
    ) -> None:
        self.data = data
        self.db = db
        self.settings = settings
        self.on_signal = on_signal
        self.stream = stream
        self.leaders: dict[str, float] = {}  # wallet -> time we started following
        self._cursor: dict[str, float] = {}
        self._fills = _LRU(50_000)
        self._emitted = _LRU(10_000)
        self._groups: dict[tuple, _Group] = {}
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.polls = 0
        self.poll_errors = 0
        self.last_poll_at: float | None = None

    # ---------------------------------------------------------------- leaders
    def set_leaders(self, wallets: list[str]) -> None:
        now = time.time()
        wanted = {w.lower() for w in wallets}
        for wallet in list(self.leaders):
            if wallet not in wanted:
                self.leaders.pop(wallet, None)
                self._cursor.pop(wallet, None)
        for wallet in wanted:
            if wallet not in self.leaders:
                self.leaders[wallet] = now
                self._cursor[wallet] = now

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="watcher-poll"),
            asyncio.create_task(self._flush_loop(), name="watcher-flush"),
        ]
        if self.stream is not None and self.settings().execution.use_realtime_stream:
            self._tasks.append(asyncio.create_task(self.stream.run(self.ingest),
                                                   name="watcher-stream"))

    async def stop(self) -> None:
        self._running = False
        if self.stream is not None:
            await self.stream.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        # Drop half-built groups; they are re-detected by polling after a restart
        # only if still within the signal age limit.
        self._groups.clear()

    # ----------------------------------------------------------------- inputs
    async def ingest(self, event: TradeEvent) -> None:
        followed_since = self.leaders.get(event.wallet)
        if followed_since is None:
            return
        if not self._fills.add(event.fill_key()):
            return
        self._store(event)
        # Never copy history from before we started watching this wallet.
        if event.ts < followed_since - 2:
            return
        group_key = self._group_key(event)
        if group_key in self._emitted:
            return
        group = self._groups.get(group_key)
        if group is None:
            self._groups[group_key] = _Group(
                signal=Signal(
                    wallet=event.wallet,
                    asset_id=event.asset_id,
                    condition_id=event.condition_id,
                    side=event.side,
                    kind=event.kind,
                    price=event.price,
                    size=event.size,
                    usdc=event.usdc,
                    ts=event.ts,
                    tx_hash=event.tx_hash,
                    detected_at=time.time(),
                    source=event.source,
                    title=event.title,
                    outcome=event.outcome,
                    slug=event.slug,
                    event_slug=event.event_slug,
                    icon=event.icon,
                ),
                first_seen=time.monotonic(),
            )
            return
        sig = group.signal
        total = sig.size + event.size
        if total > 0:
            sig.price = (sig.price * sig.size + event.price * event.size) / total
        sig.size = total
        sig.usdc += event.usdc
        sig.fills += 1
        sig.ts = min(sig.ts, event.ts)

    def _group_key(self, event: TradeEvent) -> tuple:
        target = event.asset_id or event.condition_id
        if event.tx_hash:
            return (event.wallet, target, event.side, event.kind, event.tx_hash.lower())
        return (event.wallet, target, event.side, event.kind, int(event.ts // 10))

    def _store(self, event: TradeEvent) -> None:
        key = "|".join(str(part) for part in event.fill_key())
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO leader_trades(wallet, asset_id, condition_id, side, kind, "
                "price, size, usdc, ts, tx_hash, title, outcome, slug, source, detected_at, "
                "fill_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.wallet, event.asset_id, event.condition_id, event.side, event.kind,
                    event.price, event.size, event.usdc, event.ts, event.tx_hash, event.title,
                    event.outcome, event.slug, event.source, time.time(), key,
                ),
            )
        except Exception as error:  # never let bookkeeping stop detection
            log.debug("Could not store leader trade: %s", error)

    # ------------------------------------------------------------------ loops
    async def _poll_loop(self) -> None:
        while self._running:
            interval = self.settings().execution.poll_interval_sec
            started = time.monotonic()
            wallets = list(self.leaders)
            semaphore = asyncio.Semaphore(4)

            async def poll(wallet: str) -> None:
                async with semaphore:
                    await self._poll_wallet(wallet)

            if wallets:
                await asyncio.gather(*(poll(w) for w in wallets))
                self.last_poll_at = time.time()
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, interval - elapsed))

    async def _poll_wallet(self, wallet: str) -> None:
        since = self._cursor.get(wallet, time.time()) - 30
        try:
            events = await self.data.user_activity(wallet, since_ts=since, limit=50)
            self.polls += 1
        except Exception as error:
            self.poll_errors += 1
            log.debug("Polling %s failed: %s", wallet, error)
            return
        newest = self._cursor.get(wallet, 0.0)
        for event in events:
            event.source = "poll"
            await self.ingest(event)
            newest = max(newest, event.ts)
        if wallet in self._cursor:
            self._cursor[wallet] = max(self._cursor[wallet], newest)

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.25)
            await self.flush()

    async def flush(self, force: bool = False) -> None:
        window = self.settings().execution.aggregation_window_sec
        now = time.monotonic()
        ready = [k for k, g in self._groups.items() if force or now - g.first_seen >= window]
        for key in ready:
            group = self._groups.pop(key)
            self._emitted.add(key)
            try:
                await self.on_signal(group.signal)
            except Exception:
                log.exception("Signal handler failed")
