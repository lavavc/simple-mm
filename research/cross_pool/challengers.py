"""Causal expanding-fold runner for the registered challenger models."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from research.cross_pool.challenger_contracts import (
    FEATURE_VARIANTS,
    MODEL_FAMILIES,
    ChallengerFitFailure,
    ChallengerFoldAudit,
    ChallengerParent,
    ChallengerPrediction,
    ChallengerStudy,
    FeatureVariant,
    ModelFamily,
    ParentPrediction,
    ProjectedParentRow,
)
from research.cross_pool.challenger_estimators import (
    ChallengerFitError,
    LogisticFit,
    RegressionFit,
    additive_design,
    fit_huber,
    fit_ridge_logistic,
    fit_standardized_ols,
)
from research.cross_pool.challenger_parent import project_parent_row
from research.cross_pool.contracts import CrossPoolContractError, Direction

_DIRECTIONS: tuple[Direction, ...] = ("bsc_to_base", "base_to_bsc")
_HORIZONS_MS = (900_000, 3_600_000, 14_400_000)
_OLS_REPRODUCTION_ATOL_BPS = 1e-10


@dataclass(frozen=True)
class _FoldResult:
    predictions: tuple[ChallengerPrediction, ...]
    audit: ChallengerFoldAudit


def run_challengers(parent: ChallengerParent) -> ChallengerStudy:
    projections = _projected_panels(parent)
    parent_groups = _parent_prediction_groups(parent)
    predictions: list[ChallengerPrediction] = []
    audits: list[ChallengerFoldAudit] = []
    failures: list[ChallengerFitFailure] = []

    for family in MODEL_FAMILIES:
        for variant in FEATURE_VARIANTS:
            for direction in _DIRECTIONS:
                for horizon_ms in _HORIZONS_MS:
                    group_key = (direction, horizon_ms)
                    combination_predictions: list[ChallengerPrediction] = []
                    combination_audits: list[ChallengerFoldAudit] = []
                    try:
                        fold_groups = _fold_groups(parent_groups[group_key])
                        for fold_predictions in fold_groups:
                            fold = _run_fold(
                                family=family,
                                variant=variant,
                                projected_rows=projections[group_key],
                                sealed_validation=fold_predictions,
                            )
                            combination_predictions.extend(fold.predictions)
                            combination_audits.append(fold.audit)
                    except ChallengerFitError as exc:
                        failed_fold = fold_predictions[0]
                        failures.append(
                            ChallengerFitFailure(
                                family=family,
                                variant=variant,
                                direction=direction,
                                horizon_ms=horizon_ms,
                                fold_index=failed_fold.fold_index,
                                refit_timestamp_ms=failed_fold.refit_timestamp_ms,
                                reason=str(exc),
                            )
                        )
                        continue
                    if len(combination_predictions) != len(parent_groups[group_key]):
                        raise CrossPoolContractError(
                            "completed challenger combination does not cover the parent grid"
                        )
                    predictions.extend(combination_predictions)
                    audits.extend(combination_audits)

    ordered_predictions = tuple(sorted(predictions, key=lambda row: row.sort_key))
    if len({row.sort_key for row in ordered_predictions}) != len(ordered_predictions):
        raise CrossPoolContractError("challenger prediction keys are not unique")
    ordered_audits = tuple(
        sorted(
            audits,
            key=lambda row: (
                MODEL_FAMILIES.index(row.family),
                FEATURE_VARIANTS.index(row.variant),
                _DIRECTIONS.index(row.direction),
                row.horizon_ms,
                row.fold_index,
            ),
        )
    )
    ordered_failures = tuple(
        sorted(
            failures,
            key=lambda row: (
                MODEL_FAMILIES.index(row.family),
                FEATURE_VARIANTS.index(row.variant),
                _DIRECTIONS.index(row.direction),
                row.horizon_ms,
            ),
        )
    )
    if len(ordered_predictions) == 0:
        raise CrossPoolContractError("challenger study produced no predictions")
    return ChallengerStudy(
        predictions=ordered_predictions,
        fold_audits=ordered_audits,
        failures=ordered_failures,
        target_days_utc=parent.target_days_utc,
    )


def _run_fold(
    *,
    family: ModelFamily,
    variant: FeatureVariant,
    projected_rows: tuple[ProjectedParentRow, ...],
    sealed_validation: tuple[ParentPrediction, ...],
) -> _FoldResult:
    first = sealed_validation[0]
    training = tuple(
        row for row in projected_rows if row.target_timestamp_ms <= first.refit_timestamp_ms
    )
    by_timestamp = {row.timestamp_ms: row for row in projected_rows}
    try:
        validation = tuple(by_timestamp[row.timestamp_ms] for row in sealed_validation)
    except KeyError as exc:
        raise CrossPoolContractError("challenger validation row is missing from the panel") from exc
    if not training:
        raise ChallengerFitError("challenger fold has no observable training labels")
    if family == "arx2":
        training = tuple(row for row in training if row.previous_target_return_bps is not None)
        if any(row.previous_target_return_bps is None for row in validation):
            raise ChallengerFitError("ARX(2) validation row has no regular predecessor")
    if not training:
        raise ChallengerFitError("challenger fold has no usable training rows")

    if family == "gam":
        regression, knots, extrapolation_count = _fit_gam(
            training,
            validation,
            variant=variant,
            label=_fold_label(family, variant, first),
        )
        probabilities = None
        conditional = None
        logistic = None
    elif family == "two_part":
        regression, logistic = _fit_two_part(
            training,
            validation,
            variant=variant,
            label=_fold_label(family, variant, first),
        )
        probabilities = logistic.probabilities
        conditional = regression.predictions
        knots = ()
        extrapolation_count = 0
    else:
        training_features = _linear_features(training, family=family, variant=variant)
        validation_features = _linear_features(validation, family=family, variant=variant)
        label = _fold_label(family, variant, first)
        if family in ("ols", "arx2"):
            regression = fit_standardized_ols(
                training_features,
                _targets(training),
                validation_features,
                label=label,
            )
        elif family == "huber":
            regression = fit_huber(
                training_features,
                _targets(training),
                validation_features,
                label=label,
            )
        else:  # pragma: no cover - MODEL_FAMILIES is exhaustive.
            raise AssertionError(family)
        probabilities = None
        conditional = None
        logistic = None
        knots = ()
        extrapolation_count = 0

    fitted_predictions = np.asarray(regression.predictions, dtype=np.float64)
    if family == "two_part":
        assert probabilities is not None
        fitted_predictions = fitted_predictions * np.asarray(probabilities, dtype=np.float64)
    elif family == "ols" and variant in ("target_only", "full_source"):
        sealed_values = np.asarray(
            [
                row.baseline_prediction_bps
                if variant == "target_only"
                else row.cross_prediction_bps
                for row in sealed_validation
            ],
            dtype=np.float64,
        )
        if not np.allclose(
            fitted_predictions,
            sealed_values,
            rtol=0.0,
            atol=_OLS_REPRODUCTION_ATOL_BPS,
        ):
            maximum_difference = float(np.max(np.abs(fitted_predictions - sealed_values)))
            raise CrossPoolContractError(
                f"independent OLS reproduction differs by {maximum_difference} bps"
            )
        fitted_predictions = sealed_values

    prediction_rows = tuple(
        ChallengerPrediction(
            timestamp_ms=projected.timestamp_ms,
            target_timestamp_ms=projected.target_timestamp_ms,
            horizon_ms=projected.horizon_ms,
            refit_timestamp_ms=sealed.refit_timestamp_ms,
            fold_index=sealed.fold_index,
            direction=projected.direction,
            family=family,
            variant=variant,
            target_regime=projected.target_regime,
            source_regime=projected.source_regime,
            target_age_ms=projected.target_age_ms,
            source_age_ms=projected.source_age_ms,
            actual_bps=projected.actual_bps,
            updated=projected.updated,
            prediction_bps=float(fitted_predictions[index]),
            update_probability=(
                None if probabilities is None else float(probabilities[index])
            ),
            conditional_prediction_bps=(
                None if conditional is None else float(conditional[index])
            ),
        )
        for index, (projected, sealed) in enumerate(
            zip(validation, sealed_validation, strict=True)
        )
    )
    audit = ChallengerFoldAudit(
        family=family,
        variant=variant,
        direction=first.direction,
        horizon_ms=first.horizon_ms,
        fold_index=first.fold_index,
        refit_timestamp_ms=first.refit_timestamp_ms,
        max_training_target_timestamp_ms=max(row.target_timestamp_ms for row in training),
        training_rows=len(training),
        training_update_rows=sum(row.updated for row in training),
        condition_number=regression.condition_number,
        iterations=regression.iterations,
        robust_scale=regression.robust_scale,
        downweighted_fraction=regression.downweighted_fraction,
        logistic_condition_number=(
            None if logistic is None else logistic.condition_number
        ),
        logistic_iterations=None if logistic is None else logistic.iterations,
        spline_knots=knots,
        spline_extrapolation_count=extrapolation_count,
    )
    return _FoldResult(predictions=prediction_rows, audit=audit)


def _fit_gam(
    training: tuple[ProjectedParentRow, ...],
    validation: tuple[ProjectedParentRow, ...],
    *,
    variant: FeatureVariant,
    label: str,
) -> tuple[RegressionFit, tuple[tuple[float, float], ...], int]:
    train_linear, train_nonlinear = _gam_components(training, variant)
    valid_linear, valid_nonlinear = _gam_components(validation, variant)
    design = additive_design(
        train_linear,
        train_nonlinear,
        valid_linear,
        valid_nonlinear,
    )
    fit = fit_standardized_ols(
        design.training,
        _targets(training),
        design.validation,
        label=label,
    )
    return fit, design.knots, design.extrapolation_count


def _fit_two_part(
    training: tuple[ProjectedParentRow, ...],
    validation: tuple[ProjectedParentRow, ...],
    *,
    variant: FeatureVariant,
    label: str,
) -> tuple[RegressionFit, LogisticFit]:
    training_features = _linear_features(training, family="two_part", variant=variant)
    validation_features = _linear_features(validation, family="two_part", variant=variant)
    updates = np.asarray([float(row.updated) for row in training], dtype=np.float64)
    logistic = fit_ridge_logistic(
        training_features,
        updates,
        validation_features,
        label=f"{label} update",
    )
    update_mask = updates == 1.0
    conditional_rows = int(np.count_nonzero(update_mask))
    design_columns = training_features.shape[1] + 1
    if conditional_rows < design_columns + 1:
        raise ChallengerFitError(
            f"{label} conditional Huber requires at least {design_columns + 1} update rows"
        )
    conditional = fit_huber(
        training_features[update_mask],
        _targets(training)[update_mask],
        validation_features,
        label=f"{label} conditional",
    )
    return conditional, logistic


def _linear_features(
    rows: Sequence[ProjectedParentRow],
    *,
    family: ModelFamily,
    variant: FeatureVariant,
) -> NDArray[np.float64]:
    materialized: list[tuple[float, ...]] = []
    for row in rows:
        base: tuple[float, ...]
        features: tuple[float, ...]
        if family == "arx2":
            if row.previous_target_return_bps is None:
                raise ChallengerFitError("ARX(2) row has no regular predecessor")
            base = (
                row.target_return_bps,
                row.previous_target_return_bps,
                float(row.target_age_ms),
            )
            if variant == "target_only":
                features = base
            elif variant == "source_age":
                features = (*base, float(row.source_age_ms))
            else:
                features = (
                    *base,
                    float(row.source_age_ms),
                    row.source_return_bps,
                    row.gap_bps,
                )
        else:
            base = (row.target_return_bps, float(row.target_age_ms))
            if variant == "target_only":
                features = base
            elif variant == "source_age":
                features = (*base, float(row.source_age_ms))
            else:
                # Preserve the frozen endpoint order for exact OLS reproduction.
                features = (
                    *base,
                    row.source_return_bps,
                    row.gap_bps,
                    float(row.source_age_ms),
                )
        materialized.append(features)
    return np.asarray(materialized, dtype=np.float64)


def _gam_components(
    rows: Sequence[ProjectedParentRow],
    variant: FeatureVariant,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    linear: list[tuple[float, ...]] = []
    nonlinear: list[tuple[float, ...]] = []
    for row in rows:
        target_age = float(np.log1p(row.target_age_ms / 60_000.0))
        source_age = float(np.log1p(row.source_age_ms / 60_000.0))
        if variant == "target_only":
            linear.append((row.target_return_bps,))
            nonlinear.append((target_age,))
        elif variant == "source_age":
            linear.append((row.target_return_bps,))
            nonlinear.append((target_age, source_age))
        else:
            linear.append((row.target_return_bps, row.source_return_bps))
            nonlinear.append((target_age, source_age, row.gap_bps))
    return np.asarray(linear, dtype=np.float64), np.asarray(nonlinear, dtype=np.float64)


def _targets(rows: Sequence[ProjectedParentRow]) -> NDArray[np.float64]:
    return np.asarray([row.actual_bps for row in rows], dtype=np.float64)


def _fold_label(
    family: ModelFamily,
    variant: FeatureVariant,
    first: ParentPrediction,
) -> str:
    return (
        f"{family}/{variant}/{first.direction}/{first.horizon_ms}/fold-{first.fold_index}"
    )


def _projected_panels(
    parent: ChallengerParent,
) -> dict[tuple[Direction, int], tuple[ProjectedParentRow, ...]]:
    result: dict[tuple[Direction, int], tuple[ProjectedParentRow, ...]] = {}
    for panel in parent.panels:
        for direction in _DIRECTIONS:
            result[(direction, panel.horizon_ms)] = tuple(
                project_parent_row(
                    row,
                    direction,
                    predecessor=None if index == 0 else panel.rows[index - 1],
                )
                for index, row in enumerate(panel.rows)
            )
    return result


def _parent_prediction_groups(
    parent: ChallengerParent,
) -> dict[tuple[Direction, int], tuple[ParentPrediction, ...]]:
    groups: dict[tuple[Direction, int], tuple[ParentPrediction, ...]] = {}
    for direction in _DIRECTIONS:
        for horizon_ms in _HORIZONS_MS:
            rows = tuple(
                row
                for row in parent.predictions
                if row.direction == direction and row.horizon_ms == horizon_ms
            )
            if not rows:
                raise CrossPoolContractError("parent challenger group is empty")
            groups[(direction, horizon_ms)] = rows
    return groups


def _fold_groups(
    rows: tuple[ParentPrediction, ...],
) -> tuple[tuple[ParentPrediction, ...], ...]:
    fold_indices = tuple(sorted({row.fold_index for row in rows}))
    if fold_indices != tuple(range(len(fold_indices))):
        raise CrossPoolContractError("parent challenger folds are not contiguous")
    result = tuple(
        tuple(row for row in rows if row.fold_index == fold_index)
        for fold_index in fold_indices
    )
    for fold_index, fold in enumerate(result):
        if not fold or any(
            row.fold_index != fold_index
            or row.refit_timestamp_ms != fold[0].refit_timestamp_ms
            for row in fold
        ):
            raise CrossPoolContractError("parent challenger fold identity is inconsistent")
    return result
