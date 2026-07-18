from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, cast

import pytest

from research.cross_pool.contracts import (
    ConfidenceInterval,
    CrossPoolContractError,
    Direction,
    EventExclusion,
    EventResponse,
    EventStudyResult,
    EventSummary,
    PoolEvent,
    PoolName,
    ShockConfig,
    ShockEvent,
)
from research.cross_pool.event_study import (
    bootstrap_event_responses,
    detect_shocks,
    measure_event_responses,
)

MINUTE_MS = 60_000
DAY_MS = 86_400_000
BASE_TIMESTAMP_MS = int(datetime(2026, 1, 2, 12, tzinfo=UTC).timestamp() * 1_000)


def test_shock_cluster_retains_first_crossing_only() -> None:
    source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=5, raw_mid="1.0003"),
        _event("uni-bsc", minute=10, raw_mid="1.0006"),
        _event("uni-bsc", minute=15, raw_mid="1.0006"),
        _event("uni-bsc", minute=20, raw_mid="1.0010"),
        _event("uni-bsc", minute=29, raw_mid="1.0014"),
    )

    shocks = detect_shocks(source, ShockConfig())

    assert [shock.shock_timestamp_ms for shock in shocks] == [
        BASE_TIMESTAMP_MS + 15 * MINUTE_MS
    ]
    shock = shocks[0]
    assert shock.source_start_timestamp_ms == BASE_TIMESTAMP_MS
    assert shock.source_move_bps == pytest.approx(6.0)
    assert shock.source_move_sign == 1
    assert shock.direction == "bsc_to_base"
    assert shock.source_pool == "uni-bsc"
    assert shock.target_pool == "uni-base"


def test_exact_threshold_and_refractory_boundary_are_inclusive() -> None:
    source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=15, raw_mid="1.0005"),
        _event("uni-bsc", minute=29, raw_mid="1.0009"),
        _event("uni-bsc", minute=30, raw_mid="1.00100025"),
    )

    shocks = detect_shocks(source, ShockConfig())

    assert [shock.shock_timestamp_ms for shock in shocks] == [
        BASE_TIMESTAMP_MS + 15 * MINUTE_MS,
        BASE_TIMESTAMP_MS + 30 * MINUTE_MS,
    ]
    assert [shock.source_move_bps for shock in shocks] == pytest.approx([5.0, 5.0])


@pytest.mark.parametrize(
    ("source_pool", "direction", "target_pool"),
    (
        ("uni-bsc", "bsc_to_base", "uni-base"),
        ("uni-base", "base_to_bsc", "uni-bsc"),
    ),
)
def test_shock_direction_is_derived_from_the_source_pool(
    source_pool: PoolName,
    direction: str,
    target_pool: PoolName,
) -> None:
    shocks = detect_shocks(
        (
            _event(source_pool, minute=0, raw_mid="1"),
            _event(source_pool, minute=15, raw_mid="1.0005"),
        ),
        ShockConfig(),
    )

    assert len(shocks) == 1
    assert shocks[0].direction == direction
    assert shocks[0].target_pool == target_pool


def test_negative_cross_midnight_shock_uses_the_crossing_utc_day() -> None:
    start_ms = int(datetime(2026, 1, 2, 23, 50, tzinfo=UTC).timestamp() * 1_000)
    source = (
        _event("uni-base", timestamp_ms=start_ms, raw_mid="1"),
        _event(
            "uni-base",
            timestamp_ms=start_ms + 15 * MINUTE_MS,
            raw_mid="0.9995",
        ),
    )

    shock = detect_shocks(source, ShockConfig())[0]

    assert shock.source_move_bps == pytest.approx(-5.0)
    assert shock.source_move_sign == -1
    assert shock.shock_day_utc == date(2026, 1, 3)


def test_event_response_uses_target_as_of_not_next_swap() -> None:
    shock = _shock(minute=20)
    target = (
        _event("uni-base", minute=0, raw_mid="1"),
        _event("uni-base", minute=18, raw_mid="1.001"),
        _event("uni-base", minute=25, raw_mid="1.002"),
        _event("uni-base", minute=34, raw_mid="1.003"),
        _event("uni-base", minute=36, raw_mid="1.004"),
    )

    result = measure_event_responses(
        (shock,),
        target,
        ShockConfig(response_horizons_ms=(15 * MINUTE_MS,)),
    )

    assert result.exclusions == ()
    assert len(result.responses) == 1
    response = result.responses[0]
    assert response.target_start_timestamp_ms == BASE_TIMESTAMP_MS + 18 * MINUTE_MS
    assert response.target_end_timestamp_ms == BASE_TIMESTAMP_MS + 34 * MINUTE_MS
    expected_bps = float(
        (Decimal("1.003") / Decimal("1.001") - Decimal(1)) * Decimal(10_000)
    )
    assert response.target_response_bps == pytest.approx(expected_bps)
    assert response.direction_agrees is True


