from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import numpy as np
import pytest

from research.cross_pool.contracts import (
    CausalPanel,
    CrossPoolContractError,
    Direction,
    PanelRow,
    Regime,
    WalkForwardConfig,
)
from research.cross_pool.predictive import expanding_weekly_predictions

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
FIRST_WEDNESDAY_MS = 1_767_787_200_000
FIRST_REFIT_MS = 1_769_385_600_000


def test_expanding_predictions_recovers_known_primary_lead_and_is_deterministic() -> None:
    panel = _known_panel()
    config = _primary_config()

    result = expanding_weekly_predictions(panel, config)

    assert result == expanding_weekly_predictions(panel, config)
    assert result.predictions
    cross_errors = [abs(row.actual_bps - row.cross_prediction_bps) for row in result.predictions]
    baseline_errors = [
        abs(row.actual_bps - row.baseline_prediction_bps) for row in result.predictions
    ]
    assert max(cross_errors) < 1e-8
    assert np.mean(baseline_errors) > 0.1

    source_rows = {row.timestamp_ms: row for row in panel.rows}
    first = result.predictions[0]
    source = source_rows[first.timestamp_ms]
    assert first.target_timestamp_ms == first.timestamp_ms + HOUR_MS
    assert first.horizon_ms == HOUR_MS
    assert first.direction == "bsc_to_base"
    assert first.actual_bps == source.base_forward_return_bps
    assert first.target_regime == source.base_regime
    assert first.source_regime == source.bsc_regime


def test_reverse_direction_swaps_target_source_age_and_gap_projection() -> None:
    panel = _known_panel()

    result = expanding_weekly_predictions(
        panel,
        WalkForwardConfig(direction="base_to_bsc"),
    )

    assert max(abs(row.actual_bps - row.cross_prediction_bps) for row in result.predictions) < 1e-8
    assert (
        np.mean([abs(row.actual_bps - row.baseline_prediction_bps) for row in result.predictions])
        > 0.1
    )
    source = next(
        row for row in panel.rows if row.timestamp_ms == result.predictions[0].timestamp_ms
    )
    first = result.predictions[0]
    assert first.direction == "base_to_bsc"
    assert first.actual_bps == source.bsc_forward_return_bps
    assert first.target_regime == source.bsc_regime
    assert first.source_regime == source.base_regime

    training = _training_rows(panel, result.audits[0].refit_timestamp_ms)
    expected = np.asarray(
        [_projected_features(row, "base_to_bsc") for row in training],
        dtype=np.float64,
    )
    assert result.audits[0].feature_means == pytest.approx(
        tuple(float(value) for value in expected.mean(axis=0))
    )


def test_refit_schedule_anchors_raw_start_owns_half_open_folds_and_keeps_partial() -> None:
    panel = _known_panel()

    result = expanding_weekly_predictions(panel, _primary_config())
    trimmed = replace(panel, rows=panel.rows[48:])
    trimmed_result = expanding_weekly_predictions(trimmed, _primary_config())

    assert result.audits[0].refit_timestamp_ms == FIRST_REFIT_MS
    assert trimmed_result.audits[0].refit_timestamp_ms == FIRST_REFIT_MS
    assert all(
        datetime.fromtimestamp(audit.refit_timestamp_ms / 1_000, tz=UTC).weekday() == 0
        and audit.refit_timestamp_ms % DAY_MS == 0
        for audit in result.audits
    )
    assert all(
        row.refit_timestamp_ms <= row.timestamp_ms < row.refit_timestamp_ms + WEEK_MS
        for row in result.predictions
    )
    expected_timestamps = tuple(
        row.timestamp_ms for row in panel.rows if row.timestamp_ms >= FIRST_REFIT_MS
    )
    assert tuple(row.timestamp_ms for row in result.predictions) == expected_timestamps
    assert result.predictions[-1].timestamp_ms == panel.rows[-1].timestamp_ms
    assert [audit.fold_index for audit in result.audits] == list(range(len(result.audits)))
    assert all(
        audit.max_training_target_timestamp_ms == audit.refit_timestamp_ms
        for audit in result.audits
    )
    assert all(
        current.max_training_target_timestamp_ms > previous.max_training_target_timestamp_ms
        for previous, current in zip(result.audits, result.audits[1:], strict=False)
    )
    second_refit = result.audits[1].refit_timestamp_ms
    boundary_row = next(row for row in result.predictions if row.timestamp_ms == second_refit)
    assert boundary_row.fold_index == 1
    assert boundary_row.refit_timestamp_ms == second_refit


