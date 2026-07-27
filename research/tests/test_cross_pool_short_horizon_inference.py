from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from inspect import signature
from typing import cast

import numpy as np
import pytest
from numpy.typing import NDArray

from research.cross_pool.contracts import utc_day_from_timestamp_ms
from research.cross_pool.short_horizon import (
    SHORT_HORIZONS_MS,
    ShortHorizonResponse,
    ShortHorizonStudy,
)

DAY_MS = 86_400_000
BASE_TIMESTAMP_MS = int(datetime(2026, 1, 2, 12, tzinfo=UTC).timestamp() * 1_000)


@dataclass(frozen=True)
class _ShockSpec:
    day_index: int
    intraday_index: int
    first_update_delay_ms: int | None
    first_update_response_bps: float
    same_timestamp: bool = False


def test_inference_uses_one_frozen_pcg64_draw_matrix_for_all_horizons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    captured: list[NDArray[np.int64]] = []
    original = inference._draw_day_indices

    def capture_draws(*, day_count: int, resamples: int, seed: int) -> NDArray[np.int64]:
        draws = original(day_count=day_count, resamples=resamples, seed=seed)
        captured.append(draws.copy())
        return draws

    monkeypatch.setattr(inference, "_draw_day_indices", capture_draws)
    result = inference._infer_short_horizon(
        _study(tuple(_ShockSpec(day, 0, 10_000, float(day + 1)) for day in range(4))),
        resamples=2,
    )

    assert inference.SHORT_HORIZON_BOOTSTRAP_RESAMPLES == 2_000
    assert inference.SHORT_HORIZON_BOOTSTRAP_SEED == 20_260_715
    assert inference.SHORT_HORIZON_BOOTSTRAP_CONFIDENCE_LEVEL == 0.95
    assert len(captured) == 1
    assert captured[0].tolist() == [[2, 0, 2, 0], [1, 2, 3, 1]]
    assert tuple(summary.horizon_ms for summary in result.summaries) == SHORT_HORIZONS_MS


def test_pointwise_intervals_are_event_weighted_and_use_nearest_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    study = _study(
        (
            _ShockSpec(0, 0, 10_000, 0.0),
            _ShockSpec(1, 0, 10_000, 10.0),
            _ShockSpec(1, 1, 10_000, 10.0),
            _ShockSpec(1, 2, 10_000, 10.0),
        )
    )

    def fixed_draws(*, day_count: int, resamples: int, seed: int) -> NDArray[np.int64]:
        assert (day_count, resamples, seed) == (2, 4, 20_260_715)
        return np.asarray(((0, 0), (1, 1), (0, 1), (1, 0)), dtype=np.int64)

    monkeypatch.setattr(inference, "_draw_day_indices", fixed_draws)
    result = inference._infer_short_horizon(study, resamples=4)
    summary = result.summaries[0]

    assert summary.eligible_event_count == 4
    assert summary.shock_day_count == 2
    assert summary.unconditional_mean_response_bps.point == 7.5
    assert summary.unconditional_mean_response_bps.lower == 0.0
    assert summary.unconditional_mean_response_bps.upper == 10.0
    assert summary.unconditional_median_response_bps.point == 10.0
    interval = inference._pointwise_interval(
        point=12.5,
        samples=np.arange(2_000, dtype=np.float64),
        confidence_level=0.95,
    )
    assert (interval.lower, interval.upper) == (49.0, 1_949.0)


def test_summary_counts_censoring_latency_age_and_fee_metrics() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    result = inference._infer_short_horizon(
        _study(
            (
                _ShockSpec(0, 0, 30_000, 4.0),
                _ShockSpec(1, 0, None, 0.0),
            )
        ),
        resamples=8,
    )
    summary = result.summaries[0]

    assert summary.update_count == 1
    assert summary.right_censored_count == 1
    assert summary.update_incidence == 0.5
    assert summary.observed_update_day_count == 1
    assert summary.conditional_first_update_mean_response_bps == 4.0
    assert summary.conditional_first_update_median_response_bps == 4.0
    assert summary.conditional_first_update_median_delay_ms == 30_000.0
    assert summary.conditional_first_update_p95_delay_ms == 30_000.0
    assert summary.target_start_median_age_ms == 1_000.0
    assert summary.target_start_p95_age_ms == 1_000.0
    assert summary.target_end_median_age_ms == 15_500.0
    assert summary.target_end_p95_age_ms == 31_000.0
    assert math.isfinite(summary.mean_fee_gap_start_bps)
    assert math.isfinite(summary.mean_fee_gap_end_bps)
    assert math.isfinite(summary.mean_fee_gap_closure_bps)
    assert 0.0 <= summary.positive_fee_gap_start_share <= 1.0
    assert 0.0 <= summary.positive_fee_gap_end_share <= 1.0


