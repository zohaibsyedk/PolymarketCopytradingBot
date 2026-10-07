"""Plain data objects shared by the gateway, the engine and the web layer.

The engine never touches SDK models directly; the gateway converts everything
into these small dataclasses so the trading logic can be tested with fakes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Side = Literal["BUY", "SELL"]
SPORTS_SETTLE_SECONDS = 6 * 3600.0
EventKind = Literal["TRADE", "MERGE"]


@dataclass(slots=True)
class LeaderboardRow:
    wallet: str
    rank: int
    pnl: float
    volume: float
    name: str | None = None
    profile_image: str | None = None
    window: str = ""
    category: str | None = None


@dataclass(slots=True)
class TradeEvent:
    """One trade (or merge) by a wallet, as reported by the data API or the stream."""

    wallet: str
    asset_id: str
    condition_id: str
    side: Side
    price: float
    size: float  # shares
    ts: float  # unix seconds
    tx_hash: str = ""
    kind: EventKind = "TRADE"
    usdc: float = 0.0
    outcome: str | None = None
    outcome_index: int | None = None
    title: str | None = None
    slug: str | None = None
    event_slug: str | None = None
    icon: str | None = None
    name: str | None = None
    source: str = "poll"

    def __post_init__(self) -> None:
        self.wallet = self.wallet.lower()
        if not self.usdc:
            self.usdc = self.price * self.size

    def fill_key(self) -> tuple:
        return (
            self.wallet,
            self.tx_hash.lower(),
            self.asset_id,
            self.side,
            round(self.size, 4),
            round(self.price, 4),
        )


@dataclass(slots=True)
class FeeInfo:
    enabled: bool = False
    rate: float = 0.0
    exponent: float = 1.0
    taker_only: bool = True


@dataclass(slots=True)
class MarketInfo:
    condition_id: str
    question: str
    yes_token: str | None
    no_token: str | None
    yes_label: str = "Yes"
    no_label: str = "No"
    # Polymarket V2 markets may also expose position ids for the same outcomes.
    yes_alt: str | None = None
    no_alt: str | None = None
    yes_price: float | None = None
    no_price: float | None = None
    slug: str | None = None
    event_slug: str | None = None
    icon: str | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    enable_order_book: bool = True
    neg_risk: bool = False
    end_ts: float | None = None
    start_ts: float | None = None
    closed_ts: float | None = None
    game_start_ts: float | None = None
    sports_market_type: str | None = None
    liquidity: float | None = None
    volume_24h: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    uma_status: str | None = None
    fees: FeeInfo = field(default_factory=FeeInfo)
    fetched_at: float = 0.0

    def expected_resolution_ts(self) -> float | None:
        """When the market should settle.

        Sports markets often carry an end date days after the game as a buffer;
        the game itself settles within hours of kickoff, so use the earlier of the
        two. Other markets settle at their end date.
        """
        if self.game_start_ts and self.end_ts:
            return min(self.end_ts, self.game_start_ts + SPORTS_SETTLE_SECONDS)
        return self.end_ts or (self.game_start_ts + SPORTS_SETTLE_SECONDS if self.game_start_ts else None)

    def token_side(self, asset_id: str) -> int | None:
        """0 for the YES token, 1 for the NO token, None if unknown."""
        if not asset_id:
            return None
        if asset_id in (self.yes_token, self.yes_alt):
            return 0
        if asset_id in (self.no_token, self.no_alt):
            return 1
        return None

    def outcome_label(self, asset_id: str) -> str | None:
        idx = self.token_side(asset_id)
        if idx is None:
            return None
        return self.yes_label if idx == 0 else self.no_label

    def opposite_token(self, asset_id: str) -> str | None:
        idx = self.token_side(asset_id)
        if idx is None:
            return None
        return self.no_token if idx == 0 else self.yes_token

    def resolution_payout(self, asset_id: str) -> float | None:
        """Payout per share once the market has resolved, else None.

        A resolved market reports final outcome prices of 1/0 (or 0.5/0.5 for a
        50-50 resolution). Anything else means the result is not final yet.
        """
        if not self.closed:
            return None
        idx = self.token_side(asset_id)
        if idx is None or self.yes_price is None or self.no_price is None:
            return None
        prices = (self.yes_price, self.no_price)
        if not all(_is_final_price(p) for p in prices):
            return None
        if abs(prices[0] + prices[1] - 1.0) > 0.02:
            return None
        return round(prices[idx] * 2) / 2

    def is_resolved(self) -> bool:
        if self.yes_token is None:
            return False
        return self.resolution_payout(self.yes_token) is not None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MarketInfo":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        fees = known.get("fees")
        if isinstance(fees, dict):
            known["fees"] = FeeInfo(**{k: v for k, v in fees.items() if k in FeeInfo.__dataclass_fields__})
        return cls(**known)


def _is_final_price(price: float) -> bool:
    return price <= 0.005 or price >= 0.995 or abs(price - 0.5) <= 0.005


@dataclass(slots=True)
class BookLevel:
    price: float
    size: float


@dataclass(slots=True)
class OrderBook:
    asset_id: str
    bids: list[BookLevel]  # best (highest) first
    asks: list[BookLevel]  # best (lowest) first
    tick_size: float = 0.01
    min_order_size: float = 0.0
    neg_risk: bool = False
    ts: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return self.best_bid if self.best_ask is None else self.best_ask
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(slots=True)
class PositionInfo:
    """A wallet's position as reported by the data API."""

    wallet: str
    asset_id: str
    condition_id: str
    size: float
    avg_price: float
    current_price: float
    current_value: float
    realized_pnl: float = 0.0
    total_pnl: float = 0.0
    redeemable: bool = False
    title: str | None = None
    outcome: str | None = None
    end_date: str | None = None


@dataclass(slots=True)
class Fill:
    """Result of an order (live or paper)."""

    ok: bool
    shares: float = 0.0
    usdc: float = 0.0  # USDC paid (buy) or received (sell), excluding fees
    fee: float = 0.0
    order_id: str | None = None
    status: str = ""
    error: str | None = None
    error_code: str | None = None

    @property
    def avg_price(self) -> float:
        return self.usdc / self.shares if self.shares > 0 else 0.0


@dataclass(slots=True)
class GeoStatus:
    blocked: bool
    country: str | None = None
    region: str | None = None
    ip: str | None = None
    checked: bool = True
    error: str | None = None


@dataclass(slots=True)
class AccountInfo:
    wallet: str
    signer: str
    wallet_type: str
    cash: float
    closed_only: bool = False
    has_relayer_key: bool = False
