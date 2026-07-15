"""Paired target-day inference for cross-pool predictions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Iterable, Literal, Sequence

import numpy as np
from numpy.typing import NDArray

from research.cross_pool.contracts import (
    PREDICTIVE_BOOTSTRAP_CONFIDENCE_LEVEL,
    PREDICTIVE_BOOTSTRAP_RESAMPLES,
    PREDICTIVE_BOOTSTRAP_SEED,
    PREDICTIVE_CONDITIONAL_MOVE_THRESHOLD_BPS,
    AdequacyAudit,
    ConfidenceInterval,
    CrossPoolContractError,
    Direction,
    EvidenceClass,
    PredictionRow,
    PredictiveBootstrap,
    PredictiveInference,
    PredictiveMetrics,
    _derive_evidence_class,
)

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
FROZEN_RESAMPLES = PREDICTIVE_BOOTSTRAP_RESAMPLES
FROZEN_SEED = PREDICTIVE_BOOTSTRAP_SEED
FROZEN_CONFIDENCE_LEVEL = PREDICTIVE_BOOTSTRAP_CONFIDENCE_LEVEL


@dataclass(frozen=True)
class _DayStats:
    row_count: int
    baseline_absolute_error_sum: float
    cross_absolute_error_sum: float
    baseline_squared_error_sum: float
    cross_squared_error_sum: float
    conditional_count: int
    baseline_directional_hits: int
    cross_directional_hits: int


@dataclass
class _DayAccumulator:
    baseline_absolute_errors: list[float] = field(default_factory=list)
    cross_absolute_errors: list[float] = field(default_factory=list)
    baseline_squared_errors: list[float] = field(default_factory=list)
    cross_squared_errors: list[float] = field(default_factory=list)
    conditional_count: int = 0
    baseline_directional_hits: int = 0
    cross_directional_hits: int = 0


def summarize_predictions(rows: Sequence[PredictionRow]) -> PredictiveMetrics:
    validated = _validated_rows(rows)
    return _metrics_from_day_stats(
        _day_stats(validated),
        direction=validated[0].direction,
        horizon_ms=validated[0].horizon_ms,
    )


def bootstrap_predictive(
    rows: Sequence[PredictionRow],
    *,
    resamples: int = FROZEN_RESAMPLES,
    seed: int = FROZEN_SEED,
    confidence_level: float = FROZEN_CONFIDENCE_LEVEL,
) -> PredictiveBootstrap:
    _validate_bootstrap_config(
        resamples=resamples,
        seed=seed,
        confidence_level=confidence_level,
    )
    validated = _validated_rows(rows)
    days = _day_stats(validated)
    metrics = _metrics_from_day_stats(
        days,
        direction=validated[0].direction,
        horizon_ms=validated[0].horizon_ms,
    )
    draws = _draw_day_indices(
        day_count=len(days),
        resamples=resamples,
        seed=seed,
    )

    row_counts = np.asarray([day.row_count for day in days], dtype=np.int64)
    conditional_counts = np.asarray(
        [day.conditional_count for day in days],
        dtype=np.int64,
    )
    baseline_hits = np.asarray(
        [day.baseline_directional_hits for day in days],
        dtype=np.int64,
    )
    cross_hits = np.asarray(
        [day.cross_directional_hits for day in days],
        dtype=np.int64,
    )
    baseline_absolute = np.asarray(
        [day.baseline_absolute_error_sum for day in days],
        dtype=np.float64,
    )
    cross_absolute = np.asarray(
        [day.cross_absolute_error_sum for day in days],
        dtype=np.float64,
    )
    baseline_squared = np.asarray(
        [day.baseline_squared_error_sum for day in days],
        dtype=np.float64,
    )
    cross_squared = np.asarray(
        [day.cross_squared_error_sum for day in days],
        dtype=np.float64,
    )

    sampled_row_counts = row_counts[draws].sum(axis=1, dtype=np.int64)
    sampled_conditional_counts = conditional_counts[draws].sum(axis=1, dtype=np.int64)
    sampled_baseline_hits = baseline_hits[draws].sum(axis=1, dtype=np.int64)
    sampled_cross_hits = cross_hits[draws].sum(axis=1, dtype=np.int64)
    if (
        bool(np.any(sampled_row_counts <= 0))
        or bool(np.any(sampled_conditional_counts < 0))
        or bool(np.any(sampled_baseline_hits < 0))
        or bool(np.any(sampled_cross_hits < 0))
        or bool(np.any(sampled_baseline_hits > sampled_conditional_counts))
        or bool(np.any(sampled_cross_hits > sampled_conditional_counts))
    ):
        raise CrossPoolContractError("sampled bootstrap counts are inconsistent")
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        sampled_baseline_absolute = _sampled_fsum(baseline_absolute, draws)
        sampled_cross_absolute = _sampled_fsum(cross_absolute, draws)
        sampled_baseline_squared = _sampled_fsum(baseline_squared, draws)
        sampled_cross_squared = _sampled_fsum(cross_squared, draws)
        sampled_baseline_mae = sampled_baseline_absolute / sampled_row_counts
        sampled_cross_mae = sampled_cross_absolute / sampled_row_counts
        sampled_baseline_mse = sampled_baseline_squared / sampled_row_counts
        sampled_cross_mse = sampled_cross_squared / sampled_row_counts
        mae_improvements = sampled_baseline_mae - sampled_cross_mae
        mse_improvements = sampled_baseline_mse - sampled_cross_mse

    _require_finite_arrays(
        sampled_baseline_absolute,
        sampled_cross_absolute,
        sampled_baseline_squared,
        sampled_cross_squared,
        sampled_baseline_mae,
        sampled_cross_mae,
        sampled_baseline_mse,
        sampled_cross_mse,
        mae_improvements,
        mse_improvements,
        message="sampled bootstrap values must be finite",
    )

    valid_conditional = sampled_conditional_counts > 0
    valid_conditional_resamples = int(np.count_nonzero(valid_conditional))
    cross_accuracy_interval: ConfidenceInterval | None = None
    directional_gain_interval: ConfidenceInterval | None = None
    if valid_conditional_resamples == resamples:
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            baseline_accuracies = sampled_baseline_hits / sampled_conditional_counts
            cross_accuracies = sampled_cross_hits / sampled_conditional_counts
            directional_gains = cross_accuracies - baseline_accuracies
        _require_finite_arrays(
            baseline_accuracies,
            cross_accuracies,
            directional_gains,
            message="sampled bootstrap values must be finite",
        )
        assert metrics.cross_conditional_directional_accuracy is not None
        assert metrics.conditional_directional_accuracy_gain is not None
        cross_accuracy_interval = _confidence_interval(
            metrics.cross_conditional_directional_accuracy,
            cross_accuracies,
            confidence_level,
        )
        directional_gain_interval = _confidence_interval(
            metrics.conditional_directional_accuracy_gain,
            directional_gains,
            confidence_level,
        )

    return PredictiveBootstrap(
        direction=metrics.direction,
        horizon_ms=metrics.horizon_ms,
        resamples=resamples,
        seed=seed,
        confidence_level=confidence_level,
        mae_improvement_bps=_confidence_interval(
            metrics.mae_improvement_bps,
            mae_improvements,
            confidence_level,
        ),
        mse_improvement_bps2=_confidence_interval(
            metrics.mse_improvement_bps2,
            mse_improvements,
            confidence_level,
        ),
        cross_conditional_directional_accuracy=cross_accuracy_interval,
        conditional_directional_accuracy_gain=directional_gain_interval,
        valid_conditional_resamples=valid_conditional_resamples,
    )


def assess_adequacy(metrics: PredictiveMetrics) -> AdequacyAudit:
    return AdequacyAudit(
        target_day_count=metrics.target_day_count,
        conditional_target_day_count=metrics.conditional_target_day_count,
    )


def classify_predictive(
    metrics: PredictiveMetrics,
    bootstrap: PredictiveBootstrap,
) -> EvidenceClass:
    return _derive_evidence_class(metrics, bootstrap)


def infer_predictions(rows: Sequence[PredictionRow]) -> PredictiveInference:
    metrics = summarize_predictions(rows)
    bootstrap = bootstrap_predictive(rows)
    adequacy = assess_adequacy(metrics)
    return PredictiveInference(
        metrics=metrics,
        bootstrap=bootstrap,
        adequacy=adequacy,
        evidence_class=classify_predictive(metrics, bootstrap),
    )


def _validated_rows(rows: Sequence[PredictionRow]) -> tuple[PredictionRow, ...]:
    materialized = tuple(rows)
    if not materialized:
        raise CrossPoolContractError("inference requires at least one prediction")

    direction = materialized[0].direction
    horizon_ms = materialized[0].horizon_ms
    if direction not in ("bsc_to_base", "base_to_bsc"):
        raise CrossPoolContractError("inference requires one direction")
    if horizon_ms <= 0:
        raise CrossPoolContractError("inference requires one positive horizon")

    previous_timestamp: int | None = None
    previous_target_timestamp: int | None = None
    previous_fold_index: int | None = None
    previous_refit_timestamp: int | None = None
    refit_by_fold: dict[int, int] = {}
    for row in materialized:
        if row.direction != direction:
            raise CrossPoolContractError("inference requires one direction")
        if row.horizon_ms <= 0 or row.horizon_ms != horizon_ms:
            raise CrossPoolContractError("inference requires one positive horizon")
        if previous_timestamp is not None and row.timestamp_ms <= previous_timestamp:
            raise CrossPoolContractError("prediction timestamps must be strictly increasing")
        if row.target_timestamp_ms != row.timestamp_ms + row.horizon_ms:
            raise CrossPoolContractError(
                "prediction target timestamp must equal origin plus horizon"
            )
        if (
            previous_target_timestamp is not None
            and row.target_timestamp_ms <= previous_target_timestamp
        ):
            raise CrossPoolContractError(
                "prediction target timestamps must be unique and strictly increasing"
            )
        for value in (
            row.actual_bps,
            row.baseline_prediction_bps,
            row.cross_prediction_bps,
        ):
            if not math.isfinite(value):
                raise CrossPoolContractError("prediction values must be finite")
        for regime in (row.target_regime, row.source_regime):
            if regime not in ("early", "mixed", "late"):
                raise CrossPoolContractError("unsupported regime")
        if row.fold_index < 0:
            raise CrossPoolContractError("prediction fold indices must be nonnegative")
        if previous_fold_index is not None and row.fold_index < previous_fold_index:
            raise CrossPoolContractError("prediction fold indices must be nondecreasing")
        if (
            previous_fold_index is not None
            and row.fold_index > previous_fold_index
            and previous_refit_timestamp is not None
            and row.refit_timestamp_ms <= previous_refit_timestamp
        ):
            raise CrossPoolContractError(
                "distinct prediction folds require increasing refit timestamps"
            )
        known_refit = refit_by_fold.setdefault(row.fold_index, row.refit_timestamp_ms)
        if known_refit != row.refit_timestamp_ms:
            raise CrossPoolContractError("predictions require one refit timestamp per fold")
        if not row.refit_timestamp_ms <= row.timestamp_ms < row.refit_timestamp_ms + WEEK_MS:
            raise CrossPoolContractError("prediction must fall inside its half-open refit window")

        previous_timestamp = row.timestamp_ms
        previous_target_timestamp = row.target_timestamp_ms
        previous_fold_index = row.fold_index
        previous_refit_timestamp = row.refit_timestamp_ms
    return materialized


def _day_stats(rows: tuple[PredictionRow, ...]) -> tuple[_DayStats, ...]:
    accumulators: dict[int, _DayAccumulator] = {}
    for row in rows:
        day_index = row.target_timestamp_ms // DAY_MS
        accumulator = accumulators.setdefault(day_index, _DayAccumulator())
        baseline_error = _finite_arithmetic(
            row.actual_bps - row.baseline_prediction_bps,
            "derived prediction errors must be finite",
        )
        cross_error = _finite_arithmetic(
            row.actual_bps - row.cross_prediction_bps,
            "derived prediction errors must be finite",
        )
        baseline_absolute = _finite_arithmetic(
            abs(baseline_error),
            "derived absolute errors must be finite",
        )
        cross_absolute = _finite_arithmetic(
            abs(cross_error),
            "derived absolute errors must be finite",
        )
        baseline_squared = _finite_arithmetic(
            baseline_error * baseline_error,
            "derived squared errors must be finite",
        )
        cross_squared = _finite_arithmetic(
            cross_error * cross_error,
            "derived squared errors must be finite",
        )
        accumulator.baseline_absolute_errors.append(baseline_absolute)
        accumulator.cross_absolute_errors.append(cross_absolute)
        accumulator.baseline_squared_errors.append(baseline_squared)
        accumulator.cross_squared_errors.append(cross_squared)
        if abs(row.actual_bps) >= PREDICTIVE_CONDITIONAL_MOVE_THRESHOLD_BPS:
            accumulator.conditional_count += 1
            accumulator.baseline_directional_hits += int(
                _same_nonzero_sign(row.actual_bps, row.baseline_prediction_bps)
            )
            accumulator.cross_directional_hits += int(
                _same_nonzero_sign(row.actual_bps, row.cross_prediction_bps)
            )

    return tuple(
        _DayStats(
            row_count=len(accumulator.baseline_absolute_errors),
            baseline_absolute_error_sum=_finite_sum(
                accumulator.baseline_absolute_errors,
                "per-day absolute-error sums must be finite",
            ),
            cross_absolute_error_sum=_finite_sum(
                accumulator.cross_absolute_errors,
                "per-day absolute-error sums must be finite",
            ),
            baseline_squared_error_sum=_finite_sum(
                accumulator.baseline_squared_errors,
                "per-day squared-error sums must be finite",
            ),
            cross_squared_error_sum=_finite_sum(
                accumulator.cross_squared_errors,
                "per-day squared-error sums must be finite",
            ),
            conditional_count=accumulator.conditional_count,
            baseline_directional_hits=accumulator.baseline_directional_hits,
            cross_directional_hits=accumulator.cross_directional_hits,
        )
        for _, accumulator in sorted(accumulators.items())
    )


def _metrics_from_day_stats(
    days: tuple[_DayStats, ...],
    *,
    direction: Direction,
    horizon_ms: int,
) -> PredictiveMetrics:
    rows = sum(day.row_count for day in days)
    baseline_absolute_sum = _finite_sum(
        [day.baseline_absolute_error_sum for day in days],
        "aggregate absolute-error sums must be finite",
    )
    cross_absolute_sum = _finite_sum(
        [day.cross_absolute_error_sum for day in days],
        "aggregate absolute-error sums must be finite",
    )
    baseline_squared_sum = _finite_sum(
        [day.baseline_squared_error_sum for day in days],
        "aggregate squared-error sums must be finite",
    )
    cross_squared_sum = _finite_sum(
        [day.cross_squared_error_sum for day in days],
        "aggregate squared-error sums must be finite",
    )
    baseline_mae = _finite_arithmetic(
        baseline_absolute_sum / rows,
        "final predictive metrics must be finite",
    )
    cross_mae = _finite_arithmetic(
        cross_absolute_sum / rows,
        "final predictive metrics must be finite",
    )
    baseline_mse = _finite_arithmetic(
        baseline_squared_sum / rows,
        "final predictive metrics must be finite",
    )
    cross_mse = _finite_arithmetic(
        cross_squared_sum / rows,
        "final predictive metrics must be finite",
    )
    mae_improvement = _finite_arithmetic(
        baseline_mae - cross_mae,
        "final predictive metrics must be finite",
    )
    mse_improvement = _finite_arithmetic(
        baseline_mse - cross_mse,
        "final predictive metrics must be finite",
    )
    baseline_rmse = _finite_arithmetic(
        math.sqrt(baseline_mse),
        "final predictive metrics must be finite",
    )
    cross_rmse = _finite_arithmetic(
        math.sqrt(cross_mse),
        "final predictive metrics must be finite",
    )
    relative_oos_r2 = (
        None
        if baseline_mse == 0.0
        else _finite_arithmetic(
            1.0 - cross_mse / baseline_mse,
            "final predictive metrics must be finite",
        )
    )

    conditional_rows = sum(day.conditional_count for day in days)
    conditional_day_count = sum(day.conditional_count > 0 for day in days)
    baseline_accuracy: float | None = None
    cross_accuracy: float | None = None
    directional_gain: float | None = None
    if conditional_rows:
        baseline_accuracy = _finite_arithmetic(
            sum(day.baseline_directional_hits for day in days) / conditional_rows,
            "final predictive metrics must be finite",
        )
        cross_accuracy = _finite_arithmetic(
            sum(day.cross_directional_hits for day in days) / conditional_rows,
            "final predictive metrics must be finite",
        )
        directional_gain = _finite_arithmetic(
            cross_accuracy - baseline_accuracy,
            "final predictive metrics must be finite",
        )

    return PredictiveMetrics(
        direction=direction,
        horizon_ms=horizon_ms,
        rows=rows,
        target_day_count=len(days),
        conditional_rows=conditional_rows,
        conditional_target_day_count=conditional_day_count,
        baseline_mae_bps=baseline_mae,
        cross_mae_bps=cross_mae,
        mae_improvement_bps=mae_improvement,
        baseline_mse_bps2=baseline_mse,
        cross_mse_bps2=cross_mse,
        mse_improvement_bps2=mse_improvement,
        baseline_rmse_bps=baseline_rmse,
        cross_rmse_bps=cross_rmse,
        relative_oos_r2=relative_oos_r2,
        baseline_conditional_directional_accuracy=baseline_accuracy,
        cross_conditional_directional_accuracy=cross_accuracy,
        conditional_directional_accuracy_gain=directional_gain,
    )


def _draw_day_indices(
    *,
    day_count: int,
    resamples: int,
    seed: int,
) -> NDArray[np.int64]:
    if day_count <= 0:
        raise CrossPoolContractError("day_count must be positive")
    if resamples <= 0:
        raise CrossPoolContractError("resamples must be positive")
    if seed < 0:
        raise CrossPoolContractError("seed must be nonnegative")
    generator = np.random.Generator(np.random.PCG64(seed))
    return generator.integers(
        0,
        day_count,
        size=(resamples, day_count),
        dtype=np.int64,
        endpoint=False,
    )


def _nearest_rank_indices(resamples: int, confidence_level: float) -> tuple[int, int]:
    if resamples <= 0:
        raise CrossPoolContractError("resamples must be positive")
    if not math.isfinite(confidence_level) or not 0.0 < confidence_level < 1.0:
        raise CrossPoolContractError("confidence_level must be finite and between 0 and 1")
    confidence = Decimal(str(confidence_level))
    exact_confidence = Fraction(confidence)
    lower_rank = math.ceil((1 - exact_confidence) * resamples / 2)
    upper_rank = math.ceil((1 + exact_confidence) * resamples / 2)
    return (
        min(max(lower_rank - 1, 0), resamples - 1),
        min(max(upper_rank - 1, 0), resamples - 1),
    )


def _confidence_interval(
    point: float,
    samples: NDArray[np.float64],
    confidence_level: float,
) -> ConfidenceInterval:
    _require_finite_arrays(samples, message="bootstrap interval samples must be finite")
    lower_index, upper_index = _nearest_rank_indices(len(samples), confidence_level)
    ordered = np.sort(samples)
    return ConfidenceInterval(
        point=point,
        lower=float(ordered[lower_index]),
        upper=float(ordered[upper_index]),
    )


def _validate_bootstrap_config(
    *,
    resamples: int,
    seed: int,
    confidence_level: float,
) -> None:
    if resamples <= 0:
        raise CrossPoolContractError("resamples must be positive")
    if seed < 0:
        raise CrossPoolContractError("seed must be nonnegative")
    if not math.isfinite(confidence_level) or not 0.0 < confidence_level < 1.0:
        raise CrossPoolContractError("confidence_level must be finite and between 0 and 1")


def _same_nonzero_sign(actual: float, prediction: float) -> bool:
    return (actual > 0.0 and prediction > 0.0) or (actual < 0.0 and prediction < 0.0)


def _finite_arithmetic(value: float, message: str) -> float:
    if not math.isfinite(value):
        raise CrossPoolContractError(message)
    return value


def _finite_sum(values: Iterable[float], message: str) -> float:
    try:
        result = math.fsum(values)
    except OverflowError as exc:
        raise CrossPoolContractError(message) from exc
    return _finite_arithmetic(result, message)


def _require_finite_arrays(
    *arrays: NDArray[np.float64],
    message: str,
) -> None:
    if any(not bool(np.all(np.isfinite(array))) for array in arrays):
        raise CrossPoolContractError(message)


def _sampled_fsum(
    values: NDArray[np.float64],
    draws: NDArray[np.int64],
) -> NDArray[np.float64]:
    return np.fromiter(
        (
            _finite_sum(
                values[draw],
                "sampled bootstrap values must be finite",
            )
            for draw in draws
        ),
        dtype=np.float64,
        count=len(draws),
    )


def _target_day_label(row: PredictionRow) -> str:
    day_index = row.target_timestamp_ms // DAY_MS
    try:
        return (date(1970, 1, 1) + timedelta(days=day_index)).isoformat()
    except OverflowError as exc:
        raise CrossPoolContractError("target UTC day is outside the supported date range") from exc


def validate_prediction_rows(rows: Sequence[PredictionRow]) -> tuple[PredictionRow, ...]:
    return _validated_rows(rows)


def _prediction_improvement_sign(value: float) -> Literal[-1, 0, 1]:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0
