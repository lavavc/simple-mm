"""Cash and cNGN hold comparators for the weighted-portfolio study."""

from __future__ import annotations

import math
from typing import Sequence

from research.backtester.data import BurnEvent, Event, MintEvent, SwapEvent, V4Event
from research.backtester.params import BacktestParams
from research.backtester.pool_state import PoolState
from research.backtester.portfolio_errors import NoValidationSwapError
from research.backtester.portfolio_path import (
    EconomicResult,
    signed_max_drawdown,
)
from research.backtester.position_runtime import TerminalLiquidationError
from research.backtester.simulator import (
    PoolConfig,
    PortfolioComposition,
    TransactionCostBreakdown,
    _event_cngn_price,
    _event_liquidity,
    _event_pool_fee_rate,
    _event_sqrt_price_x96,
    _pay_wallet_cost,
    _swap_cost_breakdown,
    _tick_from_cngn_price,
)


def valid_valuation_swaps(
    events: Sequence[Event],
    pool_config: PoolConfig,
) -> tuple[SwapEvent | V4Event, ...]:
    swaps = tuple(
        event
        for event in events
        if isinstance(event, (SwapEvent, V4Event))
        and (not isinstance(event, V4Event) or event.event_type == "swap")
        and _event_cngn_price(event, pool_config) > 0
    )
    if not swaps:
        raise NoValidationSwapError("window has no valid valuation swap")
    return swaps


def cash_comparator_result(
    events: Sequence[Event],
    pool_config: PoolConfig,
    opening_capital_usd: float,
) -> EconomicResult:
    swaps = valid_valuation_swaps(events, pool_config)
    samples = ((swaps[0].block_time, opening_capital_usd),) + tuple(
        (event.block_time, opening_capital_usd) for event in swaps
    ) + ((swaps[-1].block_time, opening_capital_usd),)
    return EconomicResult(
        opening_capital_usd=opening_capital_usd,
        closing_cash_usd=opening_capital_usd,
        max_drawdown=0.0,
        terminal_liquidation_cost_usd=0.0,
        total_fees_usd=0.0,
        total_fixed_cost_usd=0.0,
        total_variable_cost_usd=0.0,
        external_marked_notional_usd=0.0,
        external_input_value_usd=0.0,
        external_output_value_usd=0.0,
        internal_cross_notional_usd=0.0,
        value_samples=samples,
    )


def mark_hold_cngn_result(
    events: Sequence[Event],
    pool_config: PoolConfig,
    opening_capital_usd: float,
) -> EconomicResult:
    swaps = valid_valuation_swaps(events, pool_config)
    entry_price = _event_cngn_price(swaps[0], pool_config)
    values = tuple(
        opening_capital_usd * _event_cngn_price(event, pool_config) / entry_price
        for event in swaps
    )
    samples = ((swaps[0].block_time, opening_capital_usd),) + tuple(
        (event.block_time, value) for event, value in zip(swaps, values, strict=True)
    ) + ((swaps[-1].block_time, values[-1]),)
    return EconomicResult(
        opening_capital_usd=opening_capital_usd,
        closing_cash_usd=values[-1],
        max_drawdown=signed_max_drawdown(samples, opening_capital_usd),
        terminal_liquidation_cost_usd=0.0,
        total_fees_usd=0.0,
        total_fixed_cost_usd=0.0,
        total_variable_cost_usd=0.0,
        external_marked_notional_usd=0.0,
        external_input_value_usd=0.0,
        external_output_value_usd=0.0,
        internal_cross_notional_usd=0.0,
        value_samples=samples,
    )


