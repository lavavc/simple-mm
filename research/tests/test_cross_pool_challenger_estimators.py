from __future__ import annotations

import numpy as np
import pytest

from research.cross_pool.challenger_estimators import (
    ChallengerFitError,
    _backtracking_logistic_candidate,
    _fit_huber,
    _fit_ridge_logistic,
    _mad_scale,
    additive_design,
    fit_huber,
    fit_ridge_logistic,
    fit_standardized_ols,
)


def test_standardized_ols_recovers_known_linear_process() -> None:
    features = np.column_stack((np.arange(20, dtype=float), np.arange(20, dtype=float) ** 2))
    targets = 3.0 + 2.0 * features[:, 0] - 0.5 * features[:, 1]
    validation = np.asarray([[20.0, 400.0], [21.0, 441.0]])

    fit = fit_standardized_ols(features, targets, validation, label="fixture")

    assert fit.predictions == pytest.approx((-157.0, -175.5), abs=1e-10)
    assert fit.iterations == 1
    assert fit.condition_number > 1.0


def test_standardized_ols_rejects_zero_variance_and_rank_failure() -> None:
    targets = np.arange(10, dtype=float)

    with pytest.raises(ChallengerFitError, match="zero-variance feature 1"):
        fit_standardized_ols(
            np.column_stack((np.arange(10, dtype=float), np.ones(10))),
            targets,
            np.zeros((1, 2)),
            label="zero",
        )

    x = np.arange(10, dtype=float)
    with pytest.raises(ChallengerFitError, match="rank deficient"):
        fit_standardized_ols(
            np.column_stack((x, 2.0 * x)),
            targets,
            np.asarray([[11.0, 22.0]]),
            label="rank",
        )


def test_additive_design_uses_training_knots_and_counts_extrapolation() -> None:
    train_linear = np.arange(6, dtype=float).reshape(-1, 1)
    validation_linear = np.asarray([[6.0], [7.0]])
    train_nonlinear = np.column_stack(
        (np.arange(6, dtype=float), np.arange(10, 16, dtype=float))
    )
    validation_nonlinear = np.asarray([[-1.0, 12.0], [8.0, 20.0]])

    design = additive_design(
        train_linear,
        train_nonlinear,
        validation_linear,
        validation_nonlinear,
    )

    assert design.training.shape == (6, 7)
    assert design.validation.shape == (2, 7)
    assert np.asarray(design.knots) == pytest.approx(
        np.asarray(((5.0 / 3.0, 10.0 / 3.0), (35.0 / 3.0, 40.0 / 3.0)))
    )
    assert design.extrapolation_count == 3


def test_additive_design_rejects_duplicate_knots() -> None:
    with pytest.raises(ChallengerFitError, match="duplicate spline knots"):
        additive_design(
            np.arange(6, dtype=float).reshape(-1, 1),
            np.column_stack((np.zeros(6), np.arange(6, dtype=float))),
            np.asarray([[1.0]]),
            np.asarray([[0.0, 1.0]]),
        )


def test_huber_downweights_outlier_and_moves_less_than_ols() -> None:
    x = np.arange(30, dtype=float).reshape(-1, 1)
    y = 1.0 + 2.0 * x[:, 0]
    y[-1] += 500.0
    validation = np.asarray([[30.0]])

    ols = fit_standardized_ols(x, y, validation, label="ols")
    huber = fit_huber(x, y, validation, label="huber")

    truth = 61.0
    assert abs(huber.predictions[0] - truth) < abs(ols.predictions[0] - truth)
    assert huber.downweighted_fraction is not None
    assert huber.downweighted_fraction > 0.0
    assert huber.robust_scale is not None and huber.robust_scale > 0.0
    assert 1 <= huber.iterations <= 100


def test_huber_rejects_zero_mad_and_nonconvergence() -> None:
    x = np.arange(10, dtype=float).reshape(-1, 1)
    with pytest.raises(ChallengerFitError, match="zero MAD scale"):
        _mad_scale(np.zeros(10), label="perfect")

    noisy = 2.0 * x[:, 0] + np.asarray([0, 1, -1, 2, -2, 3, -3, 4, -4, 20], dtype=float)
    with pytest.raises(ChallengerFitError, match="did not converge"):
        _fit_huber(
            x,
            noisy,
            np.asarray([[11.0]]),
            label="limited",
            maximum_iterations=1,
        )


def test_ridge_logistic_is_monotone_finite_and_deterministic() -> None:
    x = np.linspace(-3.0, 3.0, 40).reshape(-1, 1)
    y = (x[:, 0] + np.sin(np.arange(40)) * 0.25 > 0.0).astype(float)
    validation = np.asarray([[-2.0], [0.0], [2.0]])

    fit = fit_ridge_logistic(x, y, validation, label="logit")

    assert fit == fit_ridge_logistic(x, y, validation, label="logit")
    assert 0.0 < fit.probabilities[0] < fit.probabilities[1] < fit.probabilities[2] < 1.0
    assert 1 <= fit.iterations <= 100


def test_ridge_logistic_rejects_one_class_and_nonconvergence() -> None:
    x = np.arange(20, dtype=float).reshape(-1, 1)

    with pytest.raises(ChallengerFitError, match="both update classes"):
        fit_ridge_logistic(x, np.ones(20), np.asarray([[21.0]]), label="one-class")

    y = (x[:, 0] > 10.0).astype(float)
    with pytest.raises(ChallengerFitError, match="did not converge"):
        _fit_ridge_logistic(
            x,
            y,
            np.asarray([[21.0]]),
            label="limited",
            maximum_iterations=1,
        )


def test_logistic_line_search_evaluates_the_fiftieth_halved_step(monkeypatch) -> None:
    objectives = iter([0.0, *([1.0] * 50), 0.0])
    monkeypatch.setattr(
        "research.cross_pool.challenger_estimators._logistic_objective",
        lambda *_args: next(objectives),
    )

    accepted = _backtracking_logistic_candidate(
        np.ones((1, 1)),
        np.zeros(1),
        np.zeros(1),
        np.ones(1),
        np.zeros(1),
        label="boundary",
        maximum_halvings=50,
    )

    assert accepted[0] == pytest.approx(-(2.0**-50))
