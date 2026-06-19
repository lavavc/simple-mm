import csv
from pathlib import Path

import pytest

from scripts.report_backtest_regime_stability import (
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
            {"timestamp_ms": "1767312000000", "realized_volatility_cone_pct": "0.50"},
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


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