def test_stale_target_state_is_a_valid_zero_response() -> None:
    shock = _shock(minute=20)
    target = (
        _event("uni-base", minute=10, raw_mid="1"),
        _event("uni-base", minute=50, raw_mid="1.1"),
    )

    result = measure_event_responses(
        (shock,),
        target,
        ShockConfig(response_horizons_ms=(15 * MINUTE_MS,)),
    )

    assert result.exclusions == ()
    response = result.responses[0]
    assert response.target_start_timestamp_ms == BASE_TIMESTAMP_MS + 10 * MINUTE_MS
    assert response.target_end_timestamp_ms == response.target_start_timestamp_ms
    assert response.target_response_bps == 0.0
    assert response.direction_agrees is False


def test_missing_target_start_precedes_missing_end_exclusion() -> None:
    shock = _shock(minute=20)

    result = measure_event_responses(
        (shock,),
        (_event("uni-base", minute=21, raw_mid="1"),),
        ShockConfig(response_horizons_ms=(15 * MINUTE_MS,)),
    )

    assert result.responses == ()
    assert len(result.exclusions) == 1
    assert result.exclusions[0].reason == "missing_target_start_state"


def test_missing_target_end_is_an_explicit_exclusion() -> None:
    shock = _shock(minute=20)
    target = (
        _event("uni-base", minute=10, raw_mid="1"),
        _event("uni-base", minute=34, raw_mid="1.001"),
    )

    result = measure_event_responses(
        (shock,),
        target,
        ShockConfig(response_horizons_ms=(15 * MINUTE_MS,)),
    )

    assert result.responses == ()
    assert len(result.exclusions) == 1
    assert result.exclusions[0].reason == "missing_target_end_state"


def test_exact_target_sample_boundaries_are_observable() -> None:
    shock = _shock(minute=20)
    target = (
        _event("uni-base", minute=20, raw_mid="1"),
        _event("uni-base", minute=35, raw_mid="0.999"),
    )

    result = measure_event_responses(
        (shock,),
        target,
        ShockConfig(response_horizons_ms=(15 * MINUTE_MS,)),
    )

    assert result.exclusions == ()
    response = result.responses[0]
    assert response.target_start_timestamp_ms == shock.shock_timestamp_ms
    assert response.target_end_timestamp_ms == shock.shock_timestamp_ms + 15 * MINUTE_MS
    assert response.target_response_bps == pytest.approx(-10.0)
    assert response.direction_agrees is False


def test_reverse_direction_response_uses_the_bsc_target_stream() -> None:
    shock = _shock(minute=20, direction="base_to_bsc")
    target = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=35, raw_mid="1.001"),
    )

    result = measure_event_responses(
        (shock,),
        target,
        ShockConfig(response_horizons_ms=(15 * MINUTE_MS,)),
    )

    assert result.exclusions == ()
    response = result.responses[0]
    assert response.direction == "base_to_bsc"
    assert response.source_pool == "uni-base"
    assert response.target_pool == "uni-bsc"
    assert response.target_response_bps == pytest.approx(10.0)
    assert response.direction_agrees is True


@pytest.mark.parametrize(
    "config_kwargs",
    (
        {"lookback_ms": True},
        {"cluster_ms": 0},
        {"threshold_bps": float("nan")},
        {"response_horizons_ms": ()},
        {"response_horizons_ms": (MINUTE_MS, MINUTE_MS)},
        {"response_horizons_ms": [MINUTE_MS]},
    ),
)
def test_shock_config_rejects_noncanonical_values(
    config_kwargs: dict[str, object],
) -> None:
    with pytest.raises(CrossPoolContractError):
        ShockConfig(**config_kwargs)  # type: ignore[arg-type]


def test_sparse_source_uses_the_last_state_as_of_the_lookback() -> None:
    source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=5, raw_mid="1.0002"),
        _event("uni-bsc", minute=20, raw_mid="1.0007"),
    )

    assert detect_shocks(source, ShockConfig()) == ()


