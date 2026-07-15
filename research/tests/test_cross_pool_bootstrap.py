from __future__ import annotations

import math
from dataclasses import replace
from typing import cast

import numpy as np
import pytest

from research.cross_pool.bootstrap import (
    _draw_day_indices,
    _nearest_rank_indices,
    assess_adequacy,
    bootstrap_predictive,
    classify_predictive,
    infer_predictions,
    summarize_predictions,
    validate_prediction_rows,
)
from research.cross_pool.contracts import (
    AdequacyAudit,
    ConfidenceInterval,
    CrossPoolContractError,
    Direction,
    EvidenceClass,
    InfluenceReport,
    OmissionResult,
    PredictionRow,
    PredictiveBootstrap,
    PredictiveMetrics,
    Regime,
    RegimeSubsetInference,
)
from research.cross_pool.qa import (
    assess_prediction_influence,
    assess_regime_sensitivity,
    audit_predictions,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
FIRST_REFIT_MS = 1_767_571_200_000


def test_summarize_predictions_matches_hand_calculated_paired_metrics() -> None:
    rows = (
        _prediction_row(day=0, hour=0, actual=10.0, baseline=0.0, cross=8.0),
        _prediction_row(day=0, hour=1, actual=-10.0, baseline=-5.0, cross=-8.0),
        _prediction_row(day=0, hour=2, actual=5.0, baseline=5.0, cross=3.0),
        _prediction_row(day=0, hour=3, actual=-5.0, baseline=-5.0, cross=-3.0),
    )

    metrics = summarize_predictions(rows)

    assert metrics.direction == "bsc_to_base"
    assert metrics.horizon_ms == HOUR_MS
    assert metrics.rows == 4
    assert metrics.target_day_count == 1
    assert metrics.conditional_rows == 2
    assert metrics.conditional_target_day_count == 1
    assert metrics.baseline_mae_bps == 3.75
    assert metrics.cross_mae_bps == 2.0
    assert metrics.mae_improvement_bps == 1.75
    assert metrics.baseline_mse_bps2 == 31.25
    assert metrics.cross_mse_bps2 == 4.0
    assert metrics.mse_improvement_bps2 == 27.25
    assert metrics.baseline_rmse_bps == pytest.approx(math.sqrt(31.25))
    assert metrics.cross_rmse_bps == 2.0
    assert metrics.relative_oos_r2 == pytest.approx(0.872)
    assert metrics.baseline_conditional_directional_accuracy == 0.5
    assert metrics.cross_conditional_directional_accuracy == 1.0
    assert metrics.conditional_directional_accuracy_gain == 0.5


def test_directional_subset_includes_exact_threshold_and_zero_is_a_miss() -> None:
    rows = (
        _prediction_row(day=0, hour=0, actual=10.0, baseline=10.0, cross=0.0),
        _prediction_row(day=0, hour=1, actual=-10.0, baseline=10.0, cross=-1.0),
        _prediction_row(day=0, hour=2, actual=9.999, baseline=9.999, cross=9.999),
        _prediction_row(day=0, hour=3, actual=-9.999, baseline=-9.999, cross=-9.999),
    )

    metrics = summarize_predictions(rows)

    assert metrics.conditional_rows == 2
    assert metrics.baseline_conditional_directional_accuracy == 0.5
    assert metrics.cross_conditional_directional_accuracy == 0.5
    assert metrics.conditional_directional_accuracy_gain == 0.0


def test_relative_oos_r2_is_unavailable_when_baseline_sse_is_zero() -> None:
    rows = (
        _prediction_row(day=0, hour=0, actual=2.0, baseline=2.0, cross=1.0),
        _prediction_row(day=1, hour=0, actual=-2.0, baseline=-2.0, cross=-1.0),
    )

    assert summarize_predictions(rows).relative_oos_r2 is None


def test_extreme_finite_negative_r2_uses_canonical_mse_components() -> None:
    metrics = summarize_predictions(
        (_prediction_row(day=0, actual=0.0, baseline=-1e-50, cross=-1e3),)
    )

    assert metrics.relative_oos_r2 is not None
    assert math.isfinite(metrics.relative_oos_r2)
    assert metrics.relative_oos_r2 == (1.0 - metrics.cross_mse_bps2 / metrics.baseline_mse_bps2)


def test_target_utc_day_not_origin_day_defines_bootstrap_blocks() -> None:
    rows = (
        _prediction_row(day=0, hour=23, actual=10.0, baseline=0.0, cross=10.0),
        _prediction_row(day=1, hour=0, actual=10.0, baseline=0.0, cross=10.0),
    )

    metrics = summarize_predictions(rows)

    assert metrics.rows == 2
    assert metrics.target_day_count == 1
    assert metrics.conditional_target_day_count == 1


def test_nearest_rank_indices_are_exact_for_frozen_bootstrap() -> None:
    assert _nearest_rank_indices(2_000, 0.95) == (49, 1_949)


def test_nearest_rank_uses_decimal_confidence_text_not_binary_subtraction() -> None:
    lower, upper = _nearest_rank_indices(2_000, 0.95)
    binary_float_lower = math.ceil(((1.0 - 0.95) / 2.0) * 2_000) - 1

    assert lower == 49
    assert binary_float_lower == 50
    assert upper == 1_949


def test_nearest_rank_stays_exact_for_tiny_accepted_confidence() -> None:
    assert _nearest_rank_indices(2, 1e-30) == (0, 1)


def test_frozen_pcg64_draw_fixture_matches_four_day_production_call() -> None:
    draws = _draw_day_indices(day_count=4, resamples=2, seed=20_260_715)

    assert draws.dtype == np.int64
    assert draws.tolist() == [[2, 0, 2, 0], [1, 2, 3, 1]]


def test_bootstrap_is_deterministic_and_carries_provenance() -> None:
    rows = _adequate_rows()

    first = bootstrap_predictive(rows, resamples=128, seed=20_260_715)
    second = bootstrap_predictive(rows, resamples=128, seed=20_260_715)

    assert first == second
    assert first.direction == "bsc_to_base"
    assert first.horizon_ms == HOUR_MS
    assert first.resamples == 128
    assert first.seed == 20_260_715
    assert first.confidence_level == 0.95
    assert first.valid_conditional_resamples == 128


def test_bootstrap_repeats_all_rows_in_sampled_target_days() -> None:
    rows = (
        _prediction_row(day=0, hour=0, actual=10.0, baseline=0.0, cross=10.0),
        _prediction_row(day=1, hour=0, actual=10.0, baseline=10.0, cross=0.0),
        _prediction_row(day=1, hour=1, actual=10.0, baseline=10.0, cross=0.0),
    )

    result = bootstrap_predictive(rows, resamples=8, seed=7, confidence_level=0.5)

    assert result.mae_improvement_bps.point == pytest.approx(-10.0 / 3.0)
    assert result.mae_improvement_bps.lower == -10.0
    assert result.mae_improvement_bps.upper == pytest.approx(-10.0 / 3.0)
    assert result.mse_improvement_bps2.point == pytest.approx(-100.0 / 3.0)
    assert result.mse_improvement_bps2.lower == -100.0
    assert result.mse_improvement_bps2.upper == pytest.approx(-100.0 / 3.0)
    assert result.cross_conditional_directional_accuracy is not None
    assert result.cross_conditional_directional_accuracy.point == pytest.approx(1.0 / 3.0)
    assert result.cross_conditional_directional_accuracy.lower == 0.0
    assert result.cross_conditional_directional_accuracy.upper == pytest.approx(1.0 / 3.0)
    assert result.conditional_directional_accuracy_gain is not None
    assert result.conditional_directional_accuracy_gain.point == pytest.approx(-1.0 / 3.0)
    assert result.conditional_directional_accuracy_gain.lower == -1.0
    assert result.conditional_directional_accuracy_gain.upper == pytest.approx(-1.0 / 3.0)


def test_sampled_day_aggregation_uses_math_fsum_rounding() -> None:
    rows = (
        _prediction_row(day=0, actual=1e16, baseline=0.0, cross=1e16),
        _prediction_row(day=1, actual=1.0, baseline=0.0, cross=1.0),
        _prediction_row(day=2, actual=1.0, baseline=0.0, cross=1.0),
    )

    result = bootstrap_predictive(rows, resamples=1, seed=35, confidence_level=0.5)

    assert _draw_day_indices(day_count=3, resamples=1, seed=35).tolist() == [[0, 1, 2]]
    assert result.mae_improvement_bps.point == 3_333_333_333_333_334.0
    assert result.mae_improvement_bps.lower == 3_333_333_333_333_334.0
    assert result.mae_improvement_bps.upper == 3_333_333_333_333_334.0


def test_bootstrap_subtracts_normalized_sampled_losses() -> None:
    rows = (
        _prediction_row(day=0, actual=0.0, baseline=-1e16, cross=-1e16),
        _prediction_row(day=1, actual=0.0, baseline=-1.0, cross=0.0),
        _prediction_row(day=2, actual=0.0, baseline=-1.0, cross=0.0),
    )

    result = bootstrap_predictive(rows, resamples=1, seed=35, confidence_level=0.5)

    assert result.mae_improvement_bps.point == 0.5
    assert result.mae_improvement_bps.lower == 0.5
    assert result.mae_improvement_bps.upper == 0.5


def test_directional_intervals_are_unavailable_if_any_draw_has_no_eligible_row() -> None:
    rows = (
        _prediction_row(day=0, hour=0, actual=12.0, baseline=0.0, cross=12.0),
        _prediction_row(day=1, hour=0, actual=5.0, baseline=0.0, cross=5.0),
    )

    result = bootstrap_predictive(rows, resamples=8, seed=7, confidence_level=0.5)

    assert result.valid_conditional_resamples == 5
    assert result.cross_conditional_directional_accuracy is None
    assert result.conditional_directional_accuracy_gain is None


@pytest.mark.parametrize(
    ("resamples", "seed", "confidence_level", "match"),
    [
        (0, 0, 0.95, "resamples must be positive"),
        (-1, 0, 0.95, "resamples must be positive"),
        (1, -1, 0.95, "seed must be nonnegative"),
        (1, 0, 0.0, "confidence_level must be finite and between 0 and 1"),
        (1, 0, 1.0, "confidence_level must be finite and between 0 and 1"),
        (1, 0, math.inf, "confidence_level must be finite and between 0 and 1"),
        (1, 0, math.nan, "confidence_level must be finite and between 0 and 1"),
    ],
)
def test_bootstrap_configuration_fails_closed(
    resamples: int,
    seed: int,
    confidence_level: float,
    match: str,
) -> None:
    with pytest.raises(CrossPoolContractError, match=match):
        bootstrap_predictive(
            _adequate_rows(),
            resamples=resamples,
            seed=seed,
            confidence_level=confidence_level,
        )


def test_classification_applies_positive_evidence_requirements() -> None:
    metrics, bootstrap = _positive_classification_inputs()

    assert classify_predictive(metrics, bootstrap) == "positive_evidence"


@pytest.mark.parametrize("boundary", ["mae", "mse", "direction"])
def test_positive_class_requires_strict_interval_bounds(boundary: str) -> None:
    metrics, bootstrap = _positive_classification_inputs()
    if boundary == "mae":
        bootstrap = replace(
            bootstrap,
            mae_improvement_bps=replace(bootstrap.mae_improvement_bps, lower=0.0),
        )
    elif boundary == "mse":
        bootstrap = replace(
            bootstrap,
            mse_improvement_bps2=replace(bootstrap.mse_improvement_bps2, lower=0.0),
        )
    else:
        assert bootstrap.cross_conditional_directional_accuracy is not None
        bootstrap = replace(
            bootstrap,
            cross_conditional_directional_accuracy=replace(
                bootstrap.cross_conditional_directional_accuracy,
                lower=0.5,
            ),
        )

    assert classify_predictive(metrics, bootstrap) == "suggestive"


def test_affirmative_null_precedes_suggestive() -> None:
    metrics, bootstrap = _affirmative_null_inputs()

    assert metrics.mae_improvement_bps > 0.0
    assert metrics.mse_improvement_bps2 > 0.0
    assert classify_predictive(metrics, bootstrap) == "affirmative_null"


def test_positive_evidence_precedes_overlapping_affirmative_null() -> None:
    metrics, bootstrap = _affirmative_null_inputs()
    bootstrap = replace(
        bootstrap,
        mae_improvement_bps=ConfidenceInterval(point=0.5, lower=0.1, upper=0.9),
        mse_improvement_bps2=ConfidenceInterval(point=0.5, lower=0.1, upper=1.0),
        cross_conditional_directional_accuracy=ConfidenceInterval(
            point=0.62,
            lower=0.6,
            upper=0.8,
        ),
    )

    assert classify_predictive(metrics, bootstrap) == "positive_evidence"


def test_missing_directional_intervals_disable_positive_and_affirmative_null() -> None:
    metrics, bootstrap = _positive_classification_inputs()
    bootstrap = replace(
        bootstrap,
        valid_conditional_resamples=bootstrap.resamples - 1,
        cross_conditional_directional_accuracy=None,
        conditional_directional_accuracy_gain=None,
    )

    assert classify_predictive(metrics, bootstrap) == "suggestive"


@pytest.mark.parametrize("boundary", ["mae", "direction"])
def test_affirmative_null_requires_strict_upper_bounds(boundary: str) -> None:
    metrics, bootstrap = _affirmative_null_inputs()
    if boundary == "mae":
        bootstrap = replace(
            bootstrap,
            mae_improvement_bps=replace(bootstrap.mae_improvement_bps, upper=1.0),
        )
    else:
        assert bootstrap.conditional_directional_accuracy_gain is not None
        bootstrap = replace(
            bootstrap,
            conditional_directional_accuracy_gain=replace(
                bootstrap.conditional_directional_accuracy_gain,
                upper=0.05,
            ),
        )

    assert classify_predictive(metrics, bootstrap) == "suggestive"


def test_adequacy_gate_forces_inconclusive_before_classification() -> None:
    metrics, bootstrap = _positive_classification_inputs()
    metrics = replace(metrics, target_day_count=19)

    assert assess_adequacy(metrics).underpowered
    assert classify_predictive(metrics, bootstrap) == "inconclusive"


def test_conditional_target_day_floor_is_independently_required() -> None:
    metrics, bootstrap = _positive_classification_inputs()
    metrics = replace(metrics, conditional_target_day_count=9)

    assert assess_adequacy(metrics).underpowered
    assert classify_predictive(metrics, bootstrap) == "inconclusive"


@pytest.mark.parametrize("metric", ["mae", "mse"])
def test_zero_point_improvement_fails_positive_and_suggestive(metric: str) -> None:
    metrics, bootstrap = _positive_classification_inputs()
    if metric == "mae":
        metrics = replace(
            metrics,
            cross_mae_bps=metrics.baseline_mae_bps,
            mae_improvement_bps=0.0,
        )
        bootstrap = replace(
            bootstrap,
            mae_improvement_bps=ConfidenceInterval(point=0.0, lower=0.0, upper=2.0),
        )
    else:
        metrics = replace(
            metrics,
            cross_mse_bps2=metrics.baseline_mse_bps2,
            mse_improvement_bps2=0.0,
            cross_rmse_bps=metrics.baseline_rmse_bps,
            relative_oos_r2=0.0,
        )
        bootstrap = replace(
            bootstrap,
            mse_improvement_bps2=ConfidenceInterval(point=0.0, lower=0.0, upper=5.0),
        )

    assert classify_predictive(metrics, bootstrap) == "inconclusive"


def test_classification_rejects_mismatched_provenance_and_interval_points() -> None:
    metrics, bootstrap = _positive_classification_inputs()

    with pytest.raises(CrossPoolContractError, match="direction and horizon"):
        classify_predictive(metrics, replace(bootstrap, direction="base_to_bsc"))
    with pytest.raises(CrossPoolContractError, match="interval point"):
        classify_predictive(
            metrics,
            replace(
                bootstrap,
                mae_improvement_bps=replace(bootstrap.mae_improvement_bps, point=999.0),
            ),
        )


def test_infer_predictions_uses_frozen_configuration_and_reports_one_day() -> None:
    result = infer_predictions((_prediction_row(day=0, actual=12.0, baseline=0.0, cross=12.0),))

    assert result.bootstrap.resamples == 2_000
    assert result.bootstrap.seed == 20_260_715
    assert result.bootstrap.confidence_level == 0.95
    assert result.adequacy.underpowered
    assert result.evidence_class == "inconclusive"


def test_leave_one_day_and_fold_detect_the_influential_unit() -> None:
    rows = _influential_rows()

    report = assess_prediction_influence(rows)

    assert report.full_mae_improvement_sign == 1
    assert report.unit_dependent
    special_day = next(item for item in report.leave_one_day if item.unit == "2026-02-01")
    special_fold = next(item for item in report.leave_one_fold if item.unit == "3")
    assert special_day.mae_improvement_sign == -1
    assert special_fold.mae_improvement_sign == -1
    assert special_day.evidence_class != report.full_evidence_class


def test_influence_detects_adequacy_class_change_without_sign_change() -> None:
    rows = tuple(
        _prediction_row(
            day=day,
            actual=12.0 if day % 2 == 0 else -12.0,
            baseline=0.0,
            cross=12.0 if day % 2 == 0 else -12.0,
        )
        for day in range(20)
    )

    report = assess_prediction_influence(rows)

    assert report.full_mae_improvement_sign == 1
    assert report.full_evidence_class == "positive_evidence"
    assert all(item.mae_improvement_sign == 1 for item in report.leave_one_day)
    assert all(item.evidence_class == "inconclusive" for item in report.leave_one_day)
    assert report.unit_dependent


def test_influence_and_complete_audit_require_two_days_and_two_folds() -> None:
    one_day = (_prediction_row(day=0, actual=12.0, baseline=0.0, cross=12.0),)
    one_fold = (
        _prediction_row(day=0, actual=12.0, baseline=0.0, cross=12.0),
        _prediction_row(day=1, actual=-12.0, baseline=0.0, cross=-12.0),
    )

    with pytest.raises(CrossPoolContractError, match="at least two target days"):
        assess_prediction_influence(one_day)
    with pytest.raises(CrossPoolContractError, match="at least two observed folds"):
        assess_prediction_influence(one_fold)
    with pytest.raises(CrossPoolContractError, match="at least two observed folds"):
        audit_predictions(one_fold)


def test_regime_sensitivity_preserves_all_nine_target_source_combinations() -> None:
    regimes = cast(tuple[Regime, ...], ("early", "mixed", "late"))
    rows = tuple(
        _prediction_row(
            day=index,
            actual=12.0,
            baseline=0.0,
            cross=12.0,
            target_regime=target,
            source_regime=source,
        )
        for index, (target, source) in enumerate(
            (target, source) for target in regimes for source in regimes
        )
    )

    report = assess_regime_sensitivity(rows)

    assert report.early.row_count == 1
    assert report.late.row_count == 1
    assert report.mixed.row_count == 7
    assert report.regime_not_adjudicable
    assert not report.regime_unstable


def test_regime_sensitivity_uses_typed_empty_subset_states() -> None:
    rows = tuple(
        _prediction_row(
            day=day,
            actual=12.0,
            baseline=0.0,
            cross=12.0,
            target_regime="early",
            source_regime="late",
        )
        for day in range(20)
    )

    report = assess_regime_sensitivity(rows)

    assert report.early == RegimeSubsetInference(
        regime="early",
        row_count=0,
        inference=None,
        unavailable_reason="no_rows",
    )
    assert report.late == RegimeSubsetInference(
        regime="late",
        row_count=0,
        inference=None,
        unavailable_reason="no_rows",
    )
    assert report.mixed.row_count == 20
    assert report.mixed.inference is not None
    assert report.regime_not_adjudicable


def test_adequate_early_late_reversal_is_regime_unstable() -> None:
    early = tuple(
        _prediction_row(
            day=day,
            actual=12.0 if day % 2 == 0 else -12.0,
            baseline=0.0,
            cross=12.0 if day % 2 == 0 else -12.0,
            target_regime="early",
            source_regime="early",
        )
        for day in range(20)
    )
    late = tuple(
        _prediction_row(
            day=day,
            actual=12.0 if day % 2 == 0 else -12.0,
            baseline=12.0 if day % 2 == 0 else -12.0,
            cross=0.0,
            target_regime="late",
            source_regime="late",
        )
        for day in range(20, 40)
    )

    report = assess_regime_sensitivity(early + late)

    assert report.early.inference is not None
    assert report.late.inference is not None
    assert report.early.inference.metrics.mae_improvement_bps > 0.0
    assert report.late.inference.metrics.mae_improvement_bps < 0.0
    assert report.regime_unstable
    assert not report.regime_not_adjudicable


def test_same_sign_different_regime_classes_are_unstable() -> None:
    early = tuple(
        _prediction_row(
            day=day,
            actual=12.0,
            baseline=0.0,
            cross=12.0,
            target_regime="early",
            source_regime="early",
        )
        for day in range(20)
    )
    late_ordinary = tuple(
        _prediction_row(
            day=day,
            actual=12.0,
            baseline=11.0,
            cross=10.0,
            target_regime="late",
            source_regime="late",
        )
        for day in range(20, 39)
    )
    late_special = _prediction_row(
        day=39,
        actual=112.0,
        baseline=0.0,
        cross=112.0,
        target_regime="late",
        source_regime="late",
    )

    report = assess_regime_sensitivity(early + late_ordinary + (late_special,))

    assert report.early.inference is not None
    assert report.late.inference is not None
    assert report.early.inference.metrics.mae_improvement_bps > 0.0
    assert report.late.inference.metrics.mae_improvement_bps > 0.0
    assert report.early.inference.evidence_class == "positive_evidence"
    assert report.late.inference.evidence_class == "suggestive"
    assert report.regime_unstable
    assert not report.regime_not_adjudicable


def test_conditional_support_shortfall_makes_regimes_not_adjudicable() -> None:
    early = tuple(
        _prediction_row(
            day=day,
            actual=12.0 if day < 9 else 5.0,
            baseline=0.0,
            cross=12.0 if day < 9 else 5.0,
            target_regime="early",
            source_regime="early",
        )
        for day in range(20)
    )
    late = tuple(
        _prediction_row(
            day=day,
            actual=12.0,
            baseline=0.0,
            cross=12.0,
            target_regime="late",
            source_regime="late",
        )
        for day in range(20, 40)
    )

    report = assess_regime_sensitivity(early + late)

    assert report.early.inference is not None
    assert report.early.inference.adequacy.conditional_target_day_count == 9
    assert report.early.inference.adequacy.underpowered
    assert report.regime_not_adjudicable
    assert not report.regime_unstable


def test_zero_versus_positive_sign_is_not_an_opposite_sign_reversal() -> None:
    early = tuple(
        _prediction_row(
            day=day,
            actual=12.0,
            baseline=11.1,
            cross=11.1,
            target_regime="early",
            source_regime="early",
        )
        for day in range(20)
    )
    late = tuple(
        _prediction_row(
            day=day,
            actual=12.0,
            baseline=11.1,
            cross=4.0 if day == 39 else 12.0,
            target_regime="late",
            source_regime="late",
        )
        for day in range(20, 40)
    )

    report = assess_regime_sensitivity(early + late)

    assert report.early.inference is not None
    assert report.late.inference is not None
    assert report.early.inference.metrics.mae_improvement_bps == 0.0
    assert report.late.inference.metrics.mae_improvement_bps > 0.0
    assert report.early.inference.evidence_class == "affirmative_null"
    assert report.late.inference.evidence_class == "affirmative_null"
    assert not report.regime_unstable
    assert not report.regime_not_adjudicable


def test_complete_directional_audit_carries_consistent_provenance() -> None:
    result = audit_predictions(_adequate_rows())

    assert result.direction == "bsc_to_base"
    assert result.horizon_ms == HOUR_MS
    assert result.inference.metrics.direction == result.direction
    assert result.inference.bootstrap.horizon_ms == result.horizon_ms
    assert result.influence.direction == result.direction
    assert result.regime_sensitivity.horizon_ms == result.horizon_ms


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("empty", "at least one prediction"),
        ("timestamp", "timestamps must be strictly increasing"),
        ("target", "target timestamp must equal"),
        ("direction", "one direction"),
        ("horizon", "one positive horizon"),
        ("nonpositive_horizon", "one positive horizon"),
        ("nonfinite", "prediction values must be finite"),
        ("regime", "unsupported regime"),
        ("negative_fold", "fold indices must be nonnegative"),
        ("decreasing_fold", "fold indices must be nondecreasing"),
        ("refit_mismatch", "one refit timestamp per fold"),
        ("outside_fold", "half-open refit window"),
        ("upper_fold_boundary", "half-open refit window"),
    ],
)
def test_prediction_row_validation_fails_closed(mutation: str, match: str) -> None:
    first = _prediction_row(day=0, hour=0)
    second = _prediction_row(day=0, hour=1)
    rows: tuple[PredictionRow, ...]
    if mutation == "empty":
        rows = ()
    elif mutation == "timestamp":
        rows = (first, replace(second, timestamp_ms=first.timestamp_ms))
    elif mutation == "target":
        rows = (replace(first, target_timestamp_ms=first.target_timestamp_ms + 1),)
    elif mutation == "direction":
        rows = (first, replace(second, direction="base_to_bsc"))
    elif mutation == "horizon":
        rows = (
            first,
            replace(
                second,
                horizon_ms=2 * HOUR_MS,
                target_timestamp_ms=second.timestamp_ms + 2 * HOUR_MS,
            ),
        )
    elif mutation == "nonpositive_horizon":
        rows = (replace(first, horizon_ms=0, target_timestamp_ms=first.timestamp_ms),)
    elif mutation == "nonfinite":
        rows = (replace(first, actual_bps=math.inf),)
    elif mutation == "regime":
        rows = (replace(first, target_regime=cast(Regime, "unknown")),)
    elif mutation == "negative_fold":
        rows = (replace(first, fold_index=-1),)
    elif mutation == "decreasing_fold":
        later_fold = _prediction_row(day=7)
        rows = (
            later_fold,
            replace(_prediction_row(day=8), fold_index=0, refit_timestamp_ms=FIRST_REFIT_MS),
        )
    elif mutation == "refit_mismatch":
        rows = (first, replace(second, refit_timestamp_ms=FIRST_REFIT_MS + 1))
    elif mutation == "outside_fold":
        rows = (replace(first, refit_timestamp_ms=first.timestamp_ms + 1),)
    elif mutation == "upper_fold_boundary":
        rows = (
            replace(
                first,
                timestamp_ms=FIRST_REFIT_MS + WEEK_MS,
                target_timestamp_ms=FIRST_REFIT_MS + WEEK_MS + HOUR_MS,
            ),
        )
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(CrossPoolContractError, match=match):
        summarize_predictions(rows)


