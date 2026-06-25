"""Causal helpers for swap-level feature cones."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence


PERCENTILE_QUANTUM = Decimal("0.0000000001")


@dataclass(frozen=True)
class TimestampedValue:
    timestamp_ms: int
    value: object


@dataclass(frozen=True)
class AsOfValue:
    timestamp_ms: int
    value: object
    age_ms: int


def previous_or_equal_with_age(
    rows: Sequence[TimestampedValue],
    timestamp_ms: int,
    max_age_ms: int | None,
) -> AsOfValue | None:
    timestamps = [row.timestamp_ms for row in rows]
    index = bisect_right(timestamps, timestamp_ms) - 1
    if index < 0:
        return None

    row = rows[index]
    age_ms = timestamp_ms - row.timestamp_ms
    if max_age_ms is not None and age_ms > max_age_ms:
        return None
    return AsOfValue(timestamp_ms=row.timestamp_ms, value=row.value, age_ms=age_ms)


def causal_percentile(
    prior_values: Sequence[Decimal],
    current: Decimal,
) -> Decimal | None:
    if not prior_values:
        return None

    count = sum(1 for value in prior_values if value <= current)
    percentile = Decimal(count) / Decimal(len(prior_values))
    return percentile.quantize(PERCENTILE_QUANTUM)
