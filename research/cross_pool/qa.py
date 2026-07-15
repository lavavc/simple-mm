"""Quality audits for validated cross-pool event streams."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Sequence

from research.cross_pool.contracts import (
    CrossPoolContractError,
    GapQuantiles,
    PoolEvent,
    StreamQuality,
)


def validate_stream(
    events: Sequence[PoolEvent],
    *,
    transition_block: int,
) -> StreamQuality:
    if not events:
        raise CrossPoolContractError("stream QA requires at least one pool event")
    if transition_block <= 0:
        raise CrossPoolContractError("transition_block must be positive")

    pool = events[0].pool
    if any(event.pool != pool for event in events):
        raise CrossPoolContractError("stream QA requires one pool")

    gaps = [
        current.timestamp_ms - previous.timestamp_ms
        for previous, current in zip(events, events[1:], strict=False)
    ]
    if any(gap <= 0 for gap in gaps):
        raise CrossPoolContractError("timestamps must be strictly increasing")

    quantiles = (
        GapQuantiles(
            p50=_integer_quantile(gaps, Decimal("0.50")),
            p95=_integer_quantile(gaps, Decimal("0.95")),
            p99=_integer_quantile(gaps, Decimal("0.99")),
        )
        if gaps
        else None
    )
    pre_transition_rows = sum(event.block_number < transition_block for event in events)

    return StreamQuality(
        pool=pool,
        rows=len(events),
        first_timestamp_ms=events[0].timestamp_ms,
        last_timestamp_ms=events[-1].timestamp_ms,
        update_gap_quantiles_ms=quantiles,
        pre_transition_rows=pre_transition_rows,
        post_transition_rows=len(events) - pre_transition_rows,
    )


def _integer_quantile(values: Sequence[int], probability: Decimal) -> int:
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * probability
    lower_index = int(position)
    upper_index = lower_index if position == lower_index else lower_index + 1
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    interpolated = Decimal(ordered[lower_index]) + fraction * (
        ordered[upper_index] - ordered[lower_index]
    )
    return int(interpolated.to_integral_value(rounding=ROUND_HALF_EVEN))
