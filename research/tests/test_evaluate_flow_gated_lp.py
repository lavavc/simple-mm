from decimal import Decimal

import pytest

from research.scripts.evaluate_flow_gated_lp import (
    build_entry_states,
    build_gate_summary_rows,
    summarize_candidate_family,
    summarize_selected_rank1,
)


def test_entry_state_uses_last_train_swap_not_first_validation() -> None:
    feature_rows = [
        _feature_row(1_700_000_000_000, "100", "0.10"),
        _feature_row(1_700_000_001_000, "99", "1.00"),
        _feature_row(1_700_000_002_000, "105", "0.00"),
    ]

    states = build_entry_states(
        feature_rows,
        train_swaps=2,
        val_swaps=1,
        stride_swaps=1,
        flow_threshold=Decimal("0.90"),
        train_return_max=Decimal("0"),
    )

    assert len(states) == 1
    assert states[0]["entry_timestamp_ms"] == "1700000001000"
    assert states[0]["validation_start_timestamp_ms"] == "1700000002000"
    assert states[0]["entry_flow_pct"] == "1.00"
    assert states[0]["train_price_return"] == "-0.01"
    assert states[0]["validation_price_return"] == "0"
    assert states[0]["gate_active"] == "1"


def test_entry_state_builds_qts_gate_variants_from_entry_swap() -> None:
    feature_rows = [
        _feature_row(
            1_700_000_000_000,
            "100",
            "0.10",
            tx_hash="0x0",
            log_index="0",
            block_number="10",
        ),
        _feature_row(
            1_700_000_001_000,
            "99",
            "0.95",
            tx_hash="0x1",
            log_index="1",
            block_number="11",
        ),
        _feature_row(
            1_700_000_002_000,
            "101",
            "0.00",
            tx_hash="0x2",
            log_index="2",
            block_number="12",
        ),
    ]
    qts_rows = [
        _qts_row("0x0", "0", "10", "-0.1", "-0.1"),
        _qts_row("0x1", "1", "11", "0.01", "-0.02"),
        _qts_row("0x2", "2", "12", "", "0.50"),
    ]

    states = build_entry_states(
        feature_rows,
        qts_rows=qts_rows,
        train_swaps=2,
        val_swaps=1,
        stride_swaps=1,
        flow_threshold=Decimal("0.90"),
        train_return_max=Decimal("0"),
    )

    assert states[0]["entry_predicted_markout_20_25"] == "0.01"
    assert states[0]["entry_predicted_markout_100_25"] == "-0.02"
    assert states[0]["gate_no_gate"] == "1"
    assert states[0]["gate_strict_sign_cone"] == "1"
    assert states[0]["gate_train_flat_qts_20_25"] == "1"
    assert states[0]["gate_strict_qts_20_25"] == "1"
    assert states[0]["gate_strict_qts_100_25"] == "0"


def test_selected_rank1_summary_separates_gated_and_non_gated_windows() -> None:
    entry_states = [
        {
            "window_index": "0",
            "gate_active": "1",
            "train_price_return": "-0.01",
            "validation_price_return": "0.02",
        },
        {
            "window_index": "1",
            "gate_active": "0",
            "train_price_return": "0.01",
            "validation_price_return": "-0.03",
        },
    ]
    window_rows = [
        _window_row("0", "1", "0.010", "5", "2", "0", "0.01"),
        _window_row("0", "2", "0.900", "99", "0", "0", "0.90"),
        _window_row("1", "1", "-0.020", "1", "3", "1", "0.02"),
    ]

    summary = summarize_selected_rank1(window_rows, entry_states)

    assert summary["all"]["windows"] == "2"
    assert summary["all"]["sum_return"] == "-0.010000"
    assert summary["all"]["positive_window_rate"] == "0.500000"
    assert summary["gated"]["windows"] == "1"
    assert summary["gated"]["sum_return"] == "0.010000"
    assert summary["gated"]["mean_all_window_return"] == "0.005000"
    assert summary["gated"]["fee_cost_ratio"] == "2.500000"
    assert summary["non_gated"]["windows"] == "1"
    assert summary["non_gated"]["worst_return"] == "-0.020000"


def test_candidate_family_summary_treats_inactive_gate_as_no_position() -> None:
    entry_states = [
        {"window_index": "0", "gate_active": "1"},
        {"window_index": "1", "gate_active": "0"},
    ]
    matrix_rows = [
        _matrix_row("0", "spot", "0.01", "0.020", "3", "1", "0"),
        _matrix_row("1", "spot", "0.01", "-0.050", "9", "1", "2"),
        _matrix_row("0", "spot", "0.02", "-0.010", "1", "2", "1"),
        _matrix_row("1", "spot", "0.02", "0.030", "1", "2", "0"),
    ]

    rows = summarize_candidate_family(
        matrix_rows,
        entry_states,
        parameter_fields=("center_mode", "fixed_width_pct"),
        gated=True,
    )

    assert rows[0]["center_mode"] == "spot"
    assert rows[0]["fixed_width_pct"] == "0.01"
    assert rows[0]["active_windows"] == "1"
    assert rows[0]["sum_active_return"] == "0.020000"
    assert rows[0]["mean_all_window_return"] == "0.010000"
    assert rows[0]["fee_cost_ratio"] == "3.000000"
    assert rows[1]["fixed_width_pct"] == "0.02"
    assert rows[1]["sum_active_return"] == "-0.010000"


