from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta

import pytest

from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import V4Event
from research.backtester.params import BacktestParams, TransactionCostModel
from research.backtester.pool_state import PoolState
from research.backtester.portfolio_allocation import Allocation
from research.backtester.portfolio_catalog import SleeveDefinition, parameter_fingerprint
from research.backtester.portfolio_simulator import (
    LiquidityShareExceeded,
    _net_actions,
    simulate_portfolio,
)
from research.backtester.position_runtime import SleeveAction
from research.backtester.simulator import (
    UNISWAP_BASE_POOL,
    PortfolioComposition,
    TransactionCostBreakdown,
)


def _params(*, max_share: float | None = None) -> BacktestParams:
    return BacktestParams(
        strategy_mode="static",
        range_mode="fixed_tick_width",
        center_mode="spot",
        fixed_tick_width=100,
        max_active_liquidity_share=max_share,
        gas_cost_usd=0.0,
        transaction_costs=replace(
            TransactionCostModel(),
            mint_gas_usd=0.0,
            remove_gas_usd=0.0,
            fallback_price_impact_bps=0.0,
        ),
    )


def _sleeve(sleeve_id: str, params: BacktestParams) -> SleeveDefinition:
    payload = json.dumps(asdict(params), allow_nan=False, separators=(",", ":"), sort_keys=True)
    return SleeveDefinition(
        sleeve_id=sleeve_id,
        family="static",
        config_name=sleeve_id,
        parameter_payload=payload,
        parameter_fingerprint=parameter_fingerprint(params),
        source_constructor="test",
    )


def _swap(minute: int, *, liquidity: int = 10**15, amount_usd: float = 1_000.0) -> V4Event:
    return V4Event(
        block_time=datetime(2026, 1, 1) + timedelta(minutes=minute),
        chain="base",
        pool_id=UNISWAP_BASE_POOL.pool_address,
        event_type="swap",
        tx_hash=f"0x{minute:064x}",
        log_index=0,
        block_number=100 + minute,
        sqrt_price_x96=tick_to_sqrt_price_x96(0),
        tick=0,
        active_liquidity=liquidity,
        fee_rate=UNISWAP_BASE_POOL.fee_rate,
        amount0=-amount_usd,
        amount1=amount_usd,
        amount_usd=amount_usd,
        cngn_usd_price=1.0,
        token0_symbol="cNGN",
        token1_symbol="USDC",
    )


def test_overlapping_positions_split_one_fee_pool() -> None:
    sleeves = (_sleeve("a", _params()), _sleeve("b", _params()))
    fee_swap = _swap(2)
    result = simulate_portfolio(
        events=[_swap(0), _swap(1), fee_swap],
        sleeves=sleeves,
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        initial_pool_state=PoolState(),
    )

    l_a = result.attribution["a"].liquidity_at(fee_swap.block_time)
    l_b = result.attribution["b"].liquidity_at(fee_swap.block_time)
    total_fee = fee_swap.amount_usd * fee_swap.fee_rate
    denominator = fee_swap.active_liquidity + l_a + l_b
    assert result.attribution["a"].fees_usd == pytest.approx(total_fee * l_a / denominator)
    assert result.attribution["b"].fees_usd == pytest.approx(total_fee * l_b / denominator)
    assert result.total_fees <= total_fee


def test_opposing_inventory_actions_are_netted() -> None:
    event = _swap(2)
    common = dict(
        kind="enter",
        event=event,
        active_liquidity=event.active_liquidity,
        cngn_usd_price=1.0,
        wallet_before=PortfolioComposition(100.0, 100.0),
        wallet_after=PortfolioComposition(100.0, 100.0),
        position_before=None,
        position_after=None,
        transaction_cost=TransactionCostBreakdown("enter"),
        reason=None,
    )
    actions = [
        SleeveAction(
            sleeve_id="buy",
            inventory_swap_direction="stable_to_cngn",
            inventory_swap_notional_usd=100.0,
            **common,
        ),
        SleeveAction(
            sleeve_id="sell",
            inventory_swap_direction="cngn_to_stable",
            inventory_swap_notional_usd=60.0,
            **common,
        ),
    ]

    execution = _net_actions(actions)

    assert execution.external_notional_usd == pytest.approx(40.0)
    assert execution.internal_notional_usd == pytest.approx(60.0)
    assert execution.direction == "stable_to_cngn"


def test_aggregate_share_fails_even_when_each_sleeve_is_below_cap() -> None:
    sleeves = (_sleeve("a", _params(max_share=0.08)), _sleeve("b", _params(max_share=0.08)))
    with pytest.raises(LiquidityShareExceeded, match="aggregate synthetic"):
        simulate_portfolio(
            events=[_swap(0, liquidity=10**9), _swap(1, liquidity=10**9)],
            sleeves=sleeves,
            allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=500.0,
            initial_pool_state=PoolState(),
        )


def test_portfolio_and_sleeve_values_reconcile_exactly() -> None:
    sleeves = (_sleeve("a", _params()), _sleeve("b", _params()))
    result = simulate_portfolio(
        events=[_swap(0), _swap(1), _swap(2)],
        sleeves=sleeves,
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        initial_pool_state=PoolState(),
    )

    assert result.final_value == pytest.approx(
        result.cash_value + sum(a.final_value for a in result.attribution.values())
    )
    for index, (_, portfolio_value) in enumerate(result.value_samples):
        attributed = sum(a.value_samples[index][1] for a in result.attribution.values())
        assert portfolio_value == pytest.approx(result.cash_value + attributed)
