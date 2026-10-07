"""Polymarket implementation of the gateway protocols.

Built on the official ``polymarket-client`` SDK, which handles request signing,
wallet types (EOA, proxy, Safe, deposit wallet), exchange versions, tick sizes,
fees and approvals. This module only adapts SDK models to PolyCopy's own
dataclasses and adds rate limiting and defensive error handling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import Any

import httpx
from polymarket import (
    AsyncPublicClient,
    AsyncSecureClient,
    BuilderApiKey,
    InsufficientLiquidityError,
    PolymarketError,
    RateLimitError,
    RequestRejectedError,
    UserInputError,
)
from polymarket.models import ApiKeyCreds

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

log = logging.getLogger(__name__)

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
_CONDITION_BATCH = 20


# --------------------------------------------------------------------------- utils
class RateLimiter:
    """Token bucket shared by all calls to one service."""

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = rate_per_sec
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def _f(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp()
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e12 else float(value)
    return None


def _dec(value: float, places: str = "0.000001") -> Decimal:
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_DOWN)


def snap_to_tick(price: float, tick: float | None, *, up: bool) -> Decimal:
    """Round a price onto the market's tick grid (the CLOB rejects off-grid prices).

    Buy caps round down and sell floors round up, so snapping never loosens the
    limit PolyCopy asked for. The result stays inside [tick, 1 - tick].
    """
    step = Decimal(str(tick)) if tick and tick > 0 else Decimal("0.01")
    value = Decimal(str(price))
    units = (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    snapped = units * step
    lowest, highest = step, Decimal(1) - step
    snapped = min(max(snapped, lowest), highest)
    return snapped.quantize(step)


async def _with_retry(call, *, attempts: int = 3, base_delay: float = 0.6):
    """Retry transient failures (rate limits, timeouts, 5xx)."""
    for attempt in range(attempts):
        try:
            return await call()
        except RateLimitError as error:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(min(error.retry_after or base_delay * (2**attempt), 10))
        except RequestRejectedError as error:
            if error.status < 500 or attempt == attempts - 1:
                raise
            await asyncio.sleep(base_delay * (2**attempt))
        except (httpx.TransportError, asyncio.TimeoutError, PolymarketError) as error:
            if isinstance(error, (UserInputError, InsufficientLiquidityError)):
                raise
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(base_delay * (2**attempt))
    return None


# ------------------------------------------------------------------- conversions
def market_from_sdk(market: Any) -> MarketInfo:
    state = market.state
    yes = market.outcomes.yes
    no = market.outcomes.no
    trading = market.trading
    fees = FeeInfo()
    if trading.fees_enabled and trading.fee_schedule is not None:
        schedule = trading.fee_schedule
        fees = FeeInfo(
            enabled=True,
            rate=_f(schedule.rate),
            exponent=_f(schedule.exponent, 1.0),
            taker_only=bool(schedule.taker_only),
        )
    events = list(market.events or ())
    tags = [t.label or t.slug for t in (market.tags or ()) if (t.label or t.slug)]
    yes_id = str(yes.token_id or yes.position_id or "") or None
    no_id = str(no.token_id or no.position_id or "") or None
    yes_alt = str(yes.position_id) if yes.token_id and yes.position_id else None
    no_alt = str(no.position_id) if no.token_id and no.position_id else None
    return MarketInfo(
        condition_id=str(market.condition_id or ""),
        question=market.question or market.group_item_title or "",
        yes_token=yes_id,
        no_token=no_id,
        yes_label=yes.label or "Yes",
        no_label=no.label or "No",
        yes_alt=yes_alt,
        no_alt=no_alt,
        yes_price=_f(yes.price) if yes.price is not None else None,
        no_price=_f(no.price) if no.price is not None else None,
        slug=market.slug,
        event_slug=events[0].slug if events else None,
        icon=market.icon or market.image,
        category=market.category,
        tags=tags,
        active=bool(state.active) if state.active is not None else True,
        closed=bool(state.closed),
        accepting_orders=state.accepting_orders is not False,
        enable_order_book=state.enable_order_book is not False,
        neg_risk=bool(state.neg_risk),
        end_ts=_ts(state.end_date),
        start_ts=_ts(state.start_date),
        closed_ts=_ts(state.closed_time),
        game_start_ts=_ts(market.sports.game_start_time),
        sports_market_type=market.sports.sports_market_type,
        liquidity=_f(market.metrics.liquidity_num or market.metrics.liquidity, 0.0),
        volume_24h=_f(market.metrics.volume_24hr, 0.0),
        best_bid=_f(market.prices.best_bid) if market.prices.best_bid is not None else None,
        best_ask=_f(market.prices.best_ask) if market.prices.best_ask is not None else None,
        spread=_f(market.prices.spread) if market.prices.spread is not None else None,
        tick_size=_f(trading.minimum_tick_size) if trading.minimum_tick_size else None,
        min_order_size=_f(trading.minimum_order_size) if trading.minimum_order_size else None,
        uma_status=str(market.resolution.uma_resolution_status)
        if market.resolution.uma_resolution_status
        else None,
        fees=fees,
        fetched_at=time.time(),
    )


def trade_from_sdk(trade: Any, source: str = "poll") -> TradeEvent:
    return TradeEvent(
        wallet=str(trade.wallet),
        asset_id=str(trade.asset_id),
        condition_id=str(trade.condition_id),
        side=str(trade.side),  # type: ignore[arg-type]
        price=_f(trade.price),
        size=_f(trade.size),
        ts=_ts(trade.timestamp) or time.time(),
        tx_hash=str(trade.transaction_hash or ""),
        outcome=trade.outcome,
        outcome_index=trade.outcome_index,
        title=trade.title,
        slug=trade.slug,
        event_slug=trade.event_slug,
        icon=getattr(trade, "icon", None),
        name=trade.name or trade.pseudonym,
        source=source,
    )


def activity_to_event(item: Any, source: str = "poll") -> TradeEvent | None:
    kind = str(getattr(item, "type", ""))
    if kind == "TRADE":
        if getattr(item, "is_combo", False) or not hasattr(item, "asset_id"):
            return None
        return TradeEvent(
            wallet=str(item.wallet),
            asset_id=str(item.asset_id),
            condition_id=str(item.condition_id),
            side=str(item.side),  # type: ignore[arg-type]
            price=_f(item.price),
            size=_f(item.shares),
            usdc=_f(item.amount),
            ts=_ts(item.timestamp) or time.time(),
            tx_hash=str(item.transaction_hash or ""),
            outcome=item.outcome,
            outcome_index=item.outcome_index,
            title=item.title,
            slug=item.slug,
            event_slug=item.event_slug,
            icon=item.icon,
            name=item.name or item.pseudonym,
            source=source,
        )
    if kind == "MERGE":
        return TradeEvent(
            wallet=str(item.wallet),
            asset_id="",
            condition_id=str(item.condition_id),
            side="SELL",
            price=1.0,
            size=_f(item.amount),
            usdc=_f(item.amount),
            ts=_ts(item.timestamp) or time.time(),
            tx_hash=str(item.transaction_hash or ""),
            kind="MERGE",
            title=item.title,
            slug=item.slug,
            event_slug=item.event_slug,
            name=item.name or item.pseudonym,
            source=source,
        )
    return None


def position_from_sdk(pos: Any) -> PositionInfo:
    return PositionInfo(
        wallet=str(pos.wallet).lower(),
        asset_id=str(pos.asset_id),
        condition_id=str(pos.condition_id),
        size=_f(pos.current_size),
        avg_price=_f(pos.avg_price),
        current_price=_f(pos.current_price),
        current_value=_f(pos.current_value),
        realized_pnl=_f(pos.realized_pnl),
        total_pnl=_f(pos.total_pnl),
        redeemable=bool(pos.redeemable),
        title=pos.title,
        outcome=pos.outcome,
        end_date=pos.end_date.isoformat() if pos.end_date else None,
    )


def book_from_sdk(book: Any) -> OrderBook:
    # The SDK returns bids lowest-first and asks highest-first; flip both so
    # index 0 is always the best price.
    bids = [BookLevel(_f(level.price), _f(level.size)) for level in book.bids]
    asks = [BookLevel(_f(level.price), _f(level.size)) for level in book.asks]
    bids.sort(key=lambda lvl: lvl.price, reverse=True)
    asks.sort(key=lambda lvl: lvl.price)
    return OrderBook(
        asset_id=str(book.asset_id),
        bids=bids,
        asks=asks,
        tick_size=_f(book.tick_size, 0.01),
        min_order_size=_f(book.min_order_size, 0.0),
        neg_risk=bool(book.neg_risk),
        ts=time.time(),
    )


# ------------------------------------------------------------------- public data
class PolymarketData:
    """Read-only access to Polymarket's Data, Gamma and CLOB APIs."""

    def __init__(self, client: AsyncPublicClient | None = None) -> None:
        self.client = client or AsyncPublicClient()
        # Comfortably below Polymarket's published limits (data /trades 200 per 10s,
        # gamma /markets 300 per 10s, CLOB reads several thousand per 10s).
        self.data_limiter = RateLimiter(10, 20)
        self.gamma_limiter = RateLimiter(12, 24)
        self.clob_limiter = RateLimiter(20, 40)
        self._http = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        try:
            await self.client.close()
        finally:
            await self._http.aclose()

    async def leaderboard(
        self, *, window: str, category: str | None, limit: int
    ) -> list[LeaderboardRow]:
        rows: list[LeaderboardRow] = []
        pager = self.client.list_trader_leaderboard(
            category=category, window=window, sort_by="PNL", page_size=min(limit, 100)
        )
        await self.data_limiter.acquire()
        async for page in pager:
            for entry in page.items:
                rows.append(
                    LeaderboardRow(
                        wallet=str(entry.wallet).lower(),
                        rank=int(entry.rank),
                        pnl=_f(entry.pnl),
                        volume=_f(entry.volume),
                        name=entry.user_name,
                        profile_image=entry.profile_image,
                        window=window,
                        category=category,
                    )
                )
            if len(rows) >= limit:
                break
            await self.data_limiter.acquire()
        return rows[:limit]

    async def user_trades(
        self, wallet: str, *, since_ts: float, max_items: int
    ) -> list[TradeEvent]:
        out: list[TradeEvent] = []
        pager = self.client.list_trades(
            user=wallet, taker_only=False, start=max(1, int(since_ts)), page_size=500
        )
        await self.data_limiter.acquire()
        async for page in pager:
            for trade in page.items:
                try:
                    out.append(trade_from_sdk(trade, source="history"))
                except Exception:  # malformed row; skip it
                    continue
            if len(out) >= max_items:
                break
            await self.data_limiter.acquire()
        out.sort(key=lambda t: t.ts)
        return out[-max_items:]

    async def user_activity(
        self, wallet: str, *, since_ts: float, limit: int = 50
    ) -> list[TradeEvent]:
        await self.data_limiter.acquire()

        async def fetch():
            return await self.client.list_activity(
                user=wallet,
                activity_types=["TRADE", "MERGE"],
                sort_direction="DESC",
                start=max(1, int(since_ts)),
                page_size=limit,
            ).first_page()

        page = await _with_retry(fetch)
        events = []
        for item in page.items if page else ():
            event = activity_to_event(item)
            if event is not None:
                events.append(event)
        events.sort(key=lambda e: e.ts)
        return events

    async def user_positions(
        self, wallet: str, *, condition_ids: Iterable[str] | None = None
    ) -> list[PositionInfo]:
        ids = list(dict.fromkeys(condition_ids or []))
        batches: list[list[str] | None] = (
            [ids[i : i + _CONDITION_BATCH] for i in range(0, len(ids), _CONDITION_BATCH)]
            if ids
            else [None]
        )
        seen: dict[str, PositionInfo] = {}
        for batch in batches:
            await self.data_limiter.acquire()
            pager = self.client.list_positions(user=wallet, condition_id=batch, page_size=500)
            async for page in pager:
                for pos in page.items:
                    try:
                        info = position_from_sdk(pos)
                    except Exception:
                        continue
                    seen[info.asset_id] = info
                await self.data_limiter.acquire()
        return list(seen.values())

    async def markets_by_condition(
        self, condition_ids: Iterable[str], *, prefer_closed: bool = False
    ) -> dict[str, MarketInfo]:
        wanted = [c for c in dict.fromkeys(condition_ids) if c]
        found: dict[str, MarketInfo] = {}
        # Historical lookups are mostly resolved markets, so ask for closed ones
        # first; live lookups are mostly open markets.
        for closed in ((True, None) if prefer_closed else (None, True)):
            missing = [c for c in wanted if c.lower() not in found]
            for i in range(0, len(missing), _CONDITION_BATCH):
                batch = missing[i : i + _CONDITION_BATCH]
                await self.gamma_limiter.acquire()

                async def fetch(batch=batch, closed=closed):
                    items = []
                    pager = self.client.list_markets(
                        condition_ids=batch, closed=closed, page_size=len(batch) + 5
                    )
                    async for page in pager:
                        items.extend(page.items)
                        if len(items) >= len(batch):
                            break
                    return items

                try:
                    items = await _with_retry(fetch) or []
                except PolymarketError as error:
                    log.warning("Market lookup failed: %s", error)
                    continue
                for market in items:
                    try:
                        info = market_from_sdk(market)
                    except Exception as error:
                        log.debug("Skipping unparseable market: %s", error)
                        continue
                    if info.condition_id:
                        found[info.condition_id.lower()] = info
            wanted_missing = [c for c in wanted if c.lower() not in found]
            if not wanted_missing:
                break
        result: dict[str, MarketInfo] = {}
        for condition_id in wanted:
            info = found.get(condition_id.lower())
            if info is not None:
                result[condition_id] = info
        return result

    async def market_for_token(self, asset_id: str) -> MarketInfo | None:
        for kwargs in (
            {"clob_token_ids": [asset_id]},
            {"clob_token_ids": [asset_id], "closed": True},
            {"position_ids": [asset_id]},
        ):
            await self.gamma_limiter.acquire()
            try:
                page = await _with_retry(
                    lambda kwargs=kwargs: self.client.list_markets(page_size=2, **kwargs).first_page()
                )
            except PolymarketError:
                continue
            for market in page.items if page else ():
                try:
                    return market_from_sdk(market)
                except Exception:
                    continue
        return None

    async def order_book(self, asset_id: str) -> OrderBook:
        await self.clob_limiter.acquire()
        book = await _with_retry(lambda: self.client.get_order_book(asset_id=asset_id))
        return book_from_sdk(book)

    async def midpoints(self, asset_ids: Iterable[str]) -> dict[str, float]:
        ids = [a for a in dict.fromkeys(asset_ids) if a]
        out: dict[str, float] = {}
        for i in range(0, len(ids), 100):
            await self.clob_limiter.acquire()
            try:
                result = await _with_retry(
                    lambda batch=ids[i : i + 100]: self.client.get_midpoints(asset_ids=batch)
                )
            except PolymarketError as error:
                log.debug("Midpoint lookup failed: %s", error)
                continue
            for key, value in (result or {}).items():
                out[str(key)] = _f(value)
        return out

    async def geoblock(self) -> GeoStatus:
        try:
            response = await self._http.get(GEOBLOCK_URL)
            response.raise_for_status()
            data = response.json()
            return GeoStatus(
                blocked=bool(data.get("blocked")),
                country=data.get("country"),
                region=data.get("region"),
                ip=data.get("ip"),
            )
        except Exception as error:
            return GeoStatus(blocked=False, checked=False, error=str(error))

    async def wallet_candidates(self, private_key: str) -> list[dict[str, Any]]:
        """Derive the wallet addresses a private key can control and probe each one.

        Polymarket accounts trade through a smart wallet derived from the signer
        (proxy, Safe or deposit wallet). The one with history is the account.
        """
        from eth_account import Account
        from polymarket._internal.environment import PRODUCTION_CONFIG
        from polymarket._internal.wallet import (
            derive_beacon_deposit_wallet_address,
            derive_proxy_wallet_address,
            derive_safe_wallet_address,
            derive_uups_deposit_wallet_address,
        )

        signer = Account.from_key(private_key).address
        derivation = PRODUCTION_CONFIG.wallet_derivation
        candidates = [
            ("DEPOSIT_WALLET", derive_beacon_deposit_wallet_address(signer, derivation)),
            ("DEPOSIT_WALLET", derive_uups_deposit_wallet_address(signer, derivation)),
            ("POLY_PROXY", derive_proxy_wallet_address(signer, derivation)),
            ("GNOSIS_SAFE", derive_safe_wallet_address(signer, derivation)),
            ("EOA", signer),
        ]
        results = []
        for wallet_type, address in candidates:
            value = 0.0
            last_activity = None
            try:
                await self.data_limiter.acquire()
                portfolio = await self.client.get_portfolio_value(user=address)
                value = _f(portfolio.value)
            except Exception:
                pass
            try:
                await self.data_limiter.acquire()
                page = await self.client.list_activity(
                    user=address, sort_direction="DESC", page_size=1
                ).first_page()
                if page.items:
                    last_activity = _ts(getattr(page.items[0], "timestamp", None))
            except Exception:
                pass
            results.append(
                {
                    "wallet_type": wallet_type,
                    "address": address,
                    "portfolio_value": value,
                    "last_activity": last_activity,
                }
            )
        results.sort(
            key=lambda r: (r["last_activity"] is not None, r["last_activity"] or 0, r["portfolio_value"]),
            reverse=True,
        )
        return results


