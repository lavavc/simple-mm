from decimal import Decimal

import pytest

from research.scripts.evaluate_directional_paper_lp import (
    ReferencePricePoint,
    build_directional_attribution_rows,
    directional_archetype_configs,
    directional_policy_profiles,
    external_reference_hold_cngn_rows,
    load_reference_price_csv,
    render_markdown,
    route_directional_archetype,
    route_directional_windows,
)
from research.scripts.evaluate_flow_gated_lp import build_gate_summary_rows


def test_directional_archetype_configs_use_asymmetric_existing_controls() -> None:
    configs = directional_archetype_configs(
        initial_capital_usd=1200.0,
        mint_gas_usd=0.073,
        remove_gas_usd=0.022,
    )
    profiles = directional_policy_profiles(configs)
    balanced = profiles["balanced_v1"]

    assert len(configs) == 15
    assert set(profiles) == {
        "balanced_v1",
        "upside_wide_v1",
        "upside_tight_v1",
        "dip_wide_v1",
        "fee_tight_v1",
    }
    assert all(
        set(profile_configs) == {"upside_capture", "dip_accumulator", "fee_box"}
        for profile_configs in profiles.values()
    )

    upside = balanced["upside_capture"].params
    assert balanced["upside_capture"].name == "upside_capture_balanced_v1"
    assert upside.strategy_mode == "paper"
    assert upside.center_mode == "spot"
    assert upside.center_offset_pct == 0.0025
    assert upside.upper_width_pct == 0.015
    assert upside.lower_width_pct == 0.0025
    assert upside.profit_take_pnl_mode == "fees"
    assert upside.transaction_costs.mint_gas_usd == 0.073
    assert upside.transaction_costs.remove_gas_usd == 0.022

    dip = balanced["dip_accumulator"].params
    assert dip.center_offset_pct == -0.0025
    assert dip.lower_width_pct == 0.015
    assert dip.upper_width_pct == 0.0025
    assert dip.downward_range_fraction == 0.80

    fee_box = balanced["fee_box"].params
    assert fee_box.center_offset_pct == 0.0
    assert fee_box.lower_width_pct == 0.0025
    assert fee_box.upper_width_pct == 0.0025
    assert fee_box.fixed_width_pct == 0.005


def test_route_directional_archetype_uses_causal_entry_state_rules() -> None:
    assert route_directional_archetype(
        _entry_state(
            gate_strict_sign_cone="1",
            predicted_20="0.001",
            predicted_100="-0.001",
        )
    ) == ("upside_capture", "strict_sign_cone_positive_qts")

    assert route_directional_archetype(
        _entry_state(
            gate_strict_sign_cone="0",
            train_price_return="-0.010",
            entry_flow_pct="0.80",
            predicted_20="0",
        )
    ) == ("dip_accumulator", "flat_down_train_high_flow_or_fee_nonnegative_qts")

    assert route_directional_archetype(
        _entry_state(
            gate_strict_sign_cone="0",
            train_price_return="0.010",
            fee_intensity_pct="0.91",
            volume_pct="0.88",
            predicted_20="0.0002",
        )
    ) == ("fee_box", "high_fee_high_volume_near_flat_qts")

    assert route_directional_archetype(
        _entry_state(
            gate_strict_sign_cone="0",
            train_price_return="0.020",
            entry_flow_pct="0.10",
            fee_intensity_pct="0.10",
            volume_pct="0.10",
            predicted_20="-0.005",
        )
    ) == ("no_position", "no_directional_rule")


def test_route_directional_windows_marks_no_position_fallback_inactive() -> None:
    rows = route_directional_windows(
        [
            _entry_state("0", gate_strict_sign_cone="1", predicted_20="0.001"),
            _entry_state("1", gate_strict_sign_cone="0", predicted_20="-0.005"),
        ]
    )

    assert rows[0]["window_index"] == "0"
    assert rows[0]["directional_archetype"] == "upside_capture"
    assert rows[0]["gate_directional_active"] == "1"
    assert rows[1]["window_index"] == "1"
    assert rows[1]["directional_archetype"] == "no_position"
    assert rows[1]["gate_directional_active"] == "0"


