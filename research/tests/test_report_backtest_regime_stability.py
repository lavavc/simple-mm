import csv
from decimal import Decimal
from pathlib import Path

import pytest

from research.scripts.report_backtest_regime_stability import (
    build_stability_report,
    load_feature_by_window,
    load_window_rows,
    main as report_main,
    parameter_jump_rows,
    render_stability_markdown,
)


WINDOW_FIELDS = [
    "window_index",
    "window_start",
    "window_end",
    "skipped_reason",
    "train_rank",
    "validation_rank",
    "fixed_width_pct",
    "initial_capital_usd",
    "validation_net_return",
    "validation_max_drawdown",
    "validation_rebalance_count",
    "validation_total_fees",
    "validation_total_transaction_cost",
]


def test_parameter_jumps_require_regime_explanation():
    rows = [
        {"window_index": "1", "fixed_width_pct": "0.01", "validation_net_return": "0.01"},
        {"window_index": "2", "fixed_width_pct": "0.05", "validation_net_return": "0.01"},
    ]
    features = {
        1: {"realized_volatility_cone_pct": 0.50},
        2: {"realized_volatility_cone_pct": 0.52},
    }

    jumps = parameter_jump_rows(
        rows,
        features,
        parameter_fields=["fixed_width_pct"],
        regime_fields=["realized_volatility_cone_pct"],
        min_regime_delta=0.20,
    )

    assert jumps[0]["unexplained_jump"] is True


def test_parameter_jumps_are_explained_by_large_regime_delta():
    rows = [
        {"window_index": "1", "fixed_width_pct": "0.01", "validation_net_return": "0.01"},
        {"window_index": "2", "fixed_width_pct": "0.05", "validation_net_return": "0.01"},
    ]
    features = {
        1: {"realized_volatility_cone_pct": 0.10},
        2: {"realized_volatility_cone_pct": 0.45},
    }

    jumps = parameter_jump_rows(
        rows,
        features,
        parameter_fields=["fixed_width_pct"],
        regime_fields=["realized_volatility_cone_pct"],
        min_regime_delta=0.20,
    )

    assert jumps[0]["unexplained_jump"] is False
    assert jumps[0]["max_regime_delta"] == pytest.approx(0.35)


def test_report_separates_opportunity_and_full_costed_sections(tmp_path: Path):
    windows_path = tmp_path / "windows.csv"
    _write_csv(
        windows_path,
        WINDOW_FIELDS,
        [
            {
                "window_index": "1",
                "window_start": "2026-01-01T00:00:00+00:00",
                "window_end": "2026-01-02T00:00:00+00:00",
                "skipped_reason": "",
                "train_rank": "1",
                "validation_rank": "1",
                "fixed_width_pct": "0.01",
                "initial_capital_usd": "1000",
                "validation_net_return": "0.01",
                "validation_max_drawdown": "0.02",
                "validation_rebalance_count": "1",
                "validation_total_fees": "5",
                "validation_total_transaction_cost": "0",
            },
            {
                "window_index": "2",
                "window_start": "2026-01-02T00:00:00+00:00",
                "window_end": "2026-01-03T00:00:00+00:00",
                "skipped_reason": "",
                "train_rank": "1",
                "validation_rank": "1",
                "fixed_width_pct": "0.04",
                "initial_capital_usd": "1000",
                "validation_net_return": "0.02",
                "validation_max_drawdown": "0.03",
                "validation_rebalance_count": "2",
                "validation_total_fees": "6",
                "validation_total_transaction_cost": "3",
            },
        ],
    )
    features_path = tmp_path / "features.csv"
    _write_csv(
        features_path,
        ["timestamp_ms", "realized_volatility_cone_pct"],
        [
            {"timestamp_ms": "1767225600000", "realized_volatility_cone_pct": "0.10"},
            {"timestamp_ms": "1767355200000", "realized_volatility_cone_pct": "0.50"},
        ],
    )

    rows = load_window_rows(windows_path)
    feature_by_window = load_feature_by_window(
        features_path,
        rows,
        regime_fields=["realized_volatility_cone_pct"],
    )
    report = build_stability_report(
        rows,
        feature_by_window,
        parameter_fields=["fixed_width_pct"],
        regime_fields=["realized_volatility_cone_pct"],
        min_regime_delta=0.20,
    )
    markdown = render_stability_markdown(report)

    assert "## Opportunity Screen" in markdown
    assert "## Full Costed Backtest" in markdown
    assert "| rows | 1 |" in markdown
    assert "| fee_cost_ratio | not_available |" in markdown
    assert "| fee_cost_ratio | 2 |" in markdown
    assert "Capacity/share validity: not_available" in markdown
    assert "sGHO APY benchmark: 4.25%" in markdown
    assert "retrospective validation-window" in markdown
    assert "| mean_sgho_excess_return |" in markdown
    assert "| 1 | 2 | fixed_width_pct | 0.01 | 0.04 | 0.4 | False |" in markdown

    out_path = tmp_path / "report.md"
    assert (
        report_main(
            [
                "--windows",
                str(windows_path),
                "--features",
                str(features_path),
                "--out",
                str(out_path),
                "--parameter-fields",
                "fixed_width_pct",
                "--regime-fields",
                "realized_volatility_cone_pct",
            ]
        )
        == 0
    )
    assert out_path.read_text() == markdown