def test_observed_fold_gaps_are_valid_for_filtered_subsets() -> None:
    rows = (
        _prediction_row(day=0, actual=12.0, baseline=0.0, cross=12.0),
        _prediction_row(day=14, actual=-12.0, baseline=0.0, cross=-12.0),
    )

    assert summarize_predictions(rows).rows == 2


def test_distinct_observed_folds_require_increasing_refit_timestamps() -> None:
    rows = (
        _prediction_row(day=0),
        replace(
            _prediction_row(day=1),
            fold_index=1,
            refit_timestamp_ms=FIRST_REFIT_MS,
        ),
    )

    with pytest.raises(CrossPoolContractError, match="increasing refit timestamps"):
        validate_prediction_rows(rows)


@pytest.mark.parametrize("case", ["derived", "aggregate"])
def test_derived_and_aggregate_overflow_fail_closed(case: str) -> None:
    rows = (
        (_prediction_row(day=0, actual=1e308, baseline=-1e308, cross=0.0),)
        if case == "derived"
        else (
            _prediction_row(day=0, actual=1e154, baseline=0.0, cross=1e154),
            _prediction_row(day=1, actual=1e154, baseline=0.0, cross=1e154),
        )
    )

    with pytest.raises(CrossPoolContractError, match="finite"):
        summarize_predictions(rows)


