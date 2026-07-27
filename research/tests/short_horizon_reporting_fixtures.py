from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from research.cross_pool.contracts import PoolEvent, PoolName
from research.cross_pool.short_horizon import ShortHorizonStudy, measure_short_horizon_direction
from research.cross_pool.short_horizon_inference import ShortHorizonInference, infer_short_horizon

_MINUTE_MS = 60_000
_BASE_TIMESTAMP_MS = int(datetime(2026, 1, 2, 12, tzinfo=UTC).timestamp() * 1_000)


def small_studies() -> tuple[ShortHorizonStudy, ShortHorizonStudy]:
    bsc_source = (
        _event("uni-bsc", minute=0, raw_mid="1"),
        _event("uni-bsc", minute=15, raw_mid="1.001"),
        _event("uni-bsc", minute=31, raw_mid="1.001"),
    )
    base_target = (
        _event("uni-base", minute=0, raw_mid="1"),
        _event("uni-base", minute=15, offset_ms=30_000, raw_mid="1.0002"),
        _event("uni-base", minute=31, raw_mid="1.0002"),
    )
    base_source = (
        _event("uni-base", minute=1_440, raw_mid="1"),
        _event("uni-base", minute=1_455, raw_mid="0.999"),
        _event("uni-base", minute=1_471, raw_mid="0.999"),
    )
    bsc_target = (
        _event("uni-bsc", minute=1_440, raw_mid="1"),
        _event("uni-bsc", minute=1_456, raw_mid="0.9998"),
        _event("uni-bsc", minute=1_471, raw_mid="0.9998"),
    )
    return (
        measure_short_horizon_direction(bsc_source, base_target),
        measure_short_horizon_direction(base_source, bsc_target),
    )


def small_inferences() -> tuple[ShortHorizonInference, ...]:
    return tuple(
        infer_short_horizon(study, exclude_same_timestamp=exclude)
        for study in small_studies()
        for exclude in (False, True)
    )


def _event(
    pool: PoolName,
    *,
    minute: int,
    raw_mid: str,
    offset_ms: int = 0,
) -> PoolEvent:
    mid = Decimal(raw_mid)
    fee_multiplier = Decimal("0.9985") if pool == "uni-base" else Decimal("0.9988")
    timestamp_ms = _BASE_TIMESTAMP_MS + minute * _MINUTE_MS + offset_ms
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
