"""
Route selection for arbitrage execution.

select_route() picks the single best feasible trade from a list of candidates,
scoring by net profit (after gas and rebalance cost) with inventory alignment as tiebreak.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

# CEX-DEX directions by their cNGN inventory effect
_SELLS_CNGN_TO_CEX = frozenset({"UNI_BSC_TO_QUIDAX", "UNI_BASE_TO_QUIDAX"})
_BUYS_CNGN_FROM_CEX = frozenset({"QUIDAX_TO_UNI_BSC", "QUIDAX_TO_UNI_BASE"})
_IMBALANCE_THRESHOLD_USD = Decimal("10")


@dataclass
class RouteCandidate:
    direction: str
    pipeline: str            # "cex_dex" or "dex_dex"
    buy_venue: str
    sell_venue: str
    optimal_size_usd: Decimal
    expected_profit_usd: Decimal
    gas_usd: Decimal
    signal: dict             # passed through to execution methods


@dataclass
class SelectedRoute:
    candidate: RouteCandidate
    adjusted_size_usd: Decimal   # capped to available stablecoin
    net_profit_usd: Decimal      # after gas and rebalance penalty


def select_route(
    candidates: list[RouteCandidate],
    inventory,
) -> Optional[SelectedRoute]:
    """
    Pick the best feasible route from candidates.

    Filters: adjusted_size > 0, net_profit > 0, inventory.can_trade() passes.
    Score: net_profit = expected_profit - gas - rebalance_cost_penalty.
    Tiebreak: prefer routes that reduce current inventory imbalance.
    """
    scored: list[tuple[Decimal, bool, SelectedRoute]] = []
    imbalance = inventory.state.cngn_imbalance_usd

    for c in candidates:
        # Cap size to available stablecoin on the buy-side venue
        stable_bal = inventory.state.per_account_stable.get(c.buy_venue, Decimal("0"))
        adjusted_size = min(c.optimal_size_usd, stable_bal) if stable_bal > 0 else c.optimal_size_usd

        # Block if sell-side cNGN balance is unknown (not yet seeded) or explicitly zero.
        # Only proceed when we have a confirmed positive balance to sell.
        cngn_bal = inventory.state.per_account_cngn.get(c.sell_venue)
        if not cngn_bal:
            continue
        cngn_price = inventory.state.cngn_price_usd
        if cngn_price > 0:
            adjusted_size = min(adjusted_size, cngn_bal * cngn_price)

        if adjusted_size <= 0:
            continue

        # Net profit after gas and rebalance friction
        rebalance_bps = inventory.get_rebalance_cost_bps(c.buy_venue)
        rebalance_cost = adjusted_size * Decimal(rebalance_bps) / Decimal(10000)
        net_profit = c.expected_profit_usd - c.gas_usd - rebalance_cost

        if net_profit <= 0:
            continue

        # Risk gate (circuit breaker, volume cap, imbalance limit, daily loss)
        can, _ = inventory.can_trade(adjusted_size, c.buy_venue, c.sell_venue)
        if not can:
            continue

        # Inventory alignment tiebreak
        if imbalance > _IMBALANCE_THRESHOLD_USD:
            aligned = c.direction in _SELLS_CNGN_TO_CEX
        elif imbalance < -_IMBALANCE_THRESHOLD_USD:
            aligned = c.direction in _BUYS_CNGN_FROM_CEX
        else:
            aligned = True

        scored.append((net_profit, aligned, SelectedRoute(c, adjusted_size, net_profit)))

    if not scored:
        return None

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]
