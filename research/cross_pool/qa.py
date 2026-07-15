"""Quality audits for validated cross-pool event streams."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Sequence

from research.cross_pool.bootstrap import (
    _prediction_improvement_sign,
    _target_day_label,
    infer_predictions,
    validate_prediction_rows,
)
from research.cross_pool.contracts import (
    CrossPoolContractError,
    DirectionalPredictiveAudit,
    GapQuantiles,
    InfluenceReport,
    OmissionResult,
    PoolEvent,
    PredictionRow,
    Regime,
    RegimeSensitivity,
    RegimeSubsetInference,
    StreamQuality,
)


def validate_stream(
    events: Sequence[PoolEvent],
    *,
    transition_block: int,
) -> StreamQuality:
    if not events:
        raise CrossPoolContractError("stream QA requires at least one pool event")
    if transition_block <= 0:
        raise CrossPoolContractError("transition_block must be positive")

    pool = events[0].pool
    if any(event.pool != pool for event in events):
        raise CrossPoolContractError("stream QA requires one pool")

    gaps = [
        current.timestamp_ms - previous.timestamp_ms
        for previous, current in zip(events, events[1:], strict=False)
    ]
    if any(gap <= 0 for gap in gaps):
        raise CrossPoolContractError("timestamps must be strictly increasing")

    quantiles = (
        GapQuantiles(
            p50=_integer_quantile(gaps, Decimal("0.50")),
            p95=_integer_quantile(gaps, Decimal("0.95")),
            p99=_integer_quantile(gaps, Decimal("0.99")),
        )
        if gaps
        else None
    )
    pre_transition_rows = sum(event.block_number < transition_block for event in events)

    return StreamQuality(
        pool=pool,
        rows=len(events),
        first_timestamp_ms=events[0].timestamp_ms,
        last_timestamp_ms=events[-1].timestamp_ms,
        update_gap_quantiles_ms=quantiles,
        pre_transition_rows=pre_transition_rows,
        post_transition_rows=len(events) - pre_transition_rows,
    )


def _integer_quantile(values: Sequence[int], probability: Decimal) -> int:
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * probability
    lower_index = int(position)
    upper_index = lower_index if position == lower_index else lower_index + 1
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    interpolated = Decimal(ordered[lower_index]) + fraction * (
        ordered[upper_index] - ordered[lower_index]
    )
    return int(interpolated.to_integral_value(rounding=ROUND_HALF_EVEN))


def assess_prediction_influence(rows: Sequence[PredictionRow]) -> InfluenceReport:
    validated = validate_prediction_rows(rows)
    day_units = tuple(sorted({_target_day_label(row) for row in validated}))
    fold_units = tuple(sorted({row.fold_index for row in validated}))
    if len(day_units) < 2:
        raise CrossPoolContractError("influence analysis requires at least two target days")
    if len(fold_units) < 2:
        raise CrossPoolContractError("influence analysis requires at least two observed folds")

    full = infer_predictions(validated)
    full_sign = _prediction_improvement_sign(full.metrics.mae_improvement_bps)
    leave_one_day = tuple(
        _omission_result(
            unit=day,
            rows=tuple(row for row in validated if _target_day_label(row) != day),
        )
        for day in day_units
    )
    leave_one_fold = tuple(
        _omission_result(
            unit=str(fold),
            rows=tuple(row for row in validated if row.fold_index != fold),
        )
        for fold in fold_units
    )
    unit_dependent = any(
        omission.mae_improvement_sign != full_sign or omission.evidence_class != full.evidence_class
        for omission in leave_one_day + leave_one_fold
    )
    return InfluenceReport(
        direction=full.metrics.direction,
        horizon_ms=full.metrics.horizon_ms,
        full_mae_improvement_sign=full_sign,
        full_evidence_class=full.evidence_class,
        leave_one_day=leave_one_day,
        leave_one_fold=leave_one_fold,
        unit_dependent=unit_dependent,
    )


def assess_regime_sensitivity(rows: Sequence[PredictionRow]) -> RegimeSensitivity:
    validated = validate_prediction_rows(rows)
    early_rows = tuple(
        row for row in validated if row.target_regime == "early" and row.source_regime == "early"
    )
    late_rows = tuple(
        row for row in validated if row.target_regime == "late" and row.source_regime == "late"
    )
    mixed_rows = tuple(
        row
        for row in validated
        if not (row.target_regime == "early" and row.source_regime == "early")
        and not (row.target_regime == "late" and row.source_regime == "late")
    )
    early = _regime_subset("early", early_rows)
    mixed = _regime_subset("mixed", mixed_rows)
    late = _regime_subset("late", late_rows)

    adjudicable = (
        early.inference is not None
        and late.inference is not None
        and not early.inference.adequacy.underpowered
        and not late.inference.adequacy.underpowered
    )
    regime_unstable = False
    if adjudicable:
        assert early.inference is not None and late.inference is not None
        early_sign = _prediction_improvement_sign(early.inference.metrics.mae_improvement_bps)
        late_sign = _prediction_improvement_sign(late.inference.metrics.mae_improvement_bps)
        opposite_signs = early_sign != 0 and early_sign == -late_sign
        regime_unstable = (
            opposite_signs or early.inference.evidence_class != late.inference.evidence_class
        )

    return RegimeSensitivity(
        direction=validated[0].direction,
        horizon_ms=validated[0].horizon_ms,
        early=early,
        mixed=mixed,
        late=late,
        regime_unstable=regime_unstable,
        regime_not_adjudicable=not adjudicable,
    )


def audit_predictions(rows: Sequence[PredictionRow]) -> DirectionalPredictiveAudit:
    validated = validate_prediction_rows(rows)
    inference = infer_predictions(validated)
    influence = assess_prediction_influence(validated)
    regime_sensitivity = assess_regime_sensitivity(validated)
    return DirectionalPredictiveAudit(
        direction=inference.metrics.direction,
        horizon_ms=inference.metrics.horizon_ms,
        inference=inference,
        influence=influence,
        regime_sensitivity=regime_sensitivity,
    )


def _omission_result(
    *,
    unit: str,
    rows: tuple[PredictionRow, ...],
) -> OmissionResult:
    inference = infer_predictions(rows)
    return OmissionResult(
        unit=unit,
        mae_improvement_sign=_prediction_improvement_sign(inference.metrics.mae_improvement_bps),
        evidence_class=inference.evidence_class,
    )


def _regime_subset(
    regime: Regime,
    rows: tuple[PredictionRow, ...],
) -> RegimeSubsetInference:
    if not rows:
        return RegimeSubsetInference(
            regime=regime,
            row_count=0,
            inference=None,
            unavailable_reason="no_rows",
        )
    return RegimeSubsetInference(
        regime=regime,
        row_count=len(rows),
        inference=infer_predictions(rows),
        unavailable_reason=None,
    )