def test_observed_zero_update_is_not_imputed_into_the_censored_cohort() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    result = inference._infer_short_horizon(
        _study(
            (
                _ShockSpec(0, 0, 10_000, 0.0),
                _ShockSpec(1, 0, 10_000, 4.0),
                _ShockSpec(2, 0, None, 0.0),
            )
        ),
        resamples=8,
    )
    summary = result.summaries[0]

    assert summary.update_count == 2
    assert summary.right_censored_count == 1
    assert summary.conditional_first_update_mean_response_bps == 2.0
    assert summary.conditional_first_update_median_response_bps == 2.0


def test_all_three_max_z_families_are_available_with_complete_support() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    specs = tuple(_ShockSpec(day, 0, 10_000, float(day + 1)) for day in range(12)) + tuple(
        _ShockSpec(day, 0, None, 0.0) for day in range(12, 14)
    )

    result = inference.infer_short_horizon(_study(specs))

    assert tuple(family.metric for family in result.simultaneous_bands) == (
        "unconditional_mean_response_bps",
        "update_incidence",
        "conditional_first_update_mean_response_bps",
    )
    for family in result.simultaneous_bands:
        assert family.status == "available"
        assert family.reason is None
        assert family.valid_joint_resamples == 2_000
        assert family.critical_value is not None
        assert family.critical_value > 0.0
        assert tuple(point.horizon_ms for point in family.points) == SHORT_HORIZONS_MS
    incidence = result.simultaneous_bands[1]
    assert all(0.0 <= point.lower <= point.upper <= 1.0 for point in incidence.points)


def test_max_z_uses_ddof_one_and_the_one_sided_nearest_rank_index() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    seed_result = inference._infer_short_horizon(
        _study(tuple(_ShockSpec(day, 0, 10_000, float(day + 1)) for day in range(12))),
        resamples=4,
    )
    standardized = np.arange(2_000, dtype=np.float64) - 999.5
    bootstraps = []
    for horizon_index, summary in enumerate(seed_result.summaries, start=1):
        point = summary.unconditional_mean_response_bps.point
        samples = tuple(float(point + horizon_index * value) for value in standardized)
        bootstraps.append(
            inference._HorizonBootstrap(
                mean_response_bps=samples,
                median_response_bps=samples,
                update_incidence=tuple(0.5 for _ in samples),
                conditional_mean_response_bps=samples,
            )
        )

    family = inference._simultaneous_band(
        "unconditional_mean_response_bps",
        summaries=seed_result.summaries,
        bootstraps=tuple(bootstraps),
        confidence_level=0.95,
    )
    expected_standard_deviation = float(np.std(standardized, ddof=1))
    expected_max_statistics = np.abs(standardized) / expected_standard_deviation
    expected_critical = float(np.sort(expected_max_statistics)[1_899])

    assert inference._one_sided_nearest_rank_index(2_000, 0.95) == 1_899
    assert inference._one_sided_nearest_rank_index(1_900, 0.95) == 1_804
    assert family.status == "available"
    assert family.critical_value == pytest.approx(expected_critical)
    for horizon_index, point in enumerate(family.points, start=1):
        assert point.bootstrap_standard_deviation == pytest.approx(
            horizon_index * expected_standard_deviation
        )
        assert point.upper - point.point == pytest.approx(
            expected_critical * point.bootstrap_standard_deviation
        )


def test_nonfinite_bootstrap_standard_deviation_has_a_stable_reason() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    seed_result = inference._infer_short_horizon(
        _study(tuple(_ShockSpec(day, 0, 10_000, float(day + 1)) for day in range(12))),
        resamples=4,
    )
    extremes = tuple(1e308 if index % 2 == 0 else -1e308 for index in range(2_000))
    bootstraps = tuple(
        inference._HorizonBootstrap(
            mean_response_bps=extremes,
            median_response_bps=extremes,
            update_incidence=tuple(0.5 for _ in extremes),
            conditional_mean_response_bps=extremes,
        )
        for _ in SHORT_HORIZONS_MS
    )

    family = inference._simultaneous_band(
        "unconditional_mean_response_bps",
        summaries=seed_result.summaries,
        bootstraps=bootstraps,
        confidence_level=0.95,
    )

    assert family.status == "unavailable"
    assert family.reason == "nonfinite_bootstrap_standard_deviation"
    assert family.valid_joint_resamples == 2_000


