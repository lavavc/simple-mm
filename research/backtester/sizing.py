"""Position sizing policies: how much of the bankroll enters the position.

Strategy parameters decide where the range goes; a SizingPolicy decides how
many dollars are deployed into it at entry time. The bankroll is fixed per
simulation and the undeployed remainder stays in the wallet (optionally
accruing a report-only hurdle credit), so constant, dynamic, and cross-pool
policies are all comparable on the same denominator.

Policies see only the EntryContext — entry-time observables, never future
events — and declare free_parameter_count so selection experiments can
budget for overfitting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class EntryContext:
    block_time: datetime
    wallet_value_usd: float
    current_price: float
    current_tick: int
    # Recorded pool depth in L units at this event; excludes our virtual
    # position (the simulator dilutes fee share separately).
    active_liquidity: int


class SizingPolicy(Protocol):
    free_parameter_count: int

    def deployed_capital_usd(self, context: EntryContext) -> float: ...


@dataclass(frozen=True)
class DeployFullWallet:
    """Status quo: the entire wallet enters the position."""

    free_parameter_count = 0

    def deployed_capital_usd(self, context: EntryContext) -> float:
        return context.wallet_value_usd


@dataclass(frozen=True)
class FixedDeployment:
    """Deploy a fixed dollar amount, capped by what the wallet holds."""

    capital_usd: float

    free_parameter_count = 1

    def deployed_capital_usd(self, context: EntryContext) -> float:
        return min(self.capital_usd, context.wallet_value_usd)
