"""Joint simulation of several LP sleeves against one historical pool path."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping, Sequence

from research.backtester.data import Event, SwapEvent, V4Event
from research.backtester.pool_state import PoolState
from research.backtester.portfolio_allocation import Allocation
from research.backtester.portfolio_catalog import SleeveDefinition
from research.backtester.position_runtime import (
    SleeveAction,
    SleeveRuntime,
    SleeveSettlement,
    create_sleeve_runtime,
)
from research.backtester.simulator import (
    PoolConfig,
    PortfolioComposition,
    TransactionCostBreakdown,
    _event_fee_wallet,
    _event_pool_fee_rate,
    _portfolio_value,
    _swap_cost_breakdown,
)

AGGREGATE_LIQUIDITY_SHARE_CAP = 0.10


class LiquidityShareExceeded(ValueError):  # noqa: N818 - required public contract
    """Raised when joint synthetic liquidity exceeds the approved pool share."""


@dataclass
class SleeveAttribution:
    sleeve_id: str
    capital_budget_usd: float
    fees_usd: float = 0.0
    transaction_cost_usd: float = 0.0
    price_impact_cost_usd: float = 0.0
    final_value: float = 0.0
    value_samples: list[tuple[datetime, float]] = field(default_factory=list)
    liquidity_samples: list[tuple[datetime, float]] = field(default_factory=list)

    def liquidity_at(self, block_time: datetime) -> float:
        matches = [
            liquidity
            for timestamp, liquidity in self.liquidity_samples
            if timestamp == block_time
        ]
        if not matches:
            raise KeyError(f"no liquidity sample for {block_time.isoformat()}")
        return matches[-1]


@dataclass(frozen=True)
class NettedExecution:
    direction: Literal["stable_to_cngn", "cngn_to_stable"] | None
    external_notional_usd: float
    internal_notional_usd: float
    signed_notional_by_sleeve: Mapping[str, float]


@dataclass(frozen=True)
class PortfolioResult:
    bankroll_usd: float
    final_value: float
    cash_value: float
    total_fees: float
    total_transaction_cost: float
    total_price_impact_cost: float
    external_swap_notional_usd: float
    internal_netting_notional_usd: float
    max_aggregate_liquidity_share: float
    value_samples: list[tuple[datetime, float]]
    attribution: Mapping[str, SleeveAttribution]


def _net_actions(actions: Sequence[SleeveAction]) -> NettedExecution:
    signed = {
        action.sleeve_id: (
            action.inventory_swap_notional_usd
            if action.inventory_swap_direction == "stable_to_cngn"
            else -action.inventory_swap_notional_usd
            if action.inventory_swap_direction == "cngn_to_stable"
            else 0.0
        )
        for action in actions
    }
    buys = sum(max(value, 0.0) for value in signed.values())
    sells = sum(max(-value, 0.0) for value in signed.values())
    residual = buys - sells
    return NettedExecution(
        direction="stable_to_cngn" if residual > 0 else "cngn_to_stable" if residual < 0 else None,
        external_notional_usd=abs(residual),
        internal_notional_usd=min(buys, sells),
        signed_notional_by_sleeve=signed,
    )


def _active_positions(runtimes: Sequence[SleeveRuntime]) -> list[SleeveRuntime]:
    return [runtime for runtime in runtimes if runtime.position is not None]


def _aggregate_share(runtimes: Sequence[SleeveRuntime], historical_liquidity: int) -> float:
    synthetic = sum(runtime.position.liquidity_L for runtime in runtimes if runtime.position)
    denominator = historical_liquidity + synthetic
    share = synthetic / denominator if denominator > 0 else (1.0 if synthetic > 0 else 0.0)
    if share > AGGREGATE_LIQUIDITY_SHARE_CAP + 1e-12:
        raise LiquidityShareExceeded(
            f"aggregate synthetic liquidity share {share:.6f} exceeds 0.10"
        )
    return share


def _accrue_joint_fees(
    event: SwapEvent | V4Event,
    runtimes: Sequence[SleeveRuntime],
    historical_liquidity: int,
    pool_config: PoolConfig,
) -> None:
    active = _active_positions(runtimes)
    synthetic = sum(runtime.position.liquidity_L for runtime in active if runtime.position)
    denominator = historical_liquidity + synthetic
    _aggregate_share(runtimes, historical_liquidity)
    for runtime in active:
        position = runtime.position
        assert position is not None
        position.observed_swaps += 1
        if not position.is_in_range(runtime.current_tick):
            continue
        runtime.result.in_range_swaps += 1
        position.in_range_swaps += 1
        if denominator <= 0:
            continue
        liquidity_share = position.liquidity_L / denominator
        fee_wallet = _event_fee_wallet(
            event,
            _event_pool_fee_rate(event, pool_config),
            liquidity_share,
            runtime.current_price,
            pool_config,
        )
        position.accrued_fee_stable += fee_wallet.stable_usd
        position.accrued_fee_cngn += fee_wallet.cngn_amount
        runtime.result.total_fees += fee_wallet.value_usd(runtime.current_price)
        runtime.result.total_fee_stable += fee_wallet.stable_usd
        runtime.result.total_fee_cngn += fee_wallet.cngn_amount


def _allocated_cost(
    action: SleeveAction,
    aggregate: TransactionCostBreakdown,
    variable_fraction: float,
) -> TransactionCostBreakdown:
    return TransactionCostBreakdown(
        action=action.kind,
        gas_cost=action.transaction_cost.gas_cost,
        failed_tx_expected_cost=action.transaction_cost.failed_tx_expected_cost,
        swap_fee_cost=aggregate.swap_fee_cost * variable_fraction,
        price_impact_cost=aggregate.price_impact_cost * variable_fraction,
        slippage_cost=aggregate.slippage_cost * variable_fraction,
        latency_slippage_cost=aggregate.latency_slippage_cost * variable_fraction,
        swap_notional_usd=aggregate.swap_notional_usd * variable_fraction,
    )


def _refund_variable_cost(
    wallet: PortfolioComposition,
    action: SleeveAction,
    refund_usd: float,
) -> PortfolioComposition:
    if refund_usd <= 0:
        return wallet
    if action.variable_cost_asset == "stable":
        return PortfolioComposition(
            stable_usd=wallet.stable_usd + refund_usd,
            cngn_amount=wallet.cngn_amount,
        )
    if action.variable_cost_asset == "cngn":
        return PortfolioComposition(
            stable_usd=wallet.stable_usd,
            cngn_amount=wallet.cngn_amount + refund_usd / action.cngn_usd_price,
        )
    raise ValueError("variable cost refund requires an explicit payment asset")


def _settle_actions(
    actions: Sequence[SleeveAction],
    runtimes: Mapping[str, SleeveRuntime],
    historical_liquidity: int,
    pool_config: PoolConfig,
) -> tuple[float, float, list[tuple[str, TransactionCostBreakdown]]]:
    if not actions:
        return 0.0, 0.0, []
    execution = _net_actions(actions)
    residual_actions = [
        action
        for action in actions
        if execution.signed_notional_by_sleeve[action.sleeve_id] * (
            1 if execution.direction == "stable_to_cngn" else -1
        ) > 0
    ] if execution.direction is not None else []
    exemplar = residual_actions[0] if residual_actions else actions[0]
    runtime = runtimes[exemplar.sleeve_id]
    synthetic_liquidity = sum(
        item.position.liquidity_L for item in runtimes.values() if item.position is not None
    )
    aggregate = _swap_cost_breakdown(
        exemplar.kind,
        execution.external_notional_usd,
        int(historical_liquidity + synthetic_liquidity),
        _event_pool_fee_rate(exemplar.event, pool_config),
        runtime.current_tick,
        runtime.params,
        direction=execution.direction,
        current_price=runtime.current_price,
        current_sqrt_price_x96=runtime.current_sqrt_price_x96,
        pool_config=pool_config,
        exact_output=execution.direction == "stable_to_cngn",
    )
    residual_total = sum(
        abs(execution.signed_notional_by_sleeve[action.sleeve_id])
        for action in residual_actions
    )
    settlements: list[tuple[str, TransactionCostBreakdown]] = []
    for action in sorted(actions, key=lambda item: item.sleeve_id):
        fraction = (
            abs(execution.signed_notional_by_sleeve[action.sleeve_id]) / residual_total
            if action in residual_actions and residual_total > 0
            else 0.0
        )
        cost = _allocated_cost(action, aggregate, fraction)
        original_variable_cost = (
            action.transaction_cost.swap_fee_cost
            + action.transaction_cost.price_impact_cost
            + action.transaction_cost.slippage_cost
            + action.transaction_cost.latency_slippage_cost
        )
        allocated_variable_cost = (
            cost.swap_fee_cost
            + cost.price_impact_cost
            + cost.slippage_cost
            + cost.latency_slippage_cost
        )
        wallet_after = _refund_variable_cost(
            action.wallet_after,
            action,
            original_variable_cost - allocated_variable_cost,
        )
        runtimes[action.sleeve_id].apply_action(
            action, SleeveSettlement(wallet_after=wallet_after, transaction_cost=cost)
        )
        settlements.append((action.sleeve_id, cost))
    return execution.external_notional_usd, execution.internal_notional_usd, settlements


def simulate_portfolio(
    *,
    events: Sequence[Event],
    sleeves: Sequence[SleeveDefinition],
    allocation: Allocation,
    pool_config: PoolConfig,
    bankroll_usd: float,
    initial_pool_state: PoolState | None = None,
) -> PortfolioResult:
    if bankroll_usd <= 0:
        raise ValueError("bankroll_usd must be positive")
    by_id = {sleeve.sleeve_id: sleeve for sleeve in sleeves}
    if len(by_id) != len(sleeves):
        raise ValueError("duplicate sleeve_id")
    unknown = set(allocation.weights) - set(by_id)
    if unknown:
        raise ValueError(f"allocation references unknown sleeves: {sorted(unknown)}")

    runtimes = {
        sleeve_id: create_sleeve_runtime(
            sleeve_id=sleeve_id,
            params=by_id[sleeve_id].params,
            pool_config=pool_config,
            capital_usd=bankroll_usd * weight,
        )
        for sleeve_id, weight in allocation.weights.items()
        if weight > 0
    }
    for runtime in runtimes.values():
        if initial_pool_state is not None:
            runtime.pool_state = initial_pool_state.copy()
    cash_value = bankroll_usd * (
        1.0 - sum(allocation.weights.values())
    )
    attribution = {
        sleeve_id: SleeveAttribution(sleeve_id, runtime.capital_usd)
        for sleeve_id, runtime in runtimes.items()
    }
    portfolio_samples: list[tuple[datetime, float]] = []
    external_notional = 0.0
    internal_notional = 0.0
    max_share = 0.0

    for event in events:
        if not isinstance(event, (SwapEvent, V4Event)) or (
            isinstance(event, V4Event) and event.event_type != "swap"
        ):
            for runtime in runtimes.values():
                runtime._apply_liquidity_event(event)
            continue
        active_liquidity: int | None = None
        valid = True
        for runtime in runtimes.values():
            observed_liquidity, price_is_valid = runtime._observe_swap(event)
            active_liquidity = observed_liquidity if active_liquidity is None else active_liquidity
            valid = valid and price_is_valid
            runtime._roll_daily_return(event)
        if not valid or active_liquidity is None:
            continue

        _accrue_joint_fees(event, list(runtimes.values()), active_liquidity, pool_config)
        exit_actions = [
            action for runtime in runtimes.values()
            if (action := runtime.propose_exit(event, active_liquidity)) is not None
        ]
        ext, internal, costs = _settle_actions(
            exit_actions, runtimes, active_liquidity, pool_config
        )
        external_notional += ext
        internal_notional += internal
        for sleeve_id, cost in costs:
            attribution[sleeve_id].transaction_cost_usd += cost.total
            attribution[sleeve_id].price_impact_cost_usd += cost.price_impact_cost

        entry_actions = [
            action for runtime in runtimes.values()
            if (action := runtime.propose_entry(event, active_liquidity)) is not None
        ]
        ext, internal, costs = _settle_actions(
            entry_actions, runtimes, active_liquidity, pool_config
        )
        external_notional += ext
        internal_notional += internal
        for sleeve_id, cost in costs:
            attribution[sleeve_id].transaction_cost_usd += cost.total
            attribution[sleeve_id].price_impact_cost_usd += cost.price_impact_cost

        share = _aggregate_share(list(runtimes.values()), active_liquidity)
        max_share = max(max_share, share)
        sleeve_total = 0.0
        for sleeve_id, runtime in runtimes.items():
            value = _portfolio_value(
                runtime.position,
                runtime.wallet,
                runtime.current_price,
                runtime.current_tick,
                runtime.current_sqrt_price_x96,
                pool_config,
            )
            attribution[sleeve_id].fees_usd = runtime.result.total_fees
            attribution[sleeve_id].value_samples.append((event.block_time, value))
            attribution[sleeve_id].liquidity_samples.append(
                (event.block_time, runtime.position.liquidity_L if runtime.position else 0.0)
            )
            sleeve_total += value
            runtime.previous_swap_time = event.block_time
        portfolio_samples.append((event.block_time, cash_value + sleeve_total))

    for sleeve_id, runtime in runtimes.items():
        attribution[sleeve_id].final_value = (
            attribution[sleeve_id].value_samples[-1][1]
            if attribution[sleeve_id].value_samples else runtime.capital_usd
        )
    final_value = cash_value + sum(item.final_value for item in attribution.values())
    return PortfolioResult(
        bankroll_usd=bankroll_usd,
        final_value=final_value,
        cash_value=cash_value,
        total_fees=sum(item.fees_usd for item in attribution.values()),
        total_transaction_cost=sum(item.transaction_cost_usd for item in attribution.values()),
        total_price_impact_cost=sum(item.price_impact_cost_usd for item in attribution.values()),
        external_swap_notional_usd=external_notional,
        internal_netting_notional_usd=internal_notional,
        max_aggregate_liquidity_share=max_share,
        value_samples=portfolio_samples,
        attribution=attribution,
    )