def test_update_incidence_simultaneous_band_clips_both_probability_bounds() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    specs = (_ShockSpec(0, 0, 10_000, 1.0),) + tuple(
        _ShockSpec(day, 0, None, 0.0) for day in range(1, 20)
    )
    seed_result = inference._infer_short_horizon(_study(specs), resamples=4)
    incidence_samples = tuple(float(index % 2) for index in range(2_000))
    bootstraps = tuple(
        inference._HorizonBootstrap(
            mean_response_bps=incidence_samples,
            median_response_bps=incidence_samples,
            update_incidence=incidence_samples,
            conditional_mean_response_bps=tuple(None for _ in incidence_samples),
        )
        for _ in SHORT_HORIZONS_MS
    )

    family = inference._simultaneous_band(
        "update_incidence",
        summaries=seed_result.summaries,
        bootstraps=bootstraps,
        confidence_level=0.95,
    )

    assert family.status == "available"
    assert all(point.lower == 0.0 and point.upper == 1.0 for point in family.points)


def test_sparse_conditional_days_make_only_that_family_unavailable() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    specs = tuple(_ShockSpec(day, 0, 10_000, float(day + 1)) for day in range(9)) + tuple(
        _ShockSpec(day, 0, None, 0.0) for day in range(9, 12)
    )

    result = inference.infer_short_horizon(_study(specs))
    conditional = result.simultaneous_bands[2]

    assert result.simultaneous_bands[0].status == "available"
    assert result.simultaneous_bands[1].status == "available"
    assert conditional.status == "unavailable"
    assert conditional.reason == "insufficient_observed_update_days"
    assert conditional.points == ()
    assert conditional.critical_value is None


def test_observed_day_support_reason_precedes_joint_draw_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    specs = tuple(_ShockSpec(day, 0, 10_000, float(day + 1)) for day in range(9)) + tuple(
        _ShockSpec(day, 0, None, 0.0) for day in range(9, 12)
    )

    def all_censored_draws(*, day_count: int, resamples: int, seed: int) -> NDArray[np.int64]:
        assert (day_count, resamples, seed) == (12, 2_000, 20_260_715)
        return np.full((resamples, day_count), 11, dtype=np.int64)

    monkeypatch.setattr(inference, "_draw_day_indices", all_censored_draws)
    conditional = inference.infer_short_horizon(_study(specs)).simultaneous_bands[2]

    assert conditional.reason == "insufficient_observed_update_days"
    assert conditional.valid_joint_resamples == 0


def test_zero_bootstrap_variance_has_a_stable_unavailable_reason() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    result = inference.infer_short_horizon(
        _study(tuple(_ShockSpec(day, 0, 10_000, 1.0) for day in range(12)))
    )

    for family in result.simultaneous_bands:
        assert family.status == "unavailable"
        assert family.reason == "zero_bootstrap_standard_deviation"
        assert family.points == ()


@pytest.mark.parametrize(
    ("invalid_draw_count", "expected_status", "expected_reason", "expected_valid"),
    (
        (100, "available", None, 1_900),
        (
            101,
            "unavailable",
            "insufficient_valid_joint_resamples",
            1_899,
        ),
    ),
)
def test_conditional_joint_resample_threshold_is_inclusive(
    monkeypatch: pytest.MonkeyPatch,
    invalid_draw_count: int,
    expected_status: str,
    expected_reason: str | None,
    expected_valid: int,
) -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    specs = tuple(_ShockSpec(day, 0, 10_000, float(day + 1)) for day in range(11)) + (
        _ShockSpec(11, 0, None, 0.0),
    )

    def threshold_draws(*, day_count: int, resamples: int, seed: int) -> NDArray[np.int64]:
        assert (day_count, resamples, seed) == (12, 2_000, 20_260_715)
        draws = np.empty((resamples, day_count), dtype=np.int64)
        draws[:invalid_draw_count] = 11
        for draw_index in range(invalid_draw_count, resamples):
            draws[draw_index] = (draw_index - invalid_draw_count) % 11
        return draws

    monkeypatch.setattr(inference, "_draw_day_indices", threshold_draws)
    result = inference.infer_short_horizon(_study(specs))
    conditional = result.simultaneous_bands[2]

    assert conditional.status == expected_status
    assert conditional.reason == expected_reason
    assert conditional.valid_joint_resamples == expected_valid


