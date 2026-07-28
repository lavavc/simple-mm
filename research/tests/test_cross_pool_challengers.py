from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from research.cross_pool.challenger_estimators import ChallengerFitError
from research.cross_pool.challenger_parent import load_challenger_parent
from research.cross_pool.challengers import (
    _fold_groups,
    _parent_prediction_groups,
    _projected_panels,
    _run_fold,
    run_challengers,
)

PARENT_DIR = Path("research/results/cross_pool_lead_lag")


@pytest.fixture(scope="module")
def parent_and_study():
    parent = load_challenger_parent(PARENT_DIR)
    return parent, run_challengers(parent)


def test_ols_endpoints_reproduce_sealed_parent(parent_and_study) -> None:
    parent, study = parent_and_study
    predictions = {
        (
            row.direction,
            row.horizon_ms,
            row.timestamp_ms,
            row.family,
            row.variant,
        ): row
        for row in study.predictions
    }

    for sealed in parent.predictions:
        target = predictions[
            (
                sealed.direction,
                sealed.horizon_ms,
                sealed.timestamp_ms,
                "ols",
                "target_only",
            )
        ]
        full = predictions[
            (
                sealed.direction,
                sealed.horizon_ms,
                sealed.timestamp_ms,
                "ols",
                "full_source",
            )
        ]
        assert target.prediction_bps == sealed.baseline_prediction_bps
        assert full.prediction_bps == sealed.cross_prediction_bps


def test_each_model_combination_is_complete_or_explicitly_failed(parent_and_study) -> None:
    parent, study = parent_and_study
    expected_rows = {
        (direction, horizon): sum(
            row.direction == direction and row.horizon_ms == horizon
            for row in parent.predictions
        )
        for direction in ("bsc_to_base", "base_to_bsc")
        for horizon in (900_000, 3_600_000, 14_400_000)
    }
    counts: dict[tuple[str, str, str, int], int] = {}
    for row in study.predictions:
        key = (row.family, row.variant, row.direction, row.horizon_ms)
        counts[key] = counts.get(key, 0) + 1
    failures = {
        (row.family, row.variant, row.direction, row.horizon_ms)
        for row in study.failures
    }

    assert len(counts) + len(failures) == 5 * 3 * 2 * 3
    assert set(counts).isdisjoint(failures)
    for family, variant, direction, horizon in counts:
        assert counts[(family, variant, direction, horizon)] == expected_rows[
            (direction, horizon)
        ]


def test_training_labels_are_observable_and_validation_grid_is_parent_exact(
    parent_and_study,
) -> None:
    parent, study = parent_and_study

    assert all(
        audit.max_training_target_timestamp_ms <= audit.refit_timestamp_ms
        for audit in study.fold_audits
    )
    parent_keys = {
        (row.direction, row.horizon_ms, row.timestamp_ms) for row in parent.predictions
    }
    assert {
        (row.direction, row.horizon_ms, row.timestamp_ms) for row in study.predictions
    } == parent_keys


def test_predictions_preserve_state_timestamp_update_and_directional_metadata(
    parent_and_study,
) -> None:
    parent, study = parent_and_study
    panels = {
        (row.timestamp_ms, row.horizon_ms): row
        for panel in parent.panels
        for row in panel.rows
    }

    for prediction in study.predictions[::997]:
        panel = panels[(prediction.timestamp_ms, prediction.horizon_ms)]
        if prediction.direction == "bsc_to_base":
            assert prediction.updated == (
                panel.base_forward_state_timestamp_ms > panel.base_state_timestamp_ms
            )
            assert prediction.target_age_ms == panel.base_age_ms
            assert prediction.source_age_ms == panel.bsc_age_ms
        else:
            assert prediction.updated == (
                panel.bsc_forward_state_timestamp_ms > panel.bsc_state_timestamp_ms
            )
            assert prediction.target_age_ms == panel.bsc_age_ms
            assert prediction.source_age_ms == panel.base_age_ms


def test_study_order_and_two_part_components_are_canonical(parent_and_study) -> None:
    _, study = parent_and_study
    keys = tuple(row.sort_key for row in study.predictions)

    assert keys == tuple(sorted(set(keys)))
    for row in study.predictions:
        if row.family == "two_part":
            assert row.update_probability is not None
            assert row.conditional_prediction_bps is not None
            assert row.prediction_bps == pytest.approx(
                row.update_probability * row.conditional_prediction_bps
            )
        else:
            assert row.update_probability is None
            assert row.conditional_prediction_bps is None


def test_validation_contamination_cannot_change_fold_fit_or_transforms(
    parent_and_study,
) -> None:
    parent, _ = parent_and_study
    key = ("bsc_to_base", 3_600_000)
    projected = _projected_panels(parent)[key]
    sealed_fold = _fold_groups(_parent_prediction_groups(parent)[key])[0]
    original = _run_fold(
        family="gam",
        variant="source_age",
        projected_rows=projected,
        sealed_validation=sealed_fold,
    )
    validation_timestamps = {row.timestamp_ms for row in sealed_fold}
    contaminated_timestamp = sealed_fold[-1].timestamp_ms
    contaminated = tuple(
        replace(
            row,
            actual_bps=row.actual_bps
            + (10_000.0 if row.timestamp_ms in validation_timestamps else 0.0),
            source_age_ms=(
                row.source_age_ms + 10_000_000_000
                if row.timestamp_ms == contaminated_timestamp
                else row.source_age_ms
            ),
        )
        for row in projected
    )
    rerun = _run_fold(
        family="gam",
        variant="source_age",
        projected_rows=contaminated,
        sealed_validation=sealed_fold,
    )

    assert rerun.audit.training_rows == original.audit.training_rows
    assert rerun.audit.condition_number == original.audit.condition_number
    assert rerun.audit.spline_knots == original.audit.spline_knots
    original_predictions = {
        row.timestamp_ms: row.prediction_bps for row in original.predictions
    }
    rerun_predictions = {row.timestamp_ms: row.prediction_bps for row in rerun.predictions}
    assert all(
        rerun_predictions[timestamp] == original_predictions[timestamp]
        for timestamp in original_predictions
        if timestamp != contaminated_timestamp
    )


def test_arx2_fails_when_validation_predecessor_is_missing(parent_and_study) -> None:
    parent, _ = parent_and_study
    key = ("bsc_to_base", 3_600_000)
    projected = _projected_panels(parent)[key]
    sealed_fold = _fold_groups(_parent_prediction_groups(parent)[key])[0]
    first_validation_timestamp = sealed_fold[0].timestamp_ms
    missing_predecessor = tuple(
        replace(row, previous_target_return_bps=None)
        if row.timestamp_ms == first_validation_timestamp
        else row
        for row in projected
    )

    with pytest.raises(ChallengerFitError, match="validation row has no regular predecessor"):
        _run_fold(
            family="arx2",
            variant="target_only",
            projected_rows=missing_predecessor,
            sealed_validation=sealed_fold,
        )