def test_directional_attribution_rows_separate_static_hold_and_down_window_deltas() -> None:
    entry_states = [
        _entry_state(
            "0",
            validation_price_return="-0.020000",
            directional_archetype="dip_accumulator",
            directional_route_reason="flat_down_train_high_flow_or_fee_nonnegative_qts",
            gate_directional_active="1",
        ),
        _entry_state(
            "1",
            validation_price_return="0.010000",
            directional_archetype="no_position",
            directional_route_reason="no_directional_rule",
            gate_directional_active="0",
        ),
    ]
    result_rows = [
        _result_row("0", "directional_paper_lp", "rule_v1", "0.030000", fees="12", tx_cost="2"),
        _result_row("0", "passive_static_lp", "static_spot_w0100", "0.010000", fees="8"),
        _result_row("0", "hold_cngn", "mark_to_pool_sqrt_mid", "-0.020000"),
        _result_row("0", "no_position", "idle_cash", "0"),
        _result_row("1", "directional_paper_lp", "rule_v1", "0"),
        _result_row("1", "passive_static_lp", "static_spot_w0100", "0.020000", fees="8"),
        _result_row("1", "hold_cngn", "mark_to_pool_sqrt_mid", "0.010000"),
        _result_row("1", "no_position", "idle_cash", "0"),
    ]
    summary_rows = build_gate_summary_rows(
        result_rows,
        entry_states,
        gate_fields=("gate_directional_active",),
        identity_fields=("strategy", "config"),
    )

    rows = build_directional_attribution_rows(
        result_rows,
        entry_states,
        summary_rows,
        gate_field="gate_directional_active",
        initial_capital_usd=1000.0,
    )

    assert rows == [
        {
            "gate": "gate_directional_active",
            "window_index": "0",
            "directional_archetype": "dip_accumulator",
            "directional_route_reason": "flat_down_train_high_flow_or_fee_nonnegative_qts",
            "directional_config": "rule_v1",
            "static_config": "static_spot_w0100",
            "validation_price_return": "-0.020000",
            "down_window": "1",
            "directional_net_return": "0.030000",
            "static_net_return": "0.010000",
            "hold_mark_return": "-0.020000",
            "hold_external_config": "",
            "hold_external_return": "",
            "no_position_return": "0.000000",
            "directional_minus_static": "0.020000",
            "directional_minus_hold_mark": "0.050000",
            "directional_minus_hold_external": "",
            "directional_minus_no_position": "0.030000",
            "down_window_directional_minus_static": "0.020000",
            "down_window_directional_minus_hold_mark": "0.050000",
            "directional_total_fees_usd": "12.000000",
            "directional_total_transaction_cost_usd": "2.000000",
            "directional_fee_net_return_component": "0.010000",
            "directional_range_inventory_return_component": "0.020000",
        }
    ]


def test_external_reference_hold_cngn_rows_use_previous_or_equal_reference_prices() -> None:
    entry_states = [
        _entry_state(
            "0",
            validation_start_timestamp_ms="1000",
            validation_end_timestamp_ms="4000",
        )
    ]
    reference_prices = [
        ReferencePricePoint(timestamp_ms=900, price=Decimal("1000")),
        ReferencePricePoint(timestamp_ms=2000, price=Decimal("9999")),
        ReferencePricePoint(timestamp_ms=3000, price=Decimal("1100")),
    ]

    rows = external_reference_hold_cngn_rows(
        entry_states,
        reference_prices,
        source_name="binance_reference",
        max_age_ms=1500,
    )

    assert rows == [
        {
            "window_index": "0",
            "window_start": "1000",
            "window_end": "4000",
            "strategy": "hold_cngn_external",
            "config": "mark_to_binance_reference",
            "archetype": "",
            "route_reason": "",
            "center_offset_pct": "",
            "lower_width_pct": "",
            "upper_width_pct": "",
            "profit_take_pnl_mode": "",
            "downward_range_fraction": "",
            "validation_net_return": "0.1",
            "validation_total_fees": "0",
            "validation_total_transaction_cost": "0",
            "validation_rebalance_count": "0",
            "validation_max_drawdown": "0",
            "validation_final_value": "",
            "validation_fee_to_transaction_cost_ratio": "0",
        }
    ]


def test_external_reference_hold_cngn_rows_fail_when_reference_is_stale() -> None:
    entry_states = [
        _entry_state(
            "0",
            validation_start_timestamp_ms="1000",
            validation_end_timestamp_ms="4000",
        )
    ]

    with pytest.raises(ValueError, match="missing external reference price"):
        external_reference_hold_cngn_rows(
            entry_states,
            [ReferencePricePoint(timestamp_ms=0, price=Decimal("1000"))],
            source_name="binance_reference",
            max_age_ms=500,
        )


def test_load_reference_price_csv_accepts_common_binance_reference_fields(tmp_path) -> None:
    path = tmp_path / "binance_reference.csv"
    path.write_text("timestamp_ms,reference_price\n1000,1395.5\n2000,1397\n")

    assert load_reference_price_csv(path) == [
        ReferencePricePoint(timestamp_ms=1000, price=Decimal("1395.5")),
        ReferencePricePoint(timestamp_ms=2000, price=Decimal("1397")),
    ]