def test_same_timestamp_sensitivity_removes_complete_shock_families() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    study = _study(
        (
            _ShockSpec(0, 0, 10_000, 2.0, same_timestamp=True),
            _ShockSpec(1, 0, 10_000, 4.0),
        )
    )

    primary = inference._infer_short_horizon(study, resamples=4)
    sensitivity = inference._infer_short_horizon(
        study,
        exclude_same_timestamp=True,
        resamples=4,
    )

    assert primary.cohort == "primary"
    assert primary.eligible_event_count == 2
    assert sensitivity.cohort == "exclude_same_timestamp"
    assert sensitivity.eligible_event_count == 1
    assert all(summary.eligible_event_count == 1 for summary in sensitivity.summaries)


def test_same_timestamp_sensitivity_fails_closed_when_no_family_remains() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    study = _study((_ShockSpec(0, 0, 10_000, 2.0, same_timestamp=True),))

    with pytest.raises(inference.CrossPoolContractError, match="complete shock family"):
        inference.infer_short_horizon(study, exclude_same_timestamp=True)


def test_bootstrap_groups_cross_midnight_updates_by_shock_day() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    near_midnight_ms = int(datetime(2026, 1, 1, 23, 59, 50, tzinfo=UTC).timestamp() * 1_000)
    study = _study(
        (
            _ShockSpec(0, 0, 30_000, 1.0),
            _ShockSpec(1, -23, 30_000, 2.0),
        ),
        base_timestamp_ms=near_midnight_ms,
    )

    result = inference._infer_short_horizon(study, resamples=4)

    assert {row.shock_day_utc for row in study.responses} == {
        datetime(2026, 1, 1, tzinfo=UTC).date(),
        datetime(2026, 1, 2, tzinfo=UTC).date(),
    }
    assert result.shock_day_count == 2


def test_nested_horizon_contract_rejects_missing_reversed_and_divergent_updates() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    study = _study((_ShockSpec(0, 0, 10_000, 2.0),))

    with pytest.raises(inference.CrossPoolContractError, match="complete nested"):
        inference._validated_rows(
            study.responses[:-1],
            expected_direction=study.direction,
        )

    reversed_status = list(study.responses)
    reversed_status[1] = replace(
        reversed_status[1],
        first_update_status="right_censored",
        first_update_timestamp_ms=None,
        first_update_delay_ms=None,
        first_update_raw_mid=None,
        first_update_response_bps=None,
        first_update_direction_agrees=None,
    )
    with pytest.raises(inference.CrossPoolContractError, match="nested across"):
        inference._validated_rows(
            tuple(reversed_status),
            expected_direction=study.direction,
        )

    divergent_update = list(study.responses)
    last = divergent_update[-1]
    assert last.first_update_timestamp_ms is not None
    assert last.first_update_delay_ms is not None
    divergent_update[-1] = replace(
        last,
        first_update_timestamp_ms=last.first_update_timestamp_ms + 1,
        first_update_delay_ms=last.first_update_delay_ms + 1,
    )
    with pytest.raises(inference.CrossPoolContractError, match="update binding"):
        inference._validated_rows(
            tuple(divergent_update),
            expected_direction=study.direction,
        )


def test_inference_contract_rejects_reordered_horizons_and_boolean_config() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")
    study = _study((_ShockSpec(0, 0, 10_000, 2.0),))
    result = inference._infer_short_horizon(study, resamples=4)

    with pytest.raises(inference.CrossPoolContractError, match="canonical"):
        replace(result, summaries=tuple(reversed(result.summaries)))
    with pytest.raises(inference.CrossPoolContractError, match="positive integer"):
        inference._infer_short_horizon(study, resamples=cast(int, True))


def test_public_inference_entry_point_freezes_bootstrap_settings() -> None:
    inference = import_module("research.cross_pool.short_horizon_inference")

    assert tuple(signature(inference.infer_short_horizon).parameters) == (
        "study",
        "exclude_same_timestamp",
    )