def test_weekday_candidate_at_boundary_stays_and_just_after_rolls_one_week() -> None:
    monday = _utc_ms(2026, 1, 5)
    panel = _known_panel(common_start_ms=monday, days=35)
    just_after = replace(panel, common_interval_start_ms=monday + 1)

    at_boundary = expanding_weekly_predictions(panel, _primary_config())
    after_boundary = expanding_weekly_predictions(just_after, _primary_config())

    assert at_boundary.audits[0].refit_timestamp_ms == _utc_ms(2026, 1, 19)
    assert after_boundary.audits[0].refit_timestamp_ms == _utc_ms(2026, 1, 26)


def test_validation_outlier_does_not_change_current_fold_training_scaler() -> None:
    panel = _known_panel(days=20)
    baseline = expanding_weekly_predictions(panel, _primary_config())
    first_refit = baseline.audits[0].refit_timestamp_ms
    contaminated_rows = tuple(
        replace(row, base_trailing_return_bps=1e12) if row.timestamp_ms == first_refit else row
        for row in panel.rows
    )

    contaminated = expanding_weekly_predictions(
        replace(panel, rows=contaminated_rows),
        _primary_config(),
    )

    assert len(baseline.audits) == 1
    assert contaminated.audits[0].feature_means == baseline.audits[0].feature_means
    assert contaminated.audits[0].feature_scales == baseline.audits[0].feature_scales


@pytest.mark.parametrize("direction", ["bsc_to_base", "base_to_bsc"])
def test_fold_audit_uses_direction_relative_population_scalers(
    direction: Direction,
) -> None:
    panel = _known_panel(days=20)
    result = expanding_weekly_predictions(
        panel,
        WalkForwardConfig(direction=direction),
    )
    audit = result.audits[0]
    training = _training_rows(panel, audit.refit_timestamp_ms)
    matrix = np.asarray(
        [_projected_features(row, direction) for row in training],
        dtype=np.float64,
    )

    assert audit.feature_means == pytest.approx(
        tuple(float(value) for value in matrix.mean(axis=0))
    )
    assert audit.feature_scales == pytest.approx(
        tuple(float(value) for value in matrix.std(axis=0, ddof=0))
    )
    assert len(audit.feature_means) == 5
    assert len(audit.feature_scales) == 5


def test_intercept_recovers_constant_target_without_scaling_it() -> None:
    panel = _known_panel(days=20)
    constant_rows = tuple(replace(row, base_forward_return_bps=7.25) for row in panel.rows)

    result = expanding_weekly_predictions(
        replace(panel, rows=constant_rows),
        _primary_config(),
    )

    assert all(row.actual_bps == 7.25 for row in result.predictions)
    assert all(row.baseline_prediction_bps == pytest.approx(7.25) for row in result.predictions)
    assert all(row.cross_prediction_bps == pytest.approx(7.25) for row in result.predictions)


@pytest.mark.parametrize("feature_index", range(5))
def test_zero_variance_in_each_cross_feature_fails_closed(feature_index: int) -> None:
    panel = _known_panel(days=20)
    rows = tuple(_replace_feature(row, feature_index, 1.0) for row in panel.rows)

    with pytest.raises(CrossPoolContractError, match=f"zero-variance feature {feature_index}"):
        expanding_weekly_predictions(replace(panel, rows=rows), _primary_config())


def test_exact_feature_collinearity_fails_the_cross_design_rank_check() -> None:
    panel = _known_panel(days=20)
    rows = tuple(
        replace(row, bsc_trailing_return_bps=row.base_trailing_return_bps) for row in panel.rows
    )

    with pytest.raises(CrossPoolContractError, match="cross design is rank deficient"):
        expanding_weekly_predictions(replace(panel, rows=rows), _primary_config())


def test_full_rank_near_collinearity_above_default_condition_limit_fails() -> None:
    panel = _near_collinear_panel(epsilon=1e-12)
    condition, rank = _first_fold_cross_condition(panel)

    assert rank == 6
    assert condition > 1e12
    with pytest.raises(CrossPoolContractError, match="cross design condition number"):
        expanding_weekly_predictions(panel, _primary_config())


