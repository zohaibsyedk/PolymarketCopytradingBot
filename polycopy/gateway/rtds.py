"""Realtime trade feed from Polymarket's RTDS websocket.

The ``activity/trades`` topic streams every trade on the platform with the
trader's wallet attached, which lets PolyCopy react to a leader's trade within
about a second instead of waiting for the next poll. Polling keeps running as a
safety net, so a dropped connection never means a missed trade.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from polycopy.models import TradeEvent

log = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
SUBSCRIBE = {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}
PING_INTERVAL = 5.0
STALE_AFTER = 90.0


def parse_activity_payload(payload: dict[str, Any]) -> TradeEvent | None:
    try:
        wallet = str(payload.get("proxyWallet") or payload.get("proxy_wallet") or "")
        asset = str(payload.get("asset") or payload.get("asset_id") or payload.get("token_id") or "")
        side = str(payload.get("side") or "").upper()
        if not wallet or not asset or side not in ("BUY", "SELL"):
            return None
        ts_raw = payload.get("timestamp") or time.time()
        ts = float(ts_raw)
        if ts > 1e12:
            ts /= 1000.0
        price = float(payload.get("price") or 0)
        size = float(payload.get("size") or 0)
        if price <= 0 or size <= 0:
            return None
        outcome_index = payload.get("outcomeIndex")
        return TradeEvent(
            wallet=wallet,
            asset_id=asset,
            condition_id=str(payload.get("conditionId") or payload.get("condition_id") or ""),
            side=side,  # type: ignore[arg-type]
            price=price,
            size=size,
            ts=ts,
            tx_hash=str(payload.get("transactionHash") or payload.get("transaction_hash") or ""),
            outcome=payload.get("outcome"),
            outcome_index=int(outcome_index) if isinstance(outcome_index, (int, float)) else None,
            title=payload.get("title"),
            slug=payload.get("slug"),
            event_slug=payload.get("eventSlug"),
            icon=payload.get("icon"),
            name=payload.get("name") or payload.get("pseudonym"),
            source="stream",
        )
    except (TypeError, ValueError):
        return None


class ActivityStream:
    def __init__(self, url: str = RTDS_URL) -> None:
        self.url = url
        self._stop = asyncio.Event()
        self._connected = False
        self.messages = 0
        self.last_message_at: float | None = None
        self.connected_since: float | None = None
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def stop(self) -> None:
        self._stop.set()

    async def run(self, on_trade: Callable[[TradeEvent], Awaitable[None] | None]) -> None:
        backoff = 1.0
        self._stop.clear()
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url, ping_interval=None, open_timeout=15, max_size=2**23
                ) as ws:
                    await ws.send(json.dumps(SUBSCRIBE))
                    self._connected = True
                    self.connected_since = time.time()
                    self.last_message_at = time.time()
                    self.last_error = None
                    backoff = 1.0
                    log.info("Realtime trade stream connected")
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        await self._consume(ws, on_trade)
                    finally:
                        pinger.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await pinger
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = str(error) or type(error).__name__
                log.info("Realtime stream disconnected: %s", self.last_error)
            finally:
                self._connected = False
            if self._stop.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 60.0)

    async def _consume(self, ws, on_trade) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=STALE_AFTER)
            except asyncio.TimeoutError:
                raise ConnectionError("no messages received; reconnecting") from None
            self.last_message_at = time.time()
            if not raw or raw in ("PONG", "pong", "PING"):
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            for item in message if isinstance(message, list) else [message]:
                if not isinstance(item, dict):
                    continue
                if item.get("topic") != "activity" or item.get("type") not in (
                    "trades",
                    "orders_matched",
                ):
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                event = parse_activity_payload(payload)
                if event is None:
                    continue
                self.messages += 1
                result = on_trade(event)
                if inspect.isawaitable(result):
                    await result

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await ws.send("PING")