@pytest.mark.parametrize(
    "case",
    ("mixed_pool", "duplicate_timestamp", "zero_mid"),
)
def test_shock_detection_rejects_malformed_source_streams(
    case: str,
) -> None:
    if case == "mixed_pool":
        source = (
            _event("uni-bsc", minute=0, raw_mid="1"),
            _event("uni-base", minute=15, raw_mid="1.001"),
        )
    elif case == "duplicate_timestamp":
        source = (
            _event("uni-bsc", minute=15, raw_mid="1"),
            _event("uni-bsc", minute=15, raw_mid="1.001"),
        )
    else:
        source = (
            _event("uni-bsc", minute=0, raw_mid="1"),
            replace(_event("uni-bsc", minute=15, raw_mid="1"), raw_mid=Decimal(0)),
        )
    with pytest.raises(CrossPoolContractError):
        detect_shocks(source, ShockConfig())


def test_response_measurement_rejects_a_mismatched_target_pool() -> None:
    target = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=40, raw_mid="1.001"),
    )

    with pytest.raises(CrossPoolContractError, match="target stream pool"):
        measure_event_responses(
            (_shock(minute=20),),
            target,
            ShockConfig(response_horizons_ms=(15 * MINUTE_MS,)),
        )


def test_response_measurement_rejects_unsorted_shocks_and_target_events() -> None:
    config = ShockConfig(response_horizons_ms=(15 * MINUTE_MS,))
    target = (
        _event("uni-base", minute=0, raw_mid="1"),
        _event("uni-base", minute=60, raw_mid="1.001"),
    )
    with pytest.raises(CrossPoolContractError, match="shock timestamps"):
        measure_event_responses(
            (_shock(minute=40), _shock(minute=20)),
            target,
            config,
        )
    with pytest.raises(CrossPoolContractError, match="target stream timestamps"):
        measure_event_responses(
            (_shock(minute=20),),
            (target[1], target[0]),
            config,
        )


def test_event_response_contract_derives_endpoint_and_agreement_fields() -> None:
    result = measure_event_responses(
        (_shock(minute=20),),
        (
            _event("uni-base", minute=0, raw_mid="1"),
            _event("uni-base", minute=35, raw_mid="1.001"),
        ),
        ShockConfig(response_horizons_ms=(15 * MINUTE_MS,)),
    )
    response = result.responses[0]

    with pytest.raises(CrossPoolContractError, match="target start"):
        replace(
            response,
            target_start_timestamp_ms=response.shock_timestamp_ms + 1,
        )
    with pytest.raises(CrossPoolContractError, match="direction agreement"):
        replace(response, direction_agrees=not response.direction_agrees)
    with pytest.raises(CrossPoolContractError, match="source move"):
        replace(response, source_move_bps=0.0)


def test_event_contracts_reject_noncanonical_sign_fields() -> None:
    shock = _shock(minute=20)
    response = _response(day=0, minute=10, response_bps=1.0, agrees=True)

    with pytest.raises(CrossPoolContractError, match="shock sign"):
        replace(shock, source_move_sign=cast("Literal[-1, 1]", True))
    with pytest.raises(CrossPoolContractError, match="shock sign"):
        replace(shock, source_move_sign=cast("Literal[-1, 1]", 1.0))
    with pytest.raises(CrossPoolContractError, match="direction agreement"):
        replace(response, direction_agrees=cast(bool, 1))


def test_event_study_result_rejects_duplicate_colliding_and_unsorted_keys() -> None:
    result = measure_event_responses(
        (_shock(minute=20), _shock(minute=40)),
        (
            _event("uni-base", minute=0, raw_mid="1"),
            _event("uni-base", minute=35, raw_mid="1.001"),
            _event("uni-base", minute=55, raw_mid="1.002"),
        ),
        ShockConfig(response_horizons_ms=(15 * MINUTE_MS,)),
    )
    first = result.responses[0]
    collision = EventExclusion(
        direction=first.direction,
        shock_timestamp_ms=first.shock_timestamp_ms,
        shock_day_utc=first.shock_day_utc,
        horizon_ms=first.horizon_ms,
        reason="missing_target_end_state",
    )

    with pytest.raises(CrossPoolContractError, match="unique"):
        EventStudyResult(responses=(first, first), exclusions=())
    with pytest.raises(CrossPoolContractError, match="both"):
        EventStudyResult(responses=(first,), exclusions=(collision,))
    with pytest.raises(CrossPoolContractError, match="canonical ordering"):
        EventStudyResult(
            responses=tuple(reversed(result.responses)),
            exclusions=(),
        )


