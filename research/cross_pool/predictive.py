"""Expanding weekly OLS for direction-relative cross-pool prediction."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import TypeAlias

import numpy as np
from numpy.typing import NDArray

from research.cross_pool.contracts import (
    CausalPanel,
    CrossPoolContractError,
    Direction,
    FoldAudit,
    PanelRow,
    PredictionRow,
    Regime,
    WalkForwardConfig,
    WalkForwardResult,
)

_DAY_MS = 86_400_000
_WEEK_MS = 7 * _DAY_MS
_CROSS_FEATURE_COUNT = 5
_BASELINE_FEATURE_COUNT = 2

FeatureVector: TypeAlias = tuple[float, float, float, float, float]
FloatArray: TypeAlias = NDArray[np.float64]


@dataclass(frozen=True)
class _ProjectedRow:
    timestamp_ms: int
    target_timestamp_ms: int
    horizon_ms: int
    features: FeatureVector
    actual_bps: float
    target_regime: Regime
    source_regime: Regime


def expanding_weekly_predictions(
    panel: CausalPanel,
    config: WalkForwardConfig,
) -> WalkForwardResult:
    _validate_config(config)
    _validate_panel(panel)
    projected = tuple(_project_row(row, config.direction) for row in panel.rows)
    first_refit = _weekday_boundary_at_or_after(
        panel.common_interval_start_ms + config.initial_train_days * _DAY_MS,
        config.refit_weekday,
    )
    eligible_validation = tuple(row for row in projected if row.timestamp_ms >= first_refit)
    if not eligible_validation:
        raise CrossPoolContractError("walk-forward schedule has no post-refit validation rows")

    predictions: list[PredictionRow] = []
    audits: list[FoldAudit] = []
    last_fold_index = (eligible_validation[-1].timestamp_ms - first_refit) // _WEEK_MS
    for calendar_fold_index in range(last_fold_index + 1):
        refit_timestamp_ms = first_refit + calendar_fold_index * _WEEK_MS
        validation = tuple(
            row
            for row in eligible_validation
            if refit_timestamp_ms <= row.timestamp_ms < refit_timestamp_ms + _WEEK_MS
        )
        if not validation:
            continue
        training = tuple(row for row in projected if row.target_timestamp_ms <= refit_timestamp_ms)
        if len(training) < _CROSS_FEATURE_COUNT + 1:
            raise CrossPoolContractError(
                "cross design requires at least 6 rows with observable labels"
            )

        training_features = _feature_matrix(training)
        validation_features = _feature_matrix(validation)
        training_targets = _target_vector(training)
        feature_means = np.mean(training_features, axis=0)
        feature_scales = np.std(training_features, axis=0, ddof=0)
        _validate_scaler(feature_means, feature_scales)
        standardized_training = (training_features - feature_means) / feature_scales
        standardized_validation = (validation_features - feature_means) / feature_scales

        baseline_training = _with_intercept(standardized_training[:, :_BASELINE_FEATURE_COUNT])
        cross_training = _with_intercept(standardized_training)
        baseline_coefficients = _fit_checked_ols(
            baseline_training,
            training_targets,
            label="baseline",
            maximum_condition_number=config.maximum_condition_number,
        )
        cross_coefficients = _fit_checked_ols(
            cross_training,
            training_targets,
            label="cross",
            maximum_condition_number=config.maximum_condition_number,
        )
        baseline_values = _predict_checked(
            _with_intercept(standardized_validation[:, :_BASELINE_FEATURE_COUNT]),
            baseline_coefficients,
            label="baseline",
        )
        cross_values = _predict_checked(
            _with_intercept(standardized_validation),
            cross_coefficients,
            label="cross",
        )
        fold_index = len(audits)
        audits.append(
            FoldAudit(
                fold_index=fold_index,
                refit_timestamp_ms=refit_timestamp_ms,
                max_training_target_timestamp_ms=max(row.target_timestamp_ms for row in training),
                feature_means=tuple(float(value) for value in feature_means),
                feature_scales=tuple(float(value) for value in feature_scales),
            )
        )
        predictions.extend(
            PredictionRow(
                timestamp_ms=row.timestamp_ms,
                target_timestamp_ms=row.target_timestamp_ms,
                horizon_ms=row.horizon_ms,
                refit_timestamp_ms=refit_timestamp_ms,
                fold_index=fold_index,
                direction=config.direction,
                target_regime=row.target_regime,
                source_regime=row.source_regime,
                actual_bps=row.actual_bps,
                baseline_prediction_bps=float(baseline_values[index]),
                cross_prediction_bps=float(cross_values[index]),
            )
            for index, row in enumerate(validation)
        )

    if not predictions:
        raise CrossPoolContractError("walk-forward schedule produced no predictions")
    return WalkForwardResult(predictions=tuple(predictions), audits=tuple(audits))


def _validate_config(config: WalkForwardConfig) -> None:
    if config.direction not in ("bsc_to_base", "base_to_bsc"):
        raise CrossPoolContractError(f"unsupported direction {config.direction!r}")
    if config.initial_train_days <= 0:
        raise CrossPoolContractError("initial_train_days must be positive")
    if not 0 <= config.refit_weekday <= 6:
        raise CrossPoolContractError("refit_weekday must be between 0 and 6")
    if not isfinite(config.maximum_condition_number) or config.maximum_condition_number <= 0:
        raise CrossPoolContractError("maximum_condition_number must be positive and finite")


def _validate_panel(panel: CausalPanel) -> None:
    if panel.horizon_ms <= 0:
        raise CrossPoolContractError("panel horizon must be positive")
    if not panel.rows:
        raise CrossPoolContractError("walk-forward prediction requires at least one panel row")
    for row in panel.rows:
        if row.horizon_ms != panel.horizon_ms:
            raise CrossPoolContractError("row horizon does not match panel horizon")
        if row.base_age_ms < 0 or row.bsc_age_ms < 0:
            raise CrossPoolContractError("panel state ages must be non-negative")
        values = (
            row.base_trailing_return_bps,
            row.bsc_trailing_return_bps,
            row.base_minus_bsc_gap_bps,
            row.base_forward_return_bps,
            row.bsc_forward_return_bps,
        )
        if not all(isfinite(value) for value in values):
            raise CrossPoolContractError("panel contains a non-finite predictive input")
    for previous, current in zip(panel.rows, panel.rows[1:], strict=False):
        if current.timestamp_ms <= previous.timestamp_ms:
            raise CrossPoolContractError("panel timestamps must be strictly increasing")
        if current.timestamp_ms - previous.timestamp_ms != panel.horizon_ms:
            raise CrossPoolContractError("panel rows must follow the regular horizon clock")


def _project_row(row: PanelRow, direction: Direction) -> _ProjectedRow:
    if direction == "bsc_to_base":
        features: FeatureVector = (
            row.base_trailing_return_bps,
            float(row.base_age_ms),
            row.bsc_trailing_return_bps,
            row.base_minus_bsc_gap_bps,
            float(row.bsc_age_ms),
        )
        actual_bps = row.base_forward_return_bps
        target_regime = row.base_regime
        source_regime = row.bsc_regime
    else:
        features = (
            row.bsc_trailing_return_bps,
            float(row.bsc_age_ms),
            row.base_trailing_return_bps,
            -row.base_minus_bsc_gap_bps,
            float(row.base_age_ms),
        )
        actual_bps = row.bsc_forward_return_bps
        target_regime = row.bsc_regime
        source_regime = row.base_regime
    return _ProjectedRow(
        timestamp_ms=row.timestamp_ms,
        target_timestamp_ms=row.timestamp_ms + row.horizon_ms,
        horizon_ms=row.horizon_ms,
        features=features,
        actual_bps=actual_bps,
        target_regime=target_regime,
        source_regime=source_regime,
    )


def _weekday_boundary_at_or_after(timestamp_ms: int, weekday: int) -> int:
    day_index = timestamp_ms // _DAY_MS
    day_start_ms = day_index * _DAY_MS
    current_weekday = (day_index + 3) % 7
    days_ahead = (weekday - current_weekday) % 7
    boundary_ms = day_start_ms + days_ahead * _DAY_MS
    return boundary_ms if boundary_ms >= timestamp_ms else boundary_ms + _WEEK_MS


def _feature_matrix(rows: tuple[_ProjectedRow, ...]) -> FloatArray:
    matrix: FloatArray = np.asarray(
        [row.features for row in rows],
        dtype=np.float64,
    )
    if not np.all(np.isfinite(matrix)):
        raise CrossPoolContractError("feature matrix contains non-finite values")
    return matrix


def _target_vector(rows: tuple[_ProjectedRow, ...]) -> FloatArray:
    targets: FloatArray = np.asarray(
        [row.actual_bps for row in rows],
        dtype=np.float64,
    )
    if not np.all(np.isfinite(targets)):
        raise CrossPoolContractError("target vector contains non-finite values")
    return targets


def _validate_scaler(means: FloatArray, scales: FloatArray) -> None:
    if not np.all(np.isfinite(means)) or not np.all(np.isfinite(scales)):
        raise CrossPoolContractError("training scaler contains non-finite values")
    zero_variance = np.flatnonzero(scales == 0.0)
    if zero_variance.size:
        raise CrossPoolContractError(
            f"training scaler has zero-variance feature {int(zero_variance[0])}"
        )


def _with_intercept(features: FloatArray) -> FloatArray:
    design: FloatArray = np.column_stack((np.ones(features.shape[0], dtype=np.float64), features))
    return design


def _fit_checked_ols(
    design: FloatArray,
    targets: FloatArray,
    *,
    label: str,
    maximum_condition_number: float,
) -> FloatArray:
    if design.shape[0] < design.shape[1]:
        raise CrossPoolContractError(f"{label} design requires at least {design.shape[1]} rows")
    try:
        singular_values: FloatArray = np.linalg.svd(design, compute_uv=False)
    except np.linalg.LinAlgError as exc:
        raise CrossPoolContractError(f"{label} design SVD did not converge") from exc
    if not np.all(np.isfinite(singular_values)):
        raise CrossPoolContractError(f"{label} design has non-finite singular values")
    relative_cutoff = np.finfo(np.float64).eps * max(design.shape)
    rank = int(np.sum(singular_values > singular_values[0] * relative_cutoff))
    if rank < design.shape[1]:
        raise CrossPoolContractError(f"{label} design is rank deficient")
    condition_number = float(singular_values[0] / singular_values[-1])
    if not isfinite(condition_number):
        raise CrossPoolContractError(f"{label} design condition number is non-finite")
    if condition_number > maximum_condition_number:
        raise CrossPoolContractError(
            f"{label} design condition number {condition_number} exceeds {maximum_condition_number}"
        )
    try:
        coefficients, _, fitted_rank, fitted_singular_values = np.linalg.lstsq(
            design,
            targets,
            rcond=relative_cutoff,
        )
    except np.linalg.LinAlgError as exc:
        raise CrossPoolContractError(f"{label} least-squares solve did not converge") from exc
    if fitted_rank < design.shape[1]:
        raise CrossPoolContractError(f"{label} design is rank deficient")
    if not np.all(np.isfinite(fitted_singular_values)) or not np.all(np.isfinite(coefficients)):
        raise CrossPoolContractError(f"{label} least-squares solve produced non-finite values")
    result: FloatArray = coefficients
    return result


def _predict_checked(
    design: FloatArray,
    coefficients: FloatArray,
    *,
    label: str,
) -> FloatArray:
    predictions: FloatArray = design @ coefficients
    if not np.all(np.isfinite(predictions)):
        raise CrossPoolContractError(f"{label} predictions contain non-finite values")
    return predictions
