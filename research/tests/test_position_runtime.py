from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import Event, V4Event
from research.backtester.params import BacktestParams, TransactionCostModel
from research.backtester.position_runtime import create_sleeve_runtime
from research.backtester.simulator import UNISWAP_BASE_POOL, simulate_pool


def _swap(*, minute: int, tick: int, amount0: float, amount1: float) -> V4Event:
    return V4Event(
        block_time=datetime(2026, 1, 1) + timedelta(minutes=minute),
        chain="base",
        pool_id=UNISWAP_BASE_POOL.pool_address,
        event_type="swap",
        tx_hash=f"0x{minute:064x}",
        log_index=0,
        block_number=100 + minute,
        sqrt_price_x96=tick_to_sqrt_price_x96(tick),
        tick=tick,
        active_liquidity=10**15,
        fee_rate=UNISWAP_BASE_POOL.fee_rate,
        amount0=amount0,
        amount1=amount1,
        amount_usd=1_000.0,
        cngn_usd_price=1.0001**tick,
        token0_symbol="cNGN",
        token1_symbol="USDC",
    )


def _events_that_enter_accrue_fee_exit_and_reenter() -> list[Event]:
    return [
        _swap(minute=0, tick=0, amount0=-1_000_000_000, amount1=1_000_000_000),
        _swap(minute=1, tick=1, amount0=-1_000_000_000, amount1=1_000_000_000),
        _swap(minute=2, tick=2, amount0=-1_000_000_000, amount1=1_000_000_000),
        _swap(minute=3, tick=-60, amount0=1_000_000_000, amount1=-1_000_000_000),
        _swap(minute=4, tick=-59, amount0=-1_000_000_000, amount1=1_000_000_000),
    ]


def _paper_params_with_zero_gas() -> BacktestParams:
    return BacktestParams(
        strategy_mode="paper",
        range_mode="fixed_tick_width",
        center_mode="spot",
        fixed_tick_width=50,
        stop_loss_return=None,
        downward_range_fraction=0.5,
        gas_cost_usd=0.0,
        transaction_costs=replace(
            TransactionCostModel(),
            mint_gas_usd=0.0,
            remove_gas_usd=0.0,
            fallback_price_impact_bps=0.0,
        ),
    )


def test_runtime_matches_simulate_pool_for_complete_lifecycle() -> None:
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    params = _paper_params_with_zero_gas()
    expected = simulate_pool(events, params, UNISWAP_BASE_POOL, 500.0)
    assert expected.total_fees > 0
    assert expected.rebalance_count == 1
    assert len(expected.episodes) == 2

    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=params,
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )
    actual = runtime.run(events)

    assert actual == expected


def test_runtime_exposes_ordered_phases_and_preserves_complete_lifecycle() -> None:
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    params = _paper_params_with_zero_gas()
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=params,
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )

    for method_name in (
        "_apply_liquidity_event",
        "_observe_swap",
        "_roll_daily_return",
        "_accrue_position_fee",
        "_exit_decision",
        "_apply_exit",
        "_maybe_enter",
        "_record_event_value",
        "finalize",
    ):
        assert callable(getattr(runtime, method_name, None)), method_name

    assert runtime.run(events) == simulate_pool(events, params, UNISWAP_BASE_POOL, 500.0)


def test_exit_decision_does_not_mutate_wallet_or_position() -> None:
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    params = _paper_params_with_zero_gas()
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=params,
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )
    runtime.run(events[:3])
    assert runtime.position is not None

    exit_event = events[3]
    assert isinstance(exit_event, V4Event)
    active_liquidity, price_is_valid = runtime._observe_swap(exit_event)
    assert price_is_valid
    wallet_before = deepcopy(runtime.wallet)
    position_before = deepcopy(runtime.position)

    exit_reason, exit_cost = runtime._exit_decision(exit_event, active_liquidity)

    assert exit_reason is not None
    assert exit_cost.action == "exit"
    assert runtime.wallet == wallet_before
    assert runtime.position == position_before
