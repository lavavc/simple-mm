"""Parent-bound short-horizon cross-pool response measurement."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal, Sequence

from research.cross_pool.contracts import (
    CrossPoolContractError,
    Direction,
    PoolEvent,
    PoolName,
    ShockConfig,
    utc_day_from_timestamp_ms,
)
from research.cross_pool.event_study import detect_shocks, measure_event_responses

SHORT_HORIZONS_MS = (
    30_000,
    60_000,
    120_000,
    180_000,
    300_000,
    600_000,
    900_000,
)

FirstUpdateStatus = Literal["observed", "right_censored"]
_BPS = Decimal("10000")
_DECIMAL_PRECISION = 80


@dataclass(frozen=True)
class ShortHorizonResponse:
    direction: Direction
    source_pool: PoolName
    target_pool: PoolName
    shock_timestamp_ms: int
    shock_day_utc: date
    horizon_ms: int
    source_move_bps: float
    source_move_sign: Literal[-1, 1]
    source_shock_raw_mid: Decimal
    source_shock_bid: Decimal
    source_shock_ask: Decimal
    source_end_timestamp_ms: int
    source_end_age_ms: int
    source_end_raw_mid: Decimal
    source_end_bid: Decimal
    source_end_ask: Decimal
    target_start_timestamp_ms: int
    target_start_age_ms: int
    target_start_raw_mid: Decimal
    target_start_bid: Decimal
    target_start_ask: Decimal
    target_end_timestamp_ms: int
    target_end_age_ms: int
    target_end_raw_mid: Decimal
    target_end_bid: Decimal
    target_end_ask: Decimal
    target_response_bps: float
    zero_response: bool
    direction_agrees: bool
    target_same_timestamp: bool
    first_update_status: FirstUpdateStatus
    first_update_timestamp_ms: int | None
    first_update_delay_ms: int | None
    first_update_raw_mid: Decimal | None
    first_update_response_bps: float | None
    first_update_direction_agrees: bool | None
    fee_gap_start_bps: float
    fee_gap_end_bps: float
    fee_gap_start_positive_bps: float
    fee_gap_end_positive_bps: float
    fee_gap_closure_bps: float

    def __post_init__(self) -> None:
        expected_pools = {
            "bsc_to_base": ("uni-bsc", "uni-base"),
            "base_to_bsc": ("uni-base", "uni-bsc"),
        }
        if expected_pools.get(self.direction) != (self.source_pool, self.target_pool):
            raise CrossPoolContractError("short-horizon direction and pools must agree")
        _require_positive_int(self.shock_timestamp_ms, "shock timestamp")
        if self.shock_day_utc != utc_day_from_timestamp_ms(self.shock_timestamp_ms):
            raise CrossPoolContractError("shock UTC day must match its timestamp")
        _require_positive_int(self.horizon_ms, "horizon")
        if self.horizon_ms not in SHORT_HORIZONS_MS:
            raise CrossPoolContractError("short-horizon response uses an unsupported horizon")
        _require_finite(self.source_move_bps, "source move")
        expected_sign = 1 if self.source_move_bps > 0.0 else -1
        if (
            self.source_move_bps == 0.0
            or isinstance(self.source_move_sign, bool)
            or not isinstance(self.source_move_sign, int)
            or self.source_move_sign != expected_sign
        ):
            raise CrossPoolContractError("source move sign must match the nonzero move")

        endpoint_ms = self.shock_timestamp_ms + self.horizon_ms
        _require_positive_int(self.source_end_timestamp_ms, "source end timestamp")
        _require_nonnegative_int(self.source_end_age_ms, "source end age")
        _require_positive_int(self.target_start_timestamp_ms, "target start timestamp")
        _require_nonnegative_int(self.target_start_age_ms, "target start age")
        _require_positive_int(self.target_end_timestamp_ms, "target end timestamp")
        _require_nonnegative_int(self.target_end_age_ms, "target end age")
        if not self.shock_timestamp_ms <= self.source_end_timestamp_ms <= endpoint_ms:
            raise CrossPoolContractError("source end timestamp must be as of the endpoint")
        if self.source_end_age_ms != endpoint_ms - self.source_end_timestamp_ms:
            raise CrossPoolContractError("source end age must match its timestamp")
        if self.target_start_timestamp_ms > self.shock_timestamp_ms:
            raise CrossPoolContractError("target start timestamp must be as of the shock")
        if not (self.target_start_timestamp_ms <= self.target_end_timestamp_ms <= endpoint_ms):
            raise CrossPoolContractError("target end timestamp must be as of the endpoint")
        if self.target_start_age_ms != self.shock_timestamp_ms - self.target_start_timestamp_ms:
            raise CrossPoolContractError("target start age must match its timestamp")
        if self.target_end_age_ms != endpoint_ms - self.target_end_timestamp_ms:
            raise CrossPoolContractError("target end age must match its timestamp")

        _validate_fee_band(
            self.source_shock_raw_mid,
            self.source_shock_bid,
            self.source_shock_ask,
            "source shock",
        )
        _validate_fee_band(
            self.source_end_raw_mid,
            self.source_end_bid,
            self.source_end_ask,
            "source end",
        )
        _validate_fee_band(
            self.target_start_raw_mid,
            self.target_start_bid,
            self.target_start_ask,
            "target start",
        )
        _validate_fee_band(
            self.target_end_raw_mid,
            self.target_end_bid,
            self.target_end_ask,
            "target end",
        )
        expected_response = _return_bps(
            self.target_start_raw_mid,
            self.target_end_raw_mid,
        )
        if self.target_response_bps != expected_response:
            raise CrossPoolContractError("target response must match the bound prices")
        if not isinstance(self.zero_response, bool):
            raise CrossPoolContractError("zero response flag must be a boolean")
        if self.zero_response != (self.target_response_bps == 0.0):
            raise CrossPoolContractError("zero response flag must match the response")
        expected_agreement = _direction_agrees(
            self.source_move_bps,
            self.target_response_bps,
        )
        if not isinstance(self.direction_agrees, bool):
            raise CrossPoolContractError("direction agreement must be a boolean")
        if self.direction_agrees != expected_agreement:
            raise CrossPoolContractError("direction agreement must match response signs")
        if not isinstance(self.target_same_timestamp, bool):
            raise CrossPoolContractError("same-timestamp flag must be a boolean")
        if self.target_same_timestamp != (
            self.target_start_timestamp_ms == self.shock_timestamp_ms
        ):
            raise CrossPoolContractError("same-timestamp flag must match the target start")

        update_fields = (
            self.first_update_timestamp_ms,
            self.first_update_delay_ms,
            self.first_update_raw_mid,
            self.first_update_response_bps,
            self.first_update_direction_agrees,
        )
        if self.first_update_status == "right_censored":
            if any(value is not None for value in update_fields):
                raise CrossPoolContractError(
                    "right-censored rows cannot contain first-update values"
                )
        elif self.first_update_status == "observed":
            if any(value is None for value in update_fields):
                raise CrossPoolContractError("observed rows require every first-update value")
            assert self.first_update_timestamp_ms is not None
            assert self.first_update_delay_ms is not None
            assert self.first_update_raw_mid is not None
            assert self.first_update_response_bps is not None
            assert self.first_update_direction_agrees is not None
            _require_positive_int(
                self.first_update_timestamp_ms,
                "first-update timestamp",
            )
            _require_positive_int(self.first_update_delay_ms, "first-update delay")
            if not isinstance(self.first_update_direction_agrees, bool):
                raise CrossPoolContractError("first-update agreement must be a boolean")
            if not (self.shock_timestamp_ms < self.first_update_timestamp_ms <= endpoint_ms):
                raise CrossPoolContractError(
                    "first update must be strictly post-shock and within the horizon"
                )
            if (
                self.first_update_delay_ms
                != self.first_update_timestamp_ms - self.shock_timestamp_ms
            ):
                raise CrossPoolContractError("first-update delay must match timestamps")
            if (
                not isinstance(self.first_update_raw_mid, Decimal)
                or not self.first_update_raw_mid.is_finite()
                or self.first_update_raw_mid <= 0
            ):
                raise CrossPoolContractError("first-update raw mid must be positive and finite")
            if self.first_update_response_bps != _return_bps(
                self.target_start_raw_mid,
                self.first_update_raw_mid,
            ):
                raise CrossPoolContractError("first-update response must match the bound prices")
            if self.first_update_direction_agrees != _direction_agrees(
                self.source_move_bps,
                self.first_update_response_bps,
            ):
                raise CrossPoolContractError("first-update agreement must match response signs")
        else:
            raise CrossPoolContractError("unsupported first-update status")

        expected_start_gap = _fee_gap_from_quotes(
            self.source_move_sign,
            source_bid=self.source_shock_bid,
            source_ask=self.source_shock_ask,
            target_bid=self.target_start_bid,
            target_ask=self.target_start_ask,
        )
        expected_end_gap = _fee_gap_from_quotes(
            self.source_move_sign,
            source_bid=self.source_end_bid,
            source_ask=self.source_end_ask,
            target_bid=self.target_end_bid,
            target_ask=self.target_end_ask,
        )
        if self.fee_gap_start_bps != expected_start_gap:
            raise CrossPoolContractError("fee gap start must match the bound quotes")
        if self.fee_gap_end_bps != expected_end_gap:
            raise CrossPoolContractError("fee gap end must match the bound quotes")
        if self.fee_gap_start_positive_bps != max(expected_start_gap, 0.0):
            raise CrossPoolContractError("fee gap start positive part must reconcile")
        if self.fee_gap_end_positive_bps != max(expected_end_gap, 0.0):
            raise CrossPoolContractError("fee gap end positive part must reconcile")
        if self.fee_gap_closure_bps != expected_start_gap - expected_end_gap:
            raise CrossPoolContractError("fee gap closure must reconcile")


@dataclass(frozen=True)
class ShortHorizonExclusion:
    direction: Direction
    shock_timestamp_ms: int
    reason: Literal[
        "missing_target_start_state",
        "missing_target_end_state",
        "missing_source_end_state",
    ]

    def __post_init__(self) -> None:
        if self.direction not in ("bsc_to_base", "base_to_bsc"):
            raise CrossPoolContractError("unsupported short-horizon direction")
        _require_positive_int(self.shock_timestamp_ms, "exclusion shock timestamp")
        if self.reason not in (
            "missing_target_start_state",
            "missing_target_end_state",
            "missing_source_end_state",
        ):
            raise CrossPoolContractError("unsupported common-support exclusion")


@dataclass(frozen=True)
class ShortHorizonStudy:
    direction: Direction
    detected_shock_count: int
    eligible_shock_count: int
    responses: tuple[ShortHorizonResponse, ...]
    exclusions: tuple[ShortHorizonExclusion, ...]

    def __post_init__(self) -> None:
        if self.direction not in ("bsc_to_base", "base_to_bsc"):
            raise CrossPoolContractError("unsupported short-horizon direction")
        _require_nonnegative_int(self.detected_shock_count, "detected shock count")
        _require_nonnegative_int(self.eligible_shock_count, "eligible shock count")
        response_keys = tuple((row.shock_timestamp_ms, row.horizon_ms) for row in self.responses)
        if response_keys != tuple(sorted(response_keys)):
            raise CrossPoolContractError("short-horizon responses must be canonical")
        if len(set(response_keys)) != len(response_keys):
            raise CrossPoolContractError("short-horizon response keys must be unique")
        if any(row.direction != self.direction for row in self.responses):
            raise CrossPoolContractError("study responses must use one direction")
        by_shock: dict[int, list[ShortHorizonResponse]] = {}
        for row in self.responses:
            by_shock.setdefault(row.shock_timestamp_ms, []).append(row)
        for rows in by_shock.values():
            if tuple(row.horizon_ms for row in rows) != SHORT_HORIZONS_MS:
                raise CrossPoolContractError("each eligible shock must contain all seven horizons")
            if len({row.target_same_timestamp for row in rows}) != 1:
                raise CrossPoolContractError(
                    "same-timestamp status must agree across a shock family"
                )
        if self.eligible_shock_count != len(by_shock):
            raise CrossPoolContractError("eligible shock count must reconcile")

        exclusion_keys = tuple(row.shock_timestamp_ms for row in self.exclusions)
        if exclusion_keys != tuple(sorted(exclusion_keys)):
            raise CrossPoolContractError("short-horizon exclusions must be canonical")
        if len(set(exclusion_keys)) != len(exclusion_keys):
            raise CrossPoolContractError("short-horizon exclusion keys must be unique")
        if any(row.direction != self.direction for row in self.exclusions):
            raise CrossPoolContractError("study exclusions must use one direction")
        if set(exclusion_keys) & set(by_shock):
            raise CrossPoolContractError(
                "a shock cannot be both eligible and common-support excluded"
            )
        if self.detected_shock_count != self.eligible_shock_count + len(self.exclusions):
            raise CrossPoolContractError("detected shock count must reconcile")

    @property
    def responses_excluding_same_timestamp(self) -> tuple[ShortHorizonResponse, ...]:
        return tuple(row for row in self.responses if not row.target_same_timestamp)


def measure_short_horizon_direction(
    source: Sequence[PoolEvent],
    target: Sequence[PoolEvent],
) -> ShortHorizonStudy:
    config = ShockConfig(response_horizons_ms=SHORT_HORIZONS_MS)
    source_events = _validate_input_stream(source, "source")
    target_events = _validate_input_stream(target, "target")

    direction: Direction = "bsc_to_base" if source_events[0].pool == "uni-bsc" else "base_to_bsc"
    expected_target_pool: PoolName = "uni-base" if source_events[0].pool == "uni-bsc" else "uni-bsc"
    if target_events[0].pool != expected_target_pool:
        raise CrossPoolContractError("short-horizon target pool must oppose source pool")
    shocks = detect_shocks(source_events, config)
    maximum_horizon_config = ShockConfig(response_horizons_ms=(SHORT_HORIZONS_MS[-1],))
    target_support = measure_event_responses(
        shocks,
        target_events,
        maximum_horizon_config,
    )
    target_exclusions = {
        exclusion.shock_timestamp_ms: exclusion.reason for exclusion in target_support.exclusions
    }
    exclusions: list[ShortHorizonExclusion] = []
    eligible_shocks = []
    for shock in shocks:
        target_reason = target_exclusions.get(shock.shock_timestamp_ms)
        if target_reason is not None:
            exclusions.append(
                ShortHorizonExclusion(
                    direction=shock.direction,
                    shock_timestamp_ms=shock.shock_timestamp_ms,
                    reason=target_reason,
                )
            )
            continue
        if shock.shock_timestamp_ms + SHORT_HORIZONS_MS[-1] > source_events[-1].timestamp_ms:
            exclusions.append(
                ShortHorizonExclusion(
                    direction=shock.direction,
                    shock_timestamp_ms=shock.shock_timestamp_ms,
                    reason="missing_source_end_state",
                )
            )
            continue
        eligible_shocks.append(shock)
    eligible = tuple(eligible_shocks)
    if not eligible:
        return ShortHorizonStudy(
            direction=direction,
            detected_shock_count=len(shocks),
            eligible_shock_count=0,
            responses=(),
            exclusions=tuple(exclusions),
        )

    parent_result = measure_event_responses(eligible, target_events, config)
    if parent_result.exclusions:
        raise CrossPoolContractError("common-support shocks cannot produce parent event exclusions")

    target_timestamps = tuple(event.timestamp_ms for event in target_events)
    source_timestamps = tuple(event.timestamp_ms for event in source_events)
    shocks_by_timestamp = {shock.shock_timestamp_ms: shock for shock in eligible}
    rows: list[ShortHorizonResponse] = []
    for parent in parent_result.responses:
        shock = shocks_by_timestamp[parent.shock_timestamp_ms]
        start_index = bisect_right(target_timestamps, parent.shock_timestamp_ms) - 1
        end_index = (
            bisect_right(
                target_timestamps,
                parent.shock_timestamp_ms + parent.horizon_ms,
            )
            - 1
        )
        source_shock_index = (
            bisect_right(
                source_timestamps,
                parent.shock_timestamp_ms,
            )
            - 1
        )
        source_end_index = (
            bisect_right(
                source_timestamps,
                parent.shock_timestamp_ms + parent.horizon_ms,
            )
            - 1
        )
        target_start = target_events[start_index]
        target_end = target_events[end_index]
        source_shock = source_events[source_shock_index]
        source_end = source_events[source_end_index]
        first_update_index = start_index + 1
        first_update = (
            target_events[first_update_index]
            if first_update_index < len(target_events)
            and target_events[first_update_index].timestamp_ms
            <= parent.shock_timestamp_ms + parent.horizon_ms
            else None
        )
        if first_update is None:
            first_update_status: FirstUpdateStatus = "right_censored"
            first_update_timestamp_ms = None
            first_update_delay_ms = None
            first_update_raw_mid = None
            first_update_response_bps = None
            first_update_direction_agrees = None
        else:
            first_update_status = "observed"
            first_update_response_bps = _return_bps(
                target_start.raw_mid,
                first_update.raw_mid,
            )
            first_update_timestamp_ms = first_update.timestamp_ms
            first_update_delay_ms = first_update.timestamp_ms - parent.shock_timestamp_ms
            first_update_raw_mid = first_update.raw_mid
            first_update_direction_agrees = _direction_agrees(
                shock.source_move_bps,
                first_update_response_bps,
            )

        fee_gap_start_bps = _fee_gap_bps(
            shock.source_move_sign,
            source_shock,
            target_start,
        )
        fee_gap_end_bps = _fee_gap_bps(
            shock.source_move_sign,
            source_end,
            target_end,
        )

        rows.append(
            ShortHorizonResponse(
                direction=parent.direction,
                source_pool=parent.source_pool,
                target_pool=parent.target_pool,
                shock_timestamp_ms=parent.shock_timestamp_ms,
                shock_day_utc=parent.shock_day_utc,
                horizon_ms=parent.horizon_ms,
                source_move_bps=parent.source_move_bps,
                source_move_sign=shock.source_move_sign,
                source_shock_raw_mid=source_shock.raw_mid,
                source_shock_bid=source_shock.fee_adjusted_bid,
                source_shock_ask=source_shock.fee_adjusted_ask,
                source_end_timestamp_ms=source_end.timestamp_ms,
                source_end_age_ms=(
                    parent.shock_timestamp_ms + parent.horizon_ms - source_end.timestamp_ms
                ),
                source_end_raw_mid=source_end.raw_mid,
                source_end_bid=source_end.fee_adjusted_bid,
                source_end_ask=source_end.fee_adjusted_ask,
                target_start_timestamp_ms=parent.target_start_timestamp_ms,
                target_start_age_ms=(parent.shock_timestamp_ms - parent.target_start_timestamp_ms),
                target_start_raw_mid=target_start.raw_mid,
                target_start_bid=target_start.fee_adjusted_bid,
                target_start_ask=target_start.fee_adjusted_ask,
                target_end_timestamp_ms=parent.target_end_timestamp_ms,
                target_end_age_ms=(
                    parent.shock_timestamp_ms + parent.horizon_ms - parent.target_end_timestamp_ms
                ),
                target_end_raw_mid=target_end.raw_mid,
                target_end_bid=target_end.fee_adjusted_bid,
                target_end_ask=target_end.fee_adjusted_ask,
                target_response_bps=parent.target_response_bps,
                zero_response=parent.target_response_bps == 0.0,
                direction_agrees=parent.direction_agrees,
                target_same_timestamp=(
                    parent.target_start_timestamp_ms == parent.shock_timestamp_ms
                ),
                first_update_status=first_update_status,
                first_update_timestamp_ms=first_update_timestamp_ms,
                first_update_delay_ms=first_update_delay_ms,
                first_update_raw_mid=first_update_raw_mid,
                first_update_response_bps=first_update_response_bps,
                first_update_direction_agrees=first_update_direction_agrees,
                fee_gap_start_bps=fee_gap_start_bps,
                fee_gap_end_bps=fee_gap_end_bps,
                fee_gap_start_positive_bps=max(fee_gap_start_bps, 0.0),
                fee_gap_end_positive_bps=max(fee_gap_end_bps, 0.0),
                fee_gap_closure_bps=fee_gap_start_bps - fee_gap_end_bps,
            )
        )

    return ShortHorizonStudy(
        direction=direction,
        detected_shock_count=len(shocks),
        eligible_shock_count=len(eligible),
        responses=tuple(rows),
        exclusions=tuple(exclusions),
    )


def _return_bps(start_mid: Decimal, end_mid: Decimal) -> float:
    with localcontext() as context:
        context.prec = _DECIMAL_PRECISION
        value = (end_mid / start_mid - Decimal(1)) * _BPS
    result = float(value)
    if not math.isfinite(result):
        raise CrossPoolContractError("short-horizon response must be finite")
    return result


def _direction_agrees(source_move_bps: float, response_bps: float) -> bool:
    return (source_move_bps > 0.0 and response_bps > 0.0) or (
        source_move_bps < 0.0 and response_bps < 0.0
    )


def _fee_gap_bps(
    source_move_sign: Literal[-1, 1],
    source: PoolEvent,
    target: PoolEvent,
) -> float:
    return _fee_gap_from_quotes(
        source_move_sign,
        source_bid=source.fee_adjusted_bid,
        source_ask=source.fee_adjusted_ask,
        target_bid=target.fee_adjusted_bid,
        target_ask=target.fee_adjusted_ask,
    )


def _fee_gap_from_quotes(
    source_move_sign: Literal[-1, 1],
    *,
    source_bid: Decimal,
    source_ask: Decimal,
    target_bid: Decimal,
    target_ask: Decimal,
) -> float:
    ratio = source_bid / target_ask if source_move_sign == 1 else target_bid / source_ask
    result = float(_BPS) * math.log(float(ratio))
    if not math.isfinite(result):
        raise CrossPoolContractError("fee-only cross-venue gap must be finite")
    return result


def _validate_fee_band(
    raw_mid: Decimal,
    bid: Decimal,
    ask: Decimal,
    label: str,
) -> None:
    if not all(
        isinstance(value, Decimal) and value.is_finite() and value > 0
        for value in (raw_mid, bid, ask)
    ):
        raise CrossPoolContractError(f"{label} fee band values must be positive")
    if not bid < raw_mid < ask:
        raise CrossPoolContractError(f"{label} fee band must contain raw mid")


def _validate_input_stream(
    events: Sequence[PoolEvent],
    label: str,
) -> tuple[PoolEvent, ...]:
    materialized = tuple(events)
    if not materialized:
        raise CrossPoolContractError(f"short-horizon {label} stream cannot be empty")
    pool = materialized[0].pool
    if pool not in ("uni-base", "uni-bsc"):
        raise CrossPoolContractError(f"short-horizon {label} pool is unsupported")
    previous_timestamp_ms: int | None = None
    for event in materialized:
        if event.pool != pool:
            raise CrossPoolContractError(f"short-horizon {label} stream must contain one pool")
        _require_positive_int(event.timestamp_ms, f"{label} event timestamp")
        if previous_timestamp_ms is not None and event.timestamp_ms <= previous_timestamp_ms:
            raise CrossPoolContractError(
                f"short-horizon {label} timestamps must be strictly increasing"
            )
        _validate_fee_band(
            event.raw_mid,
            event.fee_adjusted_bid,
            event.fee_adjusted_ask,
            f"{label} event",
        )
        previous_timestamp_ms = event.timestamp_ms
    return materialized


def _require_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CrossPoolContractError(f"{label} must be a positive integer")


def _require_nonnegative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrossPoolContractError(f"{label} must be a nonnegative integer")


def _require_finite(value: float, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CrossPoolContractError(f"{label} must be finite")
    if not math.isfinite(float(value)):
        raise CrossPoolContractError(f"{label} must be finite")
