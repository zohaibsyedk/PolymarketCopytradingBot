"""Polymarket taker fees.

Since 2026 Polymarket charges takers ``shares * rate * (p * (1 - p)) ** exponent``
(exponent is 1 on the current schedule), e.g. rate 0.07 on crypto, 0.05 on
sports and 0.04 on politics/finance/tech. The fee peaks at p = 0.5 and falls
toward 0 and 1. Market orders - which PolyCopy uses to copy - are taker orders.
"""

from __future__ import annotations

from polycopy.models import FeeInfo


def taker_fee(shares: float, price: float, fees: FeeInfo | None) -> float:
    if fees is None or not fees.enabled or fees.rate <= 0 or shares <= 0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    return shares * fees.rate * (p * (1.0 - p)) ** (fees.exponent or 1.0)


def fee_per_dollar(price: float, fees: FeeInfo | None) -> float:
    """Fee as a fraction of notional for a buy at ``price``."""
    if price <= 0:
        return 0.0
    return taker_fee(1.0, price, fees) / price
