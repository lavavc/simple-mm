"""Flatten LP position snapshots for research exports."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


BASE_FIELDS = (
    "timestamp",
    "venue",
    "pair",
    "position_value_usd",
    "volume_24h_usd",
    "token_id",
    "liquidity",
    "tick_lower",
    "tick_upper",
    "range_min",
    "range_max",
    "current_price",
    "price_position_fraction",
    "in_range",
    "our_share_pct",
    "snapshot_status",
    "snapshot_message",
)


def _fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    balance_symbols: set[str] = set()
    rate_names: set[str] = set()
    for row in rows:
        balance_symbols.update(str(symbol).lower() for symbol in row.get("balances", {}))
        rate_names.update(str(name).lower() for name in row.get("rates", {}))
    return [
        *BASE_FIELDS,
        *(f"balance_{symbol}" for symbol in sorted(balance_symbols)),
        *(f"rate_{name}" for name in sorted(rate_names)),
    ]


def _flatten_snapshot(
    row: dict[str, Any],
    balance_symbols: list[str],
    rate_names: list[str],
) -> dict[str, str]:
    flat = {
        "timestamp": _format(row.get("timestamp")),
        "venue": _format(row.get("venue")),
        "pair": _format(row.get("pair")),
        "position_value_usd": _format(row.get("position_value_usd")),
        "volume_24h_usd": _format(row.get("volume_24h_usd")),
    }
    lp_position = row.get("lp_position") or {}
    for key in BASE_FIELDS[5:]:
        flat[key] = _format(lp_position.get(key))

    balances = row.get("balances") or {}
    for symbol in balance_symbols:
        flat[f"balance_{symbol.lower()}"] = _format(balances.get(symbol))

    rates = row.get("rates") or {}
    for name in rate_names:
        flat[f"rate_{name.lower()}"] = _format(rates.get(name))

    return flat


def _format(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