# ---------------------------------------------------------------------- trading
class PolymarketAccount:
    """Authenticated wallet: balance, market orders and redemption."""

    def __init__(self, client: AsyncSecureClient, info: AccountInfo) -> None:
        self.client = client
        self.info = info
        self.order_limiter = RateLimiter(5, 10)

    @classmethod
    async def login(
        cls,
        *,
        private_key: str,
        wallet: str | None,
        credentials: dict[str, str] | None = None,
        builder_key: dict[str, str] | None = None,
    ) -> tuple["PolymarketAccount", dict[str, Any]]:
        """Authenticate and return the account plus credentials worth caching.

        ``credentials`` are the CLOB API credentials derived from the private key;
        ``builder_key`` enables gasless transactions (redeeming winnings and
        approvals) for smart-contract wallets. Both are created on first login.
        """
        if not wallet:
            raise UserInputError(
                "A Polymarket wallet address is required. Use 'Detect wallet' or copy it "
                "from your Polymarket profile."
            )
        creds = None
        if credentials:
            creds = ApiKeyCreds(
                apiKey=credentials["key"],
                secret=credentials["secret"],
                passphrase=credentials["passphrase"],
            )
        api_key = None
        if builder_key:
            api_key = BuilderApiKey(
                key=builder_key["key"],
                secret=builder_key["secret"],
                passphrase=builder_key["passphrase"],
            )

        try:
            client = await AsyncSecureClient.create(
                private_key=private_key, wallet=wallet, credentials=creds, api_key=api_key
            )
        except RequestRejectedError:
            if creds is None:
                raise
            # Cached credentials were revoked; derive fresh ones.
            client = await AsyncSecureClient.create(
                private_key=private_key, wallet=wallet, api_key=api_key
            )

        cache: dict[str, Any] = {
            "credentials": {
                "key": client.credentials.key,
                "secret": client.credentials.secret,
                "passphrase": client.credentials.passphrase,
            },
            "builder_key": builder_key,
        }

        if client.wallet_type != "EOA" and api_key is None:
            try:
                new_key = await client.create_builder_api_key()
                cache["builder_key"] = {
                    "key": new_key.key,
                    "secret": new_key.secret,
                    "passphrase": new_key.passphrase,
                }
                await client.close()
                client = await AsyncSecureClient.create(
                    private_key=private_key,
                    wallet=wallet,
                    credentials=ApiKeyCreds(
                        apiKey=cache["credentials"]["key"],
                        secret=cache["credentials"]["secret"],
                        passphrase=cache["credentials"]["passphrase"],
                    ),
                    api_key=new_key,
                )
            except Exception as error:
                log.warning("Could not create a builder API key (%s); redemption disabled", error)

        info = AccountInfo(
            wallet=str(client.wallet).lower(),
            signer=str(client.signer).lower(),
            wallet_type=str(client.wallet_type),
            cash=0.0,
            has_relayer_key=client.wallet_type == "EOA" or bool(cache.get("builder_key")),
        )
        account = cls(client, info)
        info.cash = await account.cash_balance()
        info.closed_only = await account.closed_only()
        return account, cache

    async def close(self) -> None:
        await self.client.close()

    async def cash_balance(self) -> float:
        balance = await _with_retry(
            lambda: self.client.get_balance_allowance(asset_type="COLLATERAL")
        )
        cash = int(balance.balance) / 1_000_000 if balance is not None else 0.0
        self.info.cash = cash
        return cash

    async def closed_only(self) -> bool:
        try:
            return bool(await self.client.get_closed_only_mode())
        except Exception:
            return False

    async def ensure_approvals(self) -> str:
        try:
            state = await self.client.get_trading_approvals_state()
            if state.is_fully_approved:
                return "approved"
            if not self.info.has_relayer_key:
                return "missing (orders will request approval automatically)"
            await self.client.setup_trading_approvals()
            return "approved"
        except Exception as error:
            return f"unknown ({error})"

    async def market_buy(
        self, asset_id: str, *, usdc: float, max_price: float, book: OrderBook | None = None
    ) -> Fill:
        await self.order_limiter.acquire()
        try:
            response = await self.client.place_market_order(
                asset_id=asset_id,
                side="BUY",
                amount=_dec(usdc, "0.01"),
                max_price=snap_to_tick(max_price, book.tick_size if book else None, up=False),
                order_type="FAK",
            )
        except InsufficientLiquidityError as error:
            return Fill(ok=False, error=str(error), error_code="no_liquidity")
        except (RequestRejectedError, UserInputError, PolymarketError, httpx.HTTPError) as error:
            return Fill(ok=False, error=str(error), error_code=_error_code(error))
        return await self._fill_from_response(response, side="BUY", book=book, limit=max_price)

    async def market_sell(
        self, asset_id: str, *, shares: float, min_price: float, book: OrderBook | None = None
    ) -> Fill:
        await self.order_limiter.acquire()
        try:
            response = await self.client.place_market_order(
                asset_id=asset_id,
                side="SELL",
                shares=_dec(shares, "0.01"),
                min_price=snap_to_tick(min_price, book.tick_size if book else None, up=True),
                order_type="FAK",
            )
        except InsufficientLiquidityError as error:
            return Fill(ok=False, error=str(error), error_code="no_liquidity")
        except (RequestRejectedError, UserInputError, PolymarketError, httpx.HTTPError) as error:
            return Fill(ok=False, error=str(error), error_code=_error_code(error))
        return await self._fill_from_response(response, side="SELL", book=book, limit=min_price)

    async def _fill_from_response(
        self, response: Any, *, side: str, book: OrderBook | None, limit: float
    ) -> Fill:
        if not getattr(response, "ok", False):
            return Fill(
                ok=False,
                status="rejected",
                error=getattr(response, "message", "order rejected"),
                error_code=str(getattr(response, "code", "unknown")),
            )
        order_id = str(response.order_id)
        making = _f(response.making_amount)
        taking = _f(response.taking_amount)
        status = str(response.status)
        if status == "delayed":
            # Some markets (e.g. live sports) hold taker orders for a few seconds
            # before matching. Poll until the order has a final matched size.
            matched = await self._wait_for_delayed(order_id)
            if matched <= 0:
                return Fill(ok=False, order_id=order_id, status="unfilled", error="order not filled")
            price = _estimate_price(book, side, limit)
            return Fill(True, shares=matched, usdc=matched * price, order_id=order_id, status="matched")
        if side == "BUY":
            shares, usdc = taking, making
        else:
            shares, usdc = making, taking
        if shares <= 0:
            return Fill(ok=False, order_id=order_id, status=status, error="order not filled")
        return Fill(ok=True, shares=shares, usdc=usdc, order_id=order_id, status=status)

    async def _wait_for_delayed(self, order_id: str, timeout: float = 20.0) -> float:
        deadline = time.monotonic() + timeout
        matched = 0.0
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            try:
                order = await self.client.get_order(order_id=order_id)
            except Exception:
                continue
            matched = _f(order.size_matched)
            if str(order.status).lower() not in ("delayed", "live", "unmatched_delayed"):
                return matched
        return matched

    async def redeem(self, condition_id: str) -> str:
        if not self.info.has_relayer_key:
            return "skipped: no relayer key (claim winnings on polymarket.com)"
        try:
            handle = await self.client.redeem_positions(condition_id=condition_id)
            await asyncio.wait_for(handle.wait(), timeout=180)
            return "redeemed"
        except UserInputError as error:
            if "no balance" in str(error).lower():
                return "redeemed"  # already redeemed (e.g. by Polymarket's auto-redeem)
            return f"failed: {error}"
        except Exception as error:
            return f"failed: {error}"


def _estimate_price(book: OrderBook | None, side: str, limit: float) -> float:
    if book is None:
        return limit
    best = book.best_ask if side == "BUY" else book.best_bid
    if best is None:
        return limit
    return min(best, limit) if side == "BUY" else max(best, limit)


def _error_code(error: Exception) -> str:
    if isinstance(error, RequestRejectedError):
        if error.restriction is not None:
            return "restricted"
        return error.code or f"http_{error.status}"
    if isinstance(error, UserInputError):
        return "invalid_input"
    return type(error).__name__
