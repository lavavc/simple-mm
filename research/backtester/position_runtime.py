"""Stateful runtime for a single backtest sleeve."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import BurnEvent, Event, MintEvent, SwapEvent, V4Event
from research.backtester.params import BacktestParams
from research.backtester.pool_state import PoolState
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
    _portfolio_value,
    _record_episode,
    _route_wallet_to_position,
    _snapshot_composition,
    _wallet_with_position_removed,
)
from research.backtester.sizing import DeployFullWallet, EntryContext, SizingPolicy
from research.backtester.strategy import EWMACalculator


@dataclass
class SleeveRuntime:
    sleeve_id: str
    params: BacktestParams
    pool_config: PoolConfig
    capital_usd: float
    sizing: SizingPolicy
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

    def run(self, events: list[Event], initial_pool_state: PoolState | None = None) -> SimResult:
        if initial_pool_state is not None:
            self.pool_state = initial_pool_state.copy()
        for event in events:
            if isinstance(event, MintEvent):
                self.pool_state.apply_mint(
                    event.tick_lower, event.tick_upper, event.liquidity_delta
                )
                continue
            if isinstance(event, BurnEvent):
                self.pool_state.apply_burn(
                    event.tick_lower, event.tick_upper, event.liquidity_delta
                )
                continue
            if isinstance(event, V4Event) and event.event_type == "mint":
                if (
                    event.tick_lower is not None
                    and event.tick_upper is not None
                    and event.liquidity_delta is not None
                ):
                    self.pool_state.apply_mint(
                        event.tick_lower, event.tick_upper, abs(event.liquidity_delta)
                    )
                continue
            if isinstance(event, V4Event) and event.event_type == "burn":
                if (
                    event.tick_lower is not None
                    and event.tick_upper is not None
                    and event.liquidity_delta is not None
                ):
                    self.pool_state.apply_burn(
                        event.tick_lower, event.tick_upper, abs(event.liquidity_delta)
                    )
                continue

            assert isinstance(event, (SwapEvent, V4Event))
            self.result.total_swaps += 1
            self.result.start_time = self.result.start_time or event.block_time
            self.result.end_time = event.block_time
            self.current_price = _event_cngn_price(event, self.pool_config)
            if self.current_price <= 0:
                continue

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

            if self.position is not None:
                self.position.observed_swaps += 1
                if self.position.is_in_range(self.current_tick):
                    self.result.in_range_swaps += 1
                    self.position.in_range_swaps += 1
                    if active_liquidity > 0:
                        # Recorded active_liquidity is the real pool's depth; our
                        # virtual position competes against it, so it joins the
                        # denominator. Without this, fee share is overstated
                        # exactly where positions are large relative to the pool.
                        liquidity_share = self.position.liquidity_L / (
                            active_liquidity + self.position.liquidity_L
                        )
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

                should_rebalance = False
                exit_reason: str | None = None
                exit_cost = TransactionCostBreakdown("exit")
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
                                (
                                    event.block_time - self.pending_defensive_exit_first_time
                                ).total_seconds()
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
                    should_rebalance = exit_reason is not None
                elif self.params.strategy_mode == "static":
                    should_rebalance = False
                else:
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
                            should_rebalance = True
                            exit_reason = "ewma_out_of_range"
                    elif self.params.preemptive_rebalance:
                        margin = tick_range * self.params.rebalance_threshold_pct / 100
                        if (
                            self.current_tick - self.position.tick_lower < margin
                            or self.position.tick_upper - self.current_tick < margin
                        ):
                            should_rebalance = True
                            exit_reason = "ewma_preemptive"

                if should_rebalance and self.ewma.ready:
                    position_value = self.position.value_at_sqrt_price_x96(
                        self.current_sqrt_price_x96,
                        self.current_price,
                        self.pool_config,
                    )
                    if exit_cost.total == 0.0:
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
                    _record_episode(
                        self.result,
                        self.position,
                        exit_reason or "rebalance",
                        event.block_time,
                        self.current_price,
                        self.current_tick,
                        position_value,
                        exit_cost,
                        self.pool_config,
                    )
                    self.wallet = _wallet_with_position_removed(
                        self.wallet,
                        self.position,
                        self.current_tick,
                        self.current_sqrt_price_x96,
                        self.current_price,
                        self.pool_config,
                    )
                    self.wallet = _pay_or_unwind_exit_cost(
                        self.wallet, exit_cost, self.current_price
                    )
                    self.result.total_rebalance_cost += exit_cost.total
                    self.result.rebalance_count += 1
                    if self.params.cooldown_minutes > 0:
                        self.cooldown_until_time = event.block_time + timedelta(
                            minutes=self.params.cooldown_minutes
                        )
                    if self.params.cooldown_blocks > 0 and _event_block_number(event) is not None:
                        self.cooldown_until_block = (
                            _event_block_number(event) or 0
                        ) + self.params.cooldown_blocks
                    self.position = None
                    self.pending_defensive_exit_reason = None
                    self.pending_defensive_exit_first_time = None
                    self.pending_defensive_exit_count = 0

            in_time_cooldown = (
                self.cooldown_until_time is not None and event.block_time < self.cooldown_until_time
            )
            event_block = _event_block_number(event)
            in_block_cooldown = (
                self.cooldown_until_block is not None
                and event_block is not None
                and event_block < self.cooldown_until_block
            )
            if (
                self.position is None
                and self.ewma.ready
                and self.wallet.value_usd(self.current_price) > self.params.gas_cost_usd
                and not in_time_cooldown
                and not in_block_cooldown
                and _entry_filters_pass(
                    event,
                    self.params,
                    self.ewma,
                    self.current_price,
                    active_liquidity,
                    self.pool_config,
                )
            ):
                wallet_value = self.wallet.value_usd(self.current_price)
                deploy_target = self.sizing.deployed_capital_usd(
                    EntryContext(
                        block_time=event.block_time,
                        wallet_value_usd=wallet_value,
                        current_price=self.current_price,
                        current_tick=self.current_tick,
                        active_liquidity=active_liquidity,
                    )
                )
                deploy_fraction = (
                    min(deploy_target / wallet_value, 1.0) if wallet_value > 0 else 0.0
                )
                if deploy_fraction > 0:
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
                    liquidity, deployed_capital, entry_cost, next_wallet = (
                        _route_wallet_to_position(
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
                    )
                    if liquidity > 0 and _expected_fee_apr_passes(
                        event,
                        self.params,
                        liquidity,
                        deployed_capital,
                        active_liquidity,
                        self.previous_swap_time,
                        self.pool_config,
                    ):
                        self.position = VirtualPosition(
                            tick_lower=tick_lower,
                            tick_upper=tick_upper,
                            liquidity_L=liquidity,
                            entry_price=self.current_price,
                            entry_value=deployed_capital,
                            entry_time=event.block_time,
                            entry_tick=self.current_tick,
                            entry_active_liquidity=active_liquidity,
                            deployed_capital=deployed_capital,
                            entry_transaction_cost=entry_cost,
                        )
                        self.wallet = PortfolioComposition(
                            stable_usd=next_wallet.stable_usd + idle_wallet.stable_usd,
                            cngn_amount=next_wallet.cngn_amount + idle_wallet.cngn_amount,
                        )

            if self.idle_apr > 0 and self.previous_swap_time is not None:
                elapsed_years = (event.block_time - self.previous_swap_time).total_seconds() / (
                    365.25 * 86400
                )
                if elapsed_years > 0:
                    self.result.idle_hurdle_credit += (
                        self.wallet.value_usd(self.current_price) * self.idle_apr * elapsed_years
                    )
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
            self.previous_swap_time = event.block_time

        if self.position is not None and self.result.end_time is not None:
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
        else:
            self.result.final_value = _portfolio_value(
                self.position,
                self.wallet,
                self.current_price,
                self.current_tick,
                self.current_sqrt_price_x96,
                self.pool_config,
            )
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


def create_sleeve_runtime(
    *,
    sleeve_id: str,
    params: BacktestParams,
    pool_config: PoolConfig,
    capital_usd: float,
    sizing_policy: SizingPolicy | None = None,
    idle_apr: float = 0.0,
) -> SleeveRuntime:
    return SleeveRuntime(
        sleeve_id=sleeve_id,
        params=params,
        pool_config=pool_config,
        capital_usd=capital_usd,
        sizing=sizing_policy if sizing_policy is not None else DeployFullWallet(),
        idle_apr=idle_apr,
    )
