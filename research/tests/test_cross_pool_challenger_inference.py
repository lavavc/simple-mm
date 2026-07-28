from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from research.cross_pool.challenger_contracts import (
    ChallengerContrast,
    ChallengerPrediction,
)
from research.cross_pool.challenger_inference import (
    BOOTSTRAP_RESAMPLES,
    _apply_max_t,
    _circular_block_draws,
    _contrast_row,
    _ContrastWork,
    _metric_row,
    infer_challengers,
)
from research.cross_pool.challenger_parent import load_challenger_parent
from research.cross_pool.challengers import run_challengers

PARENT_DIR = Path("research/results/cross_pool_lead_lag")


def _prediction(
    *,
    timestamp_ms: int,
    actual_bps: float,
    prediction_bps: float,
    updated: bool,
    target_age_ms: int = 1_000,
    source_age_ms: int = 1_000,
    update_probability: float | None = None,
    conditional_prediction_bps: float | None = None,
) -> ChallengerPrediction:
    return ChallengerPrediction(
        timestamp_ms=timestamp_ms,
        target_timestamp_ms=timestamp_ms + 3_600_000,
        horizon_ms=3_600_000,
        refit_timestamp_ms=timestamp_ms,
        fold_index=0,
        direction="bsc_to_base",
        family="two_part" if update_probability is not None else "ols",
        variant="target_only",
        target_regime="early",
        source_regime="late",
        target_age_ms=target_age_ms,
        source_age_ms=source_age_ms,
        actual_bps=actual_bps,
        updated=updated,
        prediction_bps=prediction_bps,
        update_probability=update_probability,
        conditional_prediction_bps=conditional_prediction_bps,
    )


def test_metric_row_pins_losses_incidence_direction_and_two_part_components() -> None:
    rows = (
        _prediction(
            timestamp_ms=1_700_000_000_000,
            actual_bps=12.0,
            prediction_bps=9.0,
            updated=True,
            update_probability=0.8,
            conditional_prediction_bps=11.0,
        ),
        _prediction(
            timestamp_ms=1_700_003_600_000,
            actual_bps=0.0,
            prediction_bps=1.0,
            updated=False,
            update_probability=0.25,
            conditional_prediction_bps=4.0,
        ),
    )

    metric = _metric_row(rows, support="all")

    assert metric.rows == 2
    assert metric.update_rows == 1
    assert metric.update_incidence == 0.5
    assert metric.mae_bps == 2.0
    assert metric.mse_bps2 == 5.0
    assert metric.rmse_bps == pytest.approx(np.sqrt(5.0))
    assert metric.directional_rows == 1
    assert metric.directional_accuracy == 1.0
    assert metric.brier_loss == pytest.approx(((1.0 - 0.8) ** 2 + 0.25**2) / 2.0)
    assert metric.log_loss == pytest.approx(-(np.log(0.8) + np.log(0.75)) / 2.0)
    assert metric.conditional_update_mae_bps == 1.0
    assert metric.calibration_error == pytest.approx((0.8 + 0.25) / 2.0 - 0.5)


def test_freshness_metric_uses_both_decision_time_ages() -> None:
    rows = (
        _prediction(
            timestamp_ms=1_700_000_000_000,
            actual_bps=1.0,
            prediction_bps=0.0,
            updated=True,
        ),
        _prediction(
            timestamp_ms=1_700_003_600_000,
            actual_bps=2.0,
            prediction_bps=0.0,
            updated=True,
            target_age_ms=3_600_001,
        ),
        _prediction(
            timestamp_ms=1_700_007_200_000,
            actual_bps=3.0,
            prediction_bps=0.0,
            updated=True,
            source_age_ms=3_600_001,
        ),
    )

    metric = _metric_row(rows, support="both_age_le_1h")

    assert metric.rows == 1
    assert metric.mae_bps == 1.0


def test_global_circular_draws_are_deterministic_seven_day_blocks() -> None:
    days = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(88))

    first = _circular_block_draws(days)
    second = _circular_block_draws(days)

    assert first.shape == (10_000, 88)
    assert np.array_equal(first, second)
    assert np.all((first >= 0) & (first < 88))
    for block_start in range(0, 84, 7):
        block = first[:25, block_start : block_start + 7]
        assert np.all(block[:, 1:] == (block[:, :-1] + 1) % 88)


@pytest.fixture(scope="module")
def study():
    parent = load_challenger_parent(PARENT_DIR)
    return run_challengers(parent)


@pytest.fixture(scope="module")
def evidence(study):
    return infer_challengers(study)