def _study(
    specs: tuple[_ShockSpec, ...],
    *,
    base_timestamp_ms: int = BASE_TIMESTAMP_MS,
) -> ShortHorizonStudy:
    responses: list[ShortHorizonResponse] = []
    for spec in specs:
        shock_timestamp_ms = (
            base_timestamp_ms + spec.day_index * DAY_MS + spec.intraday_index * 3_600_000
        )
        for horizon_ms in SHORT_HORIZONS_MS:
            observed = (
                spec.first_update_delay_ms is not None and spec.first_update_delay_ms <= horizon_ms
            )
            response_bps = spec.first_update_response_bps if observed else 0.0
            target_start_raw_mid = Decimal("1")
            target_end_raw_mid = Decimal("1") + Decimal(str(response_bps)) / Decimal(10_000)
            source_raw_mid = Decimal("1.001")
            multiplier = Decimal("0.999")
            source_bid, source_ask = _quotes(source_raw_mid, multiplier)
            target_start_bid, target_start_ask = _quotes(
                target_start_raw_mid,
                multiplier,
            )
            target_end_bid, target_end_ask = _quotes(target_end_raw_mid, multiplier)
            target_start_timestamp_ms = (
                shock_timestamp_ms if spec.same_timestamp else shock_timestamp_ms - 1_000
            )
            target_end_timestamp_ms = (
                shock_timestamp_ms + cast(int, spec.first_update_delay_ms)
                if observed
                else target_start_timestamp_ms
            )
            endpoint_ms = shock_timestamp_ms + horizon_ms
            fee_gap_start_bps = 10_000 * math.log(float(source_bid / target_start_ask))
            fee_gap_end_bps = 10_000 * math.log(float(source_bid / target_end_ask))
            first_update_raw_mid = target_end_raw_mid if observed else None
            responses.append(
                ShortHorizonResponse(
                    direction="bsc_to_base",
                    source_pool="uni-bsc",
                    target_pool="uni-base",
                    shock_timestamp_ms=shock_timestamp_ms,
                    shock_day_utc=utc_day_from_timestamp_ms(shock_timestamp_ms),
                    horizon_ms=horizon_ms,
                    source_move_bps=10.0,
                    source_move_sign=1,
                    source_shock_raw_mid=source_raw_mid,
                    source_shock_bid=source_bid,
                    source_shock_ask=source_ask,
                    source_end_timestamp_ms=endpoint_ms,
                    source_end_age_ms=0,
                    source_end_raw_mid=source_raw_mid,
                    source_end_bid=source_bid,
                    source_end_ask=source_ask,
                    target_start_timestamp_ms=target_start_timestamp_ms,
                    target_start_age_ms=shock_timestamp_ms - target_start_timestamp_ms,
                    target_start_raw_mid=target_start_raw_mid,
                    target_start_bid=target_start_bid,
                    target_start_ask=target_start_ask,
                    target_end_timestamp_ms=target_end_timestamp_ms,
                    target_end_age_ms=endpoint_ms - target_end_timestamp_ms,
                    target_end_raw_mid=target_end_raw_mid,
                    target_end_bid=target_end_bid,
                    target_end_ask=target_end_ask,
                    target_response_bps=response_bps,
                    zero_response=response_bps == 0.0,
                    direction_agrees=response_bps > 0.0,
                    target_same_timestamp=spec.same_timestamp,
                    first_update_status="observed" if observed else "right_censored",
                    first_update_timestamp_ms=(target_end_timestamp_ms if observed else None),
                    first_update_delay_ms=(spec.first_update_delay_ms if observed else None),
                    first_update_raw_mid=first_update_raw_mid,
                    first_update_response_bps=response_bps if observed else None,
                    first_update_direction_agrees=response_bps > 0.0 if observed else None,
                    fee_gap_start_bps=fee_gap_start_bps,
                    fee_gap_end_bps=fee_gap_end_bps,
                    fee_gap_start_positive_bps=max(fee_gap_start_bps, 0.0),
                    fee_gap_end_positive_bps=max(fee_gap_end_bps, 0.0),
                    fee_gap_closure_bps=fee_gap_start_bps - fee_gap_end_bps,
                )
            )
    canonical = tuple(sorted(responses, key=lambda row: (row.shock_timestamp_ms, row.horizon_ms)))
    return ShortHorizonStudy(
        direction="bsc_to_base",
        detected_shock_count=len(specs),
        eligible_shock_count=len(specs),
        responses=canonical,
        exclusions=(),
    )


def _quotes(raw_mid: Decimal, multiplier: Decimal) -> tuple[Decimal, Decimal]:
    return raw_mid * multiplier, raw_mid / multiplier