def test_event_day_bootstrap_uses_one_paired_day_draw_for_all_statistics() -> None:
    responses = (
        _response(day=0, minute=10, response_bps=2.0, agrees=True),
        _response(day=0, minute=20, response_bps=6.0, agrees=False),
        _response(day=1, minute=10, response_bps=-2.0, agrees=True),
    )

    summaries = bootstrap_event_responses(
        responses,
        resamples=8,
        seed=7,
        confidence_level=0.5,
    )

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.direction == "bsc_to_base"
    assert summary.horizon_ms == 15 * MINUTE_MS
    assert summary.event_count == 3
    assert summary.event_day_count == 2
    assert summary.mean_response_bps == ConfidenceInterval(
        point=2.0,
        lower=-2.0,
        upper=2.0,
    )
    assert summary.median_response_bps == ConfidenceInterval(
        point=2.0,
        lower=-2.0,
        upper=2.0,
    )
    assert summary.direction_agreement == ConfidenceInterval(
        point=2.0 / 3.0,
        lower=2.0 / 3.0,
        upper=1.0,
    )


def test_bootstrap_is_input_order_invariant_and_group_stable() -> None:
    base_responses = (
        _response(
            direction="base_to_bsc",
            day=0,
            minute=10,
            response_bps=2.0,
            agrees=True,
        ),
        _response(
            direction="base_to_bsc",
            day=1,
            minute=10,
            response_bps=-2.0,
            agrees=False,
        ),
    )
    base_only = bootstrap_event_responses(
        base_responses,
        resamples=16,
        seed=19,
        confidence_level=0.75,
    )[0]
    reversed_input = bootstrap_event_responses(
        tuple(reversed(base_responses)),
        resamples=16,
        seed=19,
        confidence_level=0.75,
    )[0]
    with_preceding_group = bootstrap_event_responses(
        (_response(day=0, minute=5, response_bps=1.0, agrees=True),)
        + base_responses,
        resamples=16,
        seed=19,
        confidence_level=0.75,
    )

    assert reversed_input == base_only
    assert with_preceding_group[-1] == base_only


def test_event_summaries_use_canonical_direction_and_horizon_order() -> None:
    responses = (
        _response(
            direction="base_to_bsc",
            day=0,
            minute=10,
            response_bps=-2.0,
            agrees=True,
        ),
        _response(
            day=0,
            minute=20,
            horizon_ms=60 * MINUTE_MS,
            response_bps=3.0,
            agrees=True,
        ),
        _response(day=0, minute=10, response_bps=2.0, agrees=False),
    )

    summaries = bootstrap_event_responses(
        responses,
        resamples=4,
        seed=0,
        confidence_level=0.5,
    )

    assert [
        (summary.direction, summary.horizon_ms) for summary in summaries
    ] == [
        ("bsc_to_base", 15 * MINUTE_MS),
        ("bsc_to_base", 60 * MINUTE_MS),
        ("base_to_bsc", 15 * MINUTE_MS),
    ]


def test_bootstrap_groups_cross_midnight_responses_by_source_shock_day() -> None:
    responses = (
        _response(
            day=0,
            minute=11 * 60 + 25,
            horizon_ms=60 * MINUTE_MS,
            response_bps=2.0,
            agrees=True,
        ),
        _response(
            day=0,
            minute=11 * 60 + 45,
            horizon_ms=60 * MINUTE_MS,
            response_bps=4.0,
            agrees=True,
        ),
    )

    summary = bootstrap_event_responses(
        responses,
        resamples=4,
        seed=0,
        confidence_level=0.5,
    )[0]

    assert summary.event_day_count == 1
    assert summary.mean_response_bps == ConfidenceInterval(3.0, 3.0, 3.0)
    assert summary.median_response_bps == ConfidenceInterval(3.0, 3.0, 3.0)
    assert summary.direction_agreement == ConfidenceInterval(1.0, 1.0, 1.0)


def test_empty_event_bootstrap_has_no_synthetic_summary() -> None:
    assert bootstrap_event_responses(()) == ()


@pytest.mark.parametrize(
    ("resamples", "seed", "confidence_level"),
    (
        (0, 0, 0.95),
        (10, -1, 0.95),
        (10, 0, 0.0),
        (10, 0, 1.0),
        (10, 0, float("nan")),
    ),
)
def test_event_bootstrap_rejects_invalid_configuration(
    resamples: int,
    seed: int,
    confidence_level: float,
) -> None:
    with pytest.raises(CrossPoolContractError):
        bootstrap_event_responses(
            (_response(day=0, minute=10, response_bps=1.0, agrees=True),),
            resamples=resamples,
            seed=seed,
            confidence_level=confidence_level,
        )


def test_event_bootstrap_rejects_duplicate_response_keys() -> None:
    response = _response(day=0, minute=10, response_bps=1.0, agrees=True)

    with pytest.raises(CrossPoolContractError, match="unique"):
        bootstrap_event_responses((response, response), resamples=4)


