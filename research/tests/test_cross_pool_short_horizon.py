from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module, util
from typing import cast

import pytest

from research.cross_pool.contracts import PoolEvent, PoolName

MINUTE_MS = 60_000
BASE_TIMESTAMP_MS = int(datetime(2026, 1, 2, 12, tzinfo=UTC).timestamp() * 1_000)


def test_short_horizon_module_exposes_frozen_horizon_order() -> None:
    module_spec = util.find_spec("research.cross_pool.short_horizon")

    assert module_spec is not None
    module = import_module("research.cross_pool.short_horizon")
    assert module.SHORT_HORIZONS_MS == (
        30_000,
        60_000,
        120_000,
        180_000,
        300_000,
        600_000,
        900_000,
    )


def test_no_target_update_is_an_unconditional_zero_and_right_censored() -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=15, raw_mid="1.001"),
        _event("uni-bsc", minute=31, raw_mid="1.001"),
    )
    target = (
        _event("uni-base", minute=0, raw_mid="1"),
        _event("uni-base", minute=31, raw_mid="1.1"),
    )

    study = short_horizon.measure_short_horizon_direction(source, target)

    assert study.detected_shock_count == 1
    assert study.eligible_shock_count == 1
    assert study.exclusions == ()
    assert len(study.responses) == 7
    assert tuple(row.horizon_ms for row in study.responses) == (
        30_000,
        60_000,
        120_000,
        180_000,
        300_000,
        600_000,
        900_000,
    )
    for row in study.responses:
        assert row.target_response_bps == 0.0
        assert row.zero_response is True
        assert row.direction_agrees is False
        assert row.first_update_status == "right_censored"
        assert row.first_update_timestamp_ms is None
        assert row.first_update_delay_ms is None
        assert row.first_update_raw_mid is None
        assert row.first_update_response_bps is None
        assert row.first_update_direction_agrees is None
        assert row.target_start_age_ms == 15 * MINUTE_MS
        assert row.target_end_age_ms == 15 * MINUTE_MS + row.horizon_ms


def test_first_update_is_strictly_post_shock_and_nested_across_horizons() -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=15, raw_mid="1.001"),
        _event("uni-bsc", minute=31, raw_mid="1.001"),
    )
    target = (
        _event("uni-base", minute=0, raw_mid="1"),
        _event("uni-base", minute=15, raw_mid="1.0005"),
        _event("uni-base", minute=15, offset_ms=45_000, raw_mid="1.001"),
        _event("uni-base", minute=16, offset_ms=30_000, raw_mid="1.002"),
        _event("uni-base", minute=31, raw_mid="1.1"),
    )

    study = short_horizon.measure_short_horizon_direction(source, target)

    thirty_seconds, one_minute, two_minutes, *later = study.responses
    assert thirty_seconds.first_update_status == "right_censored"
    assert thirty_seconds.target_same_timestamp is True
    assert one_minute.first_update_status == "observed"
    assert one_minute.first_update_timestamp_ms == BASE_TIMESTAMP_MS + 15 * MINUTE_MS + 45_000
    assert one_minute.first_update_delay_ms == 45_000
    assert one_minute.first_update_raw_mid == Decimal("1.001")
    expected_first_response = float(
        (Decimal("1.001") / Decimal("1.0005") - Decimal(1)) * Decimal(10_000)
    )
    assert one_minute.first_update_response_bps == expected_first_response
    assert one_minute.first_update_direction_agrees is True
    assert two_minutes.target_end_timestamp_ms == BASE_TIMESTAMP_MS + 16 * MINUTE_MS + 30_000
    assert two_minutes.target_response_bps > one_minute.target_response_bps
    assert all(
        row.first_update_response_bps == expected_first_response for row in (two_minutes, *later)
    )
    assert all(row.target_same_timestamp for row in study.responses)
    assert study.responses_excluding_same_timestamp == ()


def test_missing_source_tail_excludes_the_whole_shock_family() -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=15, raw_mid="1.001"),
        _event("uni-bsc", minute=20, raw_mid="1.001"),
    )
    target = (
        _event("uni-base", minute=0, raw_mid="1"),
        _event("uni-base", minute=31, raw_mid="1.1"),
    )

    study = short_horizon.measure_short_horizon_direction(source, target)

    assert study.detected_shock_count == 1
    assert study.eligible_shock_count == 0
    assert study.responses == ()
    assert len(study.exclusions) == 1
    assert study.exclusions[0].reason == "missing_source_end_state"