def _paired_two_part_rows(
    *,
    update_indices: set[int],
    included_indices: set[int] | None = None,
) -> tuple[
    tuple[date, ...],
    tuple[ChallengerPrediction, ...],
    tuple[ChallengerPrediction, ...],
]:
    days = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(88))
    included = set(range(88)) if included_indices is None else included_indices
    comparator: list[ChallengerPrediction] = []
    candidate: list[ChallengerPrediction] = []
    for index, day in enumerate(days):
        if index not in included:
            continue
        target_timestamp_ms = int(
            datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1_000
        )
        timestamp_ms = target_timestamp_ms - 3_600_000
        updated = index in update_indices
        comparator_conditional = 2.0 + float(index % 5)
        candidate_conditional = 1.0 + float(index % 3)
        base = _prediction(
            timestamp_ms=timestamp_ms,
            actual_bps=0.0,
            prediction_bps=0.5 * comparator_conditional,
            updated=updated,
            update_probability=0.5,
            conditional_prediction_bps=comparator_conditional,
        )
        comparator.append(replace(base, variant="source_age"))
        candidate.append(
            replace(
                base,
                variant="full_source",
                prediction_bps=0.5 * candidate_conditional,
                conditional_prediction_bps=candidate_conditional,
            )
        )
    return days, tuple(comparator), tuple(candidate)


def _source_price_contrast(
    *,
    comparator: tuple[ChallengerPrediction, ...],
    candidate: tuple[ChallengerPrediction, ...],
    target_days: tuple[date, ...],
    endpoint: str,
    draws: np.ndarray,
):
    return _contrast_row(
        family="two_part",
        direction="bsc_to_base",
        horizon_ms=3_600_000,
        support="all",
        endpoint=endpoint,
        contrast="source_price",
        comparator_variant="source_age",
        candidate_variant="full_source",
        comparator_rows=comparator,
        candidate_rows=candidate,
        failure_reason=None,
        target_days=target_days,
        draws=draws,
    )


def test_full_registry_and_global_adjustment_are_complete(evidence) -> None:
    assert len(evidence.metrics) == 5 * 3 * 2 * 3 * 3
    assert len(evidence.contrasts) == (4 * 1 + 1 * 3) * 3 * 2 * 3 * 3
    source_price = [row for row in evidence.contrasts if row.contrast == "source_price"]
    assert len(source_price) == 126
    assert any(row.status == "adjudicable" for row in source_price)
    assert any(row.status == "not_adjudicable" for row in source_price)
    for row in source_price:
        if row.status == "adjudicable":
            assert row.raw_lower is not None and row.raw_upper is not None
            assert row.simultaneous_lower is not None
            assert row.simultaneous_upper is not None
            assert row.adjusted_p_value is not None
            assert 0.0 < row.adjusted_p_value <= 1.0
        else:
            assert row.simultaneous_lower is None
            assert row.simultaneous_upper is None
            assert row.adjusted_p_value is None


def test_conditional_gate_uses_target_days_plus_separate_update_days() -> None:
    update_indices = {round(index * 88 / 20) % 88 for index in range(20)}
    days, comparator, candidate = _paired_two_part_rows(
        update_indices=update_indices,
    )

    row, _, _ = _source_price_contrast(
        comparator=comparator,
        candidate=candidate,
        target_days=days,
        endpoint="conditional_update_mae",
        draws=_circular_block_draws(days),
    )

    assert row.status == "adjudicable"
    assert row.target_days == 88
    assert row.update_days == 20

    _, comparator_19, candidate_19 = _paired_two_part_rows(
        update_indices=set(sorted(update_indices)[:-1]),
    )
    row_19, _, _ = _source_price_contrast(
        comparator=comparator_19,
        candidate=candidate_19,
        target_days=days,
        endpoint="conditional_update_mae",
        draws=_circular_block_draws(days),
    )
    assert row_19.status == "not_adjudicable"
    assert row_19.reason == "requires 20 OOS update-days"


def test_contrast_gates_41_target_days_and_zero_total_draw_without_redraw() -> None:
    days, comparator, candidate = _paired_two_part_rows(
        update_indices=set(range(41)),
        included_indices=set(range(41)),
    )
    row, _, _ = _source_price_contrast(
        comparator=comparator,
        candidate=candidate,
        target_days=days,
        endpoint="mae",
        draws=_circular_block_draws(days),
    )
    assert row.status == "not_adjudicable"
    assert row.reason == "requires 42 OOS target days"

    scattered_indices = set(range(0, 84, 2))
    _, scattered_comparator, scattered_candidate = _paired_two_part_rows(
        update_indices=scattered_indices,
        included_indices=scattered_indices,
    )
    scattered_row, _, _ = _source_price_contrast(
        comparator=scattered_comparator,
        candidate=scattered_candidate,
        target_days=days,
        endpoint="mae",
        draws=_circular_block_draws(days),
    )
    assert scattered_row.status == "not_adjudicable"
    assert scattered_row.reason == "requires 6 complete seven-day blocks"

    boundary_indices = set(range(39)) | {85, 86, 87}
    _, boundary_comparator, boundary_candidate = _paired_two_part_rows(
        update_indices=boundary_indices,
        included_indices=boundary_indices,
    )
    boundary_row, _, _ = _source_price_contrast(
        comparator=boundary_comparator,
        candidate=boundary_candidate,
        target_days=days,
        endpoint="mae",
        draws=_circular_block_draws(days),
    )
    assert boundary_row.status == "not_adjudicable"
    assert boundary_row.reason == "requires 6 complete seven-day blocks"

    _, comparator_42, candidate_42 = _paired_two_part_rows(
        update_indices=set(range(42)),
        included_indices=set(range(42)),
    )
    zero_total_draws = np.full((BOOTSTRAP_RESAMPLES, 88), 87, dtype=np.int64)
    zero_row, _, _ = _source_price_contrast(
        comparator=comparator_42,
        candidate=candidate_42,
        target_days=days,
        endpoint="mae",
        draws=zero_total_draws,
    )
    assert zero_row.status == "not_adjudicable"
    assert zero_row.reason == "at least one complete block draw has zero endpoint rows"


