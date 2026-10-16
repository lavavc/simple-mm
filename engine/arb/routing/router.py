"""
Route selection for arbitrage execution.

select_route() picks the single best feasible trade from a list of candidates,
scoring monetary profit separately from venue-local inventory improvement.
"""
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import structlog

from engine.arb.detection.cex_dex import (
    estimate_cex_dex_trade,
    estimate_max_cex_buy_usd_for_cngn,
    estimate_max_cex_dex_buy_usd_for_cngn,
)
from engine.arb.detection.dex_dex import estimate_dex_dex_trade, estimate_max_dex_buy_usd_for_cngn
from engine.arb.risk.balance_scoring import project_route
from engine.arb.risk.inventory import InventoryTracker
from engine.arb.routing.route_registry import ROUTES_BY_DIRECTION

logger = structlog.get_logger()


@dataclass
class RouteCandidate:
    direction: str
    buy_venue: str
    sell_venue: str
    optimal_size_usd: Decimal
    expected_profit_usd: Decimal
    gas_usd: Decimal
    signal: dict[str, Any]             # passed through to execution methods


@dataclass
class SelectedRoute:
    candidate: RouteCandidate
    adjusted_size_usd: Decimal   # capped to available stablecoin
    net_profit_usd: Decimal      # monetary profit after gas
    expected_profit_usd: Decimal  # recomputed at adjusted size when needed
    inventory_improvement_usd: Decimal
    selection_score: Decimal


def select_route(
    candidates: list[RouteCandidate],
    inventory: InventoryTracker,
) -> Optional[SelectedRoute]:
    """Choose the highest inventory-adjusted score among profitable, funded routes."""
    scored: list[SelectedRoute] = []
    for c in candidates:
        route_def = ROUTES_BY_DIRECTION.get(c.direction)
        if (not route_def or c.buy_venue != route_def.buy_leg.venue
                or c.sell_venue != route_def.sell_leg.venue):
            continue
        if inventory.fresh_snapshot(c.buy_venue) is None or inventory.fresh_snapshot(c.sell_venue) is None:
            continue
        timestamp = c.signal.get("timestamp")
        if timestamp is None or not 0 <= int(time.time() * 1000) - timestamp <= inventory.params.inventory_max_age_seconds * 1000:
            continue
        mark_times = c.signal.get("price_timestamps", {})
        if any(venue not in mark_times or not 0 <= int(time.time() * 1000) - mark_times[venue]
               <= inventory.params.inventory_max_age_seconds * 1000
               for venue in (c.buy_venue, c.sell_venue)):
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

        sell_cngn_cap_trade: dict[str, Any] | None = None

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

        trade = c.signal.get("optimal_arb", {})
        expected_profit_usd = c.expected_profit_usd
        if route_def.pipeline == "cex_dex" and adjusted_size != c.optimal_size_usd:
            # Detection already priced the unconstrained optimum. Only rescore when
            # inventory/depth caps force us onto a smaller trade size.
            recomputed = estimate_cex_dex_trade(c.direction, cex_depth, adjusted_size)
            if not recomputed:
                continue
            trade = recomputed
            expected_profit_usd = Decimal(str(recomputed["expected_profit_usd"]))
        elif route_def.cngn_effect == "neutral":
            assert sell_cngn_cap_trade is not None
            cngn_cap_size = Decimal(str(sell_cngn_cap_trade["optimal_size_usd"]))
            if adjusted_size == cngn_cap_size:
                # cNGN cap was binding — profit already computed at this size.
                trade = sell_cngn_cap_trade
                expected_profit_usd = Decimal(str(sell_cngn_cap_trade["expected_profit_usd"]))
            else:
                # Stablecoin cap was tighter — rescore at the reduced size.
                recomputed = estimate_dex_dex_trade(c.direction, adjusted_size)
                if not recomputed:
                    continue
                trade = recomputed
                expected_profit_usd = Decimal(str(recomputed["expected_profit_usd"]))

        net_profit = expected_profit_usd - c.gas_usd
        if not net_profit.is_finite() or net_profit < inventory.params.min_profit_usd:
            continue
        can, _ = inventory.can_trade(adjusted_size, c.buy_venue)
        if not can:
            continue
        cngn_raw = trade.get("cngn_transferred")
        if cngn_raw is None:
            continue
        cngn = Decimal(str(cngn_raw))
        if not cngn.is_finite() or cngn <= 0 or cngn > cngn_bal:
            continue
        prices = {venue: Decimal(str(value)) for venue, value in c.signal.get("prices", {}).items()}
        projections = project_route(
            inventory, c.buy_venue, c.sell_venue, prices, adjusted_size,
            cngn, adjusted_size + expected_profit_usd,
        )
        if projections is None:
            continue
        improvement = sum((p.improvement_usd for p in projections), Decimal("0"))
        score = net_profit + inventory.params.inventory_weight * improvement
        selected = SelectedRoute(c, adjusted_size, net_profit, expected_profit_usd, improvement, score)
        scored.append(selected)
        logger.debug(
            "arb_inventory_candidate", direction=c.direction, size_usd=str(adjusted_size),
            monetary_profit_usd=str(net_profit), inventory_improvement_usd=str(improvement),
            selection_score=str(score),
            accounts=[{
                "venue": p.venue, "before_cngn_ratio": str(p.before_ratio),
                "after_cngn_ratio": str(p.after_ratio), "improvement_usd": str(p.improvement_usd),
            } for p in projections],
        )
    if not scored:
        return None
    return max(scored, key=lambda route: (route.selection_score, route.net_profit_usd))