@pytest.mark.parametrize(
    ("source_pool", "target_pool", "source_move_sign"),
    (
        ("uni-bsc", "uni-base", 1),
        ("uni-bsc", "uni-base", -1),
        ("uni-base", "uni-bsc", 1),
        ("uni-base", "uni-bsc", -1),
    ),
)
def test_fee_gap_keeps_the_shock_trade_direction_across_pool_orientations(
    source_pool: PoolName,
    target_pool: PoolName,
    source_move_sign: int,
) -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    source_shock_mid = "1.001" if source_move_sign == 1 else "0.999"
    source_end_mid = "1.002" if source_move_sign == 1 else "0.998"
    target_end_mid = "1.0005" if source_move_sign == 1 else "0.9995"
    source = (
        _event(source_pool, minute=0, raw_mid="1"),
        _event(source_pool, minute=15, raw_mid=source_shock_mid),
        _event(source_pool, minute=20, raw_mid=source_end_mid),
        _event(source_pool, minute=31, raw_mid=source_shock_mid),
    )
    target = (
        _event(target_pool, minute=0, raw_mid="1"),
        _event(target_pool, minute=20, raw_mid=target_end_mid),
        _event(target_pool, minute=31, raw_mid=target_end_mid),
    )

    study = short_horizon.measure_short_horizon_direction(source, target)
    row = next(response for response in study.responses if response.horizon_ms == 300_000)

    if source_move_sign == 1:
        expected_start = 10_000 * math.log(
            float(source[1].fee_adjusted_bid / target[0].fee_adjusted_ask)
        )
        expected_end = 10_000 * math.log(
            float(source[2].fee_adjusted_bid / target[1].fee_adjusted_ask)
        )
    else:
        expected_start = 10_000 * math.log(
            float(target[0].fee_adjusted_bid / source[1].fee_adjusted_ask)
        )
        expected_end = 10_000 * math.log(
            float(target[1].fee_adjusted_bid / source[2].fee_adjusted_ask)
        )
    assert row.source_move_sign == source_move_sign
    assert row.source_end_timestamp_ms == source[2].timestamp_ms
    assert row.fee_gap_start_bps == pytest.approx(expected_start)
    assert row.fee_gap_end_bps == pytest.approx(expected_end)
    assert row.fee_gap_start_positive_bps == max(expected_start, 0.0)
    assert row.fee_gap_end_positive_bps == max(expected_end, 0.0)
    assert row.fee_gap_closure_bps == pytest.approx(expected_start - expected_end)


def test_response_contract_rejects_inconsistent_derived_and_censor_fields() -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    study = short_horizon.measure_short_horizon_direction(
        (
            _event("uni-bsc", minute=0, raw_mid="1"),
            _event("uni-bsc", minute=15, raw_mid="1.001"),
            _event("uni-bsc", minute=31, raw_mid="1.001"),
        ),
        (
            _event("uni-base", minute=0, raw_mid="1"),
            _event("uni-base", minute=31, raw_mid="1.1"),
        ),
    )
    row = study.responses[0]

    with pytest.raises(short_horizon.CrossPoolContractError, match="censored"):
        replace(row, first_update_delay_ms=1)
    with pytest.raises(short_horizon.CrossPoolContractError, match="target end age"):
        replace(row, target_end_age_ms=row.target_end_age_ms + 1)
    with pytest.raises(short_horizon.CrossPoolContractError, match="zero response"):
        replace(row, zero_response=False)
    with pytest.raises(short_horizon.CrossPoolContractError, match="fee gap closure"):
        replace(row, fee_gap_closure_bps=row.fee_gap_closure_bps + 1.0)
    with pytest.raises(short_horizon.CrossPoolContractError, match="fee band"):
        replace(row, target_start_bid=row.target_start_raw_mid)

    with pytest.raises(short_horizon.CrossPoolContractError, match="source move sign"):
        replace(row, source_move_sign=cast("object", True))
    with pytest.raises(short_horizon.CrossPoolContractError, match="boolean"):
        replace(row, zero_response=cast(bool, 0))


def test_study_contract_requires_complete_canonical_horizon_families() -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    study = short_horizon.measure_short_horizon_direction(
        (
            _event("uni-base", minute=0, raw_mid="1"),
            _event("uni-base", minute=15, raw_mid="1.001"),
            _event("uni-base", minute=31, raw_mid="1.001"),
        ),
        (
            _event("uni-bsc", minute=0, raw_mid="1"),
            _event("uni-bsc", minute=31, raw_mid="1.1"),
        ),
    )

    with pytest.raises(short_horizon.CrossPoolContractError, match="seven horizons"):
        replace(study, responses=study.responses[:-1])
    with pytest.raises(short_horizon.CrossPoolContractError, match="canonical"):
        replace(study, responses=tuple(reversed(study.responses)))
    with pytest.raises(short_horizon.CrossPoolContractError, match="reconcile"):
        replace(study, detected_shock_count=2)


def test_direction_rejects_a_mismatched_target_even_without_shocks() -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=31, raw_mid="1"),
    )
    target = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=31, raw_mid="1"),
    )

    with pytest.raises(short_horizon.CrossPoolContractError, match="target pool"):
        short_horizon.measure_short_horizon_direction(source, target)