def test_sampled_bootstrap_overflow_fails_closed() -> None:
    rows = (
        _prediction_row(day=0, actual=1e154, baseline=0.0, cross=1e154),
        _prediction_row(day=1, actual=0.0, baseline=0.0, cross=0.0),
    )

    assert math.isfinite(summarize_predictions(rows).baseline_mse_bps2)
    with pytest.raises(CrossPoolContractError, match="sampled bootstrap values must be finite"):
        bootstrap_predictive(rows, resamples=8, seed=7, confidence_level=0.5)


def test_contracts_reject_incoherent_optional_and_nested_states() -> None:
    with pytest.raises(CrossPoolContractError, match="lower must not exceed upper"):
        ConfidenceInterval(point=0.0, lower=1.0, upper=-1.0)
    assert ConfidenceInterval(point=2.0, lower=0.0, upper=1.0).point == 2.0

    metrics, bootstrap = _positive_classification_inputs()
    with pytest.raises(CrossPoolContractError, match="conditional target-day support"):
        replace(metrics, conditional_rows=5, conditional_target_day_count=10)
    with pytest.raises(CrossPoolContractError, match="conditional intervals"):
        replace(bootstrap, valid_conditional_resamples=bootstrap.resamples - 1)
    with pytest.raises(CrossPoolContractError, match="accuracy interval"):
        replace(
            bootstrap,
            cross_conditional_directional_accuracy=ConfidenceInterval(
                point=0.8,
                lower=2.0,
                upper=3.0,
            ),
        )
    with pytest.raises(CrossPoolContractError, match="gain interval"):
        replace(
            bootstrap,
            conditional_directional_accuracy_gain=ConfidenceInterval(
                point=0.3,
                lower=-3.0,
                upper=-2.0,
            ),
        )

    inference = infer_predictions(_adequate_rows())
    with pytest.raises(CrossPoolContractError, match="empty regime subset"):
        RegimeSubsetInference(
            regime="early",
            row_count=0,
            inference=inference,
            unavailable_reason="no_rows",
        )

    omission = OmissionResult(
        unit="2026-01-01",
        mae_improvement_sign=1,
        evidence_class=cast(EvidenceClass, "suggestive"),
    )
    with pytest.raises(CrossPoolContractError, match="unit_dependent"):
        InfluenceReport(
            direction=cast(Direction, "bsc_to_base"),
            horizon_ms=HOUR_MS,
            full_mae_improvement_sign=1,
            full_evidence_class="suggestive",
            leave_one_day=(replace(omission, mae_improvement_sign=-1),),
            leave_one_fold=(replace(omission, unit="0"),),
            unit_dependent=False,
        )

    assert assess_adequacy(metrics) == AdequacyAudit(
        target_day_count=20,
        conditional_target_day_count=10,
    )


