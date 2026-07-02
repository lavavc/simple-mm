from datetime import datetime, timedelta, timezone

import pytest

from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import V4Event
from research.backtester.run import WindowSpec
from research.backtester.simulator import UNISWAP_BASE_POOL
from research.scripts.evaluate_flow_gated_lp import build_gate_summary_rows
from research.scripts.evaluate_frozen_family_lp import (
    build_lp_inventory_attribution_rows,
    frozen_paper_configs,
    hold_cngn_rows,
    no_position_rows,
    render_markdown,
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


def test_build_lp_inventory_attribution_rows_selects_best_active_comparators() -> None:
    entry_states = [
        _entry_state("0", gate_active="1", price_return="0.050000"),
        _entry_state("1", gate_active="0", price_return="-0.020000"),
    ]
    result_rows = [
        _result_row("0", "frozen_paper", "paper_best", "0.070000", fees="12", tx_cost="2"),
        _result_row("0", "frozen_paper", "paper_worse", "0.010000", fees="1", tx_cost="2"),
        _result_row("0", "passive_static_lp", "static_best", "0.060000", fees="8", tx_cost="2"),
        _result_row(
            "0",
            "passive_static_lp_closed",
            "static_closed_best",
            "0.040000",
            fees="8",
            tx_cost="4",
        ),
        _result_row("0", "hold_cngn", "mark_to_pool_sqrt_mid", "0.050000"),
        _result_row("0", "hold_cngn_routed", "entry_exit_pool_route", "0.030000", tx_cost="6"),
        _result_row("0", "no_position", "idle_cash", "0"),
        _result_row("1", "frozen_paper", "paper_best", "0.090000", fees="12", tx_cost="2"),
        _result_row("1", "frozen_paper", "paper_worse", "0.020000", fees="1", tx_cost="2"),
        _result_row("1", "passive_static_lp", "static_best", "0.080000", fees="8", tx_cost="2"),
        _result_row(
            "1",
            "passive_static_lp_closed",
            "static_closed_best",
            "0.070000",
            fees="8",
            tx_cost="4",
        ),
        _result_row("1", "hold_cngn", "mark_to_pool_sqrt_mid", "-0.020000"),
        _result_row("1", "hold_cngn_routed", "entry_exit_pool_route", "-0.030000", tx_cost="6"),
        _result_row("1", "no_position", "idle_cash", "0"),
    ]
    summary_rows = build_gate_summary_rows(
        result_rows,
        entry_states,
        gate_fields=("gate_strict_sign_cone",),
        identity_fields=("strategy", "config"),
    )

    rows = build_lp_inventory_attribution_rows(
        result_rows,
        entry_states,
        summary_rows,
        gate_field="gate_strict_sign_cone",
        initial_capital_usd=1000.0,
    )

    assert rows == [
        {
            "gate": "gate_strict_sign_cone",
            "window_index": "0",
            "window_start": "start-0",
            "window_end": "end-0",
            "entry_flow_pct": "0.91",
            "entry_flow_raw": "100",
            "train_price_return": "-0.010000",
            "validation_price_return": "0.050000",
            "entry_predicted_markout_20_25": "0.001",
            "entry_predicted_markout_100_25": "0.002",
            "lp_strategy": "frozen_paper",
            "lp_config": "paper_best",
            "static_mark_config": "static_best",
            "static_closed_config": "static_closed_best",
            "lp_net_return": "0.070000",
            "static_mark_net_return": "0.060000",
            "static_closed_net_return": "0.040000",
            "hold_mark_return": "0.050000",
            "hold_routed_return": "0.030000",
            "no_position_return": "0.000000",
            "lp_minus_hold_mark": "0.020000",
            "static_mark_minus_hold_mark": "0.010000",
            "static_closed_minus_hold_mark": "-0.010000",
            "lp_minus_static_mark": "0.010000",
            "lp_minus_static_closed": "0.030000",
            "static_closed_minus_hold_routed": "0.010000",
            "lp_total_fees_usd": "12.000000",
            "lp_total_transaction_cost_usd": "2.000000",
            "lp_fee_net_return_component": "0.010000",
            "lp_range_inventory_return_component": "0.060000",
            "static_closed_total_fees_usd": "8.000000",
            "static_closed_total_transaction_cost_usd": "4.000000",
            "static_closed_fee_net_return_component": "0.004000",
            "static_closed_range_inventory_return_component": "0.036000",
        }
    ]


def test_build_lp_inventory_attribution_rows_requires_every_comparator() -> None:
    entry_states = [_entry_state("0", gate_active="1", price_return="0.050000")]
    result_rows = [
        _result_row("0", "frozen_paper", "paper_best", "0.070000"),
        _result_row("0", "passive_static_lp", "static_best", "0.060000"),
        _result_row("0", "passive_static_lp_closed", "static_closed_best", "0.040000"),
        _result_row("0", "hold_cngn", "mark_to_pool_sqrt_mid", "0.050000"),
        _result_row("0", "no_position", "idle_cash", "0"),
    ]
    summary_rows = build_gate_summary_rows(
        result_rows,
        entry_states,
        gate_fields=("gate_strict_sign_cone",),
        identity_fields=("strategy", "config"),
    )

    with pytest.raises(ValueError, match="missing attribution comparator hold_cngn_routed"):
        build_lp_inventory_attribution_rows(
            result_rows,
            entry_states,
            summary_rows,
            gate_field="gate_strict_sign_cone",
            initial_capital_usd=1000.0,
        )


def test_render_markdown_includes_lp_inventory_attribution_section() -> None:
    markdown = render_markdown(
        pool="uni-base",
        summary_rows=[],
        attribution_rows=[
            {
                "window_index": "0",
                "lp_net_return": "0.070000",
                "hold_mark_return": "0.050000",
                "lp_minus_hold_mark": "0.020000",
                "static_closed_net_return": "0.040000",
                "static_closed_minus_hold_mark": "-0.010000",
                "hold_routed_return": "0.030000",
                "static_closed_minus_hold_routed": "0.010000",
                "lp_minus_static_mark": "0.010000",
                "lp_fee_net_return_component": "0.010000",
                "lp_range_inventory_return_component": "0.060000",
            }
        ],
    )

    assert "## LP-Versus-Inventory Attribution" in markdown
    assert (
        "| 0 | 0.070000 | 0.050000 | 0.020000 | 0.040000 | -0.010000 | "
        "0.030000 | 0.010000 | 0.010000 | 0.010000 | 0.060000 |"
    ) in markdown


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


def _entry_state(window_index: str, *, gate_active: str, price_return: str) -> dict[str, str]:
    return {
        "window_index": window_index,
        "validation_start_timestamp_ms": f"start-{window_index}",
        "validation_end_timestamp_ms": f"end-{window_index}",
        "gate_strict_sign_cone": gate_active,
        "validation_price_return": price_return,
        "entry_flow_pct": "0.91",
        "entry_flow_raw": "100",
        "train_price_return": "-0.010000",
        "entry_predicted_markout_20_25": "0.001",
        "entry_predicted_markout_100_25": "0.002",
    }


def _result_row(
    window_index: str,
    strategy: str,
    config: str,
    net_return: str,
    *,
    fees: str = "0",
    tx_cost: str = "0",
) -> dict[str, str]:
    return {
        "window_index": window_index,
        "window_start": f"start-{window_index}",
        "window_end": f"end-{window_index}",
        "strategy": strategy,
        "config": config,
        "validation_net_return": net_return,
        "validation_total_fees": fees,
        "validation_total_transaction_cost": tx_cost,
        "validation_rebalance_count": "0",
        "validation_max_drawdown": "0",
        "validation_final_value": "",
        "validation_fee_to_transaction_cost_ratio": "0",
    }
