"""Paired UTC shock-day inference for short-horizon response profiles."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from statistics import median
from typing import Iterable, Literal, Sequence, cast

import numpy as np
from numpy.typing import NDArray

from research.cross_pool.bootstrap import _draw_day_indices, _nearest_rank_indices
from research.cross_pool.contracts import CrossPoolContractError, Direction
from research.cross_pool.short_horizon import (
    SHORT_HORIZONS_MS,
    ShortHorizonResponse,
    ShortHorizonStudy,
)

SHORT_HORIZON_BOOTSTRAP_RESAMPLES = 2_000
SHORT_HORIZON_BOOTSTRAP_SEED = 20_260_715
SHORT_HORIZON_BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
MIN_JOINT_VALID_RESAMPLES = 1_900
MIN_CONDITIONAL_UPDATE_DAYS = 10

Cohort = Literal["primary", "exclude_same_timestamp"]
SimultaneousMetric = Literal[
    "unconditional_mean_response_bps",
    "update_incidence",
    "conditional_first_update_mean_response_bps",
]
BandStatus = Literal["available", "unavailable"]
BandUnavailableReason = Literal[
    "insufficient_observed_update_days",
    "insufficient_valid_joint_resamples",
    "zero_bootstrap_standard_deviation",
    "nonfinite_bootstrap_standard_deviation",
]

_SIMULTANEOUS_METRICS: tuple[SimultaneousMetric, ...] = (
    "unconditional_mean_response_bps",
    "update_incidence",
    "conditional_first_update_mean_response_bps",
)
_UNAVAILABLE_REASONS: tuple[BandUnavailableReason, ...] = (
    "insufficient_observed_update_days",
    "insufficient_valid_joint_resamples",
    "zero_bootstrap_standard_deviation",
    "nonfinite_bootstrap_standard_deviation",
)


@dataclass(frozen=True)
class PointwiseInterval:
    point: float
    lower: float
    upper: float

    def __post_init__(self) -> None:
        _require_finite(self.point, "pointwise interval point")
        _require_finite(self.lower, "pointwise interval lower")
        _require_finite(self.upper, "pointwise interval upper")
        if self.lower > self.upper:
            raise CrossPoolContractError("pointwise interval lower must not exceed upper")


@dataclass(frozen=True)
class ShortHorizonSummary:
    direction: Direction
    horizon_ms: int
    eligible_event_count: int
    shock_day_count: int
    unconditional_mean_response_bps: PointwiseInterval
    unconditional_median_response_bps: PointwiseInterval
    direction_agreement_share: float
    zero_response_share: float
    update_count: int
    right_censored_count: int
    update_incidence: float
    observed_update_day_count: int
    conditional_first_update_mean_response_bps: float | None
    conditional_first_update_median_response_bps: float | None
    conditional_first_update_median_delay_ms: float | None
    conditional_first_update_p95_delay_ms: float | None
    target_start_median_age_ms: float
    target_start_p95_age_ms: float
    target_end_median_age_ms: float
    target_end_p95_age_ms: float
    mean_fee_gap_start_bps: float
    mean_fee_gap_end_bps: float
    positive_fee_gap_start_share: float
    positive_fee_gap_end_share: float
    mean_fee_gap_closure_bps: float

    def __post_init__(self) -> None:
        _require_direction(self.direction)
        if self.horizon_ms not in SHORT_HORIZONS_MS:
            raise CrossPoolContractError("summary horizon must be canonical")
        _require_positive_int(self.eligible_event_count, "eligible event count")
        _require_positive_int(self.shock_day_count, "shock day count")
        if self.shock_day_count > self.eligible_event_count:
            raise CrossPoolContractError("shock day count cannot exceed event count")
        _require_nonnegative_int(self.update_count, "update count")
        _require_nonnegative_int(self.right_censored_count, "right-censored count")
        if self.update_count + self.right_censored_count != self.eligible_event_count:
            raise CrossPoolContractError("update and censor counts must reconcile")
        _require_nonnegative_int(
            self.observed_update_day_count,
            "observed update day count",
        )
        if self.observed_update_day_count > min(
            self.shock_day_count,
            self.update_count,
        ):
            raise CrossPoolContractError("observed update day count exceeds its support")
        for value, label in (
            (self.direction_agreement_share, "direction agreement share"),
            (self.zero_response_share, "zero response share"),
            (self.update_incidence, "update incidence"),
            (self.positive_fee_gap_start_share, "positive start-gap share"),
            (self.positive_fee_gap_end_share, "positive end-gap share"),
        ):
            _require_share(value, label)
        if self.update_incidence != self.update_count / self.eligible_event_count:
            raise CrossPoolContractError("update incidence must reconcile with counts")

        conditional_values = (
            self.conditional_first_update_mean_response_bps,
            self.conditional_first_update_median_response_bps,
            self.conditional_first_update_median_delay_ms,
            self.conditional_first_update_p95_delay_ms,
        )
        if self.update_count == 0:
            if any(value is not None for value in conditional_values):
                raise CrossPoolContractError(
                    "no-update summaries cannot contain conditional values"
                )
        else:
            if any(value is None for value in conditional_values):
                raise CrossPoolContractError(
                    "observed updates require every conditional summary value"
                )
            for conditional_value in conditional_values:
                assert conditional_value is not None
                _require_finite(conditional_value, "conditional summary value")
            assert self.conditional_first_update_median_delay_ms is not None
            assert self.conditional_first_update_p95_delay_ms is not None
            if self.conditional_first_update_median_delay_ms <= 0.0:
                raise CrossPoolContractError("conditional median delay must be positive")
            if (
                self.conditional_first_update_p95_delay_ms
                < self.conditional_first_update_median_delay_ms
            ):
                raise CrossPoolContractError("conditional p95 delay must not precede its median")

        for value, label in (
            (self.target_start_median_age_ms, "target start median age"),
            (self.target_start_p95_age_ms, "target start p95 age"),
            (self.target_end_median_age_ms, "target end median age"),
            (self.target_end_p95_age_ms, "target end p95 age"),
        ):
            _require_finite(value, label)
            if value < 0.0:
                raise CrossPoolContractError(f"{label} must be nonnegative")
        if self.target_start_p95_age_ms < self.target_start_median_age_ms:
            raise CrossPoolContractError("target start p95 age must follow its median")
        if self.target_end_p95_age_ms < self.target_end_median_age_ms:
            raise CrossPoolContractError("target end p95 age must follow its median")
        for value, label in (
            (self.mean_fee_gap_start_bps, "mean fee gap start"),
            (self.mean_fee_gap_end_bps, "mean fee gap end"),
            (self.mean_fee_gap_closure_bps, "mean fee gap closure"),
        ):
            _require_finite(value, label)


@dataclass(frozen=True)
class SimultaneousBandPoint:
    horizon_ms: int
    point: float
    lower: float
    upper: float
    bootstrap_standard_deviation: float

    def __post_init__(self) -> None:
        if self.horizon_ms not in SHORT_HORIZONS_MS:
            raise CrossPoolContractError("simultaneous-band horizon must be canonical")
        for value, label in (
            (self.point, "simultaneous-band point"),
            (self.lower, "simultaneous-band lower"),
            (self.upper, "simultaneous-band upper"),
            (
                self.bootstrap_standard_deviation,
                "simultaneous-band bootstrap standard deviation",
            ),
        ):
            _require_finite(value, label)
        if self.lower > self.upper:
            raise CrossPoolContractError("simultaneous-band lower must not exceed upper")
        if self.bootstrap_standard_deviation <= 0.0:
            raise CrossPoolContractError(
                "simultaneous-band bootstrap standard deviation must be positive"
            )


@dataclass(frozen=True)
class SimultaneousBandFamily:
    metric: SimultaneousMetric
    status: BandStatus
    reason: BandUnavailableReason | None
    valid_joint_resamples: int
    critical_value: float | None
    points: tuple[SimultaneousBandPoint, ...]

    def __post_init__(self) -> None:
        if self.metric not in _SIMULTANEOUS_METRICS:
            raise CrossPoolContractError("unsupported simultaneous-band metric")
        _require_nonnegative_int(
            self.valid_joint_resamples,
            "valid joint resample count",
        )
        if self.status == "available":
            if self.reason is not None:
                raise CrossPoolContractError("available simultaneous bands cannot have a reason")
            if self.valid_joint_resamples < MIN_JOINT_VALID_RESAMPLES:
                raise CrossPoolContractError(
                    "available simultaneous bands need sufficient joint resamples"
                )
            if self.critical_value is None:
                raise CrossPoolContractError(
                    "available simultaneous bands require a critical value"
                )
            _require_finite(self.critical_value, "simultaneous-band critical value")
            if self.critical_value <= 0.0:
                raise CrossPoolContractError("simultaneous-band critical value must be positive")
            if tuple(point.horizon_ms for point in self.points) != SHORT_HORIZONS_MS:
                raise CrossPoolContractError(
                    "available simultaneous bands require canonical horizons"
                )
            if self.metric == "update_incidence" and any(
                point.lower < 0.0 or point.upper > 1.0 for point in self.points
            ):
                raise CrossPoolContractError(
                    "update-incidence simultaneous bands must remain within zero and one"
                )
        elif self.status == "unavailable":
            if self.reason not in _UNAVAILABLE_REASONS:
                raise CrossPoolContractError(
                    "unavailable simultaneous bands require a stable reason"
                )
            if self.critical_value is not None or self.points:
                raise CrossPoolContractError(
                    "unavailable simultaneous bands cannot contain estimates"
                )
        else:
            raise CrossPoolContractError("unsupported simultaneous-band status")


@dataclass(frozen=True)
class ShortHorizonInference:
    direction: Direction
    cohort: Cohort
    eligible_event_count: int
    shock_day_count: int
    resamples: int
    seed: int
    confidence_level: float
    summaries: tuple[ShortHorizonSummary, ...]
    simultaneous_bands: tuple[SimultaneousBandFamily, ...]

    def __post_init__(self) -> None:
        _require_direction(self.direction)
        if self.cohort not in ("primary", "exclude_same_timestamp"):
            raise CrossPoolContractError("unsupported short-horizon cohort")
        _require_positive_int(self.eligible_event_count, "eligible event count")
        _require_positive_int(self.shock_day_count, "shock day count")
        _validate_bootstrap_config(
            resamples=self.resamples,
            seed=self.seed,
            confidence_level=self.confidence_level,
        )
        if tuple(summary.horizon_ms for summary in self.summaries) != SHORT_HORIZONS_MS:
            raise CrossPoolContractError("inference summaries must be canonical")
        if any(
            summary.direction != self.direction
            or summary.eligible_event_count != self.eligible_event_count
            or summary.shock_day_count != self.shock_day_count
            for summary in self.summaries
        ):
            raise CrossPoolContractError("inference summaries must share direction and support")
        if tuple(family.metric for family in self.simultaneous_bands) != (_SIMULTANEOUS_METRICS):
            raise CrossPoolContractError(
                "simultaneous-band families must use canonical metric order"
            )
        if any(family.valid_joint_resamples > self.resamples for family in self.simultaneous_bands):
            raise CrossPoolContractError("valid joint resamples cannot exceed configured resamples")
        points_by_metric: dict[SimultaneousMetric, tuple[float, ...]] = {
            "unconditional_mean_response_bps": tuple(
                summary.unconditional_mean_response_bps.point for summary in self.summaries
            ),
            "update_incidence": tuple(summary.update_incidence for summary in self.summaries),
            "conditional_first_update_mean_response_bps": tuple(
                summary.conditional_first_update_mean_response_bps
                for summary in self.summaries
                if summary.conditional_first_update_mean_response_bps is not None
            ),
        }
        for family in self.simultaneous_bands:
            if (
                family.status == "available"
                and tuple(point.point for point in family.points) != points_by_metric[family.metric]
            ):
                raise CrossPoolContractError(
                    "simultaneous-band points must match summary estimates"
                )


@dataclass(frozen=True)
class _HorizonBootstrap:
    mean_response_bps: tuple[float, ...]
    median_response_bps: tuple[float, ...]
    update_incidence: tuple[float, ...]
    conditional_mean_response_bps: tuple[float | None, ...]


def infer_short_horizon(
    study: ShortHorizonStudy,
    *,
    exclude_same_timestamp: bool = False,
) -> ShortHorizonInference:
    return _infer_short_horizon(
        study,
        exclude_same_timestamp=exclude_same_timestamp,
        resamples=SHORT_HORIZON_BOOTSTRAP_RESAMPLES,
        seed=SHORT_HORIZON_BOOTSTRAP_SEED,
        confidence_level=SHORT_HORIZON_BOOTSTRAP_CONFIDENCE_LEVEL,
    )


def _infer_short_horizon(
    study: ShortHorizonStudy,
    *,
    exclude_same_timestamp: bool = False,
    resamples: int = SHORT_HORIZON_BOOTSTRAP_RESAMPLES,
    seed: int = SHORT_HORIZON_BOOTSTRAP_SEED,
    confidence_level: float = SHORT_HORIZON_BOOTSTRAP_CONFIDENCE_LEVEL,
) -> ShortHorizonInference:
    if not isinstance(exclude_same_timestamp, bool):
        raise CrossPoolContractError("same-timestamp sensitivity flag must be boolean")
    _validate_bootstrap_config(
        resamples=resamples,
        seed=seed,
        confidence_level=confidence_level,
    )
    rows = study.responses_excluding_same_timestamp if exclude_same_timestamp else study.responses
    validated = _validated_rows(rows, expected_direction=study.direction)
    shock_keys = tuple(dict.fromkeys(row.shock_timestamp_ms for row in validated))
    ordered_days = tuple(sorted({row.shock_day_utc for row in validated}))
    draws = _draw_day_indices(
        day_count=len(ordered_days),
        resamples=resamples,
        seed=seed,
    )
    expected_shape = (resamples, len(ordered_days))
    if draws.shape != expected_shape or draws.dtype != np.int64:
        raise CrossPoolContractError("paired UTC-day draw matrix has an invalid shape or dtype")
    if bool(np.any(draws < 0)) or bool(np.any(draws >= len(ordered_days))):
        raise CrossPoolContractError("paired UTC-day draw indices are out of range")

    summaries: list[ShortHorizonSummary] = []
    bootstrap_by_horizon: list[_HorizonBootstrap] = []
    for horizon_ms in SHORT_HORIZONS_MS:
        horizon_rows = tuple(row for row in validated if row.horizon_ms == horizon_ms)
        bootstrap = _bootstrap_horizon(horizon_rows, ordered_days, draws)
        summaries.append(
            _summarize_horizon(
                horizon_rows,
                bootstrap,
                confidence_level=confidence_level,
            )
        )
        bootstrap_by_horizon.append(bootstrap)

    summary_tuple = tuple(summaries)
    bootstrap_tuple = tuple(bootstrap_by_horizon)
    bands = tuple(
        _simultaneous_band(
            metric,
            summaries=summary_tuple,
            bootstraps=bootstrap_tuple,
            confidence_level=confidence_level,
        )
        for metric in _SIMULTANEOUS_METRICS
    )
    return ShortHorizonInference(
        direction=study.direction,
        cohort=("exclude_same_timestamp" if exclude_same_timestamp else "primary"),
        eligible_event_count=len(shock_keys),
        shock_day_count=len(ordered_days),
        resamples=resamples,
        seed=seed,
        confidence_level=confidence_level,
        summaries=summary_tuple,
        simultaneous_bands=bands,
    )


def _summarize_horizon(
    rows: tuple[ShortHorizonResponse, ...],
    bootstrap: _HorizonBootstrap,
    *,
    confidence_level: float,
) -> ShortHorizonSummary:
    responses = tuple(row.target_response_bps for row in rows)
    observed = tuple(row for row in rows if row.first_update_status == "observed")
    conditional_responses = tuple(cast(float, row.first_update_response_bps) for row in observed)
    conditional_delays = tuple(cast(int, row.first_update_delay_ms) for row in observed)
    mean_response = _mean(responses, "unconditional response mean must be finite")
    median_response = _median(responses)

    return ShortHorizonSummary(
        direction=rows[0].direction,
        horizon_ms=rows[0].horizon_ms,
        eligible_event_count=len(rows),
        shock_day_count=len({row.shock_day_utc for row in rows}),
        unconditional_mean_response_bps=_pointwise_interval(
            point=mean_response,
            samples=np.asarray(bootstrap.mean_response_bps, dtype=np.float64),
            confidence_level=confidence_level,
        ),
        unconditional_median_response_bps=_pointwise_interval(
            point=median_response,
            samples=np.asarray(bootstrap.median_response_bps, dtype=np.float64),
            confidence_level=confidence_level,
        ),
        direction_agreement_share=_share(row.direction_agrees for row in rows),
        zero_response_share=_share(row.zero_response for row in rows),
        update_count=len(observed),
        right_censored_count=len(rows) - len(observed),
        update_incidence=len(observed) / len(rows),
        observed_update_day_count=len({row.shock_day_utc for row in observed}),
        conditional_first_update_mean_response_bps=(
            _mean(
                conditional_responses,
                "conditional first-update mean must be finite",
            )
            if observed
            else None
        ),
        conditional_first_update_median_response_bps=(
            _median(conditional_responses) if observed else None
        ),
        conditional_first_update_median_delay_ms=(
            _median(conditional_delays) if observed else None
        ),
        conditional_first_update_p95_delay_ms=(
            _nearest_rank(conditional_delays, 0.95) if observed else None
        ),
        target_start_median_age_ms=_median(tuple(row.target_start_age_ms for row in rows)),
        target_start_p95_age_ms=_nearest_rank(
            tuple(row.target_start_age_ms for row in rows),
            0.95,
        ),
        target_end_median_age_ms=_median(tuple(row.target_end_age_ms for row in rows)),
        target_end_p95_age_ms=_nearest_rank(
            tuple(row.target_end_age_ms for row in rows),
            0.95,
        ),
        mean_fee_gap_start_bps=_mean(
            tuple(row.fee_gap_start_bps for row in rows),
            "mean start fee gap must be finite",
        ),
        mean_fee_gap_end_bps=_mean(
            tuple(row.fee_gap_end_bps for row in rows),
            "mean end fee gap must be finite",
        ),
        positive_fee_gap_start_share=_share(row.fee_gap_start_bps > 0.0 for row in rows),
        positive_fee_gap_end_share=_share(row.fee_gap_end_bps > 0.0 for row in rows),
        mean_fee_gap_closure_bps=_mean(
            tuple(row.fee_gap_closure_bps for row in rows),
            "mean fee-gap closure must be finite",
        ),
    )


def _bootstrap_horizon(
    rows: tuple[ShortHorizonResponse, ...],
    ordered_days: tuple[date, ...],
    draws: NDArray[np.int64],
) -> _HorizonBootstrap:
    rows_by_day = {
        day: tuple(row for row in rows if row.shock_day_utc == day) for day in ordered_days
    }
    if any(not day_rows for day_rows in rows_by_day.values()):
        raise CrossPoolContractError("each paired shock day must contain horizon rows")
    mean_samples: list[float] = []
    median_samples: list[float] = []
    incidence_samples: list[float] = []
    conditional_mean_samples: list[float | None] = []
    for draw in draws:
        sampled = tuple(
            row for day_index in draw for row in rows_by_day[ordered_days[int(day_index)]]
        )
        responses = tuple(row.target_response_bps for row in sampled)
        observed = tuple(row for row in sampled if row.first_update_status == "observed")
        mean_samples.append(_mean(responses, "bootstrap response mean must be finite"))
        median_samples.append(_median(responses))
        incidence_samples.append(len(observed) / len(sampled))
        conditional_mean_samples.append(
            _mean(
                tuple(cast(float, row.first_update_response_bps) for row in observed),
                "bootstrap conditional response mean must be finite",
            )
            if observed
            else None
        )
    return _HorizonBootstrap(
        mean_response_bps=tuple(mean_samples),
        median_response_bps=tuple(median_samples),
        update_incidence=tuple(incidence_samples),
        conditional_mean_response_bps=tuple(conditional_mean_samples),
    )


def _simultaneous_band(
    metric: SimultaneousMetric,
    *,
    summaries: tuple[ShortHorizonSummary, ...],
    bootstraps: tuple[_HorizonBootstrap, ...],
    confidence_level: float,
) -> SimultaneousBandFamily:
    if metric == "unconditional_mean_response_bps":
        points = tuple(summary.unconditional_mean_response_bps.point for summary in summaries)
        distributions: tuple[tuple[float | None, ...], ...] = tuple(
            bootstrap.mean_response_bps for bootstrap in bootstraps
        )
    elif metric == "update_incidence":
        points = tuple(summary.update_incidence for summary in summaries)
        distributions = tuple(bootstrap.update_incidence for bootstrap in bootstraps)
    else:
        if any(
            summary.observed_update_day_count < MIN_CONDITIONAL_UPDATE_DAYS for summary in summaries
        ):
            valid_joint = _valid_joint_count(
                tuple(bootstrap.conditional_mean_response_bps for bootstrap in bootstraps)
            )
            return _unavailable_band(
                metric,
                reason="insufficient_observed_update_days",
                valid_joint_resamples=valid_joint,
            )
        conditional_points = tuple(
            summary.conditional_first_update_mean_response_bps for summary in summaries
        )
        if any(point is None for point in conditional_points):
            raise CrossPoolContractError(
                "supported conditional bands require conditional point estimates"
            )
        points = tuple(cast(float, point) for point in conditional_points)
        distributions = tuple(bootstrap.conditional_mean_response_bps for bootstrap in bootstraps)

    joint_rows = _joint_valid_rows(distributions)
    valid_joint_resamples = len(joint_rows)
    if valid_joint_resamples < MIN_JOINT_VALID_RESAMPLES:
        return _unavailable_band(
            metric,
            reason="insufficient_valid_joint_resamples",
            valid_joint_resamples=valid_joint_resamples,
        )
    joint = np.asarray(joint_rows, dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore"):
        standard_deviations = np.std(joint, axis=0, ddof=1)
    if not bool(np.all(np.isfinite(standard_deviations))):
        return _unavailable_band(
            metric,
            reason="nonfinite_bootstrap_standard_deviation",
            valid_joint_resamples=valid_joint_resamples,
        )
    if bool(np.any(standard_deviations == 0.0)):
        return _unavailable_band(
            metric,
            reason="zero_bootstrap_standard_deviation",
            valid_joint_resamples=valid_joint_resamples,
        )

    point_array = np.asarray(points, dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        max_statistics = np.max(
            np.abs((joint - point_array) / standard_deviations),
            axis=1,
        )
    if not bool(np.all(np.isfinite(max_statistics))):
        raise CrossPoolContractError("max-z bootstrap statistics must be finite")
    critical_index = _one_sided_nearest_rank_index(
        valid_joint_resamples,
        confidence_level,
    )
    critical_value = float(np.sort(max_statistics)[critical_index])
    _require_finite(critical_value, "simultaneous-band critical value")
    if critical_value <= 0.0:
        raise CrossPoolContractError("max-z critical value must be positive")

    band_points: list[SimultaneousBandPoint] = []
    for horizon_ms, point, standard_deviation in zip(
        SHORT_HORIZONS_MS,
        points,
        standard_deviations,
        strict=True,
    ):
        width = critical_value * float(standard_deviation)
        lower = point - width
        upper = point + width
        if metric == "update_incidence":
            lower = max(0.0, lower)
            upper = min(1.0, upper)
        band_points.append(
            SimultaneousBandPoint(
                horizon_ms=horizon_ms,
                point=point,
                lower=lower,
                upper=upper,
                bootstrap_standard_deviation=float(standard_deviation),
            )
        )
    return SimultaneousBandFamily(
        metric=metric,
        status="available",
        reason=None,
        valid_joint_resamples=valid_joint_resamples,
        critical_value=critical_value,
        points=tuple(band_points),
    )


def _unavailable_band(
    metric: SimultaneousMetric,
    *,
    reason: BandUnavailableReason,
    valid_joint_resamples: int,
) -> SimultaneousBandFamily:
    return SimultaneousBandFamily(
        metric=metric,
        status="unavailable",
        reason=reason,
        valid_joint_resamples=valid_joint_resamples,
        critical_value=None,
        points=(),
    )


def _pointwise_interval(
    *,
    point: float,
    samples: NDArray[np.float64],
    confidence_level: float,
) -> PointwiseInterval:
    if samples.ndim != 1 or len(samples) == 0:
        raise CrossPoolContractError(
            "pointwise bootstrap samples must be one-dimensional and nonempty"
        )
    if not bool(np.all(np.isfinite(samples))):
        raise CrossPoolContractError("pointwise bootstrap samples must be finite")
    lower_index, upper_index = _nearest_rank_indices(
        len(samples),
        confidence_level,
    )
    ordered = np.sort(samples)
    return PointwiseInterval(
        point=point,
        lower=float(ordered[lower_index]),
        upper=float(ordered[upper_index]),
    )


def _joint_valid_rows(
    distributions: tuple[tuple[float | None, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    if len(distributions) != len(SHORT_HORIZONS_MS):
        raise CrossPoolContractError("simultaneous inference requires all seven distributions")
    sample_counts = {len(distribution) for distribution in distributions}
    if len(sample_counts) != 1 or not sample_counts or next(iter(sample_counts)) <= 0:
        raise CrossPoolContractError("simultaneous distributions require one nonempty draw count")
    joint: list[tuple[float, ...]] = []
    for draw_values in zip(*distributions, strict=True):
        if any(value is None for value in draw_values):
            continue
        row = tuple(cast(float, value) for value in draw_values)
        if not all(math.isfinite(value) for value in row):
            raise CrossPoolContractError("simultaneous bootstrap estimates must be finite")
        joint.append(row)
    return tuple(joint)


def _valid_joint_count(
    distributions: tuple[tuple[float | None, ...], ...],
) -> int:
    return len(_joint_valid_rows(distributions))


def _validated_rows(
    rows: Sequence[ShortHorizonResponse],
    *,
    expected_direction: Direction,
) -> tuple[ShortHorizonResponse, ...]:
    materialized = tuple(rows)
    if not materialized:
        raise CrossPoolContractError(
            "short-horizon inference requires at least one complete shock family"
        )
    _require_direction(expected_direction)
    if any(row.direction != expected_direction for row in materialized):
        raise CrossPoolContractError("inference rows must use the study direction")
    keys = tuple((row.shock_timestamp_ms, row.horizon_ms) for row in materialized)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise CrossPoolContractError("inference rows require unique canonical shock-horizon keys")
    by_shock: dict[int, list[ShortHorizonResponse]] = {}
    for row in materialized:
        by_shock.setdefault(row.shock_timestamp_ms, []).append(row)
    for family in by_shock.values():
        if tuple(row.horizon_ms for row in family) != SHORT_HORIZONS_MS:
            raise CrossPoolContractError(
                "inference requires complete nested canonical horizon families"
            )
        reference = family[0]
        if any(
            row.shock_day_utc != reference.shock_day_utc
            or row.source_move_bps != reference.source_move_bps
            or row.source_move_sign != reference.source_move_sign
            or row.target_start_timestamp_ms != reference.target_start_timestamp_ms
            or row.target_start_raw_mid != reference.target_start_raw_mid
            or row.target_same_timestamp != reference.target_same_timestamp
            for row in family[1:]
        ):
            raise CrossPoolContractError(
                "nested horizon families must bind one shock and target start"
            )
        observed_rows = tuple(row for row in family if row.first_update_status == "observed")
        if observed_rows:
            first_observed_index = family.index(observed_rows[0])
            if any(
                row.first_update_status != "right_censored" for row in family[:first_observed_index]
            ) or any(
                row.first_update_status != "observed" for row in family[first_observed_index:]
            ):
                raise CrossPoolContractError("first-update status must be nested across horizons")
            update_binding = (
                observed_rows[0].first_update_timestamp_ms,
                observed_rows[0].first_update_delay_ms,
                observed_rows[0].first_update_raw_mid,
                observed_rows[0].first_update_response_bps,
                observed_rows[0].first_update_direction_agrees,
            )
            if any(
                (
                    row.first_update_timestamp_ms,
                    row.first_update_delay_ms,
                    row.first_update_raw_mid,
                    row.first_update_response_bps,
                    row.first_update_direction_agrees,
                )
                != update_binding
                for row in observed_rows[1:]
            ):
                raise CrossPoolContractError("nested horizons must share the first-update binding")
    shock_sets = tuple(
        {row.shock_timestamp_ms for row in materialized if row.horizon_ms == horizon_ms}
        for horizon_ms in SHORT_HORIZONS_MS
    )
    if any(shock_set != shock_sets[0] for shock_set in shock_sets[1:]):
        raise CrossPoolContractError("every horizon must use the same shock cohort")
    return materialized


def _one_sided_nearest_rank_index(
    sample_count: int,
    confidence_level: float,
) -> int:
    _require_positive_int(sample_count, "sample count")
    _require_confidence_level(confidence_level)
    exact_confidence = Fraction(Decimal(str(confidence_level)))
    return min(
        max(math.ceil(exact_confidence * sample_count) - 1, 0),
        sample_count - 1,
    )


def _nearest_rank(values: Sequence[int | float], probability: float) -> float:
    if not values:
        raise CrossPoolContractError("nearest-rank input cannot be empty")
    _require_confidence_level(probability)
    ordered = sorted(float(value) for value in values)
    if not all(math.isfinite(value) for value in ordered):
        raise CrossPoolContractError("nearest-rank inputs must be finite")
    index = _one_sided_nearest_rank_index(len(ordered), probability)
    return ordered[index]


def _median(values: Sequence[int | float]) -> float:
    if not values:
        raise CrossPoolContractError("median input cannot be empty")
    result = float(median(values))
    _require_finite(result, "median must be finite")
    return result


def _mean(values: Sequence[float], message: str) -> float:
    if not values:
        raise CrossPoolContractError("mean input cannot be empty")
    try:
        result = math.fsum(values) / len(values)
    except OverflowError as exc:
        raise CrossPoolContractError(message) from exc
    if not math.isfinite(result):
        raise CrossPoolContractError(message)
    return result


def _share(values: Iterable[bool]) -> float:
    materialized = tuple(values)
    if not materialized or any(not isinstance(value, bool) for value in materialized):
        raise CrossPoolContractError("share inputs must be nonempty booleans")
    return sum(materialized) / len(materialized)


def _validate_bootstrap_config(
    *,
    resamples: int,
    seed: int,
    confidence_level: float,
) -> None:
    _require_positive_int(resamples, "resamples")
    _require_nonnegative_int(seed, "seed")
    _require_confidence_level(confidence_level)


def _require_confidence_level(value: float) -> None:
    _require_finite(value, "confidence level")
    if not 0.0 < value < 1.0:
        raise CrossPoolContractError("confidence level must be between zero and one")


def _require_direction(value: Direction) -> None:
    if value not in ("bsc_to_base", "base_to_bsc"):
        raise CrossPoolContractError("unsupported inference direction")


def _require_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CrossPoolContractError(f"{label} must be a positive integer")


def _require_nonnegative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrossPoolContractError(f"{label} must be a nonnegative integer")


def _require_share(value: float, label: str) -> None:
    _require_finite(value, label)
    if not 0.0 <= value <= 1.0:
        raise CrossPoolContractError(f"{label} must be between zero and one")


def _require_finite(value: float, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CrossPoolContractError(f"{label} must be finite")
    if not math.isfinite(float(value)):
        raise CrossPoolContractError(f"{label} must be finite")
