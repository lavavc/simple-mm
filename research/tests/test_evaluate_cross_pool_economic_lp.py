from __future__ import annotations

import csv
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import research.scripts.evaluate_cross_pool_economic_lp as economic_cli
from research.backtester.data import V4Event, load_v4_events
from research.backtester.simulator import UNISWAP_BASE_POOL
from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.forecast import ForecastPoint, ForecastTimeline
from research.cross_pool.manifest import ForecastCoverage, RuntimeEnvironment
from research.cross_pool.publication import OutputPublicationError
from research.scripts.evaluate_cross_pool_economic_lp import (
    FROZEN_ACTIVE_ROUTES,
    FROZEN_ENTRY_STATE_SHA256,
    FROZEN_EVALUATION_WINDOWS,
    FROZEN_EXCLUDED_WINDOWS,
    FROZEN_MINT_GAS_USD,
    FROZEN_PARAMETER_SHA256,
    FROZEN_REMOVE_GAS_USD,
    EconomicWindowRow,
    build_frozen_economic_plan,
    canonical_payload_sha256,
    current_base_entry_states,
    evaluate_frozen_windows,
    frozen_parameter_payload,
    load_primary_forecast_timeline,
    pool_mark_hold_metrics,
    render_economic_artifacts,
    summarize_economic_rows,
)

_PREDICTION_FIELDS = (
    "timestamp_ms",
    "target_timestamp_ms",
    "horizon_ms",
    "refit_timestamp_ms",
    "fold_index",
    "direction",
    "actual_bps",
    "baseline_prediction_bps",
    "cross_prediction_bps",
)


def test_direct_script_entry_point_starts_from_repository_root() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "research/scripts/evaluate_cross_pool_economic_lp.py",
            "--help",
        ],
        cwd=repository_root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--predictions" in result.stdout
    assert "--out-dir" in result.stdout


def test_frozen_economic_windows_routes_and_hashes_are_unchanged() -> None:
    states = current_base_entry_states()
    plan = build_frozen_economic_plan(states)

    assert plan.excluded_windows == FROZEN_EXCLUDED_WINDOWS == (0, 1, 2)
    assert plan.evaluation_windows == FROZEN_EVALUATION_WINDOWS == tuple(range(3, 26))
    assert dict(plan.active_routes) == dict(FROZEN_ACTIVE_ROUTES) == {
        7: "upside_capture",
        18: "fee_box",
        23: "upside_capture",
        25: "dip_accumulator",
    }
    assert plan.entry_state_contract_sha256 == FROZEN_ENTRY_STATE_SHA256
    assert plan.parameter_contract_sha256 == FROZEN_PARAMETER_SHA256
    assert plan.mint_gas_usd == FROZEN_MINT_GAS_USD == Decimal("0.073")
    assert plan.remove_gas_usd == FROZEN_REMOVE_GAS_USD == Decimal("0.022")

    tampered_states = deepcopy(states)
    tampered_states[7]["validation_end_timestamp_ms"] = str(
        int(tampered_states[7]["validation_end_timestamp_ms"]) + 1
    )
    with pytest.raises(CrossPoolContractError, match="entry-state fingerprint"):
        build_frozen_economic_plan(tampered_states)

    parameter_payload = frozen_parameter_payload()
    assert canonical_payload_sha256(parameter_payload) == FROZEN_PARAMETER_SHA256
    parameter_payload["mint_gas_usd"] = 0.074
    assert canonical_payload_sha256(parameter_payload) != FROZEN_PARAMETER_SHA256
    parameter_payload = frozen_parameter_payload()
    parameter_payload["forecast_agreement_threshold_bps"] = 7.0
    assert canonical_payload_sha256(parameter_payload) != FROZEN_PARAMETER_SHA256


def test_prediction_loader_selects_primary_hourly_origin_and_rejects_bad_rows(
    tmp_path: Path,
) -> None:
    origin_ms = 1_800_000_000_000
    path = tmp_path / "predictions.csv"
    _write_predictions(
        path,
        [
            _prediction_row(
                origin_ms + 900_000,
                direction="bsc_to_base",
                forecast="123",
                horizon_ms=900_000,
            ),
            _prediction_row(origin_ms, direction="base_to_bsc", forecast="99"),
            _prediction_row(origin_ms, direction="bsc_to_base", forecast="7.5"),
            _prediction_row(
                origin_ms + 3_600_000,
                direction="bsc_to_base",
                forecast="-8.5",
            ),
        ],
    )

    timeline = load_primary_forecast_timeline(path)

    assert tuple(point.forecast_bps for point in timeline.points) == (7.5, -8.5)
    assert all(point.horizon == timedelta(hours=1) for point in timeline.points)
    assert timeline.points[0].origin_time == datetime.fromtimestamp(
        origin_ms / 1_000,
        tz=timezone.utc,
    )

    bad_target = tmp_path / "bad-target.csv"
    row = _prediction_row(origin_ms, direction="bsc_to_base", forecast="1")
    row["target_timestamp_ms"] = str(origin_ms + 1)
    _write_predictions(bad_target, [row])
    with pytest.raises(CrossPoolContractError, match="target timestamp"):
        load_primary_forecast_timeline(bad_target)

    duplicate = tmp_path / "duplicate.csv"
    _write_predictions(
        duplicate,
        [
            _prediction_row(origin_ms, direction="bsc_to_base", forecast="1"),
            _prediction_row(origin_ms, direction="bsc_to_base", forecast="2"),
        ],
    )
    with pytest.raises(CrossPoolContractError, match="strictly increasing and unique"):
        load_primary_forecast_timeline(duplicate)


