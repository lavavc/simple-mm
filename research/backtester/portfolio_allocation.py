"""Frozen eligibility and capital-allocation rules for portfolio research."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence, TypeAlias

from research.backtester.portfolio_catalog import (
    DirectionalPolicyDefinition,
    SleeveDefinition,
)

FAMILY_CAP = 0.35
SLEEVE_CAP = 0.10
MIN_FEE_COST_RATIO = 1.0
SHRINKAGE_ETA = 0.5
SHRINKAGE_ALPHA = 0.25
SCORE_CLIP = (-2.0, 2.0)

AllocationUnit: TypeAlias = SleeveDefinition | DirectionalPolicyDefinition
AllocationRule: TypeAlias = Literal[
    "equal_config",
    "equal_family",
    "shrinkage",
    "rank_one_comparator",
]


@dataclass(frozen=True)
class TrainingMetrics:
    net_return: float
    episode_count: int
    fee_to_transaction_cost_ratio: float
    max_drawdown: float


@dataclass(frozen=True)
class Allocation:
    rule: AllocationRule
    weights: Mapping[str, float]
    cash_weight: float

    def __post_init__(self) -> None:
        if any(not math.isfinite(weight) or weight < 0 for weight in self.weights.values()):
            raise ValueError("weights must be finite and non-negative")
        if not math.isfinite(self.cash_weight) or self.cash_weight < 0:
            raise ValueError("cash_weight must be finite and non-negative")
        total = sum(self.weights.values()) + self.cash_weight
        if total > 1.0 + 1e-12:
            raise ValueError("allocation exceeds bankroll")
        if abs(total - 1.0) > 1e-12:
            raise ValueError("allocation weights and cash must reconcile exactly")


def is_eligible(metrics: TrainingMetrics) -> bool:
    return (
        math.isfinite(metrics.net_return)
        and metrics.net_return > 0
        and metrics.episode_count >= 1
        and math.isfinite(metrics.fee_to_transaction_cost_ratio)
        and metrics.fee_to_transaction_cost_ratio >= MIN_FEE_COST_RATIO
        and math.isfinite(metrics.max_drawdown)
        and metrics.max_drawdown <= 0
    )


def eligible_sleeves(
    sleeves: Sequence[AllocationUnit],
    metrics: Mapping[str, TrainingMetrics],
) -> tuple[AllocationUnit, ...]:
    _validate_unique_ids(sleeves)
    return tuple(
        sleeve
        for sleeve in sleeves
        if sleeve.sleeve_id in metrics and is_eligible(metrics[sleeve.sleeve_id])
    )


def select_rank_one(
    sleeves: Sequence[AllocationUnit],
    metrics: Mapping[str, TrainingMetrics],
) -> AllocationUnit | None:
    """Select the deterministic training-only winner among eligible units."""
    eligible = eligible_sleeves(sleeves, metrics)
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda sleeve: (
            -metrics[sleeve.sleeve_id].net_return,
            -metrics[sleeve.sleeve_id].fee_to_transaction_cost_ratio,
            -metrics[sleeve.sleeve_id].max_drawdown,
            sleeve.sleeve_id,
        ),
    )


def rank_one_comparator_allocation(
    sleeves: Sequence[AllocationUnit],
    metrics: Mapping[str, TrainingMetrics],
) -> Allocation:
    selected = select_rank_one(sleeves, metrics)
    if selected is None:
        return _allocation("rank_one_comparator", {})
    return _allocation(
        "rank_one_comparator",
        {selected.sleeve_id: SLEEVE_CAP},
    )


def remove_sleeve_from_allocation(
    allocation: Allocation,
    removed_economic_id: str,
) -> Allocation:
    """Move one sleeve's declared weight to cash without renormalizing peers."""
    if not removed_economic_id:
        raise ValueError("removed economic ID must be non-empty")
    weights = {
        economic_id: weight
        for economic_id, weight in allocation.weights.items()
        if economic_id != removed_economic_id
    }
    return Allocation(
        allocation.rule,
        weights,
        1.0 - sum(weights.values()),
    )


