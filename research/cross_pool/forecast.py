"""Causal hourly forecast selection and routed-archetype agreement."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, TypeAlias

from research.backtester.entry_eligibility import EntryEligibilityDecision
from research.backtester.sizing import EntryContext
from research.cross_pool.contracts import CrossPoolContractError

DirectionalArchetype: TypeAlias = Literal[
    "upside_capture",
    "dip_accumulator",
    "fee_box",
]

_DIRECTIONAL_ARCHETYPES: tuple[DirectionalArchetype, ...] = (
    "upside_capture",
    "dip_accumulator",
    "fee_box",
)
_ONE_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class ForecastPoint:
    origin_time: datetime
    refit_time: datetime
    horizon: timedelta
    forecast_bps: float

    def __post_init__(self) -> None:
        _require_utc(self.origin_time, field_name="origin_time")
        _require_utc(self.refit_time, field_name="refit_time")
        if (
            self.origin_time.minute != 0
            or self.origin_time.second != 0
            or self.origin_time.microsecond != 0
        ):
            raise CrossPoolContractError("origin_time must be hour-aligned")
        if self.horizon != _ONE_HOUR:
            raise CrossPoolContractError("horizon must equal one hour")
        if self.refit_time > self.origin_time:
            raise CrossPoolContractError("refit_time must not follow origin_time")
        if not math.isfinite(self.forecast_bps):
            raise CrossPoolContractError("forecast_bps must be finite")


@dataclass(frozen=True)
class ForecastTimeline:
    points: tuple[ForecastPoint, ...]
    _origins: tuple[datetime, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.points:
            raise CrossPoolContractError("forecast timeline requires at least one point")
        origins = tuple(point.origin_time for point in self.points)
        if any(
            current <= previous
            for previous, current in zip(origins, origins[1:], strict=False)
        ):
            raise CrossPoolContractError(
                "forecast origins must be strictly increasing and unique"
            )
        object.__setattr__(self, "_origins", origins)

    def as_of(self, decision_time: datetime) -> ForecastPoint:
        _require_utc(decision_time, field_name="decision_time")
        point_index = bisect_right(self._origins, decision_time) - 1
        if point_index < 0:
            raise CrossPoolContractError("no forecast at or before decision_time")
        point = self.points[point_index]
        if decision_time >= point.origin_time + point.horizon:
            raise CrossPoolContractError("forecast is stale at decision_time")
        return point


@dataclass(frozen=True)
class ForecastAgreementOverlay:
    routed_archetype: DirectionalArchetype
    timeline: ForecastTimeline
    threshold_bps: float = 5.0

    def __post_init__(self) -> None:
        if self.routed_archetype not in _DIRECTIONAL_ARCHETYPES:
            raise CrossPoolContractError(
                f"unsupported routed_archetype: {self.routed_archetype!r}"
            )
        if not math.isfinite(self.threshold_bps) or self.threshold_bps < 0.0:
            raise CrossPoolContractError(
                "threshold_bps must be finite and nonnegative"
            )

    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
        forecast_bps = self.timeline.as_of(context.block_time).forecast_bps
        if self.routed_archetype == "upside_capture":
            eligible = forecast_bps > self.threshold_bps
        elif self.routed_archetype == "dip_accumulator":
            eligible = forecast_bps < -self.threshold_bps
        else:
            eligible = -self.threshold_bps <= forecast_bps <= self.threshold_bps
        return EntryEligibilityDecision(
            eligible=eligible,
            reason="agreement" if eligible else "disagreement",
        )


def _require_utc(value: datetime, *, field_name: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise CrossPoolContractError(f"{field_name} must be UTC-aware")