def routed_hold_cngn_result(
    events: Sequence[Event],
    pool_config: PoolConfig,
    params: BacktestParams,
    opening_capital_usd: float,
    *,
    initial_pool_state: PoolState | None = None,
) -> EconomicResult:
    swap_states = _valuation_swap_states(events, pool_config, initial_pool_state)
    entry, entry_liquidity, entry_tick = swap_states[0]
    exit_event, exit_liquidity, exit_tick = swap_states[-1]
    swaps = tuple(item[0] for item in swap_states)
    entry_price = _event_cngn_price(entry, pool_config)

    def entry_cost(output_notional_usd: float) -> TransactionCostBreakdown:
        return _swap_cost_breakdown(
            "enter",
            output_notional_usd,
            entry_liquidity,
            _event_pool_fee_rate(entry, pool_config),
            entry_tick,
            params,
            direction="stable_to_cngn",
            current_price=entry_price,
            current_sqrt_price_x96=_event_sqrt_price_x96(entry, entry_tick),
            pool_config=pool_config,
            exact_output=True,
        )

    zero_cost = entry_cost(0.0)
    if zero_cost.total >= opening_capital_usd:
        raise TerminalLiquidationError("routed hold entry fixed cost exceeds capital")
    low = 0.0
    high = opening_capital_usd
    for _ in range(100):
        output = (low + high) / 2.0
        cost = entry_cost(output)
        if output + cost.total <= opening_capital_usd:
            low = output
        else:
            high = output
    output_notional = low
    entry_transaction_cost = entry_cost(output_notional)
    entry_fixed_cost = (
        entry_transaction_cost.gas_cost
        + entry_transaction_cost.failed_tx_expected_cost
    )
    entry_variable_cost = _variable_cost(entry_transaction_cost)
    stable_usd = opening_capital_usd - output_notional - entry_transaction_cost.total
    if stable_usd < -1e-8:
        raise TerminalLiquidationError("routed hold entry does not reconcile")
    stable_usd = max(stable_usd, 0.0)
    cngn_amount = output_notional / entry_price

    marked_values = tuple(
        stable_usd + cngn_amount * _event_cngn_price(event, pool_config)
        for event in swaps
    )
    pre_exit_wallet = PortfolioComposition(stable_usd, cngn_amount)
    exit_price = _event_cngn_price(exit_event, pool_config)
    exit_fixed = _swap_cost_breakdown(
        "exit",
        0.0,
        exit_liquidity,
        _event_pool_fee_rate(exit_event, pool_config),
        exit_tick,
        params,
    )
    exit_fixed_cost = exit_fixed.gas_cost + exit_fixed.failed_tx_expected_cost
    if pre_exit_wallet.value_usd(exit_price) + 1e-12 < exit_fixed_cost:
        raise TerminalLiquidationError("routed hold exit fixed cost exceeds capital")
    after_fixed = _pay_wallet_cost(pre_exit_wallet, exit_fixed_cost, exit_price)
    exit_notional = after_fixed.cngn_amount * exit_price
    if not math.isfinite(exit_notional) or exit_notional < 0.0:
        raise TerminalLiquidationError("routed hold exit notional is invalid")
    if exit_notional > 0.0:
        round_trip_cngn = exit_notional / exit_price
        if abs(round_trip_cngn - after_fixed.cngn_amount) > math.ulp(
            after_fixed.cngn_amount
        ):
            raise TerminalLiquidationError(
                "routed hold cannot represent the full-balance exit"
            )
    exit_transaction_cost = _swap_cost_breakdown(
        "exit",
        exit_notional,
        exit_liquidity,
        _event_pool_fee_rate(exit_event, pool_config),
        exit_tick,
        params,
        direction="cngn_to_stable" if exit_notional > 0 else None,
        current_price=exit_price,
        current_sqrt_price_x96=_event_sqrt_price_x96(
            exit_event,
            exit_tick,
        ),
        pool_config=pool_config,
        exact_output=False,
    )
    exit_variable_cost = _variable_cost(exit_transaction_cost)
    if not math.isfinite(exit_variable_cost) or exit_variable_cost < 0.0:
        raise TerminalLiquidationError("routed hold exit variable cost is invalid")
    if exit_variable_cost > exit_notional + 1e-12:
        raise TerminalLiquidationError("routed hold exit variable cost exceeds output")
    terminal_wallet = PortfolioComposition(
        stable_usd=(
            after_fixed.stable_usd
            + max(exit_notional - exit_variable_cost, 0.0)
        ),
        cngn_amount=0.0,
    )
    closing_cash = terminal_wallet.stable_usd
    samples = ((entry.block_time, opening_capital_usd),) + tuple(
        (event.block_time, value)
        for event, value in zip(swaps, marked_values, strict=True)
    ) + ((exit_event.block_time, closing_cash),)
    total_variable_cost = entry_variable_cost + exit_variable_cost
    external_input = output_notional + entry_variable_cost + exit_notional
    external_output = output_notional + exit_notional - exit_variable_cost
    result = EconomicResult(
        opening_capital_usd=opening_capital_usd,
        closing_cash_usd=closing_cash,
        max_drawdown=signed_max_drawdown(samples, opening_capital_usd),
        terminal_liquidation_cost_usd=exit_fixed_cost + exit_variable_cost,
        total_fees_usd=0.0,
        total_fixed_cost_usd=entry_fixed_cost + exit_fixed_cost,
        total_variable_cost_usd=total_variable_cost,
        external_marked_notional_usd=output_notional + exit_notional,
        external_input_value_usd=external_input,
        external_output_value_usd=external_output,
        internal_cross_notional_usd=0.0,
        value_samples=samples,
    )
    if not math.isfinite(result.closing_cash_usd):
        raise TerminalLiquidationError("routed hold closing cash is not finite")
    return result


def _valuation_swap_states(
    events: Sequence[Event],
    pool_config: PoolConfig,
    initial_pool_state: PoolState | None,
) -> tuple[tuple[SwapEvent | V4Event, int, int], ...]:
    pool_state = initial_pool_state.copy() if initial_pool_state is not None else PoolState()
    states: list[tuple[SwapEvent | V4Event, int, int]] = []
    for event in events:
        if isinstance(event, MintEvent):
            pool_state.apply_mint(
                event.tick_lower,
                event.tick_upper,
                event.liquidity_delta,
            )
            continue
        if isinstance(event, BurnEvent):
            pool_state.apply_burn(
                event.tick_lower,
                event.tick_upper,
                event.liquidity_delta,
            )
            continue
        if isinstance(event, V4Event) and event.event_type in {"mint", "burn"}:
            if (
                event.tick_lower is not None
                and event.tick_upper is not None
                and event.liquidity_delta is not None
            ):
                liquidity_delta = abs(event.liquidity_delta)
                if event.event_type == "mint":
                    pool_state.apply_mint(
                        event.tick_lower,
                        event.tick_upper,
                        liquidity_delta,
                    )
                else:
                    pool_state.apply_burn(
                        event.tick_lower,
                        event.tick_upper,
                        liquidity_delta,
                    )
            continue
        if not isinstance(event, (SwapEvent, V4Event)):
            continue
        if _event_cngn_price(event, pool_config) <= 0:
            continue
        tick = (
            event.tick
            if isinstance(event, V4Event)
            else _tick_from_cngn_price(event.cngn_usd_price, pool_config)
        )
        states.append((event, _event_liquidity(event, pool_state, tick), tick))
    if not states:
        raise NoValidationSwapError("window has no valid valuation swap")
    return tuple(states)


def _variable_cost(cost: TransactionCostBreakdown) -> float:
    return (
        cost.swap_fee_cost
        + cost.price_impact_cost
        + cost.slippage_cost
        + cost.latency_slippage_cost
    )