def test_event_bootstrap_converts_nonfinite_day_sums_to_contract_failure() -> None:
    responses = (
        _response(day=0, minute=10, response_bps=1e308, agrees=True),
        _response(day=0, minute=30, response_bps=1e308, agrees=True),
    )

    with pytest.raises(CrossPoolContractError, match="response sum"):
        bootstrap_event_responses(responses, resamples=1)


def test_event_summary_contract_rejects_invalid_counts_and_agreement() -> None:
    valid = EventSummary(
        direction="bsc_to_base",
        horizon_ms=15 * MINUTE_MS,
        event_count=1,
        event_day_count=1,
        mean_response_bps=ConfidenceInterval(1.0, 1.0, 1.0),
        median_response_bps=ConfidenceInterval(1.0, 1.0, 1.0),
        direction_agreement=ConfidenceInterval(1.0, 1.0, 1.0),
    )

    with pytest.raises(CrossPoolContractError, match="event_count"):
        replace(valid, event_count=0)
    with pytest.raises(CrossPoolContractError, match="cannot exceed"):
        replace(valid, event_day_count=2)
    with pytest.raises(CrossPoolContractError, match="between 0 and 1"):
        replace(
            valid,
            direction_agreement=ConfidenceInterval(1.0, 1.0, 1.1),
        )


def _event(
    pool: PoolName,
    *,
    raw_mid: str,
    minute: int | None = None,
    timestamp_ms: int | None = None,
) -> PoolEvent:
    if (minute is None) == (timestamp_ms is None):
        raise ValueError("provide exactly one event timestamp")
    resolved_timestamp_ms = (
        BASE_TIMESTAMP_MS + minute * MINUTE_MS
        if minute is not None
        else timestamp_ms
    )
    assert resolved_timestamp_ms is not None
    mid = Decimal(raw_mid)
    return PoolEvent(
        pool=pool,
        timestamp_ms=resolved_timestamp_ms,
        block_number=resolved_timestamp_ms,
        tx_hash=f"0x{resolved_timestamp_ms:x}",
        log_index=0,
        raw_mid=mid,
        fee_adjusted_bid=mid * Decimal("0.999"),
        fee_adjusted_ask=mid * Decimal("1.001"),
        stored_cngn_usd_price=mid,
        stored_price_model="sqrt_mid",
    )


def _shock(
    *,
    minute: int,
    direction: Direction = "bsc_to_base",
) -> ShockEvent:
    source_pool: PoolName = "uni-bsc" if direction == "bsc_to_base" else "uni-base"
    target_pool: PoolName = "uni-base" if direction == "bsc_to_base" else "uni-bsc"
    return ShockEvent(
        direction=direction,
        source_pool=source_pool,
        target_pool=target_pool,
        source_start_timestamp_ms=BASE_TIMESTAMP_MS + (minute - 15) * MINUTE_MS,
        shock_timestamp_ms=BASE_TIMESTAMP_MS + minute * MINUTE_MS,
        source_move_bps=6.0,
        source_move_sign=1,
        shock_day_utc=date(2026, 1, 2),
    )


def _response(
    *,
    day: int,
    minute: int,
    response_bps: float,
    agrees: bool,
    direction: Direction = "bsc_to_base",
    horizon_ms: int = 15 * MINUTE_MS,
) -> EventResponse:
    shock_timestamp_ms = BASE_TIMESTAMP_MS + day * DAY_MS + minute * MINUTE_MS
    if response_bps == 0.0:
        source_move_bps = 6.0
    elif agrees:
        source_move_bps = 6.0 if response_bps > 0.0 else -6.0
    else:
        source_move_bps = -6.0 if response_bps > 0.0 else 6.0
    source_pool: PoolName = "uni-bsc" if direction == "bsc_to_base" else "uni-base"
    target_pool: PoolName = "uni-base" if direction == "bsc_to_base" else "uni-bsc"
    return EventResponse(
        direction=direction,
        source_pool=source_pool,
        target_pool=target_pool,
        shock_timestamp_ms=shock_timestamp_ms,
        shock_day_utc=datetime.fromtimestamp(
            shock_timestamp_ms / 1_000,
            tz=UTC,
        ).date(),
        horizon_ms=horizon_ms,
        source_move_bps=source_move_bps,
        target_start_timestamp_ms=shock_timestamp_ms,
        target_end_timestamp_ms=shock_timestamp_ms + horizon_ms,
        target_response_bps=response_bps,
        direction_agrees=agrees,
    )