def test_condition_number_equal_to_configured_limit_is_accepted() -> None:
    panel = _known_panel(days=20)
    maximum_condition = _first_fold_maximum_condition(panel)

    result = expanding_weekly_predictions(
        panel,
        replace(_primary_config(), maximum_condition_number=maximum_condition),
    )

    assert result.predictions


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("direction", "unsupported direction"),
        ("training_days", "initial_train_days must be positive"),
        ("weekday", "refit_weekday must be between 0 and 6"),
        ("condition_zero", "maximum_condition_number must be positive and finite"),
        ("condition_infinite", "maximum_condition_number must be positive and finite"),
    ],
)
def test_walk_forward_config_fails_closed(mutation: str, match: str) -> None:
    config = _primary_config()
    if mutation == "direction":
        config = replace(config, direction=cast(Direction, "sideways"))
    elif mutation == "training_days":
        config = replace(config, initial_train_days=0)
    elif mutation == "weekday":
        config = replace(config, refit_weekday=7)
    elif mutation == "condition_zero":
        config = replace(config, maximum_condition_number=0.0)
    elif mutation == "condition_infinite":
        config = replace(config, maximum_condition_number=math.inf)
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(CrossPoolContractError, match=match):
        expanding_weekly_predictions(_known_panel(days=20), config)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("empty", "at least one panel row"),
        ("duplicate_time", "panel timestamps must be strictly increasing"),
        ("wrong_horizon", "row horizon does not match panel horizon"),
        ("negative_age", "state ages must be non-negative"),
        ("non_finite", "non-finite predictive input"),
        ("no_validation", "no post-refit validation rows"),
    ],
)
def test_walk_forward_panel_contract_fails_closed(mutation: str, match: str) -> None:
    panel = _known_panel(days=20)
    if mutation == "empty":
        panel = replace(panel, rows=())
    elif mutation == "duplicate_time":
        rows = list(panel.rows)
        rows[1] = replace(rows[1], timestamp_ms=rows[0].timestamp_ms)
        panel = replace(panel, rows=tuple(rows))
    elif mutation == "wrong_horizon":
        panel = replace(
            panel,
            rows=(replace(panel.rows[0], horizon_ms=15 * 60_000),) + panel.rows[1:],
        )
    elif mutation == "negative_age":
        panel = replace(
            panel,
            rows=(replace(panel.rows[0], base_age_ms=-1),) + panel.rows[1:],
        )
    elif mutation == "non_finite":
        panel = replace(
            panel,
            rows=(replace(panel.rows[0], base_trailing_return_bps=math.nan),) + panel.rows[1:],
        )
    elif mutation == "no_validation":
        panel = _known_panel(days=10)
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(CrossPoolContractError, match=match):
        expanding_weekly_predictions(panel, _primary_config())


def test_refit_with_too_few_observable_rows_fails_closed() -> None:
    panel = _known_panel(
        common_start_ms=_utc_ms(2026, 1, 3),
        days=10,
        horizon_ms=DAY_MS,
    )

    with pytest.raises(CrossPoolContractError, match="cross design requires at least 6 rows"):
        expanding_weekly_predictions(
            panel,
            WalkForwardConfig(direction="bsc_to_base", initial_train_days=1),
        )


def _primary_config() -> WalkForwardConfig:
    return WalkForwardConfig(direction="bsc_to_base")


def _known_panel(
    *,
    common_start_ms: int = FIRST_WEDNESDAY_MS,
    days: int = 42,
    horizon_ms: int = HOUR_MS,
) -> CausalPanel:
    common_end_ms = common_start_ms + days * DAY_MS
    first_timestamp_ms = _ceil_to_multiple(common_start_ms + horizon_ms, horizon_ms)
    rows = tuple(
        _known_row(index, timestamp_ms, horizon_ms)
        for index, timestamp_ms in enumerate(
            range(first_timestamp_ms, common_end_ms, horizon_ms),
            start=1,
        )
    )
    return CausalPanel(
        common_interval_start_ms=common_start_ms,
        common_interval_end_ms=common_end_ms,
        horizon_ms=horizon_ms,
        rows=rows,
    )