def test_missing_required_window_field_fails_fast(tmp_path: Path):
    path = tmp_path / "windows.csv"
    _write_csv(
        path,
        ["window_index", "window_start"],
        [{"window_index": "1", "window_start": "2026-01-01T00:00:00+00:00"}],
    )

    with pytest.raises(ValueError, match="validation_net_return"):
        load_window_rows(path)


def test_skipped_window_rows_do_not_require_validation_metrics(tmp_path: Path):
    path = tmp_path / "windows.csv"
    _write_csv(
        path,
        WINDOW_FIELDS,
        [
            {
                "window_index": "1",
                "window_start": "2026-01-01T00:00:00+00:00",
                "window_end": "2026-01-02T00:00:00+00:00",
                "skipped_reason": "too_few_swaps",
                "train_rank": "",
                "validation_rank": "",
                "fixed_width_pct": "",
                "initial_capital_usd": "",
                "validation_net_return": "",
                "validation_max_drawdown": "",
                "validation_rebalance_count": "",
                "validation_total_fees": "",
                "validation_total_transaction_cost": "",
            }
        ],
    )

    rows = load_window_rows(path)
    report = build_stability_report(
        rows,
        {},
        parameter_fields=[],
        regime_fields=[],
    )

    assert report.total_rows == 1
    assert report.selected_window_count == 0
    assert report.opportunity.rows == 0
    assert report.full_costed.rows == 0


def test_report_summarizes_selected_rows_not_candidate_grid() -> None:
    rows = [
        _window_row(
            window_index="1",
            train_rank="1",
            validation_rank="1",
            validation_net_return="0.01",
            validation_total_transaction_cost="3",
        ),
        _window_row(
            window_index="1",
            train_rank="2",
            validation_rank="2",
            validation_net_return="0.90",
            validation_total_transaction_cost="0",
        ),
        _window_row(
            window_index="2",
            train_rank="1",
            validation_rank="1",
            validation_net_return="0.02",
            validation_total_transaction_cost="3",
        ),
    ]

    report = build_stability_report(
        rows,
        {
            1: {"realized_volatility_cone_pct": 0.10},
            2: {"realized_volatility_cone_pct": 0.50},
        },
        parameter_fields=["fixed_width_pct"],
        regime_fields=["realized_volatility_cone_pct"],
    )

    assert report.opportunity.rows == 0
    assert report.full_costed.rows == 2
    assert report.full_costed.mean_return_on_capital == Decimal("0.015")


