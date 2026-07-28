"""Metrics and dependence-aware inference for challenger forecasts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from math import ceil, isfinite, sqrt
from typing import TypedDict

import numpy as np
from numpy.typing import NDArray

from research.cross_pool.challenger_contracts import (
    FEATURE_VARIANTS,
    FRESHNESS_SUPPORTS,
    MODEL_FAMILIES,
    ChallengerContrast,
    ChallengerInference,
    ChallengerMetric,
    ChallengerPrediction,
    ChallengerStudy,
    ContrastKind,
    FeatureVariant,
    FreshnessSupport,
    MetricEndpoint,
    ModelFamily,
    ReliabilityGroup,
)
from research.cross_pool.contracts import CrossPoolContractError, Direction

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20_260_715
BOOTSTRAP_BLOCK_DAYS = 7
MINIMUM_TARGET_DAYS = 42
MINIMUM_COMPLETE_BLOCKS = 6
MINIMUM_UPDATE_DAYS = 20
DIRECTIONAL_MOVE_THRESHOLD_BPS = 10.0
_DIRECTIONS: tuple[Direction, ...] = ("bsc_to_base", "base_to_bsc")
_HORIZONS_MS = (900_000, 3_600_000, 14_400_000)
_CONTINUOUS_FAMILIES: tuple[ModelFamily, ...] = ("ols", "arx2", "gam", "huber")
_CONTRASTS: tuple[
    tuple[ContrastKind, FeatureVariant, FeatureVariant], ...
] = (
    ("source_age", "target_only", "source_age"),
    ("source_price", "source_age", "full_source"),
    ("total_source", "target_only", "full_source"),
)


@dataclass(frozen=True)
class _ContrastWork:
    row_index: int
    bootstrap_values: NDArray[np.float64]
    standard_error: float


class _ContrastIdentity(TypedDict):
    family: ModelFamily
    direction: Direction
    horizon_ms: int
    support: FreshnessSupport
    endpoint: MetricEndpoint
    contrast: ContrastKind
    comparator_variant: FeatureVariant
    candidate_variant: FeatureVariant


def infer_challengers(study: ChallengerStudy) -> ChallengerInference:
    _validate_study_calendar(study)
    grouped = _prediction_groups(study)
    failure_reasons = _validate_registered_grid(study, grouped)
    metrics: list[ChallengerMetric] = []
    for family in MODEL_FAMILIES:
        for variant in FEATURE_VARIANTS:
            for direction in _DIRECTIONS:
                for horizon_ms in _HORIZONS_MS:
                    key = (family, variant, direction, horizon_ms)
                    for support in FRESHNESS_SUPPORTS:
                        rows = grouped.get(key)
                        if rows is None:
                            metrics.append(
                                _failed_metric(
                                    family=family,
                                    variant=variant,
                                    direction=direction,
                                    horizon_ms=horizon_ms,
                                    support=support,
                                    reason=failure_reasons.get(
                                        key,
                                        "registered model combination is missing",
                                    ),
                                )
                            )
                        else:
                            metrics.append(_metric_row(rows, support=support))

    draws = _circular_block_draws(study.target_days_utc)
    contrasts: list[ChallengerContrast] = []
    source_price_work: list[_ContrastWork] = []
    for family in MODEL_FAMILIES:
        endpoints: tuple[MetricEndpoint, ...] = (
            ("mae",)
            if family in _CONTINUOUS_FAMILIES
            else ("mae", "brier", "conditional_update_mae")
        )
        for endpoint in endpoints:
            for contrast, comparator_variant, candidate_variant in _CONTRASTS:
                for direction in _DIRECTIONS:
                    for horizon_ms in _HORIZONS_MS:
                        for support in FRESHNESS_SUPPORTS:
                            comparator_key = (
                                family,
                                comparator_variant,
                                direction,
                                horizon_ms,
                            )
                            candidate_key = (
                                family,
                                candidate_variant,
                                direction,
                                horizon_ms,
                            )
                            row, bootstrap_values, standard_error = _contrast_row(
                                family=family,
                                direction=direction,
                                horizon_ms=horizon_ms,
                                support=support,
                                endpoint=endpoint,
                                contrast=contrast,
                                comparator_variant=comparator_variant,
                                candidate_variant=candidate_variant,
                                comparator_rows=grouped.get(comparator_key),
                                candidate_rows=grouped.get(candidate_key),
                                failure_reason=(
                                    failure_reasons.get(comparator_key)
                                    or failure_reasons.get(candidate_key)
                                ),
                                target_days=study.target_days_utc,
                                draws=draws,
                            )
                            row_index = len(contrasts)
                            contrasts.append(row)
                            if (
                                contrast == "source_price"
                                and row.status == "adjudicable"
                            ):
                                assert bootstrap_values is not None
                                assert standard_error is not None
                                source_price_work.append(
                                    _ContrastWork(
                                        row_index=row_index,
                                        bootstrap_values=bootstrap_values,
                                        standard_error=standard_error,
                                    )
                                )

    registered_source_price = sum(
        row.contrast == "source_price" for row in contrasts
    )
    if registered_source_price != 126:
        raise CrossPoolContractError(
            f"source-price family has {registered_source_price} cells instead of 126"
        )
    critical_value = _apply_max_t(contrasts, source_price_work)
    reliability = _reliability_groups(grouped)
    return ChallengerInference(
        metrics=tuple(metrics),
        contrasts=tuple(contrasts),
        reliability=reliability,
        bootstrap_draws=BOOTSTRAP_RESAMPLES,
        bootstrap_seed=BOOTSTRAP_SEED,
        block_days=BOOTSTRAP_BLOCK_DAYS,
        simultaneous_critical_value=critical_value,
    )


def _metric_row(
    rows: tuple[ChallengerPrediction, ...],
    *,
    support: FreshnessSupport,
) -> ChallengerMetric:
    if not rows:
        raise CrossPoolContractError("metric input must not be empty")
    selected = _support_rows(rows, support)
    first = rows[0]
    if not selected:
        return ChallengerMetric(
            family=first.family,
            variant=first.variant,
            direction=first.direction,
            horizon_ms=first.horizon_ms,
            support=support,
            status="no_rows",
            reason="freshness support contains no rows",
            rows=0,
            target_days=0,
            update_rows=0,
            update_days=0,
            update_incidence=None,
            mae_bps=None,
            mse_bps2=None,
            rmse_bps=None,
            directional_rows=0,
            directional_accuracy=None,
            brier_loss=None,
            log_loss=None,
            calibration_error=None,
            conditional_update_mae_bps=None,
        )
    actual = np.asarray([row.actual_bps for row in selected], dtype=np.float64)
    predictions = np.asarray([row.prediction_bps for row in selected], dtype=np.float64)
    errors = actual - predictions
    updates = np.asarray([row.updated for row in selected], dtype=bool)
    target_days = {_target_day(row) for row in selected}
    update_days = {_target_day(row) for row in selected if row.updated}
    directional = np.abs(actual) >= DIRECTIONAL_MOVE_THRESHOLD_BPS
    directional_rows = int(np.count_nonzero(directional))
    directional_accuracy = None
    if directional_rows:
        directional_accuracy = float(
            np.mean(
                ((actual[directional] > 0.0) & (predictions[directional] > 0.0))
                | ((actual[directional] < 0.0) & (predictions[directional] < 0.0))
            )
        )

    brier = None
    log_loss = None
    calibration = None
    conditional_mae = None
    if first.family == "two_part":
        if any(
            row.update_probability is None or row.conditional_prediction_bps is None
            for row in selected
        ):
            raise CrossPoolContractError("two-part metric rows lack component predictions")
        probabilities = np.asarray(
            [row.update_probability for row in selected],
            dtype=np.float64,
        )
        update_values = updates.astype(np.float64)
        brier = float(np.mean((update_values - probabilities) ** 2))
        epsilon = np.finfo(np.float64).eps
        clipped = np.clip(probabilities, epsilon, 1.0 - epsilon)
        log_loss = float(
            -np.mean(
                update_values * np.log(clipped)
                + (1.0 - update_values) * np.log1p(-clipped)
            )
        )
        calibration = float(np.mean(probabilities) - np.mean(update_values))
        if np.any(updates):
            conditional_predictions = np.asarray(
                [row.conditional_prediction_bps for row in selected],
                dtype=np.float64,
            )
            conditional_mae = float(
                np.mean(np.abs(actual[updates] - conditional_predictions[updates]))
            )

    mse = float(np.mean(errors**2))
    return ChallengerMetric(
        family=first.family,
        variant=first.variant,
        direction=first.direction,
        horizon_ms=first.horizon_ms,
        support=support,
        status="available",
        reason=None,
        rows=len(selected),
        target_days=len(target_days),
        update_rows=int(np.count_nonzero(updates)),
        update_days=len(update_days),
        update_incidence=float(np.mean(updates)),
        mae_bps=float(np.mean(np.abs(errors))),
        mse_bps2=mse,
        rmse_bps=sqrt(mse),
        directional_rows=directional_rows,
        directional_accuracy=directional_accuracy,
        brier_loss=brier,
        log_loss=log_loss,
        calibration_error=calibration,
        conditional_update_mae_bps=conditional_mae,
    )


def _failed_metric(
    *,
    family: ModelFamily,
    variant: FeatureVariant,
    direction: Direction,
    horizon_ms: int,
    support: FreshnessSupport,
    reason: str,
) -> ChallengerMetric:
    return ChallengerMetric(
        family=family,
        variant=variant,
        direction=direction,
        horizon_ms=horizon_ms,
        support=support,
        status="model_fit_failed",
        reason=reason,
        rows=0,
        target_days=0,
        update_rows=0,
        update_days=0,
        update_incidence=None,
        mae_bps=None,
        mse_bps2=None,
        rmse_bps=None,
        directional_rows=0,
        directional_accuracy=None,
        brier_loss=None,
        log_loss=None,
        calibration_error=None,
        conditional_update_mae_bps=None,
    )


def _contrast_row(
    *,
    family: ModelFamily,
    direction: Direction,
    horizon_ms: int,
    support: FreshnessSupport,
    endpoint: MetricEndpoint,
    contrast: ContrastKind,
    comparator_variant: FeatureVariant,
    candidate_variant: FeatureVariant,
    comparator_rows: tuple[ChallengerPrediction, ...] | None,
    candidate_rows: tuple[ChallengerPrediction, ...] | None,
    failure_reason: str | None,
    target_days: tuple[date, ...],
    draws: NDArray[np.int64],
) -> tuple[ChallengerContrast, NDArray[np.float64] | None, float | None]:
    base: _ContrastIdentity = {
        "family": family,
        "direction": direction,
        "horizon_ms": horizon_ms,
        "support": support,
        "endpoint": endpoint,
        "contrast": contrast,
        "comparator_variant": comparator_variant,
        "candidate_variant": candidate_variant,
    }
    if comparator_rows is None or candidate_rows is None:
        return (
            ChallengerContrast(
                **base,
                status="not_adjudicable",
                reason=failure_reason or "matched model variant is unavailable",
                rows=0,
                target_days=0,
                complete_blocks=0,
                update_days=0,
                point=None,
                raw_lower=None,
                raw_upper=None,
                bootstrap_standard_error=None,
                simultaneous_lower=None,
                simultaneous_upper=None,
                adjusted_p_value=None,
            ),
            None,
            None,
        )
    comparator = _support_rows(comparator_rows, support)
    candidate = _support_rows(candidate_rows, support)
    _validate_matched_rows(comparator, candidate)
    if not comparator:
        return (
            ChallengerContrast(
                **base,
                status="not_adjudicable",
                reason="freshness support contains no matched rows",
                rows=0,
                target_days=0,
                complete_blocks=0,
                update_days=0,
                point=None,
                raw_lower=None,
                raw_upper=None,
                bootstrap_standard_error=None,
                simultaneous_lower=None,
                simultaneous_upper=None,
                adjusted_p_value=None,
            ),
            None,
            None,
        )

    day_index = {value: index for index, value in enumerate(target_days)}
    support_counts = np.zeros(len(day_index), dtype=np.int64)
    for row in comparator:
        support_counts[day_index[_target_day(row)]] += 1
    complete_blocks = _complete_seven_day_blocks(support_counts)
    comparator_sums, comparator_counts = _daily_loss(
        comparator,
        endpoint=endpoint,
        day_index=day_index,
    )
    candidate_sums, candidate_counts = _daily_loss(
        candidate,
        endpoint=endpoint,
        day_index=day_index,
    )
    if not np.array_equal(comparator_counts, candidate_counts):
        raise CrossPoolContractError("matched variants have different loss support")
    total_count = int(np.sum(comparator_counts))
    supported_days = len({_target_day(row) for row in comparator})
    update_days = len({_target_day(row) for row in comparator if row.updated})
    if total_count == 0:
        return (
            ChallengerContrast(
                **base,
                status="not_adjudicable",
                reason="endpoint contains no loss observations",
                rows=0,
                target_days=supported_days,
                complete_blocks=complete_blocks,
                update_days=update_days,
                point=None,
                raw_lower=None,
                raw_upper=None,
                bootstrap_standard_error=None,
                simultaneous_lower=None,
                simultaneous_upper=None,
                adjusted_p_value=None,
            ),
            None,
            None,
        )
    point = float(
        np.sum(comparator_sums) / total_count - np.sum(candidate_sums) / total_count
    )
    reason = None
    if supported_days < MINIMUM_TARGET_DAYS:
        reason = f"requires {MINIMUM_TARGET_DAYS} OOS target days"
    elif complete_blocks < MINIMUM_COMPLETE_BLOCKS:
        reason = f"requires {MINIMUM_COMPLETE_BLOCKS} complete seven-day blocks"
    elif endpoint == "conditional_update_mae" and update_days < MINIMUM_UPDATE_DAYS:
        reason = f"requires {MINIMUM_UPDATE_DAYS} OOS update-days"
    if reason is not None:
        return (
            ChallengerContrast(
                **base,
                status="not_adjudicable",
                reason=reason,
                rows=total_count,
                target_days=supported_days,
                complete_blocks=complete_blocks,
                update_days=update_days,
                point=point,
                raw_lower=None,
                raw_upper=None,
                bootstrap_standard_error=None,
                simultaneous_lower=None,
                simultaneous_upper=None,
                adjusted_p_value=None,
            ),
            None,
            None,
        )

    sampled_counts = comparator_counts[draws].sum(axis=1, dtype=np.int64)
    if np.any(sampled_counts <= 0):
        return (
            ChallengerContrast(
                **base,
                status="not_adjudicable",
                reason="at least one complete block draw has zero endpoint rows",
                rows=total_count,
                target_days=supported_days,
                complete_blocks=complete_blocks,
                update_days=update_days,
                point=point,
                raw_lower=None,
                raw_upper=None,
                bootstrap_standard_error=None,
                simultaneous_lower=None,
                simultaneous_upper=None,
                adjusted_p_value=None,
            ),
            None,
            None,
        )
    comparator_draw_loss = comparator_sums[draws].sum(axis=1) / sampled_counts
    candidate_draw_loss = candidate_sums[draws].sum(axis=1) / sampled_counts
    bootstrap_values = comparator_draw_loss - candidate_draw_loss
    if not np.all(np.isfinite(bootstrap_values)):
        raise CrossPoolContractError("bootstrap contrast contains non-finite values")
    standard_error = float(np.std(bootstrap_values, ddof=1))
    if not isfinite(standard_error) or standard_error == 0.0:
        return (
            ChallengerContrast(
                **base,
                status="not_adjudicable",
                reason="bootstrap contrast has zero variance",
                rows=total_count,
                target_days=supported_days,
                complete_blocks=complete_blocks,
                update_days=update_days,
                point=point,
                raw_lower=None,
                raw_upper=None,
                bootstrap_standard_error=None,
                simultaneous_lower=None,
                simultaneous_upper=None,
                adjusted_p_value=None,
            ),
            None,
            None,
        )
    ordered = np.sort(bootstrap_values)
    lower = float(ordered[ceil(0.025 * BOOTSTRAP_RESAMPLES) - 1])
    upper = float(ordered[ceil(0.975 * BOOTSTRAP_RESAMPLES) - 1])
    return (
        ChallengerContrast(
            **base,
            status="adjudicable",
            reason=None,
            rows=total_count,
            target_days=supported_days,
            complete_blocks=complete_blocks,
            update_days=update_days,
            point=point,
            raw_lower=lower,
            raw_upper=upper,
            bootstrap_standard_error=standard_error,
            simultaneous_lower=None,
            simultaneous_upper=None,
            adjusted_p_value=None,
        ),
        bootstrap_values,
        standard_error,
    )


def _apply_max_t(
    contrasts: list[ChallengerContrast],
    work: list[_ContrastWork],
) -> float | None:
    if not work:
        return None
    centered_columns: list[NDArray[np.float64]] = []
    for item in work:
        point = contrasts[item.row_index].point
        assert point is not None
        centered_columns.append(
            (item.bootstrap_values - point) / item.standard_error
        )
    centered = np.column_stack(centered_columns)
    maxima = np.max(np.abs(centered), axis=1)
    critical = float(
        np.sort(maxima)[ceil(0.95 * BOOTSTRAP_RESAMPLES) - 1]
    )
    for column, item in enumerate(work):
        row = contrasts[item.row_index]
        assert row.point is not None
        observed = abs(row.point / item.standard_error)
        adjusted_p = float(
            (1 + np.count_nonzero(maxima >= observed)) / (BOOTSTRAP_RESAMPLES + 1)
        )
        contrasts[item.row_index] = replace(
            row,
            simultaneous_lower=row.point - critical * item.standard_error,
            simultaneous_upper=row.point + critical * item.standard_error,
            adjusted_p_value=adjusted_p,
        )
    return critical


def _daily_loss(
    rows: tuple[ChallengerPrediction, ...],
    *,
    endpoint: MetricEndpoint,
    day_index: dict[date, int],
) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    sums = np.zeros(len(day_index), dtype=np.float64)
    counts = np.zeros(len(day_index), dtype=np.int64)
    for row in rows:
        if endpoint == "mae":
            loss = abs(row.actual_bps - row.prediction_bps)
        elif endpoint == "brier":
            if row.update_probability is None:
                raise CrossPoolContractError("Brier endpoint requires update probabilities")
            loss = (float(row.updated) - row.update_probability) ** 2
        elif endpoint == "conditional_update_mae":
            if not row.updated:
                continue
            if row.conditional_prediction_bps is None:
                raise CrossPoolContractError(
                    "conditional endpoint requires conditional predictions"
                )
            loss = abs(row.actual_bps - row.conditional_prediction_bps)
        else:  # pragma: no cover - registered endpoints are exhaustive.
            raise AssertionError(endpoint)
        index = day_index[_target_day(row)]
        sums[index] += loss
        counts[index] += 1
    return sums, counts


def _complete_seven_day_blocks(counts: NDArray[np.int64]) -> int:
    complete = 0
    run_length = 0
    for count in counts:
        if count > 0:
            run_length += 1
        else:
            complete += run_length // BOOTSTRAP_BLOCK_DAYS
            run_length = 0
    return complete + run_length // BOOTSTRAP_BLOCK_DAYS


def _reliability_groups(
    grouped: dict[
        tuple[ModelFamily, FeatureVariant, Direction, int],
        tuple[ChallengerPrediction, ...],
    ],
) -> tuple[ReliabilityGroup, ...]:
    result: list[ReliabilityGroup] = []
    for variant in FEATURE_VARIANTS:
        for direction in _DIRECTIONS:
            for horizon_ms in _HORIZONS_MS:
                rows = grouped.get(("two_part", variant, direction, horizon_ms))
                if rows is None:
                    continue
                for support in FRESHNESS_SUPPORTS:
                    selected = _support_rows(rows, support)
                    if len(selected) < 25:
                        continue
                    ordered = tuple(
                        sorted(
                            selected,
                            key=lambda row: (
                                row.update_probability,
                                row.timestamp_ms,
                            ),
                        )
                    )
                    partitions = np.array_split(np.arange(len(ordered)), 5)
                    for group_index, partition in enumerate(partitions):
                        group = tuple(ordered[int(index)] for index in partition)
                        probabilities = [
                            row.update_probability for row in group
                        ]
                        if any(value is None for value in probabilities):
                            raise CrossPoolContractError(
                                "reliability row lacks update probability"
                            )
                        result.append(
                            ReliabilityGroup(
                                family="two_part",
                                variant=variant,
                                direction=direction,
                                horizon_ms=horizon_ms,
                                support=support,
                                group_index=group_index,
                                rows=len(group),
                                mean_probability=float(
                                    np.mean(np.asarray(probabilities, dtype=np.float64))
                                ),
                                observed_incidence=float(
                                    np.mean([row.updated for row in group])
                                ),
                            )
                        )
    return tuple(sorted(result, key=lambda row: row.sort_key))


def _prediction_groups(
    study: ChallengerStudy,
) -> dict[
    tuple[ModelFamily, FeatureVariant, Direction, int],
    tuple[ChallengerPrediction, ...],
]:
    mutable: dict[
        tuple[ModelFamily, FeatureVariant, Direction, int],
        list[ChallengerPrediction],
    ] = {}
    for row in study.predictions:
        mutable.setdefault(
            (row.family, row.variant, row.direction, row.horizon_ms),
            [],
        ).append(row)
    result = {
        key: tuple(sorted(rows, key=lambda row: row.timestamp_ms))
        for key, rows in mutable.items()
    }
    for rows in result.values():
        if len({row.timestamp_ms for row in rows}) != len(rows):
            raise CrossPoolContractError("challenger group has duplicate timestamps")
    return result


def _validate_registered_grid(
    study: ChallengerStudy,
    grouped: dict[
        tuple[ModelFamily, FeatureVariant, Direction, int],
        tuple[ChallengerPrediction, ...],
    ],
) -> dict[tuple[ModelFamily, FeatureVariant, Direction, int], str]:
    expected = {
        (family, variant, direction, horizon_ms)
        for family in MODEL_FAMILIES
        for variant in FEATURE_VARIANTS
        for direction in _DIRECTIONS
        for horizon_ms in _HORIZONS_MS
    }
    failure_keys = [
        (row.family, row.variant, row.direction, row.horizon_ms)
        for row in study.failures
    ]
    if len(set(failure_keys)) != len(failure_keys):
        raise CrossPoolContractError("registered challenger grid has duplicate failures")
    failures = {
        key: row.reason for key, row in zip(failure_keys, study.failures, strict=True)
    }
    present = set(grouped)
    if present & set(failures) or present | set(failures) != expected:
        raise CrossPoolContractError(
            "registered challenger grid must contain predictions or one explicit failure"
        )
    if any(not reason for reason in failures.values()):
        raise CrossPoolContractError("registered challenger failure reason is empty")

    for direction in _DIRECTIONS:
        for horizon_ms in _HORIZONS_MS:
            reference = grouped.get(("ols", "target_only", direction, horizon_ms))
            if reference is None:
                raise CrossPoolContractError(
                    "registered challenger grid lacks its sealed OLS reference"
                )
            reference_identity = _grid_identity(reference)
            if {_target_day(row) for row in reference} != set(study.target_days_utc):
                raise CrossPoolContractError(
                    "registered challenger reference does not cover the target-day calendar"
                )
            for family in MODEL_FAMILIES:
                for variant in FEATURE_VARIANTS:
                    rows = grouped.get((family, variant, direction, horizon_ms))
                    if rows is not None and _grid_identity(rows) != reference_identity:
                        raise CrossPoolContractError(
                            "registered challenger prediction grids are not matched"
                        )
    return failures


def _grid_identity(
    rows: tuple[ChallengerPrediction, ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            row.timestamp_ms,
            row.target_timestamp_ms,
            row.actual_bps,
            row.updated,
            row.target_age_ms,
            row.source_age_ms,
            row.target_regime,
            row.source_regime,
        )
        for row in rows
    )


def _support_rows(
    rows: tuple[ChallengerPrediction, ...],
    support: FreshnessSupport,
) -> tuple[ChallengerPrediction, ...]:
    if support == "all":
        return rows
    threshold = 14_400_000 if support == "both_age_le_4h" else 3_600_000
    return tuple(
        row
        for row in rows
        if row.target_age_ms <= threshold and row.source_age_ms <= threshold
    )


def _validate_matched_rows(
    comparator: tuple[ChallengerPrediction, ...],
    candidate: tuple[ChallengerPrediction, ...],
) -> None:
    comparator_identity = tuple(
        (
            row.timestamp_ms,
            row.target_timestamp_ms,
            row.actual_bps,
            row.updated,
            row.target_age_ms,
            row.source_age_ms,
        )
        for row in comparator
    )
    candidate_identity = tuple(
        (
            row.timestamp_ms,
            row.target_timestamp_ms,
            row.actual_bps,
            row.updated,
            row.target_age_ms,
            row.source_age_ms,
        )
        for row in candidate
    )
    if comparator_identity != candidate_identity:
        raise CrossPoolContractError("challenger variants do not use matched OOS rows")


def _circular_block_draws(
    target_days: tuple[date, ...],
) -> NDArray[np.int64]:
    if len(target_days) != 88 or any(
        current - previous != timedelta(days=1)
        for previous, current in zip(target_days, target_days[1:], strict=False)
    ):
        raise CrossPoolContractError("bootstrap requires the frozen contiguous 88-day calendar")
    generator = np.random.Generator(np.random.PCG64(BOOTSTRAP_SEED))
    blocks_per_draw = ceil(len(target_days) / BOOTSTRAP_BLOCK_DAYS)
    starts = generator.integers(
        0,
        len(target_days),
        size=(BOOTSTRAP_RESAMPLES, blocks_per_draw),
        dtype=np.int64,
    )
    offsets = np.arange(BOOTSTRAP_BLOCK_DAYS, dtype=np.int64)
    blocks = (starts[:, :, None] + offsets[None, None, :]) % len(target_days)
    return blocks.reshape(BOOTSTRAP_RESAMPLES, -1)[:, : len(target_days)]


def _target_day(row: ChallengerPrediction) -> date:
    return datetime.fromtimestamp(row.target_timestamp_ms / 1_000, tz=UTC).date()


def _validate_study_calendar(study: ChallengerStudy) -> None:
    _circular_block_draws(study.target_days_utc)