def test_predictive_inference_rejects_forged_derived_outcomes() -> None:
    inference = infer_predictions(_adequate_rows())

    with pytest.raises(CrossPoolContractError, match="MAE improvement"):
        replace(inference.metrics, mae_improvement_bps=999.0)
    with pytest.raises(CrossPoolContractError, match="MAE improvement"):
        replace(
            inference.metrics,
            cross_mae_bps=inference.metrics.baseline_mae_bps,
            mae_improvement_bps=5e-13,
        )
    with pytest.raises(CrossPoolContractError, match="directional accuracy gain"):
        replace(
            inference.metrics,
            cross_conditional_directional_accuracy=(
                inference.metrics.baseline_conditional_directional_accuracy
            ),
            conditional_directional_accuracy_gain=5e-13,
        )
    with pytest.raises(CrossPoolContractError, match="relative OOS R-squared"):
        replace(inference.metrics, relative_oos_r2=0.0)
    with pytest.raises(CrossPoolContractError, match="evidence class"):
        replace(inference, evidence_class="inconclusive")

    nonfrozen = replace(
        inference.bootstrap,
        resamples=1_999,
        valid_conditional_resamples=1_999,
    )
    with pytest.raises(CrossPoolContractError, match="frozen bootstrap configuration"):
        replace(inference, bootstrap=nonfrozen)


