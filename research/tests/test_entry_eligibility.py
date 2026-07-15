from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from research.backtester.entry_eligibility import AlwaysEligibleOverlay
from research.backtester.sizing import EntryContext
from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.forecast import (
    DirectionalArchetype,
    ForecastAgreementOverlay,
    ForecastPoint,
    ForecastTimeline,
)

HOUR = timedelta(hours=1)
HOUR_0 = datetime(2026, 7, 15, 10, tzinfo=UTC)
HOUR_1 = HOUR_0 + HOUR
HOUR_2 = HOUR_1 + HOUR
HOUR_3 = HOUR_2 + HOUR


def _entry_context(block_time: datetime) -> EntryContext:
    return EntryContext(
        block_time=block_time,
        wallet_value_usd=500.0,
        current_price=1.0,
        current_tick=0,
        active_liquidity=1_000_000,
    )


def _point(
    *,
    origin_time: datetime = HOUR_1,
    refit_time: datetime = HOUR_0,
    horizon: timedelta = HOUR,
    forecast_bps: float = 6.0,
) -> ForecastPoint:
    return ForecastPoint(
        origin_time=origin_time,
        refit_time=refit_time,
        horizon=horizon,
        forecast_bps=forecast_bps,
    )


def _timeline(forecast_bps: float = 6.0) -> ForecastTimeline:
    return ForecastTimeline((_point(forecast_bps=forecast_bps),))


def test_always_eligible_overlay_is_time_agnostic() -> None:
    decision = AlwaysEligibleOverlay().evaluate(_entry_context(HOUR_1.replace(tzinfo=None)))

    assert decision.eligible is True
    assert decision.reason == "unconditional"


@pytest.mark.parametrize(
    ("archetype", "forecast_bps", "eligible"),
    [
        ("upside_capture", 5.0, False),
        ("upside_capture", 6.0, True),
        ("dip_accumulator", -5.0, False),
        ("dip_accumulator", -6.0, True),
        ("fee_box", -6.0, False),
        ("fee_box", -5.0, True),
        ("fee_box", 0.0, True),
        ("fee_box", 5.0, True),
        ("fee_box", 6.0, False),
    ],
)
def test_forecast_agreement_thresholds(
    archetype: DirectionalArchetype,
    forecast_bps: float,
    eligible: bool,
) -> None:
    overlay = ForecastAgreementOverlay(archetype, _timeline(forecast_bps))

    decision = overlay.evaluate(_entry_context(HOUR_1))

    assert decision.eligible is eligible
    assert decision.reason == ("agreement" if eligible else "disagreement")


def test_zero_threshold_is_a_valid_degenerate_band() -> None:
    upside = ForecastAgreementOverlay("upside_capture", _timeline(0.0), threshold_bps=0.0)
    fee_box = ForecastAgreementOverlay("fee_box", _timeline(0.0), threshold_bps=0.0)

    assert upside.evaluate(_entry_context(HOUR_1)).eligible is False
    assert fee_box.evaluate(_entry_context(HOUR_1)).eligible is True


def test_timeline_uses_latest_origin_at_or_before_decision() -> None:
    first = _point(origin_time=HOUR_1, forecast_bps=1.0)
    second = _point(origin_time=HOUR_2, forecast_bps=2.0)
    timeline = ForecastTimeline((first, second))

    assert timeline.as_of(HOUR_1) is first
    assert timeline.as_of(HOUR_2 - timedelta(microseconds=1)) is first
    assert timeline.as_of(HOUR_2) is second


def test_timeline_rejects_missing_stale_and_gapped_forecasts() -> None:
    first = _point(origin_time=HOUR_1)
    third = _point(origin_time=HOUR_3)
    timeline = ForecastTimeline((first, third))

    with pytest.raises(CrossPoolContractError, match="no forecast at or before"):
        timeline.as_of(HOUR_0)
    with pytest.raises(CrossPoolContractError, match="forecast is stale"):
        timeline.as_of(HOUR_2)


def test_timeline_accepts_last_instant_before_expiry() -> None:
    point = _point(origin_time=HOUR_1)

    assert ForecastTimeline((point,)).as_of(HOUR_2 - timedelta(microseconds=1)) is point


