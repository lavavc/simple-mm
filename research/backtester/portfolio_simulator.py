"""Joint simulation of several LP sleeves against one historical pool path."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping, Sequence

from research.backtester.data import Event, SwapEvent, V4Event
from research.backtester.entry_eligibility import EntryEligibilityOverlay
from research.backtester.pool_state import PoolState
from research.backtester.portfolio_allocation import Allocation
from research.backtester.portfolio_catalog import SleeveDefinition
from research.backtester.portfolio_errors import (
    ExecutionAccountingError,
    JointActionAffordabilityError,
    NoValidationSwapError,
)
from research.backtester.position_runtime import (
    ENTRY_SCALE_STEPS,
    EntryIntent,
    SleeveAction,
    SleeveRuntime,
    SleeveSettlement,
    TerminalLiquidationError,
    TerminalRemovalIntent,
    TerminalSleeveSettlement,
    create_sleeve_runtime,
)
from research.backtester.simulator import (
    PoolConfig,
    PortfolioComposition,
    TransactionCostBreakdown,
    _event_cngn_price,
    _event_fee_wallet,
    _event_pool_fee_rate,
    _portfolio_value,
    _swap_cost_breakdown,
)

AGGREGATE_LIQUIDITY_SHARE_CAP = 0.10


class LiquidityShareExceeded(ValueError):  # noqa: N818 - required public contract
    """Raised when joint synthetic liquidity exceeds the approved pool share."""

    def __init__(self, observed_share: float, cap: float) -> None:
        self.observed_share = observed_share
        self.cap = cap
        super().__init__(
            f"aggregate synthetic liquidity share {observed_share:.6f} exceeds {cap:.2f}"
        )


@dataclass
class SleeveAttribution:
    sleeve_id: str
    capital_budget_usd: float
    fees_usd: float = 0.0
    transaction_cost_usd: float = 0.0
    price_impact_cost_usd: float = 0.0
    terminal_liquidation_cost_usd: float = 0.0
    external_marked_notional_usd: float = 0.0
    external_input_value_usd: float = 0.0
    external_output_value_usd: float = 0.0
    signed_internal_cngn_value_usd: float = 0.0
    allocated_variable_cost_usd: float = 0.0
    terminal_funding_transfer_usd: float = 0.0
    final_value: float = 0.0
    value_samples: list[tuple[datetime, float]] = field(default_factory=list)
    liquidity_samples: list[tuple[datetime, float]] = field(default_factory=list)

    def liquidity_at(self, block_time: datetime) -> float:
        matches = [
            liquidity for timestamp, liquidity in self.liquidity_samples if timestamp == block_time
        ]
        if not matches:
            raise KeyError(f"no liquidity sample for {block_time.isoformat()}")
        return matches[-1]


@dataclass(frozen=True)
class NettedExecution:
    direction: Literal["stable_to_cngn", "cngn_to_stable"] | None
    external_notional_usd: float
    internal_notional_usd: float
    signed_notional_by_leg: Mapping[tuple[str, str], float]
    external_notional_by_leg: Mapping[tuple[str, str], float]


@dataclass(frozen=True)
class ActionExecutionAttribution:
    sleeve_id: str
    action_kind: str
    external_marked_notional_usd: float
    external_input_value_usd: float
    external_output_value_usd: float
    signed_internal_cngn_value_usd: float
    allocated_variable_cost_usd: float


@dataclass(frozen=True)
class ActionBatchSettlement:
    external_marked_notional_usd: float
    internal_cross_notional_usd: float
    external_input_value_usd: float
    external_output_value_usd: float
    allocated_variable_cost_usd: float
    costs: tuple[tuple[str, TransactionCostBreakdown], ...]
    action_attribution: tuple[ActionExecutionAttribution, ...]


@dataclass(frozen=True)
class EntryActionPlan:
    actions: tuple[SleeveAction, ...]
    scale: float
    intended_entry_count: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.scale) or not 0.0 <= self.scale <= 1.0:
            raise ValueError("entry action plan scale must be finite and in [0, 1]")
        if not (self.scale * ENTRY_SCALE_STEPS).is_integer():
            raise ValueError("entry action plan scale must lie on the 1/256 lattice")
        if self.intended_entry_count < 0:
            raise ValueError("intended entry count must be non-negative")


@dataclass(frozen=True)
class EntryScaleEvent:
    block_time: datetime
    block_number: int | None
    tx_hash: str | None
    log_index: int | None
    scale: float
    intended_entry_count: int
    executed_entry_count: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.scale) or not 0.0 <= self.scale <= 1.0:
            raise ValueError("entry scale event requires a scale in [0, 1]")
        if not (self.scale * ENTRY_SCALE_STEPS).is_integer():
            raise ValueError("entry scale event must lie on the 1/256 lattice")
        if self.intended_entry_count <= 0:
            raise ValueError("entry scale event requires at least one intent")
        if not 0 <= self.executed_entry_count <= self.intended_entry_count:
            raise ValueError("entry scale event has an invalid executed count")
        if self.scale > 0 and self.executed_entry_count != self.intended_entry_count:
            raise ValueError("positive entry scale must execute every frozen intent")
        if self.scale == 0 and self.executed_entry_count != 0:
            raise ValueError("zero entry scale cannot execute an entry")


@dataclass(frozen=True)
class TerminalPortfolioSettlement:
    removal_intents: tuple[TerminalRemovalIntent, ...]
    sleeves: tuple[TerminalSleeveSettlement, ...]
    cash_account_before_usd: float
    cash_account_after_usd: float
    cash_funding_transfer_usd: float
    portfolio_final_value_usd: float
    total_cost: TransactionCostBreakdown
    inventory_swap_cost: TransactionCostBreakdown
    external_marked_notional_usd: float
    external_input_value_usd: float
    external_output_value_usd: float
    allocated_variable_cost_usd: float
    terminal_position_settlement_count: int
    terminal_loose_cngn_settlement_count: int
    terminal_zero_settlement_count: int
    terminal_inventory_swap_count: int


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
    external_input_value_usd: float
    external_output_value_usd: float
    total_variable_execution_cost_usd: float
    max_aggregate_liquidity_share: float
    entry_scale_events: tuple[EntryScaleEvent, ...]
    settled_to_cash: bool
    terminal_liquidation_cost: float
    terminal_position_settlement_count: int
    terminal_loose_cngn_settlement_count: int
    terminal_zero_settlement_count: int
    terminal_inventory_swap_count: int
    terminal_fixed_cost_usd: float
    terminal_variable_cost_usd: float
    terminal_external_marked_notional_usd: float
    terminal_cash_funding_transfer_usd: float
    terminal_open_position_count: int
    terminal_cngn_amount: float
    value_samples: list[tuple[datetime, float]]
    attribution: Mapping[str, SleeveAttribution]

    @property
    def entry_action_batch_count(self) -> int:
        return len(self.entry_scale_events)

    @property
    def scaled_entry_action_batch_count(self) -> int:
        return sum(event.scale < 1.0 for event in self.entry_scale_events)

    @property
    def minimum_entry_execution_scale(self) -> float:
        return min(
            (event.scale for event in self.entry_scale_events),
            default=1.0,
        )


def _net_actions(actions: Sequence[SleeveAction]) -> NettedExecution:
    signed: dict[tuple[str, str], float] = {}
    for action in actions:
        key = (action.sleeve_id, action.kind)
        if key in signed:
            raise ValueError(f"duplicate inventory leg {key!r}")
        notional = action.inventory_swap_notional_usd
        if not math.isfinite(notional) or notional < 0:
            raise ExecutionAccountingError(
                f"inventory notional is invalid for {action.sleeve_id!r}"
            )
        if (action.inventory_swap_direction is None) != (notional == 0):
            raise ExecutionAccountingError(
                f"inventory direction and notional disagree for {action.sleeve_id!r}"
            )
        signed[key] = (
            notional
            if action.inventory_swap_direction == "stable_to_cngn"
            else -action.inventory_swap_notional_usd
            if action.inventory_swap_direction == "cngn_to_stable"
            else 0.0
        )
    buys = sum(max(value, 0.0) for value in signed.values())
    sells = sum(max(-value, 0.0) for value in signed.values())
    residual = buys - sells
    direction: Literal["stable_to_cngn", "cngn_to_stable"] | None = (
        "stable_to_cngn" if residual > 0 else "cngn_to_stable" if residual < 0 else None
    )
    external: dict[tuple[str, str], float] = {key: 0.0 for key in signed}
    if direction == "stable_to_cngn":
        scale = residual / buys
        for leg_key, value in signed.items():
            if value > 0:
                external[leg_key] = value * scale
    elif direction == "cngn_to_stable":
        scale = -residual / sells
        for leg_key, value in signed.items():
            if value < 0:
                external[leg_key] = -value * scale
    return NettedExecution(
        direction=direction,
        external_notional_usd=abs(residual),
        internal_notional_usd=min(buys, sells),
        signed_notional_by_leg=signed,
        external_notional_by_leg=external,
    )


def _active_positions(runtimes: Sequence[SleeveRuntime]) -> list[SleeveRuntime]:
    return [runtime for runtime in runtimes if runtime.position is not None]


def _validated_aggregate_share(
    synthetic_liquidity: float,
    historical_liquidity: int,
) -> float:
    if (
        not math.isfinite(synthetic_liquidity)
        or synthetic_liquidity < 0
        or not math.isfinite(historical_liquidity)
        or historical_liquidity < 0
    ):
        raise ExecutionAccountingError("aggregate liquidity depth is invalid")
    synthetic = synthetic_liquidity
    denominator = historical_liquidity + synthetic
    if not math.isfinite(denominator):
        raise ExecutionAccountingError("aggregate liquidity depth is invalid")
    share = synthetic / denominator if denominator > 0 else (1.0 if synthetic > 0 else 0.0)
    if share > AGGREGATE_LIQUIDITY_SHARE_CAP + 1e-12:
        raise LiquidityShareExceeded(share, AGGREGATE_LIQUIDITY_SHARE_CAP)
    return share


def _aggregate_share(runtimes: Sequence[SleeveRuntime], historical_liquidity: int) -> float:
    synthetic = sum(runtime.position.liquidity_L for runtime in runtimes if runtime.position)
    return _validated_aggregate_share(synthetic, historical_liquidity)


def _projected_aggregate_share(
    runtimes: Mapping[str, SleeveRuntime],
    actions: Sequence[SleeveAction],
    historical_liquidity: int,
) -> float:
    projected = {action.sleeve_id: action.position_after for action in actions}
    synthetic = sum(
        position.liquidity_L
        for sleeve_id, runtime in runtimes.items()
        if (position := projected.get(sleeve_id, runtime.position)) is not None
    )
    return _validated_aggregate_share(synthetic, historical_liquidity)


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


def _variable_cost(cost: TransactionCostBreakdown) -> float:
    return (
        cost.swap_fee_cost
        + cost.price_impact_cost
        + cost.slippage_cost
        + cost.latency_slippage_cost
    )


def _adjust_variable_cost(
    wallet: PortfolioComposition,
    action: SleeveAction,
    adjustment_usd: float,
) -> PortfolioComposition:
    if adjustment_usd == 0:
        return wallet
    if action.variable_cost_asset == "stable":
        stable_usd = wallet.stable_usd + adjustment_usd
        cngn_amount = wallet.cngn_amount
    elif action.variable_cost_asset == "cngn":
        if action.cngn_usd_price <= 0:
            raise ValueError("cNGN variable-cost adjustment requires a positive price")
        stable_usd = wallet.stable_usd
        cngn_amount = wallet.cngn_amount + adjustment_usd / action.cngn_usd_price
    else:
        raise ValueError("variable cost adjustment requires an explicit payment asset")
    tolerance = 1e-12
    if stable_usd < -tolerance or cngn_amount < -tolerance:
        raise JointActionAffordabilityError(
            "joint variable-cost allocation exceeds the action wallet"
        )
    return PortfolioComposition(
        stable_usd=max(stable_usd, 0.0),
        cngn_amount=max(cngn_amount, 0.0),
    )


def _variable_cost_model(runtime: SleeveRuntime) -> tuple[float, float, float]:
    model = runtime.params.transaction_costs
    return (
        model.swap_slippage_bps,
        model.latency_slippage_bps,
        model.fallback_price_impact_bps,
    )


def _aggregate_variable_cost(
    *,
    actions: Sequence[SleeveAction],
    execution: NettedExecution,
    runtimes: Mapping[str, SleeveRuntime],
    historical_liquidity: int,
    pool_config: PoolConfig,
) -> TransactionCostBreakdown:
    if execution.direction is None or execution.external_notional_usd == 0:
        return TransactionCostBreakdown("joint_inventory")
    residual_actions = [
        action
        for action in actions
        if execution.external_notional_by_leg[(action.sleeve_id, action.kind)] > 0
    ]
    models = {_variable_cost_model(runtimes[action.sleeve_id]) for action in residual_actions}
    if len(models) != 1:
        raise ExecutionAccountingError(
            "joint inventory legs have incompatible variable-cost models"
        )
    ordered = sorted(residual_actions, key=lambda item: (item.sleeve_id, item.kind))
    exemplar = ordered[0]
    runtime = runtimes[exemplar.sleeve_id]
    state = {
        (
            item.current_tick,
            item.current_sqrt_price_x96,
            item.current_price,
        )
        for item in (runtimes[action.sleeve_id] for action in residual_actions)
    }
    if len(state) != 1:
        raise ExecutionAccountingError("joint inventory legs require one observed pool state")
    synthetic_liquidity = sum(
        item.position.liquidity_L for item in runtimes.values() if item.position is not None
    )
    active_liquidity = historical_liquidity + synthetic_liquidity
    base_notional = execution.external_notional_usd
    exact_output_notional = sum(
        execution.external_notional_by_leg[(action.sleeve_id, action.kind)]
        for action in residual_actions
        if action.kind == "enter"
    )
    alpha = exact_output_notional / base_notional

    def compute(notional_usd: float, *, exact_output: bool) -> TransactionCostBreakdown:
        return _swap_cost_breakdown(
            "joint_inventory",
            notional_usd,
            active_liquidity,
            _event_pool_fee_rate(exemplar.event, pool_config),
            runtime.current_tick,
            runtime.params,
            direction=execution.direction,
            current_price=runtime.current_price,
            current_sqrt_price_x96=runtime.current_sqrt_price_x96,
            pool_config=pool_config,
            exact_output=exact_output,
        )

    if alpha == 0:
        priced = compute(base_notional, exact_output=False)
    elif alpha == 1:
        priced = compute(base_notional, exact_output=True)
    else:
        variable_cost = 0.0
        priced = TransactionCostBreakdown("joint_inventory")
        for _ in range(100):
            priced = compute(
                base_notional + alpha * variable_cost,
                exact_output=False,
            )
            next_cost = _variable_cost(priced)
            if not math.isfinite(next_cost) or next_cost < 0:
                raise ExecutionAccountingError("joint inventory fixed-point cost is invalid")
            tolerance = 1e-12 * max(1.0, base_notional, next_cost)
            if abs(next_cost - variable_cost) <= tolerance:
                variable_cost = next_cost
                break
            variable_cost = next_cost
        else:
            raise ExecutionAccountingError("joint inventory fixed-point cost did not converge")
        if _variable_cost(priced) != variable_cost:
            priced = compute(
                base_notional + alpha * variable_cost,
                exact_output=False,
            )

    return TransactionCostBreakdown(
        action="joint_inventory",
        swap_fee_cost=priced.swap_fee_cost,
        price_impact_cost=priced.price_impact_cost,
        slippage_cost=priced.slippage_cost,
        latency_slippage_cost=priced.latency_slippage_cost,
        swap_notional_usd=base_notional,
    )


def _prepare_action_settlements(
    actions: Sequence[SleeveAction],
    runtimes: Mapping[str, SleeveRuntime],
    historical_liquidity: int,
    pool_config: PoolConfig,
) -> tuple[
    NettedExecution,
    tuple[tuple[SleeveAction, SleeveSettlement, TransactionCostBreakdown], ...],
]:
    if not actions:
        return _net_actions(()), ()
    sleeve_ids = [action.sleeve_id for action in actions]
    if len(set(sleeve_ids)) != len(sleeve_ids):
        raise ValueError("a sleeve may submit at most one action per event")
    unknown = set(sleeve_ids) - set(runtimes)
    if unknown:
        raise ValueError(f"actions reference unknown runtimes: {sorted(unknown)}")
    event = actions[0].event
    if any(action.event != event for action in actions[1:]):
        raise ValueError("joint settlement requires one ordered event")
    for action in actions:
        runtime = runtimes[action.sleeve_id]
        if runtime.wallet != action.wallet_before:
            raise ValueError("runtime wallet does not match action wallet snapshot")
        if runtime.position != action.position_before:
            raise ValueError("runtime position does not match action position snapshot")
    observed_states = {
        (
            runtimes[action.sleeve_id].current_tick,
            runtimes[action.sleeve_id].current_sqrt_price_x96,
            runtimes[action.sleeve_id].current_price,
        )
        for action in actions
    }
    if len(observed_states) != 1:
        raise ExecutionAccountingError("joint inventory legs require one observed pool state")
    execution = _net_actions(actions)
    aggregate = _aggregate_variable_cost(
        actions=actions,
        execution=execution,
        runtimes=runtimes,
        historical_liquidity=historical_liquidity,
        pool_config=pool_config,
    )
    prepared: list[tuple[SleeveAction, SleeveSettlement, TransactionCostBreakdown]] = []
    for action in sorted(actions, key=lambda item: item.sleeve_id):
        runtime = runtimes[action.sleeve_id]
        external_notional = execution.external_notional_by_leg[(action.sleeve_id, action.kind)]
        fraction = (
            external_notional / execution.external_notional_usd
            if execution.external_notional_usd > 0
            else 0.0
        )
        cost = _allocated_cost(action, aggregate, fraction)
        wallet_after = _adjust_variable_cost(
            action.wallet_after,
            action,
            _variable_cost(action.transaction_cost) - _variable_cost(cost),
        )
        settlement = SleeveSettlement(
            wallet_after=wallet_after,
            transaction_cost=cost,
        )
        before_value = _portfolio_value(
            action.position_before,
            action.wallet_before,
            runtime.current_price,
            runtime.current_tick,
            runtime.current_sqrt_price_x96,
            pool_config,
        )
        after_value = _portfolio_value(
            action.position_after,
            settlement.wallet_after,
            runtime.current_price,
            runtime.current_tick,
            runtime.current_sqrt_price_x96,
            pool_config,
        )
        expected_after = before_value - cost.total
        tolerance = 1e-8 * max(
            1.0,
            abs(before_value),
            abs(after_value),
            cost.total,
        )
        if (
            not math.isfinite(before_value)
            or not math.isfinite(after_value)
            or expected_after < -tolerance
            or abs(after_value - expected_after) > tolerance
        ):
            raise ExecutionAccountingError(
                f"action value reconciliation failed for {action.sleeve_id!r}"
            )
        prepared.append((action, settlement, cost))
    return execution, tuple(prepared)


def _settle_actions(
    actions: Sequence[SleeveAction],
    runtimes: Mapping[str, SleeveRuntime],
    historical_liquidity: int,
    pool_config: PoolConfig,
) -> ActionBatchSettlement:
    execution, prepared = _prepare_action_settlements(
        actions,
        runtimes,
        historical_liquidity,
        pool_config,
    )
    action_attribution: list[ActionExecutionAttribution] = []
    for action, _, cost in prepared:
        key = (action.sleeve_id, action.kind)
        external = execution.external_notional_by_leg[key]
        variable_cost = _variable_cost(cost)
        exact_output = action.kind == "enter"
        signed = execution.signed_notional_by_leg[key]
        signed_external = math.copysign(external, signed) if signed != 0 else 0.0
        action_attribution.append(
            ActionExecutionAttribution(
                sleeve_id=action.sleeve_id,
                action_kind=action.kind,
                external_marked_notional_usd=external,
                external_input_value_usd=(external + variable_cost if exact_output else external),
                external_output_value_usd=(external if exact_output else external - variable_cost),
                signed_internal_cngn_value_usd=signed - signed_external,
                allocated_variable_cost_usd=variable_cost,
            )
        )
    external_input = sum(item.external_input_value_usd for item in action_attribution)
    external_output = sum(item.external_output_value_usd for item in action_attribution)
    variable_cost = sum(item.allocated_variable_cost_usd for item in action_attribution)
    tolerance = 1e-10 * max(1.0, external_input, external_output, variable_cost)
    if abs((external_input - external_output) - variable_cost) > tolerance:
        raise ExecutionAccountingError("external execution value reconciliation failed")
    for action, settlement, _ in prepared:
        runtimes[action.sleeve_id].apply_action(action, settlement)
    return ActionBatchSettlement(
        external_marked_notional_usd=execution.external_notional_usd,
        internal_cross_notional_usd=execution.internal_notional_usd,
        external_input_value_usd=external_input,
        external_output_value_usd=external_output,
        allocated_variable_cost_usd=variable_cost,
        costs=tuple((action.sleeve_id, cost) for action, _, cost in prepared),
        action_attribution=tuple(action_attribution),
    )


def _propose_actions(
    runtimes: Mapping[str, SleeveRuntime],
    event: SwapEvent | V4Event,
    active_liquidity: int,
) -> tuple[SleeveAction, ...]:
    """Freeze at most one action per sleeve before any action is applied."""
    actions: list[SleeveAction] = []
    for sleeve_id in sorted(runtimes):
        runtime = runtimes[sleeve_id]
        action = runtime.propose_exit(event, active_liquidity)
        if action is None:
            action = runtime.propose_entry(event, active_liquidity)
        if action is not None:
            actions.append(action)
    return tuple(actions)


def _plan_affordable_actions(
    runtimes: Mapping[str, SleeveRuntime],
    event: SwapEvent | V4Event,
    active_liquidity: int,
    pool_config: PoolConfig,
) -> EntryActionPlan:
    """Select the largest feasible point on the declared common-scale lattice."""
    exits: dict[str, SleeveAction] = {}
    entries: dict[str, EntryIntent] = {}
    for sleeve_id in sorted(runtimes):
        action = runtimes[sleeve_id].propose_exit(event, active_liquidity)
        if action is None:
            intent = runtimes[sleeve_id].propose_entry_intent(event, active_liquidity)
            if intent is not None:
                entries[sleeve_id] = intent
        else:
            exits[sleeve_id] = action

    def materialize(scale: float) -> tuple[SleeveAction, ...]:
        actions = list(exits.values())
        if scale == 0:
            return tuple(sorted(actions, key=lambda item: item.sleeve_id))
        actions.extend(
            runtimes[sleeve_id].materialize_entry(entries[sleeve_id], scale)
            for sleeve_id in sorted(entries)
        )
        return tuple(sorted(actions, key=lambda item: item.sleeve_id))

    for numerator in range(ENTRY_SCALE_STEPS, 0, -1):
        scale = numerator / ENTRY_SCALE_STEPS
        candidate = materialize(scale)
        try:
            _prepare_action_settlements(
                candidate,
                runtimes,
                active_liquidity,
                pool_config,
            )
        except JointActionAffordabilityError:
            continue
        return EntryActionPlan(candidate, scale, len(entries))

    exits_only = materialize(0.0)
    _prepare_action_settlements(
        exits_only,
        runtimes,
        active_liquidity,
        pool_config,
    )
    return EntryActionPlan(exits_only, 0.0, len(entries))


def _sum_costs(
    action: str,
    costs: Sequence[TransactionCostBreakdown],
) -> TransactionCostBreakdown:
    return TransactionCostBreakdown(
        action=action,
        gas_cost=sum(cost.gas_cost for cost in costs),
        swap_fee_cost=sum(cost.swap_fee_cost for cost in costs),
        price_impact_cost=sum(cost.price_impact_cost for cost in costs),
        slippage_cost=sum(cost.slippage_cost for cost in costs),
        latency_slippage_cost=sum(cost.latency_slippage_cost for cost in costs),
        failed_tx_expected_cost=sum(cost.failed_tx_expected_cost for cost in costs),
        swap_notional_usd=sum(cost.swap_notional_usd for cost in costs),
    )


def _allocate_terminal_inventory_cost(
    inventory_cost: TransactionCostBreakdown,
    contribution_by_id: Mapping[str, float],
) -> Mapping[str, TransactionCostBreakdown]:
    contributors = [
        (sleeve_id, contribution_by_id[sleeve_id])
        for sleeve_id in sorted(contribution_by_id)
        if contribution_by_id[sleeve_id] > 0
    ]
    total = sum(value for _, value in contributors)
    if total == 0:
        return {
            sleeve_id: TransactionCostBreakdown("terminal_inventory")
            for sleeve_id in contribution_by_id
        }
    fields = (
        "gas_cost",
        "swap_fee_cost",
        "price_impact_cost",
        "slippage_cost",
        "latency_slippage_cost",
        "failed_tx_expected_cost",
        "swap_notional_usd",
    )
    remaining = {field: getattr(inventory_cost, field) for field in fields}
    allocated: dict[str, TransactionCostBreakdown] = {
        sleeve_id: TransactionCostBreakdown("terminal_inventory")
        for sleeve_id in contribution_by_id
    }
    for index, (sleeve_id, contribution) in enumerate(contributors):
        if index == len(contributors) - 1:
            values = remaining
        else:
            fraction = contribution / total
            values = {field: getattr(inventory_cost, field) * fraction for field in fields}
            remaining = {field: remaining[field] - values[field] for field in fields}
        allocated[sleeve_id] = TransactionCostBreakdown(
            action="terminal_inventory",
            **values,
        )
    return allocated


def _terminal_account_values(
    raw_sleeve_values: Mapping[str, float],
    cash_value: float,
    *,
    tolerance: float,
) -> tuple[Mapping[str, float], float, Mapping[str, float], float, float]:
    cash_key = ("cash", "")
    raw_accounts: dict[tuple[str, str], float] = {
        ("sleeve", sleeve_id): value for sleeve_id, value in raw_sleeve_values.items()
    }
    raw_accounts[cash_key] = cash_value
    aggregate = sum(raw_accounts.values())
    if not math.isfinite(aggregate) or aggregate < -tolerance:
        raise TerminalLiquidationError("terminal aggregate portfolio is insolvent")
    final_total = max(aggregate, 0.0)
    positive_total = sum(max(value, 0.0) for value in raw_accounts.values())
    final_accounts = {key: 0.0 for key in raw_accounts}
    positive_keys = sorted(key for key, value in raw_accounts.items() if value > 0)
    if positive_keys:
        if positive_total <= 0:
            raise TerminalLiquidationError("terminal aggregate portfolio funding is malformed")
        factor = final_total / positive_total
        for key in positive_keys:
            final_accounts[key] = raw_accounts[key] * factor
        residual = final_total - sum(final_accounts.values())
        final_accounts[positive_keys[-1]] += residual
    elif final_total != 0.0:
        raise TerminalLiquidationError("terminal aggregate portfolio funding is malformed")
    transfers = {key: final_accounts[key] - raw_accounts[key] for key in raw_accounts}
    transfer_total = sum(transfers.values())
    if abs(transfer_total) > tolerance:
        raise TerminalLiquidationError("terminal aggregate funding transfers do not reconcile")
    final_sleeves = {
        sleeve_id: final_accounts[("sleeve", sleeve_id)] for sleeve_id in raw_sleeve_values
    }
    sleeve_transfers = {
        sleeve_id: transfers[("sleeve", sleeve_id)] for sleeve_id in raw_sleeve_values
    }
    return (
        final_sleeves,
        final_accounts[cash_key],
        sleeve_transfers,
        transfers[cash_key],
        final_total,
    )


def _prepare_terminal_portfolio_settlement(
    runtimes: Mapping[str, SleeveRuntime],
    event: SwapEvent | V4Event,
    historical_liquidity: int,
    pool_config: PoolConfig,
    *,
    cash_value: float,
) -> TerminalPortfolioSettlement:
    if not math.isfinite(cash_value) or cash_value < 0:
        raise TerminalLiquidationError("terminal cash account must be finite and non-negative")
    if historical_liquidity < 0:
        raise TerminalLiquidationError("terminal historical liquidity must be non-negative")
    for sleeve_id, runtime in runtimes.items():
        if runtime.sleeve_id != sleeve_id:
            raise TerminalLiquidationError(
                "terminal runtime mapping does not match sleeve identities"
            )
        if runtime.pool_config != pool_config:
            raise TerminalLiquidationError("terminal runtimes require one pool configuration")
    intents = tuple(
        runtimes[sleeve_id].propose_terminal_removal(
            event,
            historical_liquidity,
        )
        for sleeve_id in sorted(runtimes)
    )
    if not intents:
        zero_cost = TransactionCostBreakdown("terminal_portfolio")
        return TerminalPortfolioSettlement(
            removal_intents=(),
            sleeves=(),
            cash_account_before_usd=cash_value,
            cash_account_after_usd=cash_value,
            cash_funding_transfer_usd=0.0,
            portfolio_final_value_usd=cash_value,
            total_cost=zero_cost,
            inventory_swap_cost=TransactionCostBreakdown("terminal_inventory"),
            external_marked_notional_usd=0.0,
            external_input_value_usd=0.0,
            external_output_value_usd=0.0,
            allocated_variable_cost_usd=0.0,
            terminal_position_settlement_count=0,
            terminal_loose_cngn_settlement_count=0,
            terminal_zero_settlement_count=0,
            terminal_inventory_swap_count=0,
        )
    observed_states = {
        (
            runtime.current_tick,
            runtime.current_sqrt_price_x96,
            runtime.current_price,
        )
        for runtime in runtimes.values()
    }
    if len(observed_states) != 1:
        raise TerminalLiquidationError("terminal settlement requires one observed pool state")
    current_tick, current_sqrt_price_x96, current_price = next(iter(observed_states))
    if not math.isfinite(current_price) or current_price <= 0:
        raise TerminalLiquidationError("terminal settlement requires a finite positive cNGN price")
    contribution_by_id = {
        intent.sleeve_id: intent.gross_wallet_after.cngn_amount * current_price
        for intent in intents
    }
    gross_values = {
        intent.sleeve_id: intent.gross_wallet_after.value_usd(current_price) for intent in intents
    }
    if any(
        not math.isfinite(value) or value < 0
        for value in (*contribution_by_id.values(), *gross_values.values())
    ):
        raise TerminalLiquidationError("terminal settlement contains an invalid gross wallet")
    external_notional = sum(contribution_by_id.values())
    contributors = [
        runtimes[sleeve_id]
        for sleeve_id in sorted(contribution_by_id)
        if contribution_by_id[sleeve_id] > 0
    ]
    if contributors:
        cost_models = set()
        for runtime in contributors:
            fixed = _swap_cost_breakdown(
                "exit",
                0.0,
                historical_liquidity,
                _event_pool_fee_rate(event, pool_config),
                current_tick,
                runtime.params,
            )
            cost_models.add(
                (
                    *_variable_cost_model(runtime),
                    fixed.gas_cost,
                    fixed.failed_tx_expected_cost,
                )
            )
        if len(cost_models) != 1:
            raise TerminalLiquidationError(
                "terminal inventory contributors have incompatible cost models"
            )
        exemplar = contributors[0]
        priced = _swap_cost_breakdown(
            "exit",
            external_notional,
            historical_liquidity,
            _event_pool_fee_rate(event, pool_config),
            current_tick,
            exemplar.params,
            direction="cngn_to_stable",
            current_price=current_price,
            current_sqrt_price_x96=current_sqrt_price_x96,
            pool_config=pool_config,
            exact_output=False,
        )
        inventory_swap_cost = TransactionCostBreakdown(
            action="terminal_inventory",
            gas_cost=priced.gas_cost,
            swap_fee_cost=priced.swap_fee_cost,
            price_impact_cost=priced.price_impact_cost,
            slippage_cost=priced.slippage_cost,
            latency_slippage_cost=priced.latency_slippage_cost,
            failed_tx_expected_cost=priced.failed_tx_expected_cost,
            swap_notional_usd=external_notional,
        )
    else:
        inventory_swap_cost = TransactionCostBreakdown("terminal_inventory")
    variable_cost = _variable_cost(inventory_swap_cost)
    if (
        not math.isfinite(external_notional)
        or not math.isfinite(inventory_swap_cost.total)
        or not math.isfinite(variable_cost)
        or variable_cost < 0
        or variable_cost > external_notional + 1e-12 * max(1.0, external_notional, variable_cost)
    ):
        raise TerminalLiquidationError("terminal inventory swap cost is invalid")
    allocated_inventory = _allocate_terminal_inventory_cost(
        inventory_swap_cost,
        contribution_by_id,
    )
    combined_cost_by_id = {
        intent.sleeve_id: _sum_costs(
            "liquidate",
            (
                intent.removal_cost,
                allocated_inventory[intent.sleeve_id],
            ),
        )
        for intent in intents
    }
    raw_values = {
        sleeve_id: gross_values[sleeve_id] - combined_cost_by_id[sleeve_id].total
        for sleeve_id in gross_values
    }
    total_cost = _sum_costs(
        "terminal_portfolio",
        tuple(intent.removal_cost for intent in intents) + (inventory_swap_cost,),
    )
    tolerance = 1e-10 * max(
        1.0,
        cash_value,
        sum(gross_values.values()),
        total_cost.total,
    )
    (
        final_sleeve_values,
        cash_after,
        sleeve_transfers,
        cash_transfer,
        portfolio_final_value,
    ) = _terminal_account_values(
        raw_values,
        cash_value,
        tolerance=tolerance,
    )
    settlements: list[TerminalSleeveSettlement] = []
    for intent in intents:
        sleeve_id = intent.sleeve_id
        cost = combined_cost_by_id[sleeve_id]
        sleeve_variable_cost = _variable_cost(cost)
        contribution = contribution_by_id[sleeve_id]
        settlement = TerminalSleeveSettlement(
            sleeve_id=sleeve_id,
            final_wallet=PortfolioComposition(
                stable_usd=final_sleeve_values[sleeve_id],
                cngn_amount=0.0,
            ),
            transaction_cost=cost,
            external_marked_notional_usd=contribution,
            external_input_value_usd=contribution,
            external_output_value_usd=contribution - sleeve_variable_cost,
            allocated_variable_cost_usd=sleeve_variable_cost,
            raw_final_value_usd=raw_values[sleeve_id],
            terminal_funding_transfer_usd=sleeve_transfers[sleeve_id],
        )
        runtimes[sleeve_id].validate_terminal_settlement(intent, settlement)
        settlements.append(settlement)
    external_output = external_notional - variable_cost
    if (
        abs(
            cash_after
            + sum(item.final_wallet.stable_usd for item in settlements)
            - portfolio_final_value
        )
        > tolerance
    ):
        raise TerminalLiquidationError("terminal portfolio final value does not reconcile")
    return TerminalPortfolioSettlement(
        removal_intents=intents,
        sleeves=tuple(settlements),
        cash_account_before_usd=cash_value,
        cash_account_after_usd=cash_after,
        cash_funding_transfer_usd=cash_transfer,
        portfolio_final_value_usd=portfolio_final_value,
        total_cost=total_cost,
        inventory_swap_cost=inventory_swap_cost,
        external_marked_notional_usd=external_notional,
        external_input_value_usd=external_notional,
        external_output_value_usd=external_output,
        allocated_variable_cost_usd=variable_cost,
        terminal_position_settlement_count=sum(
            intent.position_before is not None for intent in intents
        ),
        terminal_loose_cngn_settlement_count=sum(
            intent.position_before is None and intent.gross_wallet_after.cngn_amount > 0
            for intent in intents
        ),
        terminal_zero_settlement_count=sum(
            intent.position_before is None and intent.gross_wallet_after.cngn_amount == 0
            for intent in intents
        ),
        terminal_inventory_swap_count=int(external_notional > 0),
    )


def _apply_terminal_portfolio_settlement(
    settlement: TerminalPortfolioSettlement,
    runtimes: Mapping[str, SleeveRuntime],
) -> None:
    if len(settlement.removal_intents) != len(
        {intent.sleeve_id for intent in settlement.removal_intents}
    ) or len(settlement.sleeves) != len({item.sleeve_id for item in settlement.sleeves}):
        raise TerminalLiquidationError(
            "terminal settlement does not match the canonical portfolio settlement"
        )
    intents = {intent.sleeve_id: intent for intent in settlement.removal_intents}
    settlements = {item.sleeve_id: item for item in settlement.sleeves}
    if set(intents) != set(runtimes) or set(settlements) != set(runtimes):
        raise TerminalLiquidationError("terminal settlement does not cover every runtime")
    for sleeve_id in sorted(runtimes):
        runtimes[sleeve_id].validate_terminal_settlement(
            intents[sleeve_id],
            settlements[sleeve_id],
        )
    if runtimes:
        first_sleeve_id = min(runtimes)
        first_intent = intents[first_sleeve_id]
        expected = _prepare_terminal_portfolio_settlement(
            runtimes,
            first_intent.event,
            first_intent.active_liquidity,
            runtimes[first_sleeve_id].pool_config,
            cash_value=settlement.cash_account_before_usd,
        )
    else:
        expected = TerminalPortfolioSettlement(
            removal_intents=(),
            sleeves=(),
            cash_account_before_usd=settlement.cash_account_before_usd,
            cash_account_after_usd=settlement.cash_account_before_usd,
            cash_funding_transfer_usd=0.0,
            portfolio_final_value_usd=settlement.cash_account_before_usd,
            total_cost=TransactionCostBreakdown("terminal_portfolio"),
            inventory_swap_cost=TransactionCostBreakdown("terminal_inventory"),
            external_marked_notional_usd=0.0,
            external_input_value_usd=0.0,
            external_output_value_usd=0.0,
            allocated_variable_cost_usd=0.0,
            terminal_position_settlement_count=0,
            terminal_loose_cngn_settlement_count=0,
            terminal_zero_settlement_count=0,
            terminal_inventory_swap_count=0,
        )
    if settlement != expected:
        raise TerminalLiquidationError(
            "terminal settlement does not match the canonical portfolio settlement"
        )
    for sleeve_id in sorted(runtimes):
        runtimes[sleeve_id].apply_terminal_settlement(
            intents[sleeve_id],
            settlements[sleeve_id],
        )


def simulate_portfolio(
    *,
    events: Sequence[Event],
    sleeves: Sequence[SleeveDefinition],
    allocation: Allocation,
    pool_config: PoolConfig,
    bankroll_usd: float,
    settle_to_cash: bool,
    initial_pool_state: PoolState | None = None,
    entry_overlays_by_sleeve: Mapping[str, EntryEligibilityOverlay] | None = None,
) -> PortfolioResult:
    if not math.isfinite(bankroll_usd) or bankroll_usd <= 0:
        raise ValueError("bankroll_usd must be finite positive")
    by_id = {sleeve.sleeve_id: sleeve for sleeve in sleeves}
    if len(by_id) != len(sleeves):
        raise ValueError("duplicate sleeve_id")
    unknown = set(allocation.weights) - set(by_id)
    if unknown:
        raise ValueError(f"allocation references unknown sleeves: {sorted(unknown)}")
    entry_overlays = {} if entry_overlays_by_sleeve is None else entry_overlays_by_sleeve
    unknown_overlays = set(entry_overlays) - set(by_id)
    if unknown_overlays:
        raise ValueError(f"entry overlays reference unknown sleeves: {sorted(unknown_overlays)}")

    runtimes = {
        sleeve_id: create_sleeve_runtime(
            sleeve_id=sleeve_id,
            params=by_id[sleeve_id].params,
            pool_config=pool_config,
            capital_usd=bankroll_usd * weight,
            entry_eligibility=entry_overlays.get(sleeve_id),
        )
        for sleeve_id, weight in allocation.weights.items()
        if weight > 0
    }
    for runtime in runtimes.values():
        if initial_pool_state is not None:
            runtime.pool_state = initial_pool_state.copy()
    cash_value = bankroll_usd * allocation.cash_weight
    attribution = {
        sleeve_id: SleeveAttribution(sleeve_id, runtime.capital_usd)
        for sleeve_id, runtime in runtimes.items()
    }
    portfolio_samples: list[tuple[datetime, float]] = []
    external_notional = 0.0
    internal_notional = 0.0
    external_input_value = 0.0
    external_output_value = 0.0
    variable_execution_cost = 0.0
    max_share = 0.0
    entry_scale_events: list[EntryScaleEvent] = []
    last_valid_event: SwapEvent | V4Event | None = None
    last_active_liquidity: int | None = None
    opening_sample_recorded = False

    for event in events:
        if not isinstance(event, (SwapEvent, V4Event)) or (
            isinstance(event, V4Event) and event.event_type != "swap"
        ):
            for runtime in runtimes.values():
                runtime._apply_liquidity_event(event)
            continue
        active_liquidity: int | None = 0 if not runtimes else None
        valid = _event_cngn_price(event, pool_config) > 0 if not runtimes else True
        for runtime in runtimes.values():
            observed_liquidity, price_is_valid = runtime._observe_swap(event)
            active_liquidity = observed_liquidity if active_liquidity is None else active_liquidity
            valid = valid and price_is_valid
            runtime._roll_daily_return(event)
        if not valid or active_liquidity is None:
            continue
        last_valid_event = event
        last_active_liquidity = active_liquidity
        if not opening_sample_recorded:
            portfolio_samples.append((event.block_time, bankroll_usd))
            for sleeve_id, runtime in runtimes.items():
                attribution[sleeve_id].value_samples.append((event.block_time, runtime.capital_usd))
                attribution[sleeve_id].liquidity_samples.append((event.block_time, 0.0))
            opening_sample_recorded = True

        _accrue_joint_fees(event, list(runtimes.values()), active_liquidity, pool_config)
        entry_plan = _plan_affordable_actions(
            runtimes,
            event,
            active_liquidity,
            pool_config,
        )
        projected_share = _projected_aggregate_share(
            runtimes,
            entry_plan.actions,
            active_liquidity,
        )
        if entry_plan.intended_entry_count > 0:
            entry_scale_events.append(
                EntryScaleEvent(
                    block_time=event.block_time,
                    block_number=(event.block_number if isinstance(event, V4Event) else None),
                    tx_hash=event.tx_hash if isinstance(event, V4Event) else None,
                    log_index=event.log_index if isinstance(event, V4Event) else None,
                    scale=entry_plan.scale,
                    intended_entry_count=entry_plan.intended_entry_count,
                    executed_entry_count=sum(
                        action.kind == "enter" for action in entry_plan.actions
                    ),
                )
            )
        batch = _settle_actions(entry_plan.actions, runtimes, active_liquidity, pool_config)
        external_notional += batch.external_marked_notional_usd
        internal_notional += batch.internal_cross_notional_usd
        external_input_value += batch.external_input_value_usd
        external_output_value += batch.external_output_value_usd
        variable_execution_cost += batch.allocated_variable_cost_usd
        for item in batch.action_attribution:
            sleeve_attribution = attribution[item.sleeve_id]
            sleeve_attribution.external_marked_notional_usd += item.external_marked_notional_usd
            sleeve_attribution.external_input_value_usd += item.external_input_value_usd
            sleeve_attribution.external_output_value_usd += item.external_output_value_usd
            sleeve_attribution.signed_internal_cngn_value_usd += item.signed_internal_cngn_value_usd
            sleeve_attribution.allocated_variable_cost_usd += item.allocated_variable_cost_usd
        for sleeve_id, cost in batch.costs:
            attribution[sleeve_id].transaction_cost_usd += cost.total
            attribution[sleeve_id].price_impact_cost_usd += cost.price_impact_cost

        share = _aggregate_share(list(runtimes.values()), active_liquidity)
        if abs(share - projected_share) > 1e-12:
            raise ExecutionAccountingError(
                "projected and applied aggregate liquidity share diverged"
            )
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

    terminal_liquidation_cost = 0.0
    terminal_position_settlement_count = 0
    terminal_loose_cngn_settlement_count = 0
    terminal_zero_settlement_count = 0
    terminal_inventory_swap_count = 0
    terminal_fixed_cost_usd = 0.0
    terminal_variable_cost_usd = 0.0
    terminal_external_marked_notional_usd = 0.0
    terminal_cash_funding_transfer_usd = 0.0
    if settle_to_cash:
        if last_valid_event is None or last_active_liquidity is None:
            raise NoValidationSwapError("terminal cash settlement requires a valid swap")
        terminal = _prepare_terminal_portfolio_settlement(
            runtimes,
            last_valid_event,
            last_active_liquidity,
            pool_config,
            cash_value=cash_value,
        )
        _apply_terminal_portfolio_settlement(terminal, runtimes)
        cash_value = terminal.cash_account_after_usd
        external_notional += terminal.external_marked_notional_usd
        external_input_value += terminal.external_input_value_usd
        external_output_value += terminal.external_output_value_usd
        variable_execution_cost += terminal.allocated_variable_cost_usd
        for terminal_item in terminal.sleeves:
            sleeve_attribution = attribution[terminal_item.sleeve_id]
            sleeve_attribution.external_marked_notional_usd += (
                terminal_item.external_marked_notional_usd
            )
            sleeve_attribution.external_input_value_usd += terminal_item.external_input_value_usd
            sleeve_attribution.external_output_value_usd += terminal_item.external_output_value_usd
            sleeve_attribution.allocated_variable_cost_usd += (
                terminal_item.allocated_variable_cost_usd
            )
            sleeve_attribution.transaction_cost_usd += terminal_item.transaction_cost.total
            sleeve_attribution.price_impact_cost_usd += (
                terminal_item.transaction_cost.price_impact_cost
            )
            sleeve_attribution.terminal_liquidation_cost_usd += terminal_item.transaction_cost.total
            sleeve_attribution.terminal_funding_transfer_usd += (
                terminal_item.terminal_funding_transfer_usd
            )
        terminal_liquidation_cost = terminal.total_cost.total
        terminal_position_settlement_count = terminal.terminal_position_settlement_count
        terminal_loose_cngn_settlement_count = terminal.terminal_loose_cngn_settlement_count
        terminal_zero_settlement_count = terminal.terminal_zero_settlement_count
        terminal_inventory_swap_count = terminal.terminal_inventory_swap_count
        terminal_variable_cost_usd = terminal.allocated_variable_cost_usd
        terminal_fixed_cost_usd = terminal.total_cost.total - terminal_variable_cost_usd
        terminal_external_marked_notional_usd = terminal.external_marked_notional_usd
        terminal_cash_funding_transfer_usd = terminal.cash_funding_transfer_usd
        sleeve_total = 0.0
        for sleeve_id, runtime in runtimes.items():
            if runtime.position is not None:
                raise ValueError("terminal cash settlement left an open position")
            if runtime.wallet.cngn_amount != 0.0:
                raise ValueError("terminal cash settlement left cNGN inventory")
            value = runtime.wallet.stable_usd
            attribution[sleeve_id].value_samples.append((last_valid_event.block_time, value))
            attribution[sleeve_id].liquidity_samples.append((last_valid_event.block_time, 0.0))
            sleeve_total += value
        portfolio_samples.append((last_valid_event.block_time, cash_value + sleeve_total))

    for sleeve_id, runtime in runtimes.items():
        attribution[sleeve_id].final_value = (
            attribution[sleeve_id].value_samples[-1][1]
            if attribution[sleeve_id].value_samples
            else runtime.capital_usd
        )
    final_value = cash_value + sum(item.final_value for item in attribution.values())
    terminal_open_position_count = sum(
        runtime.position is not None for runtime in runtimes.values()
    )
    terminal_cngn_amount = sum(runtime.wallet.cngn_amount for runtime in runtimes.values())
    return PortfolioResult(
        bankroll_usd=bankroll_usd,
        final_value=final_value,
        cash_value=cash_value,
        total_fees=sum(item.fees_usd for item in attribution.values()),
        total_transaction_cost=sum(item.transaction_cost_usd for item in attribution.values()),
        total_price_impact_cost=sum(item.price_impact_cost_usd for item in attribution.values()),
        external_swap_notional_usd=external_notional,
        internal_netting_notional_usd=internal_notional,
        external_input_value_usd=external_input_value,
        external_output_value_usd=external_output_value,
        total_variable_execution_cost_usd=variable_execution_cost,
        max_aggregate_liquidity_share=max_share,
        entry_scale_events=tuple(entry_scale_events),
        settled_to_cash=settle_to_cash,
        terminal_liquidation_cost=terminal_liquidation_cost,
        terminal_position_settlement_count=terminal_position_settlement_count,
        terminal_loose_cngn_settlement_count=(terminal_loose_cngn_settlement_count),
        terminal_zero_settlement_count=terminal_zero_settlement_count,
        terminal_inventory_swap_count=terminal_inventory_swap_count,
        terminal_fixed_cost_usd=terminal_fixed_cost_usd,
        terminal_variable_cost_usd=terminal_variable_cost_usd,
        terminal_external_marked_notional_usd=(terminal_external_marked_notional_usd),
        terminal_cash_funding_transfer_usd=(terminal_cash_funding_transfer_usd),
        terminal_open_position_count=terminal_open_position_count,
        terminal_cngn_amount=terminal_cngn_amount,
        value_samples=portfolio_samples,
        attribution=attribution,
    )