def _known_row(index: int, timestamp_ms: int, horizon_ms: int) -> PanelRow:
    base_trailing = math.sin(index * 0.17) + (index % 11) * 0.03
    base_age = 10_000 + (index * 7_919) % 3_000_000
    bsc_trailing = math.cos(index * 0.11) + (index % 7) * 0.05
    gap = math.sin(index * 0.07) * 2.0 + math.cos(index * 0.19)
    bsc_age = 5_000 + (index * 104_729) % 3_500_000
    base_forward = (
        1.75
        + 0.25 * base_trailing
        - 0.000001 * base_age
        + 0.8 * bsc_trailing
        + 0.45 * gap
        - 0.0000008 * bsc_age
    )
    bsc_forward = (
        -0.5
        + 0.35 * bsc_trailing
        - 0.0000007 * bsc_age
        + 0.6 * base_trailing
        + 0.3 * -gap
        - 0.0000005 * base_age
    )
    base_regime = cast(Regime, ("early", "mixed", "late")[index % 3])
    bsc_regime = cast(Regime, ("late", "early", "mixed")[index % 3])
    return PanelRow(
        timestamp_ms=timestamp_ms,
        horizon_ms=horizon_ms,
        base_state_timestamp_ms=timestamp_ms - base_age,
        bsc_state_timestamp_ms=timestamp_ms - bsc_age,
        base_forward_state_timestamp_ms=timestamp_ms,
        bsc_forward_state_timestamp_ms=timestamp_ms,
        base_lag_block_number=index,
        bsc_lag_block_number=index,
        base_state_block_number=index + 1,
        bsc_state_block_number=index + 1,
        base_forward_block_number=index + 2,
        bsc_forward_block_number=index + 2,
        base_age_ms=base_age,
        bsc_age_ms=bsc_age,
        base_mid=1.0 + index * 1e-6,
        bsc_mid=1.0 + index * 2e-6,
        base_trailing_return_bps=base_trailing,
        bsc_trailing_return_bps=bsc_trailing,
        base_minus_bsc_gap_bps=gap,
        base_forward_return_bps=base_forward,
        bsc_forward_return_bps=bsc_forward,
        base_regime=base_regime,
        bsc_regime=bsc_regime,
    )


def _training_rows(panel: CausalPanel, refit_timestamp_ms: int) -> tuple[PanelRow, ...]:
    return tuple(
        row for row in panel.rows if row.timestamp_ms + row.horizon_ms <= refit_timestamp_ms
    )


def _projected_features(row: PanelRow, direction: Direction) -> tuple[float, ...]:
    if direction == "bsc_to_base":
        return (
            row.base_trailing_return_bps,
            float(row.base_age_ms),
            row.bsc_trailing_return_bps,
            row.base_minus_bsc_gap_bps,
            float(row.bsc_age_ms),
        )
    return (
        row.bsc_trailing_return_bps,
        float(row.bsc_age_ms),
        row.base_trailing_return_bps,
        -row.base_minus_bsc_gap_bps,
        float(row.base_age_ms),
    )


def _replace_feature(row: PanelRow, feature_index: int, value: float) -> PanelRow:
    if feature_index == 0:
        return replace(row, base_trailing_return_bps=value)
    if feature_index == 1:
        return replace(row, base_age_ms=int(value))
    if feature_index == 2:
        return replace(row, bsc_trailing_return_bps=value)
    if feature_index == 3:
        return replace(row, base_minus_bsc_gap_bps=value)
    if feature_index == 4:
        return replace(row, bsc_age_ms=int(value))
    raise AssertionError(feature_index)


def _near_collinear_panel(*, epsilon: float) -> CausalPanel:
    panel = _known_panel(days=20)
    rows = tuple(
        replace(
            row,
            bsc_trailing_return_bps=row.base_trailing_return_bps + epsilon * math.sin(index * 0.31),
        )
        for index, row in enumerate(panel.rows, start=1)
    )
    return replace(panel, rows=rows)


def _first_fold_cross_condition(panel: CausalPanel) -> tuple[float, int]:
    refit = FIRST_REFIT_MS
    training = _training_rows(panel, refit)
    matrix = np.asarray(
        [_projected_features(row, "bsc_to_base") for row in training],
        dtype=np.float64,
    )
    standardized = (matrix - matrix.mean(axis=0)) / matrix.std(axis=0, ddof=0)
    design = np.column_stack((np.ones(len(standardized)), standardized))
    singular_values = np.linalg.svd(design, compute_uv=False)
    cutoff = np.finfo(np.float64).eps * max(design.shape)
    rank = int(np.sum(singular_values > singular_values[0] * cutoff))
    return float(singular_values[0] / singular_values[-1]), rank


def _first_fold_maximum_condition(panel: CausalPanel) -> float:
    training = _training_rows(panel, FIRST_REFIT_MS)
    matrix = np.asarray(
        [_projected_features(row, "bsc_to_base") for row in training],
        dtype=np.float64,
    )
    standardized = (matrix - matrix.mean(axis=0)) / matrix.std(axis=0, ddof=0)
    baseline = np.column_stack((np.ones(len(standardized)), standardized[:, :2]))
    cross = np.column_stack((np.ones(len(standardized)), standardized))
    return max(float(np.linalg.cond(baseline)), float(np.linalg.cond(cross)))


def _ceil_to_multiple(value: int, interval: int) -> int:
    return -(-value // interval) * interval


def _utc_ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1_000)
