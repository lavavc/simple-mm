"""Stateful runtime for a single backtest sleeve."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import BurnEvent, Event, MintEvent, SwapEvent, V4Event
from research.backtester.entry_eligibility import EntryEligibilityOverlay
from research.backtester.params import BacktestParams
from research.backtester.pool_state import PoolState
from research.backtester.portfolio_errors import NoValidationSwapError
from research.backtester.simulator import (
    PoolConfig,
    PortfolioComposition,
    SimResult,
    TransactionCostBreakdown,
    VirtualPosition,
    _calculate_entry_range,
    _cngn_to_native_price,
    _defensive_exit_mark,
    _defensive_exit_quality_passes,
    _entry_filters_pass,
    _event_block_number,
    _event_cngn_price,
    _event_fee_wallet,
    _event_liquidity,
    _event_pool_fee_rate,
    _event_sqrt_price_x96,
    _event_swap_volume_usd,
    _exit_cost_breakdown,
    _expected_fee_apr_passes,
    _fast_price_to_tick,
    _is_defensive_exit,
    _paper_exit_reason,
    _pay_or_unwind_exit_cost,
    _pay_wallet_cost,
    _portfolio_value,
    _record_episode,
    _route_wallet_to_position,
    _snapshot_composition,
    _swap_cost_breakdown,
    _wallet_with_position_removed,
)
from research.backtester.sizing import DeployFullWallet, EntryContext, SizingPolicy
from research.backtester.strategy import EWMACalculator


@dataclass(frozen=True)
class SleeveAction:
    sleeve_id: str
    kind: Literal["enter", "exit", "liquidate"]
    event: Event
    active_liquidity: float
    cngn_usd_price: float
    wallet_before: PortfolioComposition
    wallet_after: PortfolioComposition
    position_before: VirtualPosition | None
    position_after: VirtualPosition | None
    transaction_cost: TransactionCostBreakdown
    reason: str | None
    variable_cost_asset: Literal["stable", "cngn"] | None
    inventory_swap_direction: Literal["stable_to_cngn", "cngn_to_stable"] | None = None
    inventory_swap_notional_usd: float = 0.0


@dataclass(frozen=True)
class SleeveSettlement:
    wallet_after: PortfolioComposition
    transaction_cost: TransactionCostBreakdown


class TerminalLiquidationError(ValueError):
    """Raised when a validation boundary cannot settle exactly to USD cash."""


def _variable_cost_tolerance(notional: float, variable_cost: float) -> float:
    return 1e-12 * max(1.0, notional, variable_cost)


@dataclass
class SleeveRuntime:
    sleeve_id: str
    params: BacktestParams
    pool_config: PoolConfig
    capital_usd: float
    sizing: SizingPolicy
    entry_eligibility: EntryEligibilityOverlay | None = None
    idle_apr: float = 0.0
    ewma: EWMACalculator = field(init=False)
    pool_state: PoolState = field(default_factory=PoolState)
    position: VirtualPosition | None = None
    result: SimResult = field(default_factory=SimResult)
    wallet: PortfolioComposition = field(init=False)
    current_price: float = 0.0
    current_tick: int = 0
    current_sqrt_price_x96: int = field(default_factory=lambda: tick_to_sqrt_price_x96(0))
    current_active_liquidity: int = 0
    day_start_value: float = field(init=False)
    current_day: date | None = None
    validation_start: PortfolioComposition | None = None
    cooldown_until_time: datetime | None = None
    cooldown_until_block: int | None = None
    previous_swap_time: datetime | None = None
    pending_defensive_exit_reason: str | None = None
    pending_defensive_exit_first_time: datetime | None = None
    pending_defensive_exit_count: int = 0
    qualified_price_observations: list[tuple[datetime, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.ewma = EWMACalculator(self.params.ewma_lambda)
        self.wallet = PortfolioComposition(stable_usd=self.capital_usd, cngn_amount=0.0)
        self.day_start_value = self.capital_usd

    def _apply_liquidity_event(self, event: Event) -> bool:
        if isinstance(event, MintEvent):
            self.pool_state.apply_mint(event.tick_lower, event.tick_upper, event.liquidity_delta)
            return True
        if isinstance(event, BurnEvent):
            self.pool_state.apply_burn(event.tick_lower, event.tick_upper, event.liquidity_delta)
            return True
        if isinstance(event, V4Event) and event.event_type in ("mint", "burn"):
            if (
                event.tick_lower is not None
                and event.tick_upper is not None
                and event.liquidity_delta is not None
            ):
                liquidity_delta = abs(event.liquidity_delta)
                if event.event_type == "mint":
                    self.pool_state.apply_mint(event.tick_lower, event.tick_upper, liquidity_delta)
                else:
                    self.pool_state.apply_burn(event.tick_lower, event.tick_upper, liquidity_delta)
            return True
        return False

    def _observe_swap(self, event: SwapEvent | V4Event) -> tuple[int, bool]:
        self.result.total_swaps += 1
        self.result.start_time = self.result.start_time or event.block_time
        self.result.end_time = event.block_time
        self.current_price = _event_cngn_price(event, self.pool_config)
        if self.current_price <= 0:
            return 0, False

        native_price = _cngn_to_native_price(self.current_price, self.pool_config)
        self.ewma.update(native_price)
        self.current_tick = (
            event.tick
            if isinstance(event, V4Event)
            else _fast_price_to_tick(
                native_price, self.pool_config.token0_decimals, self.pool_config.token1_decimals
            )
        )
        self.current_sqrt_price_x96 = _event_sqrt_price_x96(event, self.current_tick)
        active_liquidity = _event_liquidity(event, self.pool_state, self.current_tick)
        self.current_active_liquidity = active_liquidity
        if _event_swap_volume_usd(event) >= self.params.exit_price_min_swap_volume_usd:
            self.qualified_price_observations.append((event.block_time, self.current_price))

        if self.validation_start is None:
            self.validation_start = _snapshot_composition(
                self.position,
                self.wallet,
                self.current_tick,
                self.current_sqrt_price_x96,
                self.current_price,
                self.pool_config,
            )
        return active_liquidity, True

    def _roll_daily_return(self, event: SwapEvent | V4Event) -> None:
        event_day = event.block_time.date()
        if self.current_day is not None and event_day != self.current_day:
            current_val = _portfolio_value(
                self.position,
                self.wallet,
                self.current_price,
                self.current_tick,
                self.current_sqrt_price_x96,
                self.pool_config,
            )
            if self.day_start_value > 0:
                self.result.daily_returns.append(current_val / self.day_start_value - 1.0)
            self.day_start_value = current_val
        self.current_day = event_day

    def _accrue_position_fee(self, event: SwapEvent | V4Event, active_liquidity: int) -> None:
        if self.position is None:
            return
        self.position.observed_swaps += 1
        if not self.position.is_in_range(self.current_tick):
            return
        self.result.in_range_swaps += 1
        self.position.in_range_swaps += 1
        if active_liquidity <= 0:
            return
        # Recorded active_liquidity is the real pool's depth; our virtual
        # position competes against it, so it joins the denominator.
        liquidity_share = self.position.liquidity_L / (active_liquidity + self.position.liquidity_L)
        fee_wallet = _event_fee_wallet(
            event,
            _event_pool_fee_rate(event, self.pool_config),
            liquidity_share,
            self.current_price,
            self.pool_config,
        )
        self.position.accrued_fee_stable += fee_wallet.stable_usd
        self.position.accrued_fee_cngn += fee_wallet.cngn_amount
        self.result.total_fees += fee_wallet.value_usd(self.current_price)
        self.result.total_fee_stable += fee_wallet.stable_usd
        self.result.total_fee_cngn += fee_wallet.cngn_amount

    def _exit_decision(
        self, event: SwapEvent | V4Event, active_liquidity: int
    ) -> tuple[str | None, TransactionCostBreakdown]:
        exit_reason: str | None = None
        exit_cost = TransactionCostBreakdown("exit")
        if self.position is None:
            return exit_reason, exit_cost
        if self.params.strategy_mode == "paper":
            defensive_price, defensive_tick = _defensive_exit_mark(
                event,
                self.current_price,
                self.current_tick,
                self.pool_config,
                self.params,
                self.qualified_price_observations,
            )
            exit_reason, exit_cost = _paper_exit_reason(
                self.position,
                event,
                self.current_tick,
                self.current_sqrt_price_x96,
                self.current_price,
                defensive_tick,
                defensive_price,
                active_liquidity,
                self.pool_config,
                self.params,
            )
            if _is_defensive_exit(exit_reason):
                if not _defensive_exit_quality_passes(event, self.params):
                    exit_reason = None
                    exit_cost = TransactionCostBreakdown("exit")
                else:
                    if self.pending_defensive_exit_reason == exit_reason:
                        self.pending_defensive_exit_count += 1
                    else:
                        self.pending_defensive_exit_reason = exit_reason
                        self.pending_defensive_exit_first_time = event.block_time
                        self.pending_defensive_exit_count = 1
                    required_swaps = max(self.params.exit_confirmation_swaps, 1)
                    required_minutes = max(self.params.exit_confirmation_minutes, 0.0)
                    elapsed_minutes = (
                        (event.block_time - self.pending_defensive_exit_first_time).total_seconds()
                        / 60
                        if self.pending_defensive_exit_first_time is not None
                        else 0.0
                    )
                    if (
                        self.pending_defensive_exit_count < required_swaps
                        or elapsed_minutes < required_minutes
                    ):
                        exit_reason = None
                        exit_cost = TransactionCostBreakdown("exit")
            else:
                self.pending_defensive_exit_reason = None
                self.pending_defensive_exit_first_time = None
                self.pending_defensive_exit_count = 0
        elif self.params.strategy_mode != "static":
            tick_range = self.position.tick_upper - self.position.tick_lower
            if not self.position.is_in_range(self.current_tick):
                distance = (
                    self.position.tick_lower - self.current_tick
                    if self.current_tick < self.position.tick_lower
                    else self.current_tick - self.position.tick_upper
                )
                if (
                    tick_range > 0
                    and (distance / tick_range * 100) >= self.params.rebalance_threshold_pct
                ):
                    exit_reason = "ewma_out_of_range"
            elif self.params.preemptive_rebalance:
                margin = tick_range * self.params.rebalance_threshold_pct / 100
                if (
                    self.current_tick - self.position.tick_lower < margin
                    or self.position.tick_upper - self.current_tick < margin
                ):
                    exit_reason = "ewma_preemptive"

        if exit_reason is not None and self.ewma.ready and exit_cost.total == 0.0:
            exit_cost = _exit_cost_breakdown(
                self.position,
                self.current_tick,
                self.current_sqrt_price_x96,
                self.current_price,
                active_liquidity,
                _event_pool_fee_rate(event, self.pool_config),
                self.pool_config,
                self.params,
            )
        return (exit_reason if self.ewma.ready else None), exit_cost

    def _apply_exit(
        self,
        event: SwapEvent | V4Event,
        active_liquidity: int,
        exit_reason: str | None,
        exit_cost: TransactionCostBreakdown,
    ) -> None:
        action = self._build_exit_action(event, active_liquidity, exit_reason, exit_cost)
        if action is not None:
            self.apply_action(action)

    def _build_exit_action(
        self,
        event: SwapEvent | V4Event,
        active_liquidity: int,
        exit_reason: str | None,
        exit_cost: TransactionCostBreakdown,
    ) -> SleeveAction | None:
        if self.position is None or exit_reason is None:
            return None
        wallet_after = _wallet_with_position_removed(
            self.wallet,
            self.position,
            self.current_tick,
            self.current_sqrt_price_x96,
            self.current_price,
            self.pool_config,
        )
        wallet_after = _pay_or_unwind_exit_cost(wallet_after, exit_cost, self.current_price)
        return SleeveAction(
            sleeve_id=self.sleeve_id,
            kind="exit",
            event=event,
            active_liquidity=active_liquidity,
            cngn_usd_price=self.current_price,
            wallet_before=deepcopy(self.wallet),
            wallet_after=wallet_after,
            position_before=deepcopy(self.position),
            position_after=None,
            transaction_cost=exit_cost,
            reason=exit_reason,
            inventory_swap_direction=(
                "cngn_to_stable" if exit_cost.swap_notional_usd > 0 else None
            ),
            inventory_swap_notional_usd=exit_cost.swap_notional_usd,
            variable_cost_asset="stable" if exit_cost.swap_notional_usd > 0 else None,
        )

    def propose_exit(
        self, event: SwapEvent | V4Event, active_liquidity: int
    ) -> SleeveAction | None:
        exit_reason, exit_cost = self._exit_decision(event, active_liquidity)
        return self._build_exit_action(event, active_liquidity, exit_reason, exit_cost)

    def propose_terminal_liquidation(
        self,
        event: SwapEvent | V4Event,
        active_liquidity: float,
    ) -> SleeveAction:
        """Remove any position and convert every remaining cNGN unit to USD."""
        if not math.isfinite(self.current_price) or self.current_price <= 0:
            raise TerminalLiquidationError(
                "terminal liquidation requires a finite positive cNGN price"
            )
        if active_liquidity < 0:
            raise TerminalLiquidationError(
                "terminal liquidation requires non-negative active liquidity"
            )
        wallet_after_removal = (
            _wallet_with_position_removed(
                self.wallet,
                self.position,
                self.current_tick,
                self.current_sqrt_price_x96,
                self.current_price,
                self.pool_config,
            )
            if self.position is not None
            else deepcopy(self.wallet)
        )
        requires_transaction = (
            self.position is not None or wallet_after_removal.cngn_amount > 0.0
        )
        if requires_transaction:
            fixed = _swap_cost_breakdown(
                "exit",
                0.0,
                active_liquidity,
                _event_pool_fee_rate(event, self.pool_config),
                self.current_tick,
                self.params,
            )
            removal_value = wallet_after_removal.value_usd(self.current_price)
            fixed_cost = fixed.gas_cost + fixed.failed_tx_expected_cost
            if not math.isfinite(removal_value):
                raise TerminalLiquidationError(
                    "terminal liquidation wallet value is invalid"
                )
            tolerance = 1e-12 * max(1.0, removal_value, fixed_cost)
            if removal_value + tolerance < fixed_cost:
                raise TerminalLiquidationError(
                    "terminal liquidation fixed cost exceeds wallet value"
                )
            after_fixed = _pay_wallet_cost(
                wallet_after_removal,
                fixed_cost,
                self.current_price,
            )
            swap_notional = after_fixed.cngn_amount * self.current_price
            if not math.isfinite(swap_notional) or swap_notional < 0.0:
                raise TerminalLiquidationError(
                    "terminal liquidation swap notional is invalid"
                )
            if after_fixed.cngn_amount > 0.0 and swap_notional == 0.0:
                raise TerminalLiquidationError(
                    "terminal liquidation swap notional is invalid"
                )
            if swap_notional == 0.0:
                terminal_cost = TransactionCostBreakdown(
                    action="liquidate",
                    gas_cost=fixed.gas_cost,
                    failed_tx_expected_cost=fixed.failed_tx_expected_cost,
                )
                wallet_after = after_fixed
            else:
                round_trip_cngn = swap_notional / self.current_price
                if abs(round_trip_cngn - after_fixed.cngn_amount) > math.ulp(
                    after_fixed.cngn_amount
                ):
                    raise TerminalLiquidationError(
                        "terminal liquidation cannot represent the full-balance swap"
                    )
                priced = _swap_cost_breakdown(
                    "exit",
                    swap_notional,
                    active_liquidity,
                    _event_pool_fee_rate(event, self.pool_config),
                    self.current_tick,
                    self.params,
                    direction="cngn_to_stable",
                    current_price=self.current_price,
                    current_sqrt_price_x96=self.current_sqrt_price_x96,
                    pool_config=self.pool_config,
                    exact_output=False,
                )
                terminal_cost = TransactionCostBreakdown(
                    action="liquidate",
                    gas_cost=fixed.gas_cost,
                    failed_tx_expected_cost=fixed.failed_tx_expected_cost,
                    swap_fee_cost=priced.swap_fee_cost,
                    price_impact_cost=priced.price_impact_cost,
                    slippage_cost=priced.slippage_cost,
                    latency_slippage_cost=priced.latency_slippage_cost,
                    swap_notional_usd=swap_notional,
                )
                variable_cost = (
                    terminal_cost.swap_fee_cost
                    + terminal_cost.price_impact_cost
                    + terminal_cost.slippage_cost
                    + terminal_cost.latency_slippage_cost
                )
                if not math.isfinite(variable_cost) or variable_cost < 0:
                    raise TerminalLiquidationError(
                        "terminal liquidation variable cost is invalid"
                    )
                if variable_cost > swap_notional + _variable_cost_tolerance(
                    swap_notional,
                    variable_cost,
                ):
                    raise TerminalLiquidationError(
                        "terminal liquidation variable cost exceeds swap output"
                    )
                wallet_after = PortfolioComposition(
                    stable_usd=(
                        after_fixed.stable_usd
                        + max(swap_notional - variable_cost, 0.0)
                    ),
                    cngn_amount=0.0,
                )
        else:
            terminal_cost = TransactionCostBreakdown("liquidate")
            wallet_after = wallet_after_removal
        expected_value = (
            wallet_after_removal.value_usd(self.current_price) - terminal_cost.total
        )
        actual_value = wallet_after.value_usd(self.current_price)
        tolerance = 1e-9 * max(
            1.0,
            abs(expected_value),
            abs(actual_value),
            terminal_cost.total,
        )
        if (
            not math.isfinite(expected_value)
            or not math.isfinite(actual_value)
            or expected_value < -tolerance
            or abs(actual_value - expected_value) > tolerance
        ):
            raise TerminalLiquidationError(
                "terminal liquidation value reconciliation failed"
            )
        if wallet_after.cngn_amount != 0.0:
            raise TerminalLiquidationError(
                "terminal liquidation left cNGN inventory"
            )
        wallet_after = PortfolioComposition(
            stable_usd=wallet_after.stable_usd,
            cngn_amount=0.0,
        )
        return SleeveAction(
            sleeve_id=self.sleeve_id,
            kind="liquidate",
            event=event,
            active_liquidity=active_liquidity,
            cngn_usd_price=self.current_price,
            wallet_before=deepcopy(self.wallet),
            wallet_after=wallet_after,
            position_before=deepcopy(self.position),
            position_after=None,
            transaction_cost=terminal_cost,
            reason="validation_boundary",
            variable_cost_asset="stable" if terminal_cost.swap_notional_usd > 0 else None,
            inventory_swap_direction=(
                "cngn_to_stable" if terminal_cost.swap_notional_usd > 0 else None
            ),
            inventory_swap_notional_usd=terminal_cost.swap_notional_usd,
        )

    def apply_action(
        self, action: SleeveAction, settlement: SleeveSettlement | None = None
    ) -> None:
        if action.sleeve_id != self.sleeve_id:
            raise ValueError(
                f"action sleeve {action.sleeve_id!r} does not match runtime sleeve "
                f"{self.sleeve_id!r}"
            )
        if self.wallet != action.wallet_before:
            raise ValueError("runtime wallet does not match action wallet snapshot")
        if self.position != action.position_before:
            raise ValueError("runtime position does not match action position snapshot")

        wallet_after = settlement.wallet_after if settlement is not None else action.wallet_after
        transaction_cost = (
            settlement.transaction_cost if settlement is not None else action.transaction_cost
        )
        variable_cost = (
            transaction_cost.swap_fee_cost
            + transaction_cost.price_impact_cost
            + transaction_cost.slippage_cost
            + transaction_cost.latency_slippage_cost
        )
        external_notional = transaction_cost.swap_notional_usd
        if not math.isfinite(external_notional) or external_notional < 0:
            raise ValueError("action external notional is invalid")
        if not math.isfinite(variable_cost) or variable_cost < 0:
            raise ValueError("action variable execution cost is invalid")
        external_input = (
            external_notional + variable_cost
            if action.kind == "enter"
            else external_notional
        )
        external_output = (
            external_notional
            if action.kind == "enter"
            else external_notional - variable_cost
        )
        if external_output < -_variable_cost_tolerance(
            external_notional,
            variable_cost,
        ):
            raise ValueError("action variable execution cost exceeds external notional")
        self.result.external_swap_notional_usd += external_notional
        self.result.external_input_value_usd += external_input
        self.result.external_output_value_usd += max(external_output, 0.0)
        self.result.total_variable_execution_cost_usd += variable_cost
        if action.kind in ("exit", "liquidate"):
            if action.kind == "exit" and (
                action.position_before is None or action.reason is None
            ):
                raise ValueError("exit action requires a position and reason")
            if action.kind == "liquidate" and action.reason is None:
                raise ValueError("terminal liquidation requires a reason")
            if action.position_before is not None:
                position_value = action.position_before.value_at_sqrt_price_x96(
                    self.current_sqrt_price_x96,
                    action.cngn_usd_price,
                    self.pool_config,
                )
                _record_episode(
                    self.result,
                    action.position_before,
                    action.reason or "validation_boundary",
                    action.event.block_time,
                    action.cngn_usd_price,
                    self.current_tick,
                    position_value,
                    transaction_cost,
                    self.pool_config,
                )
            else:
                self.result.total_transaction_cost += transaction_cost.total
                self.result.total_exit_cost += transaction_cost.total
                self.result.total_gas_cost += transaction_cost.gas_cost
                self.result.total_swap_fee_cost += transaction_cost.swap_fee_cost
                self.result.total_price_impact_cost += transaction_cost.price_impact_cost
                self.result.total_slippage_cost += transaction_cost.slippage_cost
                self.result.total_latency_slippage_cost += (
                    transaction_cost.latency_slippage_cost
                )
                self.result.total_failed_tx_expected_cost += (
                    transaction_cost.failed_tx_expected_cost
                )
            if action.kind == "exit":
                self.result.total_rebalance_cost += transaction_cost.total
                self.result.rebalance_count += 1
                if self.params.cooldown_minutes > 0:
                    self.cooldown_until_time = action.event.block_time + timedelta(
                        minutes=self.params.cooldown_minutes
                    )
                if (
                    self.params.cooldown_blocks > 0
                    and _event_block_number(action.event) is not None
                ):
                    self.cooldown_until_block = (
                        _event_block_number(action.event) or 0
                    ) + self.params.cooldown_blocks
            self.pending_defensive_exit_reason = None
            self.pending_defensive_exit_first_time = None
            self.pending_defensive_exit_count = 0

        self.wallet = deepcopy(wallet_after)
        self.position = deepcopy(action.position_after)
        if settlement is not None and action.kind == "enter" and self.position is not None:
            self.position.entry_transaction_cost = transaction_cost

    def propose_entry(
        self,
        event: SwapEvent | V4Event,
        active_liquidity: int,
        *,
        deployment_scale: float = 1.0,
        eligibility_preapproved: bool = False,
    ) -> SleeveAction | None:
        if not math.isfinite(deployment_scale) or not 0.0 <= deployment_scale <= 1.0:
            raise ValueError("deployment_scale must be finite and between zero and one")
        in_time_cooldown = (
            self.cooldown_until_time is not None and event.block_time < self.cooldown_until_time
        )
        event_block = _event_block_number(event)
        in_block_cooldown = (
            self.cooldown_until_block is not None
            and event_block is not None
            and event_block < self.cooldown_until_block
        )
        if not (
            self.position is None
            and self.ewma.ready
            and self.wallet.value_usd(self.current_price) > self.params.gas_cost_usd
            and not in_time_cooldown
            and not in_block_cooldown
        ):
            return None
        if not eligibility_preapproved and not _entry_filters_pass(
                event,
                self.params,
                self.ewma,
                self.current_price,
                active_liquidity,
                self.pool_config,
            ):
            return None
        wallet_value = self.wallet.value_usd(self.current_price)
        entry_context = EntryContext(
            block_time=event.block_time,
            wallet_value_usd=wallet_value,
            current_price=self.current_price,
            current_tick=self.current_tick,
            active_liquidity=active_liquidity,
        )
        if (
            not eligibility_preapproved
            and
            self.entry_eligibility is not None
            and not self.entry_eligibility.evaluate(entry_context).eligible
        ):
            return None
        deploy_target = self.sizing.deployed_capital_usd(entry_context) * deployment_scale
        deploy_fraction = min(deploy_target / wallet_value, 1.0) if wallet_value > 0 else 0.0
        if deploy_fraction <= 0:
            return None
        deploy_wallet = PortfolioComposition(
            stable_usd=self.wallet.stable_usd * deploy_fraction,
            cngn_amount=self.wallet.cngn_amount * deploy_fraction,
        )
        idle_wallet = PortfolioComposition(
            stable_usd=self.wallet.stable_usd * (1.0 - deploy_fraction),
            cngn_amount=self.wallet.cngn_amount * (1.0 - deploy_fraction),
        )
        tick_lower, tick_upper = _calculate_entry_range(
            event,
            self.params,
            self.ewma,
            self.current_price,
            self.current_tick,
            self.pool_config,
        )
        route = _route_wallet_to_position(
            deploy_wallet,
            tick_lower,
            tick_upper,
            self.current_tick,
            self.current_sqrt_price_x96,
            self.current_price,
            active_liquidity,
            _event_pool_fee_rate(event, self.pool_config),
            self.pool_config,
            self.params,
        )
        if route.liquidity <= 0:
            return None
        if not eligibility_preapproved and not _expected_fee_apr_passes(
            event,
            self.params,
            route.liquidity,
            route.deployed_capital,
            active_liquidity,
            self.previous_swap_time,
            self.pool_config,
        ):
            return None
        position_after = VirtualPosition(
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity_L=route.liquidity,
            entry_price=self.current_price,
            entry_value=route.deployed_capital,
            entry_time=event.block_time,
            entry_tick=self.current_tick,
            entry_active_liquidity=active_liquidity,
            deployed_capital=route.deployed_capital,
            entry_transaction_cost=route.transaction_cost,
        )
        wallet_after = PortfolioComposition(
            stable_usd=route.remaining_wallet.stable_usd + idle_wallet.stable_usd,
            cngn_amount=route.remaining_wallet.cngn_amount + idle_wallet.cngn_amount,
        )
        return SleeveAction(
            sleeve_id=self.sleeve_id,
            kind="enter",
            event=event,
            active_liquidity=active_liquidity,
            cngn_usd_price=self.current_price,
            wallet_before=deepcopy(self.wallet),
            wallet_after=wallet_after,
            position_before=None,
            position_after=position_after,
            transaction_cost=route.transaction_cost,
            reason=None,
            inventory_swap_direction=route.inventory_swap_direction,
            inventory_swap_notional_usd=route.transaction_cost.swap_notional_usd,
            variable_cost_asset=(
                "stable"
                if route.inventory_swap_direction == "stable_to_cngn"
                else "cngn"
                if route.inventory_swap_direction == "cngn_to_stable"
                else None
            ),
        )

    def _maybe_enter(self, event: SwapEvent | V4Event, active_liquidity: int) -> None:
        action = self.propose_entry(event, active_liquidity)
        if action is not None:
            self.apply_action(action)

    def _record_event_value(self, event: SwapEvent | V4Event) -> None:
        self.result.value_samples.append(
            (
                event.block_time,
                _portfolio_value(
                    self.position,
                    self.wallet,
                    self.current_price,
                    self.current_tick,
                    self.current_sqrt_price_x96,
                    self.pool_config,
                ),
            )
        )

    def finalize(
        self,
        *,
        settle_to_cash: bool,
        terminal_event: SwapEvent | V4Event | None,
    ) -> SimResult:
        if settle_to_cash:
            if terminal_event is None:
                raise NoValidationSwapError(
                    "terminal cash settlement requires a valid swap"
                )
            synthetic_liquidity = (
                self.position.liquidity_L if self.position is not None else 0.0
            )
            action = self.propose_terminal_liquidation(
                terminal_event,
                self.current_active_liquidity + synthetic_liquidity,
            )
            self.apply_action(action)
            if self.position is not None:
                raise TerminalLiquidationError(
                    "terminal cash settlement left an open position"
                )
            if self.wallet.cngn_amount != 0.0:
                raise TerminalLiquidationError(
                    "terminal cash settlement left cNGN inventory"
                )
            self.result.final_value = self.wallet.stable_usd
            self.result.settled_to_cash = True
            self.result.terminal_liquidation_cost = action.transaction_cost.total
            self.result.value_samples.append(
                (terminal_event.block_time, self.result.final_value)
            )
        elif self.position is not None and self.result.end_time is not None:
            terminal_position = self.position
            terminal_exit_reason = "end_of_data"
            terminal_exit_cost = TransactionCostBreakdown("end_of_data")
            terminal_exit_value = terminal_position.value_at_sqrt_price_x96(
                self.current_sqrt_price_x96,
                self.current_price,
                self.pool_config,
            )
            if self.params.transaction_costs.close_position_on_end:
                terminal_exit_reason = "end_of_data_close"
                terminal_exit_cost = _exit_cost_breakdown(
                    terminal_position,
                    self.current_tick,
                    self.current_sqrt_price_x96,
                    self.current_price,
                    self.current_active_liquidity,
                    self.pool_config.fee_rate,
                    self.pool_config,
                    self.params,
                )
                self.wallet = _wallet_with_position_removed(
                    self.wallet,
                    terminal_position,
                    self.current_tick,
                    self.current_sqrt_price_x96,
                    self.current_price,
                    self.pool_config,
                )
                self.wallet = _pay_or_unwind_exit_cost(
                    self.wallet, terminal_exit_cost, self.current_price
                )
                self.position = None
            self.result.final_value = _portfolio_value(
                self.position,
                self.wallet,
                self.current_price,
                self.current_tick,
                self.current_sqrt_price_x96,
                self.pool_config,
            )
            _record_episode(
                self.result,
                terminal_position,
                terminal_exit_reason,
                self.result.end_time,
                self.current_price,
                self.current_tick,
                terminal_exit_value,
                terminal_exit_cost,
                self.pool_config,
            )
        elif not settle_to_cash:
            self.result.final_value = _portfolio_value(
                self.position,
                self.wallet,
                self.current_price,
                self.current_tick,
                self.current_sqrt_price_x96,
                self.pool_config,
            )
        self.result.terminal_open_position_count = int(self.position is not None)
        self.result.terminal_cngn_amount = self.wallet.cngn_amount
        if self.day_start_value > 0 and self.result.final_value > 0:
            self.result.daily_returns.append(self.result.final_value / self.day_start_value - 1.0)

        if self.validation_start is None:
            self.validation_start = PortfolioComposition(
                stable_usd=self.capital_usd, cngn_amount=0.0
            )
        hodl_end_value = self.validation_start.value_usd(self.current_price)
        self.result.divergent_loss = (
            (self.result.final_value / hodl_end_value - 1.0) if hodl_end_value > 0 else 0.0
        )
        return self.result

    def run(
        self,
        events: list[Event],
        initial_pool_state: PoolState | None = None,
        *,
        settle_to_cash: bool,
    ) -> SimResult:
        if initial_pool_state is not None:
            self.pool_state = initial_pool_state.copy()
        terminal_event: SwapEvent | V4Event | None = None
        opening_sample_recorded = bool(self.result.value_samples)
        for event in events:
            if self._apply_liquidity_event(event):
                continue
            assert isinstance(event, (SwapEvent, V4Event))
            active_liquidity, price_is_valid = self._observe_swap(event)
            if not price_is_valid:
                continue
            terminal_event = event
            if not opening_sample_recorded:
                self.result.value_samples.append((event.block_time, self.capital_usd))
                opening_sample_recorded = True
            self._roll_daily_return(event)
            self._accrue_position_fee(event, active_liquidity)
            exit_action = self.propose_exit(event, active_liquidity)
            if exit_action is not None:
                self.apply_action(exit_action)
            else:
                entry_action = self.propose_entry(event, active_liquidity)
                if entry_action is not None:
                    self.apply_action(entry_action)
            if self.idle_apr > 0 and self.previous_swap_time is not None:
                elapsed_years = (event.block_time - self.previous_swap_time).total_seconds() / (
                    365.25 * 86400
                )
                if elapsed_years > 0:
                    self.result.idle_hurdle_credit += (
                        self.wallet.value_usd(self.current_price) * self.idle_apr * elapsed_years
                    )
            self._record_event_value(event)
            self.previous_swap_time = event.block_time
        return self.finalize(
            settle_to_cash=settle_to_cash,
            terminal_event=terminal_event,
        )


def create_sleeve_runtime(
    *,
    sleeve_id: str,
    params: BacktestParams,
    pool_config: PoolConfig,
    capital_usd: float,
    sizing_policy: SizingPolicy | None = None,
    entry_eligibility: EntryEligibilityOverlay | None = None,
    idle_apr: float = 0.0,
) -> SleeveRuntime:
    return SleeveRuntime(
        sleeve_id=sleeve_id,
        params=params,
        pool_config=pool_config,
        capital_usd=capital_usd,
        sizing=sizing_policy if sizing_policy is not None else DeployFullWallet(),
        entry_eligibility=entry_eligibility,
        idle_apr=idle_apr,
    )