def test_nearest_rank_interval_and_centered_max_t_plus_one_rule() -> None:
    days, comparator, candidate = _paired_two_part_rows(update_indices=set(range(88)))
    draws = _circular_block_draws(days)
    row, bootstrap_values, standard_error = _source_price_contrast(
        comparator=comparator,
        candidate=candidate,
        target_days=days,
        endpoint="mae",
        draws=draws,
    )
    assert bootstrap_values is not None
    assert standard_error is not None
    comparator_loss = np.asarray(
        [abs(row.actual_bps - row.prediction_bps) for row in comparator]
    )
    candidate_loss = np.asarray(
        [abs(row.actual_bps - row.prediction_bps) for row in candidate]
    )
    independently_sampled = comparator_loss[draws].mean(axis=1) - candidate_loss[
        draws
    ].mean(axis=1)
    ordered = np.sort(independently_sampled)
    assert row.raw_lower == ordered[249]
    assert row.raw_upper == ordered[9_749]
    assert standard_error == np.std(independently_sampled, ddof=1)

    registered = ChallengerContrast(
        family="ols",
        direction="bsc_to_base",
        horizon_ms=3_600_000,
        support="all",
        endpoint="mae",
        contrast="source_price",
        comparator_variant="source_age",
        candidate_variant="full_source",
        status="adjudicable",
        reason=None,
        rows=88,
        target_days=88,
        complete_blocks=12,
        update_days=20,
        point=1.0,
        raw_lower=0.0,
        raw_upper=2.0,
        bootstrap_standard_error=1.0,
        simultaneous_lower=None,
        simultaneous_upper=None,
        adjusted_p_value=None,
    )
    synthetic = 1.0 + np.arange(BOOTSTRAP_RESAMPLES, dtype=np.float64) / 10_000.0
    contrasts = [registered]
    critical = _apply_max_t(
        contrasts,
        [_ContrastWork(row_index=0, bootstrap_values=synthetic, standard_error=1.0)],
    )
    assert critical == 0.9499
    assert contrasts[0].simultaneous_lower == pytest.approx(0.0501)
    assert contrasts[0].simultaneous_upper == pytest.approx(1.9499)
    assert contrasts[0].adjusted_p_value == pytest.approx(1 / 10_001)


def test_missing_registered_model_cell_is_a_package_error(study) -> None:
    missing_key = ("ols", "source_age", "bsc_to_base", 3_600_000)
    incomplete = replace(
        study,
        predictions=tuple(
            row
            for row in study.predictions
            if (row.family, row.variant, row.direction, row.horizon_ms) != missing_key
        ),
    )

    with pytest.raises(ValueError, match="registered challenger grid"):
        infer_challengers(incomplete)


def test_freshness_support_only_reduces_rows_and_reliability_is_stable(evidence) -> None:
    keyed = {
        (row.family, row.variant, row.direction, row.horizon_ms, row.support): row
        for row in evidence.metrics
    }
    for family in ("ols", "arx2", "gam", "huber", "two_part"):
        for variant in ("target_only", "source_age", "full_source"):
            for direction in ("bsc_to_base", "base_to_bsc"):
                for horizon in (900_000, 3_600_000, 14_400_000):
                    unrestricted = keyed[(family, variant, direction, horizon, "all")]
                    four_hour = keyed[
                        (family, variant, direction, horizon, "both_age_le_4h")
                    ]
                    one_hour = keyed[
                        (family, variant, direction, horizon, "both_age_le_1h")
                    ]
                    assert one_hour.rows <= four_hour.rows <= unrestricted.rows

    assert all(0 <= row.group_index < 5 for row in evidence.reliability)
    assert tuple(row.sort_key for row in evidence.reliability) == tuple(
        sorted(row.sort_key for row in evidence.reliability)
    )