def test_regime_and_directional_audits_reject_forged_robustness_states() -> None:
    audit = audit_predictions(_adequate_rows())

    with pytest.raises(CrossPoolContractError, match="regime flags"):
        replace(
            audit.regime_sensitivity,
            regime_not_adjudicable=False,
        )

    duplicated_mixed = RegimeSubsetInference(
        regime="mixed",
        row_count=audit.inference.metrics.rows,
        inference=audit.inference,
        unavailable_reason=None,
    )
    duplicated_partition = replace(
        audit.regime_sensitivity,
        mixed=duplicated_mixed,
    )
    with pytest.raises(CrossPoolContractError, match="regime subset rows"):
        replace(audit, regime_sensitivity=duplicated_partition)


def _prediction_row(
    *,
    day: int,
    hour: int = 0,
    actual: float = 1.0,
    baseline: float = 0.0,
    cross: float = 0.0,
    target_regime: Regime = "early",
    source_regime: Regime = "early",
    direction: Direction = "bsc_to_base",
) -> PredictionRow:
    timestamp_ms = FIRST_REFIT_MS + day * DAY_MS + hour * HOUR_MS
    fold_index = day // 7
    return PredictionRow(
        timestamp_ms=timestamp_ms,
        target_timestamp_ms=timestamp_ms + HOUR_MS,
        horizon_ms=HOUR_MS,
        refit_timestamp_ms=FIRST_REFIT_MS + fold_index * WEEK_MS,
        fold_index=fold_index,
        direction=direction,
        target_regime=target_regime,
        source_regime=source_regime,
        actual_bps=actual,
        baseline_prediction_bps=baseline,
        cross_prediction_bps=cross,
    )


