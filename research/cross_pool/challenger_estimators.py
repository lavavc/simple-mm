"""Deterministic NumPy estimators for the challenger study."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import TypeAlias

import numpy as np
from numpy.typing import NDArray

FloatArray: TypeAlias = NDArray[np.float64]

HUBER_DELTA = 1.345
HUBER_MAD_FACTOR = 1.482602218505602
RIDGE_LOGISTIC_PENALTY = 0.1
CONVERGENCE_TOLERANCE = 1e-10
MAXIMUM_ITERATIONS = 100
MAXIMUM_LINE_SEARCH_HALVINGS = 50
MAXIMUM_CONDITION_NUMBER = 1e12


class ChallengerFitError(RuntimeError):
    """Raised when a registered estimator cannot satisfy its fit contract."""


@dataclass(frozen=True)
class RegressionFit:
    predictions: tuple[float, ...]
    coefficients: tuple[float, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    condition_number: float
    iterations: int
    robust_scale: float | None = None
    downweighted_fraction: float | None = None


@dataclass(frozen=True)
class LogisticFit:
    probabilities: tuple[float, ...]
    coefficients: tuple[float, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    condition_number: float
    iterations: int


@dataclass(frozen=True)
class AdditiveDesign:
    training: FloatArray
    validation: FloatArray
    knots: tuple[tuple[float, float], ...]
    extrapolation_count: int


def fit_standardized_ols(
    training_features: FloatArray,
    training_targets: FloatArray,
    validation_features: FloatArray,
    *,
    label: str,
    maximum_condition_number: float = MAXIMUM_CONDITION_NUMBER,
) -> RegressionFit:
    training, targets, validation = _validated_regression_arrays(
        training_features,
        training_targets,
        validation_features,
        label=label,
    )
    standardized_training, standardized_validation, means, scales = _standardize(
        training,
        validation,
        label=label,
    )
    design = _with_intercept(standardized_training)
    validation_design = _with_intercept(standardized_validation)
    coefficients, condition_number = _checked_least_squares(
        design,
        targets,
        label=label,
        maximum_condition_number=maximum_condition_number,
    )
    predictions = _checked_predictions(validation_design, coefficients, label=label)
    return RegressionFit(
        predictions=_float_tuple(predictions),
        coefficients=_float_tuple(coefficients),
        feature_means=_float_tuple(means),
        feature_scales=_float_tuple(scales),
        condition_number=condition_number,
        iterations=1,
    )


def fit_huber(
    training_features: FloatArray,
    training_targets: FloatArray,
    validation_features: FloatArray,
    *,
    label: str,
    maximum_condition_number: float = MAXIMUM_CONDITION_NUMBER,
) -> RegressionFit:
    return _fit_huber(
        training_features,
        training_targets,
        validation_features,
        label=label,
        maximum_condition_number=maximum_condition_number,
        maximum_iterations=MAXIMUM_ITERATIONS,
    )


def _fit_huber(
    training_features: FloatArray,
    training_targets: FloatArray,
    validation_features: FloatArray,
    *,
    label: str,
    maximum_iterations: int,
    maximum_condition_number: float = MAXIMUM_CONDITION_NUMBER,
) -> RegressionFit:
    if maximum_iterations <= 0:
        raise ChallengerFitError(f"{label} maximum iterations must be positive")
    training, targets, validation = _validated_regression_arrays(
        training_features,
        training_targets,
        validation_features,
        label=label,
    )
    standardized_training, standardized_validation, means, scales = _standardize(
        training,
        validation,
        label=label,
    )
    design = _with_intercept(standardized_training)
    validation_design = _with_intercept(standardized_validation)
    coefficients, _ = _checked_least_squares(
        design,
        targets,
        label=f"{label} initialization",
        maximum_condition_number=maximum_condition_number,
    )

    final_condition = math_nan()
    for iteration in range(1, maximum_iterations + 1):
        residuals = targets - design @ coefficients
        scale = _mad_scale(residuals, label=label)
        weights = _huber_weights(residuals, scale, label=label)
        square_root_weights = np.sqrt(weights)
        weighted_design = design * square_root_weights[:, None]
        weighted_targets = targets * square_root_weights
        updated, final_condition = _checked_least_squares(
            weighted_design,
            weighted_targets,
            label=f"{label} weighted",
            maximum_condition_number=maximum_condition_number,
        )
        denominator = max(1.0, float(np.linalg.norm(coefficients)))
        relative_change = float(np.linalg.norm(updated - coefficients) / denominator)
        if not isfinite(relative_change):
            raise ChallengerFitError(f"{label} coefficient change is non-finite")
        coefficients = updated
        if relative_change <= CONVERGENCE_TOLERANCE:
            final_residuals = targets - design @ coefficients
            final_scale = _mad_scale(final_residuals, label=label)
            final_weights = _huber_weights(final_residuals, final_scale, label=label)
            predictions = _checked_predictions(
                validation_design,
                coefficients,
                label=label,
            )
            return RegressionFit(
                predictions=_float_tuple(predictions),
                coefficients=_float_tuple(coefficients),
                feature_means=_float_tuple(means),
                feature_scales=_float_tuple(scales),
                condition_number=final_condition,
                iterations=iteration,
                robust_scale=final_scale,
                downweighted_fraction=float(np.mean(final_weights < 1.0)),
            )
    raise ChallengerFitError(f"{label} Huber fit did not converge")


def fit_ridge_logistic(
    training_features: FloatArray,
    training_updates: FloatArray,
    validation_features: FloatArray,
    *,
    label: str,
    maximum_condition_number: float = MAXIMUM_CONDITION_NUMBER,
) -> LogisticFit:
    return _fit_ridge_logistic(
        training_features,
        training_updates,
        validation_features,
        label=label,
        maximum_condition_number=maximum_condition_number,
        maximum_iterations=MAXIMUM_ITERATIONS,
    )


def _fit_ridge_logistic(
    training_features: FloatArray,
    training_updates: FloatArray,
    validation_features: FloatArray,
    *,
    label: str,
    maximum_iterations: int,
    maximum_condition_number: float = MAXIMUM_CONDITION_NUMBER,
) -> LogisticFit:
    if maximum_iterations <= 0:
        raise ChallengerFitError(f"{label} maximum iterations must be positive")
    training, updates, validation = _validated_regression_arrays(
        training_features,
        training_updates,
        validation_features,
        label=label,
    )
    if not np.all((updates == 0.0) | (updates == 1.0)):
        raise ChallengerFitError(f"{label} update labels must be binary")
    if np.unique(updates).size != 2:
        raise ChallengerFitError(f"{label} logistic training requires both update classes")
    standardized_training, standardized_validation, means, scales = _standardize(
        training,
        validation,
        label=label,
    )
    design = _with_intercept(standardized_training)
    validation_design = _with_intercept(standardized_validation)
    coefficients = np.zeros(design.shape[1], dtype=np.float64)
    penalty = np.ones(design.shape[1], dtype=np.float64)
    penalty[0] = 0.0
    final_condition = math_nan()

    for iteration in range(1, maximum_iterations + 1):
        linear_predictor = design @ coefficients
        probabilities = _sigmoid(linear_predictor)
        gradient = design.T @ (probabilities - updates) / design.shape[0]
        gradient += RIDGE_LOGISTIC_PENALTY * penalty * coefficients
        variances = probabilities * (1.0 - probabilities)
        hessian = (design.T * variances) @ design / design.shape[0]
        hessian += np.diag(RIDGE_LOGISTIC_PENALTY * penalty)
        final_condition = _checked_condition(
            hessian,
            label=f"{label} logistic Hessian",
            maximum_condition_number=maximum_condition_number,
        )
        try:
            newton_step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError as exc:
            raise ChallengerFitError(f"{label} logistic Newton solve failed") from exc
        if not np.all(np.isfinite(newton_step)):
            raise ChallengerFitError(f"{label} logistic Newton step is non-finite")

        accepted = _backtracking_logistic_candidate(
            design,
            updates,
            coefficients,
            newton_step,
            penalty,
            label=label,
        )
        accepted_step = accepted - coefficients
        denominator = max(1.0, float(np.linalg.norm(coefficients)))
        relative_change = float(np.linalg.norm(accepted_step) / denominator)
        coefficients = accepted
        if relative_change <= CONVERGENCE_TOLERANCE:
            validation_probabilities = _sigmoid(validation_design @ coefficients)
            if not np.all(np.isfinite(validation_probabilities)) or np.any(
                (validation_probabilities < 0.0) | (validation_probabilities > 1.0)
            ):
                raise ChallengerFitError(f"{label} logistic probabilities are invalid")
            return LogisticFit(
                probabilities=_float_tuple(validation_probabilities),
                coefficients=_float_tuple(coefficients),
                feature_means=_float_tuple(means),
                feature_scales=_float_tuple(scales),
                condition_number=final_condition,
                iterations=iteration,
            )
    raise ChallengerFitError(f"{label} logistic fit did not converge")


def additive_design(
    training_linear: FloatArray,
    training_nonlinear: FloatArray,
    validation_linear: FloatArray,
    validation_nonlinear: FloatArray,
) -> AdditiveDesign:
    train_linear = _two_dimensional(training_linear, "training linear features")
    train_nonlinear = _two_dimensional(training_nonlinear, "training nonlinear features")
    valid_linear = _two_dimensional(validation_linear, "validation linear features")
    valid_nonlinear = _two_dimensional(
        validation_nonlinear,
        "validation nonlinear features",
    )
    if train_linear.shape[0] != train_nonlinear.shape[0]:
        raise ChallengerFitError("additive training rows do not align")
    if valid_linear.shape[0] != valid_nonlinear.shape[0]:
        raise ChallengerFitError("additive validation rows do not align")
    if train_linear.shape[1] != valid_linear.shape[1]:
        raise ChallengerFitError("additive linear columns do not align")
    if train_nonlinear.shape[1] != valid_nonlinear.shape[1]:
        raise ChallengerFitError("additive nonlinear columns do not align")
    if train_nonlinear.shape[1] == 0:
        raise ChallengerFitError("additive design requires a nonlinear feature")

    training_parts: list[FloatArray] = [train_linear]
    validation_parts: list[FloatArray] = [valid_linear]
    knots: list[tuple[float, float]] = []
    extrapolation_count = 0
    for column in range(train_nonlinear.shape[1]):
        train_values = train_nonlinear[:, column]
        valid_values = valid_nonlinear[:, column]
        quantiles = np.quantile(train_values, (1.0 / 3.0, 2.0 / 3.0), method="linear")
        lower_knot = float(quantiles[0])
        upper_knot = float(quantiles[1])
        if not lower_knot < upper_knot:
            raise ChallengerFitError(f"duplicate spline knots for nonlinear feature {column}")
        knots.append((lower_knot, upper_knot))
        training_parts.append(_hinge_basis(train_values, lower_knot, upper_knot))
        validation_parts.append(_hinge_basis(valid_values, lower_knot, upper_knot))
        extrapolation_count += int(
            np.count_nonzero(
                (valid_values < np.min(train_values))
                | (valid_values > np.max(train_values))
            )
        )
    training = np.column_stack(training_parts).astype(np.float64, copy=False)
    validation = np.column_stack(validation_parts).astype(np.float64, copy=False)
    if not np.all(np.isfinite(training)) or not np.all(np.isfinite(validation)):
        raise ChallengerFitError("additive design contains non-finite values")
    return AdditiveDesign(
        training=training,
        validation=validation,
        knots=tuple(knots),
        extrapolation_count=extrapolation_count,
    )


def _validated_regression_arrays(
    training_features: FloatArray,
    training_targets: FloatArray,
    validation_features: FloatArray,
    *,
    label: str,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    training = _two_dimensional(training_features, f"{label} training features")
    validation = _two_dimensional(validation_features, f"{label} validation features")
    targets = np.asarray(training_targets, dtype=np.float64)
    if targets.ndim != 1:
        raise ChallengerFitError(f"{label} targets must be one-dimensional")
    if training.shape[0] != targets.shape[0]:
        raise ChallengerFitError(f"{label} training features and targets do not align")
    if training.shape[1] == 0 or validation.shape[1] != training.shape[1]:
        raise ChallengerFitError(f"{label} feature columns do not align")
    if training.shape[0] < training.shape[1] + 1:
        raise ChallengerFitError(f"{label} has too few training rows")
    if validation.shape[0] == 0:
        raise ChallengerFitError(f"{label} requires validation rows")
    if not np.all(np.isfinite(targets)):
        raise ChallengerFitError(f"{label} targets contain non-finite values")
    return training, targets, validation


def _two_dimensional(values: FloatArray, label: str) -> FloatArray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 2:
        raise ChallengerFitError(f"{label} must be two-dimensional")
    if not np.all(np.isfinite(array)):
        raise ChallengerFitError(f"{label} contains non-finite values")
    return array


def _standardize(
    training: FloatArray,
    validation: FloatArray,
    *,
    label: str,
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    means = np.mean(training, axis=0)
    scales = np.std(training, axis=0, ddof=0)
    if not np.all(np.isfinite(means)) or not np.all(np.isfinite(scales)):
        raise ChallengerFitError(f"{label} scaler is non-finite")
    zero_variance = np.flatnonzero(scales == 0.0)
    if zero_variance.size:
        raise ChallengerFitError(
            f"{label} zero-variance feature {int(zero_variance[0])}"
        )
    standardized_training = (training - means) / scales
    standardized_validation = (validation - means) / scales
    if not np.all(np.isfinite(standardized_validation)):
        raise ChallengerFitError(f"{label} standardized validation is non-finite")
    return standardized_training, standardized_validation, means, scales


def _with_intercept(features: FloatArray) -> FloatArray:
    return np.column_stack((np.ones(features.shape[0], dtype=np.float64), features))


def _checked_least_squares(
    design: FloatArray,
    targets: FloatArray,
    *,
    label: str,
    maximum_condition_number: float,
) -> tuple[FloatArray, float]:
    if design.shape[0] < design.shape[1]:
        raise ChallengerFitError(f"{label} design has too few rows")
    condition_number = _checked_condition(
        design,
        label=f"{label} design",
        maximum_condition_number=maximum_condition_number,
    )
    relative_cutoff = np.finfo(np.float64).eps * max(design.shape)
    try:
        coefficients, _, rank, singular_values = np.linalg.lstsq(
            design,
            targets,
            rcond=relative_cutoff,
        )
    except np.linalg.LinAlgError as exc:
        raise ChallengerFitError(f"{label} least-squares solve failed") from exc
    if rank < design.shape[1]:
        raise ChallengerFitError(f"{label} design is rank deficient")
    if not np.all(np.isfinite(singular_values)) or not np.all(np.isfinite(coefficients)):
        raise ChallengerFitError(f"{label} least-squares solve is non-finite")
    return coefficients.astype(np.float64, copy=False), condition_number


def _checked_condition(
    matrix: FloatArray,
    *,
    label: str,
    maximum_condition_number: float,
) -> float:
    if not isfinite(maximum_condition_number) or maximum_condition_number <= 0.0:
        raise ChallengerFitError("maximum condition number must be positive and finite")
    try:
        singular_values = np.linalg.svd(matrix, compute_uv=False)
    except np.linalg.LinAlgError as exc:
        raise ChallengerFitError(f"{label} SVD failed") from exc
    if singular_values.size == 0 or not np.all(np.isfinite(singular_values)):
        raise ChallengerFitError(f"{label} singular values are invalid")
    relative_cutoff = np.finfo(np.float64).eps * max(matrix.shape)
    rank = int(np.sum(singular_values > singular_values[0] * relative_cutoff))
    if rank < min(matrix.shape):
        raise ChallengerFitError(f"{label} is rank deficient")
    condition_number = float(singular_values[0] / singular_values[-1])
    if not isfinite(condition_number):
        raise ChallengerFitError(f"{label} condition number is non-finite")
    if condition_number > maximum_condition_number:
        raise ChallengerFitError(
            f"{label} condition number {condition_number} exceeds {maximum_condition_number}"
        )
    return condition_number


def _checked_predictions(
    design: FloatArray,
    coefficients: FloatArray,
    *,
    label: str,
) -> FloatArray:
    predictions = design @ coefficients
    if not np.all(np.isfinite(predictions)):
        raise ChallengerFitError(f"{label} predictions are non-finite")
    return predictions.astype(np.float64, copy=False)


def _mad_scale(residuals: FloatArray, *, label: str) -> float:
    residual_median = float(np.median(residuals))
    scale = HUBER_MAD_FACTOR * float(np.median(np.abs(residuals - residual_median)))
    if not isfinite(scale) or scale <= 0.0:
        raise ChallengerFitError(f"{label} Huber residuals have zero MAD scale")
    return scale


def _huber_weights(residuals: FloatArray, scale: float, *, label: str) -> FloatArray:
    standardized = residuals / scale
    absolute = np.abs(standardized)
    weights = np.ones_like(absolute)
    tail = absolute > HUBER_DELTA
    weights[tail] = HUBER_DELTA / absolute[tail]
    if not np.all(np.isfinite(weights)) or np.any(weights <= 0.0) or np.any(weights > 1.0):
        raise ChallengerFitError(f"{label} Huber weights are invalid")
    result: FloatArray = weights
    return result


def _sigmoid(values: FloatArray) -> FloatArray:
    probabilities = np.empty_like(values, dtype=np.float64)
    nonnegative = values >= 0.0
    probabilities[nonnegative] = 1.0 / (1.0 + np.exp(-values[nonnegative]))
    exponentials = np.exp(values[~nonnegative])
    probabilities[~nonnegative] = exponentials / (1.0 + exponentials)
    return probabilities


def _logistic_objective(
    design: FloatArray,
    updates: FloatArray,
    coefficients: FloatArray,
    penalty: FloatArray,
) -> float:
    linear_predictor = design @ coefficients
    likelihood = np.mean(np.logaddexp(0.0, linear_predictor) - updates * linear_predictor)
    ridge = 0.5 * RIDGE_LOGISTIC_PENALTY * float(
        np.dot(penalty * coefficients, coefficients)
    )
    return float(likelihood + ridge)


def _backtracking_logistic_candidate(
    design: FloatArray,
    updates: FloatArray,
    coefficients: FloatArray,
    newton_step: FloatArray,
    penalty: FloatArray,
    *,
    label: str,
    maximum_halvings: int = MAXIMUM_LINE_SEARCH_HALVINGS,
) -> FloatArray:
    if maximum_halvings < 0:
        raise ChallengerFitError(f"{label} maximum halvings must be nonnegative")
    objective = _logistic_objective(design, updates, coefficients, penalty)
    step_scale = 1.0
    for halving in range(maximum_halvings + 1):
        candidate = coefficients - step_scale * newton_step
        candidate_objective = _logistic_objective(design, updates, candidate, penalty)
        if isfinite(candidate_objective) and candidate_objective <= objective:
            return candidate
        if halving < maximum_halvings:
            step_scale *= 0.5
    raise ChallengerFitError(f"{label} logistic line search failed")


def _hinge_basis(values: FloatArray, lower_knot: float, upper_knot: float) -> FloatArray:
    return np.column_stack(
        (
            values,
            np.maximum(0.0, values - lower_knot),
            np.maximum(0.0, values - upper_knot),
        )
    )


def _float_tuple(values: FloatArray) -> tuple[float, ...]:
    return tuple(float(value) for value in values)


def math_nan() -> float:
    return float("nan")