def test_load_feature_by_window_includes_window_end_and_skips_blank_values(
    tmp_path: Path,
) -> None:
    features_path = tmp_path / "features.csv"
    _write_csv(
        features_path,
        [
            "timestamp_ms",
            "realized_volatility_cone_pct",
            "dex_premium_cone_pct",
        ],
        [
            {
                "timestamp_ms": "1767225600000",
                "realized_volatility_cone_pct": "0.10",
                "dex_premium_cone_pct": "",
            },
            {
                "timestamp_ms": "1767312000000",
                "realized_volatility_cone_pct": "0.30",
                "dex_premium_cone_pct": "",
            },
            {
                "timestamp_ms": "1767312000001",
                "realized_volatility_cone_pct": "0.50",
                "dex_premium_cone_pct": "",
            },
        ],
    )

    feature_by_window = load_feature_by_window(
        features_path,
        [
            _window_row(
                window_index="1",
                window_start="2026-01-01T00:00:00+00:00",
                window_end="2026-01-02T00:00:00+00:00",
            )
        ],
        regime_fields=["realized_volatility_cone_pct", "dex_premium_cone_pct"],
    )

    assert feature_by_window == {1: {"realized_volatility_cone_pct": 0.3}}


def test_parameter_jumps_skip_unavailable_regime_fields() -> None:
    rows = [
        {"window_index": "1", "fixed_width_pct": "0.01", "validation_net_return": "0.01"},
        {"window_index": "2", "fixed_width_pct": "0.05", "validation_net_return": "0.01"},
    ]
    jumps = parameter_jump_rows(
        rows,
        {
            1: {"realized_volatility_cone_pct": 0.10},
            2: {"realized_volatility_cone_pct": 0.40},
        },
        parameter_fields=["fixed_width_pct"],
        regime_fields=["realized_volatility_cone_pct", "dex_premium_cone_pct"],
        min_regime_delta=0.20,
    )

    assert jumps[0]["regime_deltas"] == {
        "realized_volatility_cone_pct": pytest.approx(0.30)
    }


def test_parameter_jumps_fail_when_no_common_regime_values() -> None:
    rows = [
        {"window_index": "1", "fixed_width_pct": "0.01", "validation_net_return": "0.01"},
        {"window_index": "2", "fixed_width_pct": "0.05", "validation_net_return": "0.01"},
    ]

    with pytest.raises(ValueError, match="no common usable regime fields"):
        parameter_jump_rows(
            rows,
            {
                1: {"realized_volatility_cone_pct": 0.10},
                2: {"dex_premium_cone_pct": 0.40},
            },
            parameter_fields=["fixed_width_pct"],
            regime_fields=["realized_volatility_cone_pct", "dex_premium_cone_pct"],
            min_regime_delta=0.20,
        )


def test_sgho_comparison_uses_paired_hurdle_return() -> None:
    row = _window_row(
        window_index="1",
        window_start="2026-01-01T00:00:00+00:00",
        window_end="2026-01-02T00:00:00+00:00",
        validation_net_return="0.01",
        validation_total_transaction_cost="3",
    )

    report = build_stability_report(
        [row],
        {},
        parameter_fields=[],
        regime_fields=[],
        sgho_apy=Decimal("0.0425"),
    )

    hurdle = report.full_costed.mean_sgho_hurdle_return
    excess = report.full_costed.mean_sgho_excess_return
    assert hurdle is not None
    assert excess is not None
    assert Decimal("0") < hurdle < Decimal("0.001")
    assert excess == Decimal("0.01") - hurdle
    assert report.full_costed.windows_above_sgho_hurdle == 1


def _window_row(**overrides: str) -> dict[str, str]:
    row = {
        "window_index": "1",
        "window_start": "2026-01-01T00:00:00+00:00",
        "window_end": "2026-01-02T00:00:00+00:00",
        "skipped_reason": "",
        "train_rank": "1",
        "validation_rank": "1",
        "fixed_width_pct": "0.01",
        "initial_capital_usd": "1000",
        "validation_net_return": "0.01",
        "validation_max_drawdown": "0.02",
        "validation_rebalance_count": "1",
        "validation_total_fees": "5",
        "validation_total_transaction_cost": "0",
    }
    row.update(overrides)
    return row


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
