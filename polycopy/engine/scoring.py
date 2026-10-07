"""Copy-trading backtest and leader scoring.

Leaderboard PnL says how much a wallet made; it does not say how much *a copier*
would have made. This module replays a wallet's recent trades the way PolyCopy
would copy them - same entry rules, a fixed stake per signal, a worse price than
the leader got (slippage), taker fees, mirrored sells, settlement at resolution -
and scores the wallet on the result.

The score rewards:
  * a positive return per dollar after costs, shrunk toward zero for small samples
  * statistical confidence that the edge is skill rather than luck: both a t-test
    on per-position returns and a calibration test (did the trader's picks win
    more often than their entry prices implied?)
  * consistency (profitable days, profitable in both halves of the window)
and penalises:
  * PnL concentrated in one or two positions
  * deep drawdowns
  * inactivity, bot-like trade frequency and long-dated markets
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace

from polycopy.engine.fees import taker_fee
from polycopy.engine.rules import EntryRules, check_entry
from polycopy.models import MarketInfo, TradeEvent

DAY = 86400.0


@dataclass(slots=True)
class SimParams:
    stake_usdc: float = 100.0
    slippage: float = 0.01
    lookback_days: int = 30
    max_entries_per_asset: int = 3
    order_merge_seconds: float = 60.0
    min_resolved_positions: int = 15
    max_trades_per_day: float = 150.0
    min_short_horizon_share: float = 0.25
    rules: EntryRules = field(default_factory=EntryRules)


@dataclass(slots=True)
class SimPosition:
    asset_id: str
    condition_id: str
    title: str
    outcome: str | None
    category: str | None
    first_entry_ts: float
    entry_prices: list[float] = field(default_factory=list)
    hours_to_resolution: float | None = None
    shares: float = 0.0
    cost: float = 0.0  # remaining cost basis
    invested: float = 0.0  # total USDC put in (stakes + fees)
    realized: float = 0.0
    fees: float = 0.0
    entries: int = 0
    status: str = "open"  # open | sold | resolved
    closed_ts: float | None = None
    payout: float | None = None
    mark_value: float = 0.0

    @property
    def pnl(self) -> float:
        return self.realized + (self.mark_value - self.cost if self.status == "open" else 0.0)


@dataclass(slots=True)
class LeaderMetrics:
    wallet: str
    score: float = 0.0
    eligible: bool = False
    reason: str | None = None
    closed_positions: int = 0
    open_positions: int = 0
    resolved_positions: int = 0
    copied_entries: int = 0
    total_invested: float = 0.0
    total_pnl: float = 0.0
    roi: float = 0.0
    roi_shrunk: float = 0.0
    win_rate: float = 0.0
    win_rate_lower: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    t_stat: float = 0.0
    skill_z: float = 0.0
    expected_win_rate: float = 0.0
    profitable_days: float = 0.0
    first_half_pnl: float = 0.0
    second_half_pnl: float = 0.0
    concentration: float = 0.0
    max_drawdown: float = 0.0
    recent_pnl_7d: float = 0.0
    open_pnl: float = 0.0
    trades_per_day: float = 0.0
    short_horizon_share: float = 0.0
    avg_entry_price: float = 0.0
    avg_hours_to_resolution: float = 0.0
    total_trades: int = 0
    last_trade_ts: float | None = None
    median_trade_usdc: float = 0.0
    top_categories: list[str] = field(default_factory=list)
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class SimResult:
    metrics: LeaderMetrics
    positions: list[SimPosition]
    curve: list[tuple[float, float]]


# ------------------------------------------------------------------- helpers
def aggregate_orders(trades: list[TradeEvent], merge_seconds: float) -> list[TradeEvent]:
    """Merge the many fills of one order into a single event.

    A single market order often fills against several resting orders and shows
    up as several trade rows. Copying each row would multiply the stake, so
    fills with the same transaction hash (or, without a hash, the same asset and
    side within ``merge_seconds``) are combined into one size-weighted event.
    """
    merged: list[TradeEvent] = []
    open_groups: dict[tuple, TradeEvent] = {}
    for trade in sorted(trades, key=lambda t: t.ts):
        key = (trade.asset_id, trade.side, trade.tx_hash) if trade.tx_hash else None
        group = open_groups.get(key) if key else None
        if group is None and not trade.tx_hash:
            for candidate in reversed(merged[-10:]):
                if (
                    candidate.asset_id == trade.asset_id
                    and candidate.side == trade.side
                    and not candidate.tx_hash
                    and trade.ts - candidate.ts <= merge_seconds
                ):
                    group = candidate
                    break
        if group is None:
            clone = replace(trade)
            merged.append(clone)
            if key:
                open_groups[key] = clone
            continue
        total = group.size + trade.size
        if total > 0:
            group.price = (group.price * group.size + trade.price * trade.size) / total
        group.size = total
        group.usdc += trade.usdc
    return merged


def wilson_lower(wins: int, n: int, z: float = 1.2816) -> float:
    """Lower bound of the Wilson score interval (default 80% two-sided)."""
    if n == 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


# -------------------------------------------------------------------- simulate
def simulate_copy(
    wallet: str,
    trades: list[TradeEvent],
    markets: dict[str, MarketInfo],
    params: SimParams,
    *,
    now: float | None = None,
) -> SimResult:
    now = now or time.time()
    start = now - params.lookback_days * DAY
    window = [t for t in trades if t.ts >= start and t.kind == "TRADE"]
    orders = aggregate_orders(window, params.order_merge_seconds)

    leader_hold: dict[str, float] = defaultdict(float)
    positions: dict[str, SimPosition] = {}
    skip_reasons: dict[str, int] = defaultdict(int)
    buy_usdc_total = 0.0
    buy_usdc_eligible = 0.0
    order_sizes: list[float] = []
    categories: dict[str, float] = defaultdict(float)

    for order in orders:
        market = markets.get(order.condition_id) or markets.get(order.condition_id.lower())
        if order.side == "BUY":
            leader_hold[order.asset_id] += order.size
            buy_usdc_total += order.usdc
            order_sizes.append(order.usdc)
            reason = check_entry(
                market, ts=order.ts, price=order.price, leader_usdc=order.usdc, rules=params.rules
            )
            if reason is None and market is not None and market.token_side(order.asset_id) is None:
                reason = "outcome token not found in market"
            if reason is not None:
                skip_reasons[_bucket_reason(reason)] += 1
                continue
            assert market is not None
            buy_usdc_eligible += order.usdc
            pos = positions.get(order.asset_id)
            if pos is not None and pos.entries >= params.max_entries_per_asset:
                skip_reasons["position cap reached"] += 1
                continue
            entry_px = min(order.price + params.slippage, 0.99)
            shares = params.stake_usdc / entry_px
            fee = taker_fee(shares, entry_px, market.fees)
            if pos is None:
                pos = SimPosition(
                    asset_id=order.asset_id,
                    condition_id=order.condition_id,
                    title=market.question or (order.title or ""),
                    outcome=market.outcome_label(order.asset_id) or order.outcome,
                    category=market_category(market),
                    first_entry_ts=order.ts,
                    hours_to_resolution=((market.expected_resolution_ts() or order.ts) - order.ts) / 3600.0,
                )
                positions[order.asset_id] = pos
            elif pos.status == "sold":
                pos.status = "open"
                pos.closed_ts = None
            pos.entry_prices.append(entry_px)
            pos.shares += shares
            pos.cost += params.stake_usdc + fee
            pos.invested += params.stake_usdc + fee
            pos.fees += fee
            pos.entries += 1
            categories[pos.category or "other"] += params.stake_usdc
        else:
            before = leader_hold[order.asset_id]
            leader_hold[order.asset_id] = max(0.0, before - order.size)
            pos = positions.get(order.asset_id)
            if pos is None or pos.shares <= 0 or before <= 0:
                continue
            fraction = _clamp(order.size / before, 0.0, 1.0)
            sell_shares = pos.shares * fraction
            exit_px = max(order.price - params.slippage, 0.01)
            fee = taker_fee(sell_shares, exit_px, market.fees if market else None)
            proceeds = sell_shares * exit_px - fee
            cost_out = pos.cost * fraction
            pos.realized += proceeds - cost_out
            pos.fees += fee
            pos.cost -= cost_out
            pos.shares -= sell_shares
            if pos.shares < 1e-6 or fraction > 0.999:
                pos.shares = 0.0
                pos.cost = 0.0
                pos.status = "sold"
                pos.closed_ts = order.ts

    # Settle what is left at resolution, or mark it to market if still open.
    for pos in positions.values():
        if pos.status != "open" or pos.shares <= 0:
            continue
        market = markets.get(pos.condition_id) or markets.get(pos.condition_id.lower())
        payout = market.resolution_payout(pos.asset_id) if market else None
        if payout is not None:
            pos.payout = payout
            pos.realized += pos.shares * payout - pos.cost
            pos.cost = 0.0
            pos.status = "resolved"
            pos.closed_ts = (market.closed_ts if market else None) or (
                market.end_ts if market else None
            ) or now
            pos.closed_ts = min(pos.closed_ts, now)
        else:
            price = None
            if market is not None:
                side = market.token_side(pos.asset_id)
                price = market.yes_price if side == 0 else market.no_price
            pos.mark_value = pos.shares * (price if price is not None else pos.cost / pos.shares)

    metrics = _metrics(
        wallet,
        list(positions.values()),
        trades=window,
        orders=orders,
        now=now,
        params=params,
        buy_usdc_total=buy_usdc_total,
        buy_usdc_eligible=buy_usdc_eligible,
        order_sizes=order_sizes,
        categories=categories,
    )
    metrics.skip_reasons = dict(skip_reasons)
    curve = _equity_curve(list(positions.values()))
    score_metrics(metrics, params, now=now)
    return SimResult(metrics=metrics, positions=list(positions.values()), curve=curve)


def _bucket_reason(reason: str) -> str:
    if reason.startswith("resolves in") and "too soon" in reason:
        return "resolves too soon"
    if reason.startswith("resolves in"):
        return "resolves too late"
    if reason.startswith("price"):
        return "price outside band"
    if reason.startswith("leader trade"):
        return "trade too small"
    if reason.startswith("excluded keyword"):
        return "excluded keyword"
    return reason


def market_category(market: MarketInfo) -> str | None:
    if market.category:
        return market.category.lower()
    for tag in market.tags:
        lowered = tag.lower()
        if lowered in {"sports", "crypto", "politics", "finance", "tech", "culture", "weather",
                       "economics", "geopolitics", "esports", "mentions"}:
            return lowered
    if market.sports_market_type or market.game_start_ts:
        return "sports"
    return market.tags[0].lower() if market.tags else None


def _metrics(
    wallet: str,
    positions: list[SimPosition],
    *,
    trades: list[TradeEvent],
    orders: list[TradeEvent],
    now: float,
    params: SimParams,
    buy_usdc_total: float,
    buy_usdc_eligible: float,
    order_sizes: list[float],
    categories: dict[str, float],
) -> LeaderMetrics:
    m = LeaderMetrics(wallet=wallet)
    closed = [p for p in positions if p.status in ("sold", "resolved")]
    open_ = [p for p in positions if p.status == "open"]
    resolved = [p for p in positions if p.status == "resolved"]
    m.closed_positions = len(closed)
    m.open_positions = len(open_)
    m.resolved_positions = len(resolved)
    m.copied_entries = sum(p.entries for p in positions)
    m.total_trades = len(trades)
    m.last_trade_ts = max((t.ts for t in trades), default=None)
    span_days = max(1.0, min(params.lookback_days, (now - min((t.ts for t in trades), default=now)) / DAY))
    m.trades_per_day = len(orders) / span_days
    m.short_horizon_share = buy_usdc_eligible / buy_usdc_total if buy_usdc_total > 0 else 0.0
    m.median_trade_usdc = _median(order_sizes)
    m.top_categories = [c for c, _ in sorted(categories.items(), key=lambda kv: -kv[1])[:3]]
    m.open_pnl = sum(p.pnl for p in open_)

    if not closed:
        return m

    pnls = [p.realized for p in closed]
    invested = [p.invested for p in closed]
    m.total_invested = sum(invested)
    m.total_pnl = sum(pnls)
    m.roi = m.total_pnl / m.total_invested if m.total_invested else 0.0
    prior = 10 * params.stake_usdc
    m.roi_shrunk = m.total_pnl / (m.total_invested + prior)

    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    m.win_rate = len(wins) / len(pnls)
    m.win_rate_lower = wilson_lower(len(wins), len(pnls))
    m.avg_win = sum(wins) / len(wins) if wins else 0.0
    m.avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_loss = -sum(losses)
    m.profit_factor = (sum(wins) / gross_loss) if gross_loss > 0 else (99.0 if wins else 0.0)

    returns = [p.realized / p.invested for p in closed if p.invested > 0]
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        sd = math.sqrt(var)
        m.t_stat = mean / sd * math.sqrt(len(returns)) if sd > 1e-9 else (5.0 if mean > 0 else 0.0)

    # Calibration test on positions held to resolution: under no skill a share
    # bought at price p wins with probability p.
    if resolved:
        probs = [sum(p.entry_prices) / len(p.entry_prices) for p in resolved]
        won = sum(1 for p in resolved if (p.payout or 0) >= 0.5)
        expected = sum(probs)
        variance = sum(q * (1 - q) for q in probs)
        m.expected_win_rate = expected / len(resolved)
        m.skill_z = (won - expected) / math.sqrt(variance) if variance > 1e-9 else 0.0

    all_prices = [px for p in positions for px in p.entry_prices]
    m.avg_entry_price = sum(all_prices) / len(all_prices) if all_prices else 0.0
    hours = [p.hours_to_resolution for p in positions if p.hours_to_resolution is not None]
    m.avg_hours_to_resolution = sum(hours) / len(hours) if hours else 0.0

    # Daily PnL, halves and drawdown based on closing time.
    by_day: dict[int, float] = defaultdict(float)
    for p in closed:
        by_day[int((p.closed_ts or now) // DAY)] += p.realized
    m.profitable_days = sum(1 for v in by_day.values() if v > 0) / len(by_day)
    midpoint = now - params.lookback_days * DAY / 2
    m.first_half_pnl = sum(p.realized for p in closed if (p.closed_ts or now) < midpoint)
    m.second_half_pnl = sum(p.realized for p in closed if (p.closed_ts or now) >= midpoint)
    m.recent_pnl_7d = sum(p.realized for p in closed if (p.closed_ts or now) >= now - 7 * DAY)

    positive = sum(wins)
    m.concentration = max(wins) / positive if positive > 0 else 1.0

    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for p in sorted(closed, key=lambda p: p.closed_ts or now):
        cum += p.realized
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    m.max_drawdown = max_dd
    return m


def score_metrics(m: LeaderMetrics, params: SimParams, *, now: float | None = None) -> LeaderMetrics:
    """Turn metrics into a 0-100 score and an eligibility verdict."""
    now = now or time.time()
    m.score = 0.0
    m.eligible = False
    if m.closed_positions < params.min_resolved_positions:
        m.reason = (
            f"only {m.closed_positions} copyable positions closed "
            f"(need {params.min_resolved_positions})"
        )
        return m
    if m.trades_per_day > params.max_trades_per_day:
        m.reason = f"{m.trades_per_day:.0f} orders/day looks like a bot or market maker"
        return m
    if m.short_horizon_share < params.min_short_horizon_share:
        m.reason = (
            f"only {m.short_horizon_share:.0%} of volume is in copyable short-dated markets"
        )
        return m
    if m.roi_shrunk <= 0:
        m.reason = "not profitable once copy slippage and fees are included"
        return m

    conf_t = _clamp(m.t_stat / 3.0, 0.0, 1.0)
    conf_z = _clamp(m.skill_z / 2.5, 0.0, 1.0)
    confidence = 0.5 * conf_t + 0.5 * conf_z
    f_conf = 0.35 + 0.65 * confidence
    f_cons = 0.6 + 0.4 * m.profitable_days
    f_halves = 1.0 if (m.first_half_pnl > 0 and m.second_half_pnl > 0) else 0.65
    f_conc = 1.0 - 0.6 * _clamp((m.concentration - 0.35) / 0.65, 0.0, 1.0)
    dd_ratio = _clamp(m.max_drawdown / (10 * params.stake_usdc), 0.0, 1.0)
    f_dd = 1.0 - 0.5 * dd_ratio
    f_recent = 1.0
    if m.last_trade_ts is not None and now - m.last_trade_ts > 7 * DAY:
        f_recent *= 0.5
    if m.recent_pnl_7d < 0:
        f_recent *= 0.85

    raw = m.roi_shrunk * f_conf * f_cons * f_halves * f_conc * f_dd * f_recent
    m.score = round(100.0 * (1.0 - math.exp(-raw / 0.04)), 1)
    m.eligible = True
    m.reason = None
    return m


def _equity_curve(positions: list[SimPosition], max_points: int = 80) -> list[tuple[float, float]]:
    closed = sorted(
        (p for p in positions if p.status in ("sold", "resolved")), key=lambda p: p.closed_ts or 0
    )
    curve: list[tuple[float, float]] = []
    cum = 0.0
    for p in closed:
        cum += p.realized
        curve.append((p.closed_ts or 0.0, round(cum, 2)))
    if len(curve) > max_points:
        step = len(curve) / max_points
        curve = [curve[int(i * step)] for i in range(max_points - 1)] + [curve[-1]]
    return curve