def test_execution_provenance_must_match_sealed_statistics() -> None:
    runtime = RuntimeEnvironment(
        python_version="3.12.4",
        numpy_version="2.4.4",
        numpy_build_sha256="a" * 64,
        machine_architecture="arm64",
        byte_order="little",
        bit_generator="PCG64",
        draw_dtype="int64",
        matplotlib_version="3.10.8",
        matplotlib_backend="Agg",
        freetype_version="2.6.1",
        font_name="DejaVu Sans",
        font_file_sha256="b" * 64,
        figure_rcparams_sha256="c" * 64,
    )
    manifest = {
        "provenance": {
            "code_commit": "d" * 40,
            "source_diff_sha256": "e" * 64,
            "runtime": runtime.__dict__,
        }
    }

    economic_cli.require_matching_execution_provenance(
        manifest,
        code_commit="d" * 40,
        source_diff_sha256="e" * 64,
        runtime=runtime,
    )
    with pytest.raises(OutputPublicationError, match="source/runtime provenance"):
        economic_cli.require_matching_execution_provenance(
            manifest,
            code_commit="f" * 40,
            source_diff_sha256="e" * 64,
            runtime=runtime,
        )


def test_pool_mark_hold_uses_exact_sqrt_path_and_interim_drawdown() -> None:
    q96 = 2**96
    events = [
        _swap_event(0, q96),
        _swap_event(1, 2 * q96),
        _swap_event(2, q96),
    ]

    net_return, drawdown = pool_mark_hold_metrics(events)

    assert net_return == Decimal("0")
    assert drawdown == Decimal("0.75")


def test_summary_uses_isolated_window_returns_and_drawdowns() -> None:
    rows = _fixture_rows()

    summaries, metrics = summarize_economic_rows(rows)

    assert summaries.original.aggregate_net_return == Decimal("0.08")
    assert summaries.original.worst_within_window_drawdown_magnitude == Decimal(
        "0.10"
    )
    assert summaries.gated.active_window_count == 1
    assert summaries.static.active_window_count == 23
    assert summaries.pool_mark_hold_cngn.active_window_count == 23
    assert summaries.cash.active_window_count == 0
    assert metrics.gated_net_return == Decimal("0.03")
    assert metrics.original_worst_drawdown_magnitude == Decimal("0.10")


def test_economic_artifacts_are_deterministic_and_normalize_signed_zero() -> None:
    plan = build_frozen_economic_plan(current_base_entry_states())
    coverage = ForecastCoverage(
        routed_entry_decision_count=4,
        forecast_covered_entry_decision_count=4,
        maximum_selected_forecast_age_ms=3_599_999,
        first_routed_entry_timestamp_ms=plan.entries[4].entry_timestamp_ms,
        last_routed_entry_timestamp_ms=plan.entries[-1].entry_timestamp_ms,
    )
    rows = _fixture_rows()
    cash_index = next(index for index, row in enumerate(rows) if row.strategy == "cash")
    cash = rows[cash_index]
    rows[cash_index] = EconomicWindowRow(
        **{
            **cash.__dict__,
            "net_return": Decimal("-0"),
        }
    )

    first = render_economic_artifacts(
        plan,
        rows,
        coverage,
        prediction_sha256="a" * 64,
        flow_markout_feature_sha256="b" * 64,
    )
    second = render_economic_artifacts(
        plan,
        rows,
        coverage,
        prediction_sha256="a" * 64,
        flow_markout_feature_sha256="b" * 64,
    )

    assert tuple(artifact.relative_name for artifact in first.artifacts) == (
        "frozen_policy_windows.csv",
        "frozen_policy_summary.csv",
        "frozen_policy_exclusions.csv",
        "frozen_policy_report.md",
        "lp_performance.png",
    )
    assert tuple(artifact.content for artifact in first.artifacts) == tuple(
        artifact.content for artifact in second.artifacts
    )
    windows_csv = first.artifacts[0].content.decode()
    assert ",-0," not in windows_csv
    assert first.manifest_input.artifacts["lp_performance.png"] == (
        first.artifacts[-1].sha256
    )