def test_directional_attribution_rows_include_external_hold_when_available() -> None:
    entry_states = [
        _entry_state(
            "0",
            validation_price_return="0.010000",
            directional_archetype="upside_capture",
            directional_route_reason="strict_sign_cone_positive_qts",
            gate_directional_active="1",
        )
    ]
    result_rows = [
        _result_row("0", "directional_paper_lp", "rule_v1", "0.030000", fees="12", tx_cost="2"),
        _result_row("0", "passive_static_lp", "static_spot_w0100", "0.010000", fees="8"),
        _result_row("0", "hold_cngn", "mark_to_pool_sqrt_mid", "0.010000"),
        _result_row("0", "hold_cngn_external", "mark_to_binance_reference", "0.080000"),
        _result_row("0", "no_position", "idle_cash", "0"),
    ]
    summary_rows = build_gate_summary_rows(
        result_rows,
        entry_states,
        gate_fields=("gate_directional_active",),
        identity_fields=("strategy", "config"),
    )

    rows = build_directional_attribution_rows(
        result_rows,
        entry_states,
        summary_rows,
        gate_field="gate_directional_active",
        initial_capital_usd=1000.0,
    )

    assert rows[0]["hold_external_config"] == "mark_to_binance_reference"
    assert rows[0]["hold_external_return"] == "0.080000"
    assert rows[0]["directional_minus_hold_external"] == "-0.050000"


def test_directional_attribution_requires_hold_mark_comparator() -> None:
    entry_states = [
        _entry_state(
            "0",
            directional_archetype="upside_capture",
            directional_route_reason="strict_sign_cone_positive_qts",
            gate_directional_active="1",
        )
    ]
    result_rows = [
        _result_row("0", "directional_paper_lp", "rule_v1", "0.030000"),
        _result_row("0", "passive_static_lp", "static_spot_w0100", "0.010000"),
        _result_row("0", "no_position", "idle_cash", "0"),
    ]
    summary_rows = build_gate_summary_rows(
        result_rows,
        entry_states,
        gate_fields=("gate_directional_active",),
        identity_fields=("strategy", "config"),
    )

    with pytest.raises(ValueError, match="missing attribution comparator hold_cngn"):
        build_directional_attribution_rows(
            result_rows,
            entry_states,
            summary_rows,
            gate_field="gate_directional_active",
            initial_capital_usd=1000.0,
        )


def test_render_markdown_groups_gate_summaries_so_directional_active_is_visible() -> None:
    markdown = render_markdown(
        pool="uni-base",
        entry_states=[
            _entry_state(
                "0",
                directional_archetype="upside_capture",
                gate_directional_active="1",
            )
        ],
        summary_rows=[
            _summary_row("gate_train_flat_qts_20_25", "hold_cngn", "mark_to_pool_sqrt_mid"),
            _summary_row("gate_directional_active", "directional_paper_lp", "rule_v1"),
        ],
        attribution_rows=[],
    )

    assert "## gate_directional_active" in markdown
    assert "| directional_paper_lp | rule_v1 | 1 | 0.010000 |" in markdown


def _entry_state(
    window_index: str = "0",
    *,
    validation_start_timestamp_ms: str | None = None,
    validation_end_timestamp_ms: str | None = None,
    gate_strict_sign_cone: str = "0",
    train_price_return: str = "0.010",
    validation_price_return: str = "0.010000",
    entry_flow_pct: str = "0.10",
    fee_intensity_pct: str = "0.10",
    volume_pct: str = "0.10",
    predicted_20: str = "",
    predicted_100: str = "",
    directional_archetype: str = "",
    directional_route_reason: str = "",
    gate_directional_active: str = "0",
) -> dict[str, str]:
    return {
        "window_index": window_index,
        "validation_start_timestamp_ms": validation_start_timestamp_ms or f"start-{window_index}",
        "validation_end_timestamp_ms": validation_end_timestamp_ms or f"end-{window_index}",
        "gate_strict_sign_cone": gate_strict_sign_cone,
        "train_price_return": train_price_return,
        "validation_price_return": validation_price_return,
        "entry_flow_pct": entry_flow_pct,
        "entry_fee_intensity_proxy_cone_pct_1h": fee_intensity_pct,
        "entry_volume_cone_pct_1h": volume_pct,
        "entry_predicted_markout_20_25": predicted_20,
        "entry_predicted_markout_100_25": predicted_100,
        "directional_archetype": directional_archetype,
        "directional_route_reason": directional_route_reason,
        "gate_directional_active": gate_directional_active,
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
        "strategy": strategy,
        "config": config,
        "validation_net_return": net_return,
        "validation_total_fees": fees,
        "validation_total_transaction_cost": tx_cost,
        "validation_rebalance_count": "0",
        "validation_max_drawdown": "0",
    }


def _summary_row(gate: str, strategy: str, config: str) -> dict[str, str]:
    return {
        "gate": gate,
        "strategy": strategy,
        "config": config,
        "active_windows": "1",
        "sum_active_return": "0.010000",
        "mean_all_window_return": "0.010000",
        "worst_active_return": "0.010000",
        "positive_active_rate": "1.000000",
        "fee_cost_ratio": "2.000000",
        "leave_one_active_window_out_min_return": "not_available",
        "active_minus_hold_cngn": "0.001000",
    }