@pytest.mark.parametrize(
    ("field_name", "bad_time"),
    [
        ("origin_time", HOUR_1.replace(tzinfo=None)),
        ("origin_time", HOUR_1.astimezone(timezone(timedelta(hours=1)))),
        ("refit_time", HOUR_0.replace(tzinfo=None)),
        ("refit_time", HOUR_0.astimezone(timezone(timedelta(hours=-1)))),
    ],
)
def test_forecast_point_rejects_naive_or_non_utc_times(
    field_name: str,
    bad_time: datetime,
) -> None:
    with pytest.raises(CrossPoolContractError, match=f"{field_name} must be UTC-aware"):
        if field_name == "origin_time":
            _point(origin_time=bad_time)
        else:
            _point(refit_time=bad_time)


@pytest.mark.parametrize(
    "origin_time",
    [
        HOUR_1 + timedelta(minutes=1),
        HOUR_1 + timedelta(seconds=1),
        HOUR_1 + timedelta(microseconds=1),
    ],
)
def test_forecast_point_rejects_non_hour_aligned_origins(origin_time: datetime) -> None:
    with pytest.raises(CrossPoolContractError, match="origin_time must be hour-aligned"):
        _point(origin_time=origin_time)


def test_forecast_point_accepts_refit_at_origin() -> None:
    point = _point(origin_time=HOUR_1, refit_time=HOUR_1)

    assert point.refit_time == point.origin_time


def test_forecast_point_rejects_refit_after_origin() -> None:
    with pytest.raises(CrossPoolContractError, match="refit_time must not follow"):
        _point(origin_time=HOUR_1, refit_time=HOUR_1 + timedelta(microseconds=1))


@pytest.mark.parametrize("horizon", [timedelta(minutes=15), timedelta(hours=4)])
def test_forecast_point_requires_one_hour_horizon(horizon: timedelta) -> None:
    with pytest.raises(CrossPoolContractError, match="horizon must equal one hour"):
        _point(horizon=horizon)


@pytest.mark.parametrize("forecast_bps", [float("nan"), float("inf"), float("-inf")])
def test_forecast_point_rejects_nonfinite_forecasts(forecast_bps: float) -> None:
    with pytest.raises(CrossPoolContractError, match="forecast_bps must be finite"):
        _point(forecast_bps=forecast_bps)


@pytest.mark.parametrize(
    ("points", "match"),
    [
        ((), "at least one point"),
        ((_point(), _point()), "strictly increasing"),
        (
            (_point(origin_time=HOUR_2), _point(origin_time=HOUR_1)),
            "strictly increasing",
        ),
    ],
)
def test_timeline_requires_nonempty_strictly_increasing_origins(
    points: tuple[ForecastPoint, ...],
    match: str,
) -> None:
    with pytest.raises(CrossPoolContractError, match=match):
        ForecastTimeline(points)


@pytest.mark.parametrize(
    "decision_time",
    [
        HOUR_1.replace(tzinfo=None),
        HOUR_1.astimezone(timezone(timedelta(hours=2))),
    ],
)
def test_timeline_rejects_naive_or_non_utc_decision_times(
    decision_time: datetime,
) -> None:
    with pytest.raises(CrossPoolContractError, match="decision_time must be UTC-aware"):
        _timeline().as_of(decision_time)


@pytest.mark.parametrize("threshold_bps", [-1.0, float("nan"), float("inf")])
def test_overlay_rejects_invalid_thresholds(threshold_bps: float) -> None:
    with pytest.raises(CrossPoolContractError, match="threshold_bps"):
        ForecastAgreementOverlay(
            "upside_capture",
            _timeline(),
            threshold_bps=threshold_bps,
        )


def test_overlay_rejects_unknown_archetype_at_runtime() -> None:
    with pytest.raises(CrossPoolContractError, match="unsupported routed_archetype"):
        ForecastAgreementOverlay(
            cast(DirectionalArchetype, "momentum"),
            _timeline(),
        )


@pytest.mark.parametrize("decision_time", [HOUR_0, HOUR_2])
def test_overlay_propagates_missing_and_stale_forecast_errors(
    decision_time: datetime,
) -> None:
    overlay = ForecastAgreementOverlay("upside_capture", _timeline())

    with pytest.raises(CrossPoolContractError):
        overlay.evaluate(_entry_context(decision_time))
