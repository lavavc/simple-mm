"""
Route selection for arbitrage execution.

select_route() picks the single best feasible trade from a list of candidates,
scoring by net profit (after gas and rebalance cost) with inventory alignment as tiebreak.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from engine.core.arbitrage.cex_dex import (
    estimate_cex_dex_trade,
    estimate_max_cex_buy_usd_for_cngn,
    estimate_max_cex_dex_buy_usd_for_cngn,
)
from engine.core.arbitrage.dex_dex import estimate_dex_dex_trade, estimate_max_dex_buy_usd_for_cngn
from engine.core.arbitrage.route_registry import ROUTES_BY_DIRECTION

_IMBALANCE_THRESHOLD_USD = Decimal("10")


@dataclass
class RouteCandidate:
    direction: str
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
    expected_profit_usd: Decimal  # recomputed at adjusted size when needed


def select_route(
    candidates: list[RouteCandidate],
    inventory,
) -> Optional[SelectedRoute]:
    """
    Pick the best feasible route from candidates.

    Filters: adjusted_size > 0, net_profit >= min_profit_usd, inventory.can_trade() passes.
    Score: net_profit = expected_profit - gas - rebalance_cost_penalty.
    Tiebreak: prefer routes that reduce current inventory imbalance (2=reduces, 1=neutral, 0=worsens).
    """
    scored: list[tuple[Decimal, int, SelectedRoute]] = []
    imbalance = inventory.state.cngn_imbalance_usd

    for c in candidates:
        route_def = ROUTES_BY_DIRECTION.get(c.direction)
        if not route_def:
            continue
        cex_venue = route_def.buy_leg.venue if route_def.buy_leg.leg_type == "api" else route_def.sell_leg.venue
        cex_depth = c.signal.get("depth", {}).get(cex_venue)

        # Block if buy-side stablecoin balance is unknown or zero — mirrors cNGN check below.
        stable_bal = inventory.state.per_account_stable.get(c.buy_venue)
        if not stable_bal:
            continue
        adjusted_size = min(c.optimal_size_usd, stable_bal, inventory.params.max_single_trade_usd)

        # Block if sell-side cNGN balance is unknown (not yet seeded) or explicitly zero.
        # Only proceed when we have a confirmed positive balance to sell.
        cngn_bal = inventory.state.per_account_cngn.get(c.sell_venue)
        if not cngn_bal:
            continue

        if route_def.cngn_effect == "buys_cngn_from_cex":
            # CEX buy → DEX sell: cap against what the CEX orderbook can absorb for our cNGN
            adjusted_size = min(
                adjusted_size,
                estimate_max_cex_buy_usd_for_cngn(cex_depth, cngn_bal),
            )
        elif route_def.cngn_effect == "neutral":
            # DEX → DEX: binary-search the exact buy-side USD that exhausts sell-side cNGN
            sell_cngn_cap_trade = estimate_max_dex_buy_usd_for_cngn(c.direction, cngn_bal)
            if not sell_cngn_cap_trade:
                continue
            adjusted_size = min(adjusted_size, Decimal(str(sell_cngn_cap_trade["optimal_size_usd"])))
        else:
            # DEX buy → CEX sell: exact reverse cap from DEX buy output to sell-side cNGN wallet.
            sell_cngn_cap_trade = estimate_max_cex_dex_buy_usd_for_cngn(c.direction, cex_depth, cngn_bal)
            if not sell_cngn_cap_trade:
                continue
            adjusted_size = min(adjusted_size, Decimal(str(sell_cngn_cap_trade["optimal_size_usd"])))

        if adjusted_size <= 0:
            continue

        expected_profit_usd = c.expected_profit_usd
        if route_def.pipeline == "cex_dex" and adjusted_size != c.optimal_size_usd:
            # Detection already priced the unconstrained optimum. Only rescore when
            # inventory/depth caps force us onto a smaller trade size.
            recomputed = estimate_cex_dex_trade(c.direction, cex_depth, adjusted_size)
            if not recomputed:
                continue
            expected_profit_usd = Decimal(str(recomputed["expected_profit_usd"]))
        elif route_def.cngn_effect == "neutral":
            cngn_cap_size = Decimal(str(sell_cngn_cap_trade["optimal_size_usd"]))
            if adjusted_size == cngn_cap_size:
                # cNGN cap was binding — profit already computed at this size.
                expected_profit_usd = Decimal(str(sell_cngn_cap_trade["expected_profit_usd"]))
            else:
                # Stablecoin cap was tighter — rescore at the reduced size.
                recomputed = estimate_dex_dex_trade(c.direction, adjusted_size)
                if not recomputed:
                    continue
                expected_profit_usd = Decimal(str(recomputed["expected_profit_usd"]))

        # Net profit after gas and rebalance friction
        rebalance_bps = inventory.get_rebalance_cost_bps(c.buy_venue)
        rebalance_cost = adjusted_size * Decimal(rebalance_bps) / Decimal(10000)
        net_profit = expected_profit_usd - c.gas_usd - rebalance_cost

        if net_profit < inventory.params.min_profit_usd:
            continue

        # Risk gate (circuit breaker, volume cap, imbalance limit, daily loss)
        can, _ = inventory.can_trade(adjusted_size, c.buy_venue, c.sell_venue)
        if not can:
            continue

        # Inventory alignment score: 2=reduces imbalance, 1=neutral, 0=worsens.
        if imbalance > _IMBALANCE_THRESHOLD_USD:
            alignment = 2 if route_def.cngn_effect == "sells_cngn_to_cex" else (1 if route_def.cngn_effect == "neutral" else 0)
        elif imbalance < -_IMBALANCE_THRESHOLD_USD:
            alignment = 2 if route_def.cngn_effect == "buys_cngn_from_cex" else (1 if route_def.cngn_effect == "neutral" else 0)
        else:
            alignment = 1

        scored.append((net_profit, alignment, SelectedRoute(c, adjusted_size, net_profit, expected_profit_usd)))

    if not scored:
        return None

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]
