"""Shared helpers for normalizing venue order payload values."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any


def decimal_from_order_value(value: Any) -> Decimal:
    """Extract a Decimal value from Quidax-style scalar or object payloads."""
    if isinstance(value, dict):
        for key in ("amount", "value"):
            nested = value.get(key)
            if nested is not None:
                return Decimal(str(nested))
        return Decimal("0")
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def coerce_timestamp_ms(value: Any) -> int | None:
    """Normalize timestamps from ints, numeric strings, or ISO datetimes."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        numeric = int(value)
        return numeric if numeric > 10_000_000_000 else numeric * 1000

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        numeric = int(text)
        return numeric if numeric > 10_000_000_000 else numeric * 1000

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None
