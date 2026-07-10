from __future__ import annotations

import math
from dataclasses import replace

import pytest

from research.backtester.portfolio_allocation import (
    FAMILY_CAP,
    MIN_FEE_COST_RATIO,
    SCORE_CLIP,
    SHRINKAGE_ALPHA,
    SHRINKAGE_ETA,
    SLEEVE_CAP,
    Allocation,
    TrainingMetrics,
    eligible_sleeves,
    equal_config_weights,
    equal_family_weights,
    is_eligible,
    shrinkage_weights,
)
from research.backtester.portfolio_catalog import SleeveDefinition


def sleeve(sleeve_id: str, family: str) -> SleeveDefinition:
    return SleeveDefinition(
        sleeve_id=sleeve_id,
        family=family,  # type: ignore[arg-type]
        config_name=sleeve_id,
        parameter_payload="{}",
        parameter_fingerprint=sleeve_id,
        source_constructor="test",
    )


def passing_metrics(**changes: float | int) -> TrainingMetrics:
    metrics = TrainingMetrics(0.10, 1, 2.0, -0.05)
    return replace(metrics, **changes)


def test_frozen_constants() -> None:
    assert FAMILY_CAP == 0.35
    assert SLEEVE_CAP == 0.10
    assert MIN_FEE_COST_RATIO == 1.0
    assert SHRINKAGE_ETA == 0.5
    assert SHRINKAGE_ALPHA == 0.25
    assert SCORE_CLIP == (-2.0, 2.0)


@pytest.mark.parametrize(
    "metrics",
    [
        passing_metrics(net_return=0.0),
        passing_metrics(net_return=-0.01),
        passing_metrics(episode_count=0),
        passing_metrics(fee_to_transaction_cost_ratio=0.99),
        passing_metrics(net_return=math.nan),
        passing_metrics(net_return=math.inf),
        passing_metrics(fee_to_transaction_cost_ratio=math.nan),
    ],
)
def test_eligibility_fails_closed(metrics: TrainingMetrics) -> None:
    assert not is_eligible(metrics)


def test_eligible_sleeves_require_metrics_and_preserve_catalog_order() -> None:
    sleeves = [sleeve("b", "paper"), sleeve("missing", "paper"), sleeve("a", "ewma")]
    metrics = {"a": passing_metrics(), "b": passing_metrics()}

    assert [item.sleeve_id for item in eligible_sleeves(sleeves, metrics)] == ["b", "a"]


def test_equal_config_weights_apply_sleeve_and_family_caps_without_redistribution() -> None:
    sleeves = [sleeve(str(index), "ewma") for index in range(4)]
    metrics = {item.sleeve_id: passing_metrics() for item in sleeves}

    allocation = equal_config_weights(sleeves, metrics)

    assert allocation.rule == "equal_config"
    assert allocation.weights == pytest.approx({str(index): 0.0875 for index in range(4)})
    assert allocation.cash_weight == pytest.approx(0.65)


def test_equal_family_weights_ignore_family_grid_size() -> None:
    sleeves = [sleeve("a", "ewma"), sleeve("b", "ewma"), sleeve("c", "paper")]
    metrics = {item.sleeve_id: passing_metrics() for item in sleeves}

    allocation = equal_family_weights(sleeves, metrics)

    assert allocation.weights["a"] == pytest.approx(0.175)
    assert allocation.weights["b"] == pytest.approx(0.175)
    assert allocation.weights["c"] == pytest.approx(0.35)
    assert allocation.cash_weight == pytest.approx(0.30)


def test_equal_family_weights_leave_only_family_cap_residual_as_cash() -> None:
    sleeves = [sleeve("a", "ewma"), sleeve("b", "paper")]
    metrics = {item.sleeve_id: passing_metrics() for item in sleeves}

    allocation = equal_family_weights(sleeves, metrics)

    assert allocation.weights == pytest.approx({"a": 0.35, "b": 0.35})
    assert allocation.cash_weight == pytest.approx(0.30)


def test_no_eligible_sleeves_holds_cash() -> None:
    metrics = {"a": TrainingMetrics(-0.01, 1, 2.0, -0.02)}

    allocation = equal_family_weights([sleeve("a", "ewma")], metrics)

    assert allocation.weights == {}
    assert allocation.cash_weight == 1.0


def test_shrinkage_is_deterministic_and_clips_risk_score() -> None:
    sleeves = [sleeve("high", "ewma"), sleeve("low", "ewma")]
    metrics = {
        "high": passing_metrics(net_return=10.0, max_drawdown=-0.01),
        "low": passing_metrics(net_return=0.01, max_drawdown=-1.0),
    }

    first = shrinkage_weights(sleeves, metrics)
    second = shrinkage_weights(sleeves, metrics)

    assert first == second
    assert first.rule == "shrinkage"
    assert first.weights == pytest.approx({"high": 0.10, "low": 0.10})
    assert first.cash_weight == pytest.approx(0.80)


def test_shrinkage_rejects_non_finite_drawdown() -> None:
    item = sleeve("a", "ewma")

    with pytest.raises(ValueError, match="finite max_drawdown"):
        shrinkage_weights([item], {"a": passing_metrics(max_drawdown=math.nan)})


@pytest.mark.parametrize("cash_weight", [math.nan, math.inf, -0.01])
def test_allocation_rejects_invalid_cash_weight(cash_weight: float) -> None:
    with pytest.raises(ValueError, match="cash_weight"):
        Allocation("equal_config", {}, cash_weight)


def test_allocation_rejects_invalid_weights_and_overallocation() -> None:
    with pytest.raises(ValueError, match="weights"):
        Allocation("equal_config", {"a": math.nan}, 0.0)
    with pytest.raises(ValueError, match="weights"):
        Allocation("equal_config", {"a": -0.01}, 1.0)
    with pytest.raises(ValueError, match="exceeds bankroll"):
        Allocation("equal_config", {"a": 0.1}, 0.91)
