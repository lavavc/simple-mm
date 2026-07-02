from datetime import datetime, timedelta, timezone

import pytest

from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import V4Event
from research.backtester.run import WindowSpec
from research.backtester.simulator import UNISWAP_BASE_POOL
from research.scripts.evaluate_frozen_family_lp import (
    frozen_paper_configs,
    hold_cngn_rows,
    no_position_rows,
    routed_hold_cngn_rows,
    static_lp_configs,
)


def test_frozen_paper_configs_match_handoff_family() -> None:
    configs = frozen_paper_configs(
        initial_capital_usd=1200.0,
        mint_gas_usd=0.073,
        remove_gas_usd=0.022,
    )
    config_by_name = {name: params for name, params in configs}

    assert len(configs) == 40
    assert "paper_spot_w0100_pt0050_sl-0025" in config_by_name
    params = config_by_name["paper_spot_w0100_pt0050_sl-0025"]
    assert params.strategy_mode == "paper"
    assert params.center_mode == "spot"
    assert params.fixed_width_pct == 0.01
    assert params.harvest_upward_range_fraction == 0.04
    assert params.exit_confirmation_swaps == 1
    assert params.exit_price_mode == "spot"
    assert params.out_of_range_overshoot_fraction == 0.0
    assert params.transaction_costs.mint_gas_usd == 0.073


def test_static_lp_configs_vary_width_only() -> None:
    configs = static_lp_configs(initial_capital_usd=450.0, mint_gas_usd=0.015, remove_gas_usd=0.015)

    assert [name for name, _params in configs] == [
        "static_spot_w0025",
        "static_spot_w0050",
        "static_spot_w0100",
        "static_spot_w0150",
        "static_spot_w0200",
    ]
    assert all(params.strategy_mode == "static" for _name, params in configs)
    assert all(params.harvest_upward_range_fraction is None for _name, params in configs)


def test_baseline_rows_are_window_level_comparators() -> None:
    entry_states = [
        {"window_index": "0", "validation_price_return": "0.015"},
        {"window_index": "1", "validation_price_return": "-0.020"},
    ]

    idle_rows = no_position_rows(entry_states)
    hold_rows = hold_cngn_rows(entry_states)

    assert idle_rows[0]["strategy"] == "no_position"
    assert idle_rows[0]["config"] == "idle_cash"
    assert idle_rows[0]["validation_net_return"] == "0"
    assert idle_rows[1]["validation_max_drawdown"] == "0"
    assert hold_rows[0]["strategy"] == "hold_cngn"
    assert hold_rows[0]["config"] == "mark_to_pool_sqrt_mid"
    assert hold_rows[0]["validation_net_return"] == "0.015"
    assert hold_rows[1]["validation_net_return"] == "-0.020"


def test_routed_hold_cngn_charges_entry_and_exit_costs() -> None:
    events = [
        _event(0, 0),
        _event(1, 0),
        _event(2, 0),
        _event(3, 100),
    ]
    spec = WindowSpec(
        mode="swap_count",
        train_swaps=2,
        val_swaps=2,
        stride_swaps=2,
        min_train_swaps=2,
        min_train_liquidity_events=0,
        min_val_swaps=2,
    )

    zero_cost_rows = routed_hold_cngn_rows(
        events,
        UNISWAP_BASE_POOL,
        spec,
        initial_capital_usd=1000.0,
        gas_cost_usd=0.0,
        max_windows=None,
    )
    costed_rows = routed_hold_cngn_rows(
        events,
        UNISWAP_BASE_POOL,
        spec,
        initial_capital_usd=1000.0,
        gas_cost_usd=1.0,
        max_windows=None,
    )

    assert len(zero_cost_rows) == 1
    assert zero_cost_rows[0]["strategy"] == "hold_cngn_routed"
    assert zero_cost_rows[0]["config"] == "entry_exit_pool_route"
    assert float(zero_cost_rows[0]["validation_net_return"]) == pytest.approx(
        float(UNISWAP_BASE_POOL.fee_rate) * -2 + _price_return_for_ticks(0, 100),
        abs=0.0001,
    )
    assert float(costed_rows[0]["validation_net_return"]) < float(
        zero_cost_rows[0]["validation_net_return"]
    )
    assert float(costed_rows[0]["validation_total_transaction_cost"]) > float(
        zero_cost_rows[0]["validation_total_transaction_cost"]
    )


def _event(minute: int, tick: int) -> V4Event:
    return V4Event(
        block_time=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minute),
        chain="base",
        pool_id=UNISWAP_BASE_POOL.pool_address,
        event_type="swap",
        tx_hash=f"0x{minute}",
        log_index=minute,
        block_number=minute,
        sqrt_price_x96=tick_to_sqrt_price_x96(tick),
        tick=tick,
        active_liquidity=10**18,
        fee_rate=UNISWAP_BASE_POOL.fee_rate,
        amount0=1.0,
        amount1=1.0,
        amount_usd=100.0,
        cngn_usd_price=0.0,
        token0_symbol="cNGN",
        token1_symbol="USDC",
    )


def _price_return_for_ticks(start_tick: int, end_tick: int) -> float:
    start = float(1.0001**start_tick)
    end = float(1.0001**end_tick)
    return end / start - 1.0
