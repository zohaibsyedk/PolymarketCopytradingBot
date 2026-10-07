"""Position sizing and portfolio limits (pure functions, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field

from polycopy.config import RiskSettings


@dataclass(slots=True)
class ExposureSnapshot:
    equity: float  # cash + market value of the bot's open positions
    cash: float
    bot_pnl_total: float = 0.0  # realized + unrealized PnL produced by the bot
    total_exposure: float = 0.0  # cost basis of open positions
    market_exposure: dict[str, float] = field(default_factory=dict)  # by condition id
    leader_exposure: dict[str, float] = field(default_factory=dict)  # by leader wallet
    open_positions: int = 0


@dataclass(slots=True)
class StakeRequest:
    wallet: str
    condition_id: str
    price: float
    leader_score: float
    leader_usdc: float
    leader_median_usdc: float
    consensus: int = 0  # other followed leaders already holding this outcome
    is_new_position: bool = True


@dataclass(slots=True)
class StakeDecision:
    stake: float
    reason: str | None  # set when the stake is zero
    bankroll: float
    multipliers: dict[str, float] = field(default_factory=dict)


def bankroll_for(snapshot: ExposureSnapshot, risk: RiskSettings) -> float:
    if risk.bankroll_mode == "fixed":
        allocated = risk.allocated_bankroll_usdc + snapshot.bot_pnl_total
        return max(0.0, min(snapshot.equity, allocated))
    return max(0.0, snapshot.equity)


def compute_stake(
    request: StakeRequest, snapshot: ExposureSnapshot, risk: RiskSettings
) -> StakeDecision:
    bankroll = bankroll_for(snapshot, risk)
    if bankroll <= 0:
        return StakeDecision(0.0, "no bankroll available", bankroll)
    if request.is_new_position and snapshot.open_positions >= risk.max_open_positions:
        return StakeDecision(0.0, f"max open positions ({risk.max_open_positions}) reached", bankroll)

    base = bankroll * risk.per_trade_pct
    leader_mult = 0.6 + 0.8 * min(max(request.leader_score, 0.0), 100.0) / 100.0
    ratio = (
        request.leader_usdc / request.leader_median_usdc if request.leader_median_usdc > 0 else 1.0
    )
    conviction = min(1.5, max(0.5, 0.5 + 0.5 * ratio))
    consensus = min(1.5, 1.0 + 0.25 * max(request.consensus, 0))
    # Long shots lose most of the time; size them down to cut variance.
    price_mult = 1.0 if request.price >= 0.25 else 0.5 + 2.0 * request.price
    stake = base * leader_mult * conviction * consensus * price_mult
    multipliers = {
        "leader": round(leader_mult, 3),
        "conviction": round(conviction, 3),
        "consensus": round(consensus, 3),
        "price": round(price_mult, 3),
    }

    limits = {
        "max trade size": risk.max_trade_usdc,
        "market exposure cap": bankroll * risk.max_market_exposure_pct
        - snapshot.market_exposure.get(request.condition_id, 0.0),
        "leader exposure cap": bankroll * risk.max_leader_exposure_pct
        - snapshot.leader_exposure.get(request.wallet, 0.0),
        "total exposure cap": bankroll * risk.max_total_exposure_pct - snapshot.total_exposure,
        "cash reserve": snapshot.cash - bankroll * risk.cash_reserve_pct,
    }
    binding = None
    for name, limit in limits.items():
        if limit < stake:
            stake = limit
            binding = name
    stake = round(max(stake, 0.0), 2)
    if stake < risk.min_trade_usdc:
        return StakeDecision(0.0, f"{binding or 'stake'} leaves only ${max(stake, 0):.2f}", bankroll,
                             multipliers)
    return StakeDecision(stake, None, bankroll, multipliers)


def chase_cap(leader_price: float, max_chase_cents: float, max_chase_pct: float) -> float:
    """Highest price PolyCopy will pay when copying a leader's buy."""
    cap = min(leader_price + max_chase_cents, leader_price * (1.0 + max_chase_pct))
    return round(min(max(cap, leader_price), 0.99), 4)


def daily_loss_exceeded(day_pnl: float, day_start_equity: float, risk: RiskSettings) -> bool:
    if day_start_equity <= 0:
        return False
    return day_pnl < -risk.daily_loss_limit_pct * day_start_equity


def drawdown_exceeded(
    base_capital: float, peak_pnl: float, current_pnl: float, risk: RiskSettings
) -> bool:
    peak_equity = base_capital + peak_pnl
    if peak_equity <= 0:
        return False
    return (peak_pnl - current_pnl) / peak_equity > risk.max_drawdown_pause_pct