def _adequate_rows() -> tuple[PredictionRow, ...]:
    return tuple(
        _prediction_row(
            day=day,
            actual=12.0 if day % 2 == 0 else -12.0,
            baseline=0.0,
            cross=12.0 if day % 2 == 0 else -12.0,
        )
        for day in range(28)
    )


def _influential_rows() -> tuple[PredictionRow, ...]:
    ordinary = tuple(
        _prediction_row(
            day=day,
            actual=12.0,
            baseline=11.0,
            cross=10.0,
        )
        for day in range(27)
    )
    special = _prediction_row(
        day=27,
        actual=112.0,
        baseline=0.0,
        cross=112.0,
    )
    return ordinary + (special,)


def _positive_classification_inputs() -> tuple[PredictiveMetrics, PredictiveBootstrap]:
    metrics = PredictiveMetrics(
        direction="bsc_to_base",
        horizon_ms=HOUR_MS,
        rows=20,
        target_day_count=20,
        conditional_rows=10,
        conditional_target_day_count=10,
        baseline_mae_bps=2.0,
        cross_mae_bps=1.0,
        mae_improvement_bps=1.0,
        baseline_mse_bps2=4.0,
        cross_mse_bps2=1.0,
        mse_improvement_bps2=3.0,
        baseline_rmse_bps=2.0,
        cross_rmse_bps=1.0,
        relative_oos_r2=0.75,
        baseline_conditional_directional_accuracy=0.5,
        cross_conditional_directional_accuracy=0.8,
        conditional_directional_accuracy_gain=0.8 - 0.5,
    )
    bootstrap = PredictiveBootstrap(
        direction="bsc_to_base",
        horizon_ms=HOUR_MS,
        resamples=2_000,
        seed=20_260_715,
        confidence_level=0.95,
        mae_improvement_bps=ConfidenceInterval(point=1.0, lower=0.2, upper=1.8),
        mse_improvement_bps2=ConfidenceInterval(point=3.0, lower=0.5, upper=5.0),
        cross_conditional_directional_accuracy=ConfidenceInterval(
            point=0.8,
            lower=0.6,
            upper=0.9,
        ),
        conditional_directional_accuracy_gain=ConfidenceInterval(
            point=0.8 - 0.5,
            lower=0.1,
            upper=0.4,
        ),
        valid_conditional_resamples=2_000,
    )
    return metrics, bootstrap


