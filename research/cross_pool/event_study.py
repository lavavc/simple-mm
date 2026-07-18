"""Clustered cross-pool shock detection and as-of response inference."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import Sequence

import numpy as np

from research.cross_pool.bootstrap import (
    _confidence_interval,
    _draw_day_indices,
    _validate_bootstrap_config,
)
from research.cross_pool.contracts import (
    EVENT_BOOTSTRAP_CONFIDENCE_LEVEL,
    EVENT_BOOTSTRAP_RESAMPLES,
    EVENT_BOOTSTRAP_SEED,
    CrossPoolContractError,
    Direction,
    EventExclusion,
    EventExclusionReason,
    EventResponse,
    EventStudyResult,
    EventSummary,
    PoolEvent,
    PoolName,
    ShockConfig,
    ShockEvent,
    utc_day_from_timestamp_ms,
)

_BPS = Decimal("10000")
_DECIMAL_PRECISION = 80


@dataclass(frozen=True)
class _EventDayBlock:
    responses_bps: tuple[float, ...]
    response_sum_bps: float
    agreement_hits: int

    @property
    def event_count(self) -> int:
        return len(self.responses_bps)


def detect_shocks(
    source: Sequence[PoolEvent],
    config: ShockConfig,
) -> tuple[ShockEvent, ...]:
    events = _validate_pool_stream(source, label="source")
    if len(events) < 2:
        return ()
    source_pool = events[0].pool
    direction, target_pool = _direction_for_source(source_pool)
    timestamps = tuple(event.timestamp_ms for event in events)
    threshold = Decimal(str(config.threshold_bps))
    refractory_until_ms: int | None = None
    shocks: list[ShockEvent] = []

    for event in events:
        if refractory_until_ms is not None and event.timestamp_ms < refractory_until_ms:
            continue
        cutoff_ms = event.timestamp_ms - config.lookback_ms
        start_index = bisect_right(timestamps, cutoff_ms) - 1
        if start_index < 0:
            continue
        start = events[start_index]
        with localcontext() as context:
            context.prec = _DECIMAL_PRECISION
            move_bps_decimal = (event.raw_mid / start.raw_mid - Decimal(1)) * _BPS
        if abs(move_bps_decimal) < threshold:
            continue
        move_bps = float(move_bps_decimal)
        if not math.isfinite(move_bps):
            raise CrossPoolContractError("shock source move must be finite")
        shock = ShockEvent(
            direction=direction,
            source_pool=source_pool,
            target_pool=target_pool,
            source_start_timestamp_ms=start.timestamp_ms,
            shock_timestamp_ms=event.timestamp_ms,
            source_move_bps=move_bps,
            source_move_sign=1 if move_bps > 0.0 else -1,
            shock_day_utc=utc_day_from_timestamp_ms(event.timestamp_ms),
        )
        shocks.append(shock)
        refractory_until_ms = event.timestamp_ms + config.cluster_ms
    return tuple(shocks)


def measure_event_responses(
    shocks: Sequence[ShockEvent],
    target: Sequence[PoolEvent],
    config: ShockConfig,
) -> EventStudyResult:
    validated_shocks = _validate_shocks(shocks, config)
    if not validated_shocks:
        return EventStudyResult(responses=(), exclusions=())

    target_events = _validate_pool_stream(target, label="target")
    if not target_events:
        raise CrossPoolContractError(
            "target stream requires at least one event when shocks are present"
        )
    target_pool = target_events[0].pool
    if any(shock.target_pool != target_pool for shock in validated_shocks):
        raise CrossPoolContractError("target stream pool must match every shock")

    timestamps = tuple(event.timestamp_ms for event in target_events)
    first_timestamp_ms = timestamps[0]
    last_timestamp_ms = timestamps[-1]
    responses: list[EventResponse] = []
    exclusions: list[EventExclusion] = []

    for shock in validated_shocks:
        for horizon_ms in config.response_horizons_ms:
            endpoint_ms = shock.shock_timestamp_ms + horizon_ms
            if shock.shock_timestamp_ms < first_timestamp_ms:
                exclusions.append(
                    _exclusion(
                        shock,
                        horizon_ms=horizon_ms,
                        reason="missing_target_start_state",
                    )
                )
                continue
            if endpoint_ms > last_timestamp_ms:
                exclusions.append(
                    _exclusion(
                        shock,
                        horizon_ms=horizon_ms,
                        reason="missing_target_end_state",
                    )
                )
                continue

            start_index = bisect_right(timestamps, shock.shock_timestamp_ms) - 1
            end_index = bisect_right(timestamps, endpoint_ms) - 1
            start = target_events[start_index]
            end = target_events[end_index]
            response_bps = _return_bps(start.raw_mid, end.raw_mid)
            responses.append(
                EventResponse(
                    direction=shock.direction,
                    source_pool=shock.source_pool,
                    target_pool=shock.target_pool,
                    shock_timestamp_ms=shock.shock_timestamp_ms,
                    shock_day_utc=shock.shock_day_utc,
                    horizon_ms=horizon_ms,
                    source_move_bps=shock.source_move_bps,
                    target_start_timestamp_ms=start.timestamp_ms,
                    target_end_timestamp_ms=end.timestamp_ms,
                    target_response_bps=response_bps,
                    direction_agrees=(
                        response_bps > 0.0 and shock.source_move_bps > 0.0
                    )
                    or (response_bps < 0.0 and shock.source_move_bps < 0.0),
                )
            )

    if len(responses) + len(exclusions) != (
        len(validated_shocks) * len(config.response_horizons_ms)
    ):
        raise CrossPoolContractError(
            "event study must produce one outcome per shock and horizon"
        )
    return EventStudyResult(
        responses=tuple(responses),
        exclusions=tuple(exclusions),
    )


def bootstrap_event_responses(
    responses: Sequence[EventResponse],
    *,
    resamples: int = EVENT_BOOTSTRAP_RESAMPLES,
    seed: int = EVENT_BOOTSTRAP_SEED,
    confidence_level: float = EVENT_BOOTSTRAP_CONFIDENCE_LEVEL,
) -> tuple[EventSummary, ...]:
    _validate_event_bootstrap_config(
        resamples=resamples,
        seed=seed,
        confidence_level=confidence_level,
    )
    materialized = tuple(responses)
    if not materialized:
        return ()

    outcome_keys = tuple(
        (row.direction, row.shock_timestamp_ms, row.horizon_ms)
        for row in materialized
    )
    if len(set(outcome_keys)) != len(outcome_keys):
        raise CrossPoolContractError("event response keys must be unique")

    groups: dict[tuple[Direction, int], list[EventResponse]] = {}
    for row in materialized:
        groups.setdefault((row.direction, row.horizon_ms), []).append(row)

    direction_order = {"bsc_to_base": 0, "base_to_bsc": 1}
    ordered_group_keys = sorted(
        groups,
        key=lambda key: (direction_order[key[0]], key[1]),
    )
    summaries: list[EventSummary] = []
    for direction, horizon_ms in ordered_group_keys:
        rows = tuple(
            sorted(
                groups[(direction, horizon_ms)],
                key=lambda row: row.shock_timestamp_ms,
            )
        )
        blocks = _event_day_blocks(rows)
        draws = _draw_day_indices(
            day_count=len(blocks),
            resamples=resamples,
            seed=seed,
        )
        point_responses = tuple(row.target_response_bps for row in rows)
        point_mean = _finite_mean(point_responses)
        point_median = _finite_median(point_responses)
        point_agreement = sum(row.direction_agrees for row in rows) / len(rows)
        sampled_means = np.empty(resamples, dtype=np.float64)
        sampled_medians = np.empty(resamples, dtype=np.float64)
        sampled_agreements = np.empty(resamples, dtype=np.float64)

        for sample_index, sampled_day_indices in enumerate(draws):
            sampled_blocks = tuple(blocks[int(index)] for index in sampled_day_indices)
            sampled_count = sum(block.event_count for block in sampled_blocks)
            sampled_sum = _finite_sum(
                tuple(block.response_sum_bps for block in sampled_blocks),
                message="sampled event response sum must be finite",
            )
            sampled_hits = sum(block.agreement_hits for block in sampled_blocks)
            sampled_values = tuple(
                response_bps
                for block in sampled_blocks
                for response_bps in block.responses_bps
            )
            sampled_mean = sampled_sum / sampled_count
            sampled_median = _finite_median(sampled_values)
            sampled_agreement = sampled_hits / sampled_count
            if not all(
                math.isfinite(value)
                for value in (
                    sampled_sum,
                    sampled_mean,
                    sampled_median,
                    sampled_agreement,
                )
            ):
                raise CrossPoolContractError(
                    "sampled event bootstrap values must be finite"
                )
            sampled_means[sample_index] = sampled_mean
            sampled_medians[sample_index] = sampled_median
            sampled_agreements[sample_index] = sampled_agreement

        summaries.append(
            EventSummary(
                direction=direction,
                horizon_ms=horizon_ms,
                event_count=len(rows),
                event_day_count=len(blocks),
                mean_response_bps=_confidence_interval(
                    point_mean,
                    sampled_means,
                    confidence_level,
                ),
                median_response_bps=_confidence_interval(
                    point_median,
                    sampled_medians,
                    confidence_level,
                ),
                direction_agreement=_confidence_interval(
                    point_agreement,
                    sampled_agreements,
                    confidence_level,
                ),
            )
        )
    return tuple(summaries)


def _validate_event_bootstrap_config(
    *,
    resamples: int,
    seed: int,
    confidence_level: float,
) -> None:
    if isinstance(resamples, bool) or not isinstance(resamples, int):
        raise CrossPoolContractError("resamples must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise CrossPoolContractError("seed must be a nonnegative integer")
    if isinstance(confidence_level, bool):
        raise CrossPoolContractError(
            "confidence_level must be finite and between 0 and 1"
        )
    _validate_bootstrap_config(
        resamples=resamples,
        seed=seed,
        confidence_level=confidence_level,
    )


def _event_day_blocks(
    rows: Sequence[EventResponse],
) -> tuple[_EventDayBlock, ...]:
    by_day: dict[date, list[EventResponse]] = {}
    for row in rows:
        by_day.setdefault(row.shock_day_utc, []).append(row)
    blocks: list[_EventDayBlock] = []
    for shock_day in sorted(by_day):
        day_rows = by_day[shock_day]
        responses_bps = tuple(row.target_response_bps for row in day_rows)
        response_sum_bps = _finite_sum(
            responses_bps,
            message="event day response sum must be finite",
        )
        blocks.append(
            _EventDayBlock(
                responses_bps=responses_bps,
                response_sum_bps=response_sum_bps,
                agreement_hits=sum(row.direction_agrees for row in day_rows),
            )
        )
    if not blocks:
        raise CrossPoolContractError("event bootstrap requires at least one day")
    return tuple(blocks)


def _finite_mean(values: Sequence[float]) -> float:
    if not values:
        raise CrossPoolContractError("event statistic requires at least one value")
    result = _finite_sum(
        values,
        message="event response sum must be finite",
    ) / len(values)
    if not math.isfinite(result):
        raise CrossPoolContractError("event mean must be finite")
    return result


def _finite_sum(values: Sequence[float], *, message: str) -> float:
    try:
        result = math.fsum(values)
    except OverflowError as exc:
        raise CrossPoolContractError(message) from exc
    if not math.isfinite(result):
        raise CrossPoolContractError(message)
    return result


def _finite_median(values: Sequence[float]) -> float:
    if not values:
        raise CrossPoolContractError("event statistic requires at least one value")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        result = ordered[midpoint]
    else:
        result = ordered[midpoint - 1] / 2.0 + ordered[midpoint] / 2.0
    if not math.isfinite(result):
        raise CrossPoolContractError("event median must be finite")
    return result


def _validate_pool_stream(
    events: Sequence[PoolEvent],
    *,
    label: str,
) -> tuple[PoolEvent, ...]:
    materialized = tuple(events)
    if not materialized:
        return ()
    pool = materialized[0].pool
    if pool not in ("uni-base", "uni-bsc"):
        raise CrossPoolContractError(f"{label} stream has an unsupported pool")
    previous_timestamp_ms: int | None = None
    for event in materialized:
        if event.pool != pool:
            raise CrossPoolContractError(f"{label} stream must contain one pool")
        if (
            isinstance(event.timestamp_ms, bool)
            or not isinstance(event.timestamp_ms, int)
            or event.timestamp_ms <= 0
        ):
            raise CrossPoolContractError(
                f"{label} stream timestamps must be positive integers"
            )
        if (
            previous_timestamp_ms is not None
            and event.timestamp_ms <= previous_timestamp_ms
        ):
            raise CrossPoolContractError(
                f"{label} stream timestamps must be strictly increasing"
            )
        if (
            not isinstance(event.raw_mid, Decimal)
            or not event.raw_mid.is_finite()
            or event.raw_mid <= 0
        ):
            raise CrossPoolContractError(
                f"{label} stream raw_mid values must be positive and finite"
            )
        previous_timestamp_ms = event.timestamp_ms
    return materialized


def _validate_shocks(
    shocks: Sequence[ShockEvent],
    config: ShockConfig,
) -> tuple[ShockEvent, ...]:
    materialized = tuple(shocks)
    if not materialized:
        return ()
    first = materialized[0]
    previous_timestamp_ms: int | None = None
    for shock in materialized:
        if (
            shock.direction != first.direction
            or shock.source_pool != first.source_pool
            or shock.target_pool != first.target_pool
        ):
            raise CrossPoolContractError(
                "event response measurement requires one direction"
            )
        if (
            previous_timestamp_ms is not None
            and shock.shock_timestamp_ms <= previous_timestamp_ms
        ):
            raise CrossPoolContractError(
                "shock timestamps must be strictly increasing"
            )
        if (
            previous_timestamp_ms is not None
            and shock.shock_timestamp_ms < previous_timestamp_ms + config.cluster_ms
        ):
            raise CrossPoolContractError(
                "shock timestamps must respect the refractory interval"
            )
        if shock.source_start_timestamp_ms > (
            shock.shock_timestamp_ms - config.lookback_ms
        ):
            raise CrossPoolContractError(
                "shock source start must be as of the configured lookback"
            )
        if abs(shock.source_move_bps) < config.threshold_bps:
            raise CrossPoolContractError(
                "shock source move must reach the configured threshold"
            )
        previous_timestamp_ms = shock.shock_timestamp_ms
    return materialized


def _exclusion(
    shock: ShockEvent,
    *,
    horizon_ms: int,
    reason: EventExclusionReason,
) -> EventExclusion:
    if reason not in (
        "missing_target_start_state",
        "missing_target_end_state",
    ):
        raise CrossPoolContractError("unsupported event exclusion reason")
    return EventExclusion(
        direction=shock.direction,
        shock_timestamp_ms=shock.shock_timestamp_ms,
        shock_day_utc=shock.shock_day_utc,
        horizon_ms=horizon_ms,
        reason=reason,
    )


def _return_bps(start_mid: Decimal, end_mid: Decimal) -> float:
    with localcontext() as context:
        context.prec = _DECIMAL_PRECISION
        value = (end_mid / start_mid - Decimal(1)) * _BPS
    result = float(value)
    if not math.isfinite(result):
        raise CrossPoolContractError("event target response must be finite")
    return result


def _direction_for_source(source_pool: PoolName) -> tuple[Direction, PoolName]:
    if source_pool == "uni-bsc":
        return "bsc_to_base", "uni-base"
    return "base_to_bsc", "uni-bsc"