def test_real_frozen_windows_emit_one_row_per_strategy_and_window() -> None:
    plan = build_frozen_economic_plan(current_base_entry_states())
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    points = tuple(
        ForecastPoint(
            origin_time=start + timedelta(hours=hour),
            refit_time=start,
            horizon=timedelta(hours=1),
            forecast_bps=0.0,
        )
        for hour in range(24 * 120)
    )

    events = load_v4_events(
        "research/data/derived/uni_base_pool_history_replay.csv",
        pool_id=UNISWAP_BASE_POOL.pool_address,
    )
    rows, coverage = evaluate_frozen_windows(
        plan,
        ForecastTimeline(points),
        events,
    )

    assert len(rows) == 5 * len(FROZEN_EVALUATION_WINDOWS) == 115
    assert {
        (row.window_index, row.strategy) for row in rows
    } == {
        (window_index, strategy)
        for window_index in FROZEN_EVALUATION_WINDOWS
        for strategy in (
            "original",
            "gated",
            "static",
            "pool_mark_hold_cngn",
            "cash",
        )
    }
    assert coverage.routed_entry_decision_count == 4
    assert coverage.forecast_covered_entry_decision_count == 4
    assert {
        row.window_index
        for row in rows
        if row.strategy == "original" and row.active
    } == set(FROZEN_ACTIVE_ROUTES)
    assert {
        row.window_index
        for row in rows
        if row.strategy == "gated" and row.active
    } == {18}
    assert all(
        not row.active
        for row in rows
        if row.strategy == "gated"
        and row.window_index not in FROZEN_ACTIVE_ROUTES
    )
    summaries, _metrics = summarize_economic_rows(rows)
    assert summaries.original.active_window_count == 4
    assert summaries.gated.active_window_count <= 4


def _prediction_row(
    origin_ms: int,
    *,
    direction: str,
    forecast: str,
    horizon_ms: int = 3_600_000,
) -> dict[str, str]:
    return {
        "timestamp_ms": str(origin_ms),
        "target_timestamp_ms": str(origin_ms + horizon_ms),
        "horizon_ms": str(horizon_ms),
        "refit_timestamp_ms": str(origin_ms),
        "fold_index": "0",
        "direction": direction,
        "actual_bps": "0",
        "baseline_prediction_bps": "0",
        "cross_prediction_bps": forecast,
    }


def _write_predictions(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_PREDICTION_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _swap_event(index: int, sqrt_price_x96: int) -> V4Event:
    return V4Event(
        block_time=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index),
        chain="base",
        pool_id="0xpool",
        event_type="swap",
        tx_hash=f"0x{index}",
        log_index=0,
        block_number=index,
        sqrt_price_x96=sqrt_price_x96,
        tick=0,
        active_liquidity=1_000_000,
        fee_rate=0.0001,
        amount0=1.0,
        amount1=-1.0,
        amount_usd=1.0,
        cngn_usd_price=99.0,
        token0_symbol="cNGN",
        token1_symbol="USDC",
    )


def _economic_row(
    window_index: int,
    strategy: str,
    *,
    active: bool,
    net_return: Decimal = Decimal("0"),
    drawdown: Decimal = Decimal("0"),
) -> EconomicWindowRow:
    return EconomicWindowRow(
        window_index=window_index,
        validation_start_timestamp_ms=window_index * 1_000 + 1,
        validation_end_timestamp_ms=window_index * 1_000 + 2,
        strategy=strategy,
        config="fixture",
        archetype="",
        route_reason="",
        position_status="active" if active else "cash",
        forecast_gate="not_applied",
        net_return=net_return,
        worst_within_window_drawdown_magnitude=drawdown,
        total_fees_usd=Decimal("0"),
        total_transaction_cost_usd=Decimal("0"),
        rebalance_count=0,
    )


def _fixture_rows() -> list[EconomicWindowRow]:
    rows: list[EconomicWindowRow] = []
    for window_index in FROZEN_EVALUATION_WINDOWS:
        original_active = window_index in FROZEN_ACTIVE_ROUTES
        gated_active = window_index == 18
        rows.extend(
            (
                _economic_row(
                    window_index,
                    "original",
                    active=original_active,
                    net_return=Decimal("0.02") if original_active else Decimal("0"),
                    drawdown=Decimal("0.10") if original_active else Decimal("0"),
                ),
                _economic_row(
                    window_index,
                    "gated",
                    active=gated_active,
                    net_return=Decimal("0.03") if gated_active else Decimal("0"),
                    drawdown=Decimal("0.05") if gated_active else Decimal("0"),
                ),
                _economic_row(
                    window_index,
                    "static",
                    active=True,
                    net_return=Decimal("0.01"),
                    drawdown=Decimal("0.20"),
                ),
                _economic_row(
                    window_index,
                    "pool_mark_hold_cngn",
                    active=True,
                    net_return=Decimal("-0.01"),
                    drawdown=Decimal("0.25"),
                ),
                _economic_row(window_index, "cash", active=False),
            )
        )
    return rows