@pytest.mark.parametrize(
    ("target_variant", "reason"),
    (
        ("missing_start", "missing_target_start_state"),
        ("missing_end", "missing_target_end_state"),
    ),
)
def test_target_common_support_failure_excludes_the_whole_shock_family(
    target_variant: str,
    reason: str,
) -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=15, raw_mid="1.001"),
        _event("uni-bsc", minute=31, raw_mid="1.001"),
    )
    target = (
        (
            _event("uni-base", minute=16, raw_mid="1"),
            _event("uni-base", minute=31, raw_mid="1.1"),
        )
        if target_variant == "missing_start"
        else (
            _event("uni-base", minute=0, raw_mid="1"),
            _event("uni-base", minute=20, raw_mid="1.1"),
        )
    )

    study = short_horizon.measure_short_horizon_direction(source, target)

    assert study.responses == ()
    assert study.eligible_shock_count == 0
    assert len(study.exclusions) == 1
    assert study.exclusions[0].reason == reason


def test_update_at_exact_endpoint_is_observed_even_when_price_is_unchanged() -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    study = short_horizon.measure_short_horizon_direction(
        (
            _event("uni-bsc", minute=0, raw_mid="1"),
            _event("uni-bsc", minute=15, raw_mid="1.001"),
            _event("uni-bsc", minute=31, raw_mid="1.001"),
        ),
        (
            _event("uni-base", minute=0, raw_mid="1"),
            _event("uni-base", minute=15, offset_ms=30_000, raw_mid="1"),
            _event("uni-base", minute=31, raw_mid="1.1"),
        ),
    )

    row = study.responses[0]
    assert row.horizon_ms == 30_000
    assert row.first_update_status == "observed"
    assert row.first_update_delay_ms == 30_000
    assert row.first_update_response_bps == 0.0
    assert row.first_update_direction_agrees is False


def test_response_contract_rejects_fractional_timestamp_and_age_fields() -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    study = short_horizon.measure_short_horizon_direction(
        (
            _event("uni-bsc", minute=0, raw_mid="1"),
            _event("uni-bsc", minute=15, raw_mid="1.001"),
            _event("uni-bsc", minute=31, raw_mid="1.001"),
        ),
        (
            _event("uni-base", minute=0, raw_mid="1"),
            _event("uni-base", minute=15, offset_ms=30_000, raw_mid="1"),
            _event("uni-base", minute=31, raw_mid="1.1"),
        ),
    )
    row = study.responses[0]

    with pytest.raises(short_horizon.CrossPoolContractError, match="positive integer"):
        replace(
            row,
            target_start_timestamp_ms=cast(int, float(row.target_start_timestamp_ms)),
        )
    with pytest.raises(short_horizon.CrossPoolContractError, match="nonnegative integer"):
        replace(row, source_end_age_ms=cast(int, float(row.source_end_age_ms)))
    with pytest.raises(short_horizon.CrossPoolContractError, match="positive integer"):
        replace(
            row,
            first_update_delay_ms=cast(int, float(row.first_update_delay_ms or 0)),
        )


def test_measurement_rejects_fractional_input_timestamps() -> None:
    short_horizon = import_module("research.cross_pool.short_horizon")
    source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=15, raw_mid="1.001"),
        _event("uni-bsc", minute=31, raw_mid="1.001"),
    )
    target_event = _event("uni-base", minute=0, raw_mid="1")
    target = (
        replace(
            target_event,
            timestamp_ms=cast(int, float(target_event.timestamp_ms)),
        ),
        _event("uni-base", minute=31, raw_mid="1.1"),
    )

    with pytest.raises(short_horizon.CrossPoolContractError, match="positive integer"):
        short_horizon.measure_short_horizon_direction(source, target)


def _event(
    pool: PoolName,
    *,
    minute: int,
    offset_ms: int = 0,
    raw_mid: str,
    fee_rate: str | None = None,
) -> PoolEvent:
    mid = Decimal(raw_mid)
    resolved_fee_rate = fee_rate or ("0.0015" if pool == "uni-base" else "0.0012")
    fee_multiplier = Decimal(1) - Decimal(resolved_fee_rate)
    timestamp_ms = BASE_TIMESTAMP_MS + minute * MINUTE_MS + offset_ms
    return PoolEvent(
        pool=pool,
        timestamp_ms=timestamp_ms,
        block_number=timestamp_ms,
        tx_hash=f"0x{timestamp_ms:x}",
        log_index=0,
        raw_mid=mid,
        fee_adjusted_bid=mid * fee_multiplier,
        fee_adjusted_ask=mid / fee_multiplier,
        stored_cngn_usd_price=mid,
        stored_price_model="sqrt_mid",
    )