def equal_config_weights(
    sleeves: Sequence[AllocationUnit],
    metrics: Mapping[str, TrainingMetrics],
) -> Allocation:
    eligible = eligible_sleeves(sleeves, metrics)
    if not eligible:
        return _allocation("equal_config", {})

    uncapped = {sleeve.sleeve_id: 1.0 / len(eligible) for sleeve in eligible}
    return _allocation("equal_config", _apply_caps(eligible, uncapped))


def equal_family_weights(
    sleeves: Sequence[AllocationUnit],
    metrics: Mapping[str, TrainingMetrics],
) -> Allocation:
    eligible = eligible_sleeves(sleeves, metrics)
    if not eligible:
        return _allocation("equal_family", {})
    return _allocation(
        "equal_family", _apply_caps(eligible, _family_base(eligible))
    )


def shrinkage_weights(
    sleeves: Sequence[AllocationUnit],
    metrics: Mapping[str, TrainingMetrics],
) -> Allocation:
    eligible = eligible_sleeves(sleeves, metrics)
    if not eligible:
        return _allocation("shrinkage", {})

    base = _family_base(eligible)
    tilted_terms: dict[str, float] = {}
    for sleeve in eligible:
        training = metrics[sleeve.sleeve_id]
        if not math.isfinite(training.max_drawdown):
            raise ValueError("shrinkage requires finite max_drawdown")
        if training.max_drawdown == 0:
            risk_score = SCORE_CLIP[1]
        else:
            risk_score = training.net_return / abs(training.max_drawdown)
        clipped_score = min(max(risk_score, SCORE_CLIP[0]), SCORE_CLIP[1])
        tilted_terms[sleeve.sleeve_id] = base[sleeve.sleeve_id] * math.exp(
            SHRINKAGE_ETA * clipped_score
        )

    base_total = sum(base.values())
    tilt_total = sum(tilted_terms.values())
    tilted = {
        sleeve_id: term * base_total / tilt_total
        for sleeve_id, term in tilted_terms.items()
    }
    mixed = {
        sleeve_id: (1.0 - SHRINKAGE_ALPHA) * base_weight
        + SHRINKAGE_ALPHA * tilted[sleeve_id]
        for sleeve_id, base_weight in base.items()
    }
    return _allocation("shrinkage", _apply_caps(eligible, mixed))


def _family_base(sleeves: Sequence[AllocationUnit]) -> dict[str, float]:
    by_family: dict[str, list[AllocationUnit]] = {}
    for sleeve in sleeves:
        by_family.setdefault(sleeve.family, []).append(sleeve)
    family_weight = min(1.0 / len(by_family), FAMILY_CAP)
    return {
        sleeve.sleeve_id: family_weight / len(by_family[sleeve.family])
        for sleeve in sleeves
    }


def _apply_caps(
    sleeves: Sequence[AllocationUnit], weights: Mapping[str, float]
) -> dict[str, float]:
    capped = {
        sleeve.sleeve_id: min(weights[sleeve.sleeve_id], SLEEVE_CAP)
        for sleeve in sleeves
    }
    by_family: dict[str, list[str]] = {}
    for sleeve in sleeves:
        by_family.setdefault(sleeve.family, []).append(sleeve.sleeve_id)
    for sleeve_ids in by_family.values():
        family_total = sum(capped[sleeve_id] for sleeve_id in sleeve_ids)
        if family_total > FAMILY_CAP:
            scale = FAMILY_CAP / family_total
            for sleeve_id in sleeve_ids:
                capped[sleeve_id] *= scale
    return capped


def _allocation(
    rule: AllocationRule,
    weights: Mapping[str, float],
) -> Allocation:
    owned_weights = dict(weights)
    return Allocation(rule, owned_weights, 1.0 - sum(owned_weights.values()))


def _validate_unique_ids(sleeves: Sequence[AllocationUnit]) -> None:
    sleeve_ids = [sleeve.sleeve_id for sleeve in sleeves]
    if len(sleeve_ids) != len(set(sleeve_ids)):
        raise ValueError("duplicate sleeve_id in allocation units")
