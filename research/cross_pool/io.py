"""Strict CSV parsing and stream QA for canonical pool features."""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from engine.web3_utils import coerce_hex_str
from research.cross_pool.contracts import (
    CrossPoolContractError,
    PoolEvent,
    PoolName,
    StoredPriceModel,
)

_SUPPORTED_POOLS = frozenset(("uni-base", "uni-bsc"))
_REQUIRED_FIELDS = frozenset(
    (
        "timestamp_ms",
        "pool",
        "block_number",
        "tx_hash",
        "log_index",
        "raw_sqrt_mid",
        "fee_adjusted_bid",
        "fee_adjusted_ask",
        "stored_cngn_usd_price",
        "stored_price_model",
    )
)


def load_pool_events(path: Path, *, expected_pool: PoolName) -> tuple[PoolEvent, ...]:
    if expected_pool not in _SUPPORTED_POOLS:
        raise CrossPoolContractError(f"unsupported expected pool {expected_pool}")
    if not path.is_file():
        raise CrossPoolContractError(f"pool feature CSV does not exist: {path}")

    events: list[PoolEvent] = []
    identities: set[tuple[str, str, int]] = set()
    previous_timestamp_ms: int | None = None

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CrossPoolContractError(f"pool feature CSV has no header: {path}")
        missing = sorted(_REQUIRED_FIELDS.difference(reader.fieldnames))
        if missing:
            raise CrossPoolContractError(
                f"pool feature CSV missing required columns {missing}: {path}"
            )

        for row_number, row in enumerate(reader, start=2):
            pool = _parse_pool(_required_cell(row, "pool", row_number), expected_pool, row_number)
            timestamp_ms = _parse_int(
                _required_cell(row, "timestamp_ms", row_number),
                "timestamp_ms",
                row_number,
            )
            block_number = _parse_int(
                _required_cell(row, "block_number", row_number),
                "block_number",
                row_number,
            )
            log_index = _parse_int(
                _required_cell(row, "log_index", row_number),
                "log_index",
                row_number,
            )
            if timestamp_ms <= 0:
                raise _row_error(row_number, "timestamp_ms must be positive")
            if block_number <= 0:
                raise _row_error(row_number, "block_number must be positive")
            if log_index < 0:
                raise _row_error(row_number, "log_index must be non-negative")

            tx_hash = coerce_hex_str(_required_cell(row, "tx_hash", row_number))
            identity = (pool, tx_hash, log_index)
            if identity in identities:
                raise _row_error(row_number, f"duplicate pool event identity {identity}")
            identities.add(identity)

            if previous_timestamp_ms is not None and timestamp_ms <= previous_timestamp_ms:
                raise _row_error(row_number, "timestamps must be strictly increasing")
            previous_timestamp_ms = timestamp_ms

            raw_mid = _parse_positive_decimal(
                _required_cell(row, "raw_sqrt_mid", row_number),
                "raw_sqrt_mid",
                row_number,
            )
            bid = _parse_positive_decimal(
                _required_cell(row, "fee_adjusted_bid", row_number),
                "fee_adjusted_bid",
                row_number,
            )
            ask = _parse_positive_decimal(
                _required_cell(row, "fee_adjusted_ask", row_number),
                "fee_adjusted_ask",
                row_number,
            )
            if not bid < raw_mid < ask:
                raise _row_error(
                    row_number,
                    "fee-adjusted band must satisfy bid < raw_sqrt_mid < ask",
                )

            stored_price = _parse_positive_decimal(
                _required_cell(row, "stored_cngn_usd_price", row_number),
                "stored_cngn_usd_price",
                row_number,
            )
            stored_price_model = _required_cell(row, "stored_price_model", row_number)
            if stored_price_model != "sqrt_mid":
                raise _row_error(
                    row_number,
                    f"unsupported stored_price_model {stored_price_model!r}",
                )

            events.append(
                PoolEvent(
                    pool=pool,
                    timestamp_ms=timestamp_ms,
                    block_number=block_number,
                    tx_hash=tx_hash,
                    log_index=log_index,
                    raw_mid=raw_mid,
                    fee_adjusted_bid=bid,
                    fee_adjusted_ask=ask,
                    stored_cngn_usd_price=stored_price,
                    stored_price_model=cast(StoredPriceModel, stored_price_model),
                )
            )

    return tuple(events)


def _parse_pool(value: str, expected_pool: PoolName, row_number: int) -> PoolName:
    pool = value.strip()
    if pool != expected_pool:
        raise _row_error(row_number, f"expected pool {expected_pool}, found {pool!r}")
    return expected_pool


def _required_cell(row: dict[str, str | None], field: str, row_number: int) -> str:
    value = row.get(field)
    if value is None or not value.strip():
        raise _row_error(row_number, f"{field} cannot be empty")
    return value.strip()


def _parse_int(value: str, field: str, row_number: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise _row_error(row_number, f"{field} must be an integer") from exc


def _parse_positive_decimal(value: str, field: str, row_number: int) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _row_error(row_number, f"{field} must be a decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise _row_error(row_number, f"{field} must be positive and finite")
    return parsed


def _row_error(row_number: int, message: str) -> CrossPoolContractError:
    return CrossPoolContractError(f"row {row_number}: {message}")
