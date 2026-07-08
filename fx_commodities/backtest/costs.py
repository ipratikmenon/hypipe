"""Cost model (REQUIREMENTS.md §10.2): spread + commission + slippage (+ swap
when overnight holding is enabled). All values in account currency.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    point: float                      # price units per point
    tick_value_per_lot: float         # account ccy per point per 1.0 lot
    commission_per_lot_side: float = 3.5
    slippage_points: float = 2.0

    def entry_exit_cost(self, lots: float, spread_points_at_entry: float,
                        spread_points_at_exit: float) -> float:
        """Round-trip cost: half spread each side + slippage each side +
        commission both sides."""
        spread_cost = (spread_points_at_entry + spread_points_at_exit) / 2.0
        slip_cost = 2.0 * self.slippage_points
        price_cost = (spread_cost + slip_cost) * self.tick_value_per_lot * lots
        commission = 2.0 * self.commission_per_lot_side * lots
        return price_cost + commission

    def cost_in_price_units(self, spread_points: float) -> float:
        """One-side price penalty applied to fills (half spread + slippage)."""
        return (spread_points / 2.0 + self.slippage_points) * self.point
