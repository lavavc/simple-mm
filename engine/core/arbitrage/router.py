"""
Route selection for arbitrage execution.

select_route() picks the single best feasible trade from a list of candidates,
scoring by net profit (after gas and rebalance cost) with inventory alignment as tiebreak.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from engine.core.arbitrage.cex_dex import estimate_cex_dex_trade, estimate_max_cex_buy_usd_for_cngn
from engine.core.arbitrage.dex_dex import estimate_dex_dex_trade, estimate_max_dex_buy_usd_for_cngn

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
    expected_profit_usd: Decimal  # recomputed at adjusted size when needed
    cap_reason: Optional[str] = None
    buy_wallet_stable_balance: Optional[Decimal] = None
    buy_wallet_cngn_balance: Optional[Decimal] = None
    sell_wallet_stable_balance: Optional[Decimal] = None
    sell_wallet_cngn_balance: Optional[Decimal] = None


@dataclass
class RouteEvaluation:
    route: Optional[SelectedRoute]
    snapshot: SelectedRoute
    rejection_reason: Optional[str] = None


def _fmt_usd(value: Decimal) -> str:
    return f"${value:,.2f}"


def evaluate_route_candidate(
    c: RouteCandidate,
    inventory,
) -> RouteEvaluation:
    buy_stable = inventory.state.per_account_stable.get(c.buy_venue)
    buy_cngn = inventory.state.per_account_cngn.get(c.buy_venue)
    sell_stable = inventory.state.per_account_stable.get(c.sell_venue)
    sell_cngn = inventory.state.per_account_cngn.get(c.sell_venue)

    adjusted_size = Decimal("0")
    cap_reason: Optional[str] = None
    expected_profit_usd = c.expected_profit_usd
    net_profit = Decimal("0")

    def _snapshot() -> SelectedRoute:
        return SelectedRoute(
            c,
            adjusted_size,
            net_profit,
            expected_profit_usd,
            cap_reason=cap_reason,
            buy_wallet_stable_balance=buy_stable,
            buy_wallet_cngn_balance=buy_cngn,
            sell_wallet_stable_balance=sell_stable,
            sell_wallet_cngn_balance=sell_cngn,
        )

    if not buy_stable:
        return RouteEvaluation(None, _snapshot(), "Buy wallet stable balance unavailable")

    max_trade_cap = inventory.params.max_single_trade_usd
    size_caps: list[tuple[str, Decimal]] = [
        ("buy_wallet_stable", buy_stable),
        ("max_single_trade", max_trade_cap),
    ]
    adjusted_size = min(c.optimal_size_usd, buy_stable, max_trade_cap)

    if not sell_cngn:
        return RouteEvaluation(None, _snapshot(), "Sell wallet cNGN balance unavailable")

    if c.pipeline == "cex_dex" and c.direction in _BUYS_CNGN_FROM_CEX:
        sell_cngn_cap = estimate_max_cex_buy_usd_for_cngn(c.signal.get("depth"), sell_cngn)
        size_caps.append(("sell_wallet_cngn", sell_cngn_cap))
        adjusted_size = min(adjusted_size, sell_cngn_cap)
    elif c.pipeline == "dex_dex":
        sell_cngn_cap = estimate_max_dex_buy_usd_for_cngn(c.direction, sell_cngn)
        size_caps.append(("sell_wallet_cngn", sell_cngn_cap))
        adjusted_size = min(adjusted_size, sell_cngn_cap)
    else:
        cngn_price = inventory.state.cngn_price_usd
        if cngn_price > 0:
            sell_cngn_cap = sell_cngn * cngn_price
            size_caps.append(("sell_wallet_cngn", sell_cngn_cap))
            adjusted_size = min(adjusted_size, sell_cngn_cap)

    cap_reasons = [
        name for name, cap_value in size_caps
        if cap_value == adjusted_size and cap_value < c.optimal_size_usd
    ]
    cap_reason = ", ".join(cap_reasons) if cap_reasons else None

    if adjusted_size <= 0:
        return RouteEvaluation(None, _snapshot(), "Executable size is zero after wallet and trade caps")

    if c.pipeline == "cex_dex" and adjusted_size != c.optimal_size_usd:
        recomputed = estimate_cex_dex_trade(c.direction, c.signal.get("depth"), adjusted_size)
        if not recomputed:
            return RouteEvaluation(None, _snapshot(), "Could not recompute expected profit at executable size")
        expected_profit_usd = Decimal(str(recomputed["expected_profit_usd"]))
    elif c.pipeline == "dex_dex":
        recomputed = estimate_dex_dex_trade(c.direction, adjusted_size)
        if not recomputed:
            return RouteEvaluation(None, _snapshot(), "Could not recompute expected profit at executable size")
        expected_profit_usd = Decimal(str(recomputed["expected_profit_usd"]))

    rebalance_bps = inventory.get_rebalance_cost_bps(c.buy_venue)
    rebalance_cost = adjusted_size * Decimal(rebalance_bps) / Decimal(10000)
    net_profit = expected_profit_usd - c.gas_usd - rebalance_cost

    snapshot = _snapshot()

    if net_profit < inventory.params.min_profit_usd:
        return RouteEvaluation(
            None,
            snapshot,
            f"Net profit after routing {_fmt_usd(net_profit)} is below threshold {_fmt_usd(inventory.params.min_profit_usd)}",
        )

    can_trade, why = inventory.can_trade(adjusted_size, c.buy_venue, c.sell_venue)
    if not can_trade:
        return RouteEvaluation(None, snapshot, why or "Risk gate blocked trade")

    return RouteEvaluation(snapshot, snapshot, None)


def select_route(
    candidates: list[RouteCandidate],
    inventory,
) -> Optional[SelectedRoute]:
    """
    Pick the best feasible route from candidates.

    Filters: adjusted_size > 0, net_profit >= min_profit_usd, inventory.can_trade() passes.
    Score: net_profit = expected_profit - gas - rebalance_cost_penalty.
    Tiebreak: prefer routes that reduce current inventory imbalance.
    """
    scored: list[tuple[Decimal, bool, SelectedRoute]] = []
    imbalance = inventory.state.cngn_imbalance_usd

    for c in candidates:
        evaluation = evaluate_route_candidate(c, inventory)
        route = evaluation.route
        if route is None:
            continue

        # Inventory alignment tiebreak
        if imbalance > _IMBALANCE_THRESHOLD_USD:
            aligned = c.direction in _SELLS_CNGN_TO_CEX
        elif imbalance < -_IMBALANCE_THRESHOLD_USD:
            aligned = c.direction in _BUYS_CNGN_FROM_CEX
        else:
            aligned = True

        scored.append((
            route.net_profit_usd,
            aligned,
            route,
        ))

    if not scored:
        return None

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]
