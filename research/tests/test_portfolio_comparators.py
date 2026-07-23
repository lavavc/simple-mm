from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

import research.backtester.portfolio_comparators as portfolio_comparators
from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import BurnEvent, SwapEvent, V4Event
from research.backtester.params import BacktestParams, TransactionCostModel
from research.backtester.pool_state import PoolState
from research.backtester.portfolio_comparators import (
    cash_comparator_result,
    mark_hold_cngn_result,
    routed_hold_cngn_result,
)
from research.backtester.simulator import UNISWAP_BASE_POOL, TransactionCostBreakdown


def _swap(minute: int, tick: int) -> V4Event:
    return V4Event(
        block_time=datetime(2026, 1, 1) + timedelta(minutes=minute),
        chain="base",
        pool_id=UNISWAP_BASE_POOL.pool_address,
        event_type="swap",
        tx_hash=f"0x{minute:064x}",
        log_index=0,
        block_number=minute + 1,
        sqrt_price_x96=tick_to_sqrt_price_x96(tick),
        tick=tick,
        active_liquidity=10**15,
        fee_rate=UNISWAP_BASE_POOL.fee_rate,
        amount0=-1_000.0,
        amount1=1_000.0,
        amount_usd=1_000.0,
        cngn_usd_price=1.0001**tick,
        token0_symbol="cNGN",
        token1_symbol="USDC",
    )


def test_cash_and_mark_hold_paths_include_opening_events_and_terminal() -> None:
    events = [_swap(0, 0), _swap(1, 100), _swap(2, -100)]

    cash = cash_comparator_result(events, UNISWAP_BASE_POOL, 100.0)
    mark = mark_hold_cngn_result(events, UNISWAP_BASE_POOL, 100.0)

    assert len(cash.value_samples) == len(events) + 2
    assert cash.closing_cash_usd == 100.0
    assert len(mark.value_samples) == len(events) + 2
    assert mark.closing_cash_usd == pytest.approx(100.0 * 1.0001**-100)
    assert mark.max_drawdown < 0


def test_routed_hold_charges_entry_and_terminal_execution_once() -> None:
    events = [_swap(0, 0), _swap(1, 50), _swap(2, 100)]
    params = BacktestParams(
        gas_cost_usd=0.0,
        transaction_costs=replace(
            TransactionCostModel(),
            mint_gas_usd=1.0,
            remove_gas_usd=1.0,
            swap_slippage_bps=0.0,
            latency_slippage_bps=0.0,
        ),
    )

    result = routed_hold_cngn_result(
        events,
        UNISWAP_BASE_POOL,
        params,
        100.0,
    )

    assert result.total_fixed_cost_usd == pytest.approx(2.0)
    assert result.total_variable_cost_usd > 0
    assert result.terminal_liquidation_cost_usd > 1.0
    assert result.external_input_value_usd - result.external_output_value_usd == pytest.approx(
        result.total_variable_cost_usd
    )
    assert result.value_samples[-1][1] == pytest.approx(result.closing_cash_usd)


def test_routed_hold_replays_legacy_liquidity_from_the_seeded_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 1, 1)
    entry = SwapEvent(
        block_time=start,
        blockchain="base",
        pool_address=UNISWAP_BASE_POOL.pool_address,
        amount_usd=100.0,
        token_bought_symbol="cNGN",
        token_bought_amount=100.0,
        token_sold_symbol="USDC",
        token_sold_amount=100.0,
        cngn_usd_price=1.0,
    )
    burn = BurnEvent(
        block_time=start + timedelta(minutes=1),
        blockchain="base",
        pool_address=UNISWAP_BASE_POOL.pool_address,
        tick_lower=-100,
        tick_upper=100,
        liquidity_delta=400,
        amount0=0.0,
        amount1=0.0,
    )
    exit_event = replace(entry, block_time=start + timedelta(minutes=2))
    state = PoolState()
    state.apply_mint(-100, 100, 1_000)
    observed_depth: list[float] = []

    def fake_cost(
        action: str,
        notional: float,
        active_liquidity: float,
        *args: object,
        **kwargs: object,
    ) -> TransactionCostBreakdown:
        observed_depth.append(active_liquidity)
        return TransactionCostBreakdown(action, swap_notional_usd=notional)

    monkeypatch.setattr(portfolio_comparators, "_swap_cost_breakdown", fake_cost)

    routed_hold_cngn_result(
        [entry, burn, exit_event],
        UNISWAP_BASE_POOL,
        BacktestParams(gas_cost_usd=0.0),
        100.0,
        initial_pool_state=state,
    )

    assert observed_depth[0] == 1_000
    assert observed_depth[-2:] == [600, 600]


def test_routed_hold_semantically_consumes_one_ulp_full_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_price = 2.8956003012121686e-06
    exit_price = 2.895600301212169e-06
    opening_capital = 1_277_572_036.82602

    def zero_cost(
        action: str,
        notional: float,
        *args: object,
        **kwargs: object,
    ) -> TransactionCostBreakdown:
        return TransactionCostBreakdown(
            action,
            gas_cost=1.0 if action == "enter" else 0.0,
            swap_notional_usd=notional,
        )

    monkeypatch.setattr(
        portfolio_comparators,
        "_event_cngn_price",
        lambda event, pool_config: (
            entry_price if event.block_number == 1 else exit_price
        ),
    )
    monkeypatch.setattr(
        portfolio_comparators,
        "_swap_cost_breakdown",
        zero_cost,
    )

    result = routed_hold_cngn_result(
        [_swap(0, 0), _swap(1, 0)],
        UNISWAP_BASE_POOL,
        BacktestParams(gas_cost_usd=0.0),
        opening_capital,
    )

    assert result.closing_cash_usd == pytest.approx(
        (opening_capital - 1.0) * exit_price / entry_price
    )