def test_candidate_family_summary_requires_window_coverage() -> None:
    with pytest.raises(ValueError, match="entry state"):
        summarize_candidate_family(
            [_matrix_row("99", "spot", "0.01", "0.020", "3", "1", "0")],
            [{"window_index": "0", "gate_active": "1"}],
            parameter_fields=("center_mode", "fixed_width_pct"),
            gated=True,
        )


def test_gate_summary_counts_inactive_windows_as_no_position() -> None:
    entry_states = [
        {
            "window_index": "0",
            "gate_strict_sign_cone": "1",
            "validation_price_return": "0.030",
        },
        {
            "window_index": "1",
            "gate_strict_sign_cone": "0",
            "validation_price_return": "-0.020",
        },
    ]
    rows = [
        {
            "window_index": "0",
            "strategy": "frozen_paper",
            "config": "w01",
            "validation_net_return": "0.010",
            "validation_total_fees": "4",
            "validation_total_transaction_cost": "2",
            "validation_rebalance_count": "1",
            "validation_max_drawdown": "0.001",
        },
        {
            "window_index": "1",
            "strategy": "frozen_paper",
            "config": "w01",
            "validation_net_return": "-0.050",
            "validation_total_fees": "8",
            "validation_total_transaction_cost": "2",
            "validation_rebalance_count": "2",
            "validation_max_drawdown": "0.010",
        },
    ]

    summary = build_gate_summary_rows(
        rows,
        entry_states,
        gate_fields=("gate_strict_sign_cone",),
        identity_fields=("strategy", "config"),
    )

    assert summary == [
        {
            "gate": "gate_strict_sign_cone",
            "strategy": "frozen_paper",
            "config": "w01",
            "active_windows": "1",
            "windows": "1",
            "sum_active_return": "0.010000",
            "sum_return": "0.010000",
            "mean_active_return": "0.010000",
            "mean_return": "0.010000",
            "median_return": "0.010000",
            "worst_active_return": "0.010000",
            "worst_return": "0.010000",
            "positive_active_rate": "1.000000",
            "positive_window_rate": "1.000000",
            "mean_all_window_return": "0.005000",
            "total_fees": "4.000000",
            "total_transaction_cost": "2.000000",
            "fee_cost_ratio": "2.000000",
            "total_rebalances": "1.000000",
            "worst_drawdown": "0.001000",
            "leave_one_active_window_out_min_return": "not_available",
            "inactive_window_return": "-0.050000",
            "hold_cngn_active_return": "0.030000",
            "active_minus_hold_cngn": "-0.020000",
        }
    ]


def _feature_row(
    timestamp_ms: int,
    raw_sqrt_mid: str,
    flow_pct: str,
    *,
    tx_hash: str = "0x1",
    log_index: str = "0",
    block_number: str = "1",
) -> dict[str, str]:
    return {
        "timestamp_ms": str(timestamp_ms),
        "tx_hash": tx_hash,
        "log_index": log_index,
        "block_number": block_number,
        "raw_sqrt_mid": raw_sqrt_mid,
        "swap_flow_imbalance_cone_pct_1h": flow_pct,
        "swap_flow_imbalance": "1",
        "realized_volatility_cone_pct_1h": "",
        "active_liquidity_cone_pct_1h": "",
        "active_liquidity_running_max_share_cone_pct_1h": "",
        "fee_intensity_proxy_cone_pct_1h": "",
        "volume_cone_pct_1h": "",
    }


def _window_row(
    window_index: str,
    train_rank: str,
    net_return: str,
    fees: str,
    tx_cost: str,
    rebalances: str,
    drawdown: str,
) -> dict[str, str]:
    return {
        "window_index": window_index,
        "train_rank": train_rank,
        "validation_net_return": net_return,
        "validation_total_fees": fees,
        "validation_total_transaction_cost": tx_cost,
        "validation_rebalance_count": rebalances,
        "validation_max_drawdown": drawdown,
    }


def _matrix_row(
    window_index: str,
    center_mode: str,
    fixed_width_pct: str,
    net_return: str,
    fees: str,
    tx_cost: str,
    rebalances: str,
) -> dict[str, str]:
    row = _window_row(window_index, "", net_return, fees, tx_cost, rebalances, "0")
    row["center_mode"] = center_mode
    row["fixed_width_pct"] = fixed_width_pct
    return row


def _qts_row(
    tx_hash: str,
    log_index: str,
    block_number: str,
    predicted_20_25: str,
    predicted_100_25: str,
) -> dict[str, str]:
    return {
        "tx_hash": tx_hash,
        "log_index": log_index,
        "block_number": block_number,
        "predicted_markout_20_25": predicted_20_25,
        "predicted_markout_100_25": predicted_100_25,
    }
