"""Entry rules shared by the live copier and the historical copy simulation.

Using one function for both guarantees that a leader's score reflects exactly
the trades PolyCopy would actually copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polycopy.config import FilterSettings
from polycopy.models import MarketInfo


@dataclass(slots=True)
class EntryRules:
    max_hours_to_resolution: float = 48.0
    min_minutes_to_resolution: float = 20.0
    min_entry_price: float = 0.05
    max_entry_price: float = 0.94
    min_leader_trade_usdc: float = 25.0
    skip_in_play_sports: bool = True
    excluded_keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, filters: FilterSettings) -> "EntryRules":
        return cls(
            max_hours_to_resolution=filters.max_hours_to_resolution,
            min_minutes_to_resolution=filters.min_minutes_to_resolution,
            min_entry_price=filters.min_entry_price,
            max_entry_price=filters.max_entry_price,
            min_leader_trade_usdc=filters.min_leader_trade_usdc,
            skip_in_play_sports=filters.skip_in_play_sports,
            excluded_keywords=[k.lower() for k in filters.excluded_keywords if k.strip()],
        )


def check_entry(
    market: MarketInfo | None,
    *,
    ts: float,
    price: float,
    leader_usdc: float,
    rules: EntryRules,
) -> str | None:
    """Return why a leader BUY at ``ts`` should not be copied, or None to copy it."""
    if market is None:
        return "market details unavailable"
    resolves_at = market.expected_resolution_ts()
    if resolves_at is None:
        return "market has no end date"
    seconds_left = resolves_at - ts
    if seconds_left <= 0:
        return "market already past its end date"
    hours_left = seconds_left / 3600.0
    if hours_left > rules.max_hours_to_resolution:
        return f"resolves in {hours_left:.0f}h (limit {rules.max_hours_to_resolution:.0f}h)"
    if seconds_left < rules.min_minutes_to_resolution * 60:
        return f"resolves in {seconds_left / 60:.0f}m (too soon to copy safely)"
    if not rules.min_entry_price <= price <= rules.max_entry_price:
        return (
            f"price {price:.3f} outside {rules.min_entry_price:.2f}-{rules.max_entry_price:.2f}"
        )
    if leader_usdc < rules.min_leader_trade_usdc:
        return f"leader trade ${leader_usdc:,.0f} below ${rules.min_leader_trade_usdc:,.0f}"
    if rules.skip_in_play_sports and market.game_start_ts and ts >= market.game_start_ts:
        return "game already in progress (in-play)"
    if rules.excluded_keywords:
        text = f"{market.question} {market.slug or ''} {' '.join(market.tags)}".lower()
        for keyword in rules.excluded_keywords:
            if keyword and keyword in text:
                return f"excluded keyword '{keyword}'"
    return None