def _affirmative_null_inputs() -> tuple[PredictiveMetrics, PredictiveBootstrap]:
    metrics = PredictiveMetrics(
        direction="bsc_to_base",
        horizon_ms=HOUR_MS,
        rows=20,
        target_day_count=20,
        conditional_rows=10,
        conditional_target_day_count=10,
        baseline_mae_bps=2.0,
        cross_mae_bps=1.5,
        mae_improvement_bps=0.5,
        baseline_mse_bps2=4.0,
        cross_mse_bps2=3.5,
        mse_improvement_bps2=0.5,
        baseline_rmse_bps=2.0,
        cross_rmse_bps=math.sqrt(3.5),
        relative_oos_r2=0.125,
        baseline_conditional_directional_accuracy=0.6,
        cross_conditional_directional_accuracy=0.62,
        conditional_directional_accuracy_gain=0.62 - 0.6,
    )
    bootstrap = PredictiveBootstrap(
        direction="bsc_to_base",
        horizon_ms=HOUR_MS,
        resamples=2_000,
        seed=20_260_715,
        confidence_level=0.95,
        mae_improvement_bps=ConfidenceInterval(point=0.5, lower=-0.2, upper=0.9),
        mse_improvement_bps2=ConfidenceInterval(point=0.5, lower=-1.0, upper=1.0),
        cross_conditional_directional_accuracy=ConfidenceInterval(
            point=0.62,
            lower=0.4,
            upper=0.8,
        ),
        conditional_directional_accuracy_gain=ConfidenceInterval(
            point=0.62 - 0.6,
            lower=-0.1,
            upper=0.04,
        ),
        valid_conditional_resamples=2_000,
    )
    return metrics, bootstrap
