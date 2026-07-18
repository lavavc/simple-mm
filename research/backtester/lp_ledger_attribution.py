"""Strict LP-ledger attribution and exact opening-capital accounting."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias, cast

from research.cross_pool.contracts import (
    CrossPoolContractError,
    PoolName,
)

AttributionClass: TypeAlias = Literal["exact", "ambiguous", "other"]

_OWNER_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_ZERO_OWNER = "0x" + "0" * 40
_SUPPORTED_POOLS = frozenset(("uni-base", "uni-bsc"))
_REQUIRED_FIELDS = frozenset(
    (
        "chain",
        "pool_id",
        "block_number",
        "tx_hash",
        "log_index",
        "event_order",
        "event_type",
        "token_id",
        "lp_owner",
        "liquidity_delta",
        "amount0_actual",
        "amount1_actual",
        "amount0_attribution_source",
        "amount1_attribution_source",
        "amount_attribution_status",
        "cngn_usd_price_at_event",
        "timestamp_ms",
    )
)


@dataclass(frozen=True)
class PoolAttributionOrientation:
    chain: str
    pool_id: str
    inception_block: int
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    fee_rate: Decimal


POOL_ATTRIBUTION_ORIENTATIONS: Mapping[PoolName, PoolAttributionOrientation] = MappingProxyType(
    {
        "uni-base": PoolAttributionOrientation(
            chain="base",
            pool_id=("0x84fa97768196067f0e5aa157709039a3897e219cba3002d9ad38bf44e300fe93"),
            inception_block=42_926_879,
            token0_symbol="cNGN",
            token1_symbol="USDC",
            token0_decimals=6,
            token1_decimals=6,
            fee_rate=Decimal("0.0015"),
        ),
        "uni-bsc": PoolAttributionOrientation(
            chain="bsc",
            pool_id=("0x2268f03a28f37f16cd3610dc669536f8c815d9d4cb2906feeeba9150fb2d8596"),
            inception_block=84_655_203,
            token0_symbol="USDT",
            token1_symbol="cNGN",
            token0_decimals=18,
            token1_decimals=6,
            fee_rate=Decimal("0.0012"),
        ),
    }
)


@dataclass(frozen=True)
class LedgerAttributionRow:
    pool: PoolName
    pool_id: str
    block_number: int
    tx_hash: str
    log_index: int
    event_order: int
    timestamp_ms: int
    token_id: int
    event_type: str
    owner: str | None
    liquidity_delta: Decimal
    amount0_actual: Decimal
    amount1_actual: Decimal
    cngn_usd_price: Decimal
    amount0_attribution_source: str
    amount1_attribution_source: str
    raw_attribution_status: str
    attribution_class: AttributionClass
    opening_capital_usd: Decimal | None

    def __post_init__(self) -> None:
        pool = _validated_pool(self.pool)
        orientation = POOL_ATTRIBUTION_ORIENTATIONS[pool]
        if self.pool_id.lower() != orientation.pool_id.lower():
            raise CrossPoolContractError("LP ledger pool_id violates pool orientation")
        _require_positive_int(self.block_number, "LP ledger block_number")
        _require_nonnegative_int(self.log_index, "LP ledger log_index")
        _require_nonnegative_int(self.event_order, "LP ledger event_order")
        _require_positive_int(self.timestamp_ms, "LP ledger timestamp_ms")
        _require_nonnegative_int(self.token_id, "LP ledger token_id")
        for value, label in (
            (self.tx_hash, "tx_hash"),
            (self.event_type, "event_type"),
            (self.amount0_attribution_source, "amount0_attribution_source"),
            (self.amount1_attribution_source, "amount1_attribution_source"),
            (self.raw_attribution_status, "amount_attribution_status"),
        ):
            if not value:
                raise CrossPoolContractError(f"LP ledger {label} cannot be empty")
        if self.owner is not None and _normalize_owner(self.owner) != self.owner:
            raise CrossPoolContractError("LP ledger owner must be normalized")
        _require_finite_decimal(self.liquidity_delta, "LP ledger liquidity_delta")
        _require_nonnegative_decimal(self.amount0_actual, "LP ledger amount0_actual")
        _require_nonnegative_decimal(self.amount1_actual, "LP ledger amount1_actual")
        _require_finite_decimal(self.cngn_usd_price, "LP ledger cngn_usd_price")
        expected_class = _classify_attribution(self.raw_attribution_status)
        if self.attribution_class != expected_class:
            raise CrossPoolContractError("LP ledger attribution class must match its raw status")
        if self.liquidity_delta > 0 and expected_class == "other":
            raise CrossPoolContractError(
                "positive-liquidity row has unsupported attribution status"
            )
        expected_capital = (
            opening_capital_usd(
                pool,
                amount0_actual=self.amount0_actual,
                amount1_actual=self.amount1_actual,
                cngn_usd_price=self.cngn_usd_price,
            )
            if self.liquidity_delta > 0 and expected_class == "exact"
            else None
        )
        if self.opening_capital_usd != expected_capital:
            raise CrossPoolContractError(
                "LP ledger opening capital must match exact positive liquidity"
            )


def opening_capital_usd(
    pool: str,
    *,
    amount0_actual: Decimal,
    amount1_actual: Decimal,
    cngn_usd_price: Decimal,
) -> Decimal:
    normalized_pool = _validated_pool(pool)
    _require_nonnegative_decimal(amount0_actual, "amount0_actual")
    _require_nonnegative_decimal(amount1_actual, "amount1_actual")
    _require_finite_decimal(cngn_usd_price, "cngn_usd_price")
    if cngn_usd_price <= 0:
        raise CrossPoolContractError("exact opening requires a positive price")

    orientation = POOL_ATTRIBUTION_ORIENTATIONS[normalized_pool]
    token0_is_cngn = orientation.token0_symbol.upper() == "CNGN"
    token1_is_cngn = orientation.token1_symbol.upper() == "CNGN"
    if token0_is_cngn == token1_is_cngn:
        raise CrossPoolContractError(f"unsupported pool token orientation for {normalized_pool}")
    with localcontext() as context:
        context.prec = 60
        if token0_is_cngn:
            return +(amount0_actual * cngn_usd_price + amount1_actual)
        return +(amount0_actual + amount1_actual * cngn_usd_price)


def load_ledger_attribution_rows(
    pool: str,
    path: Path,
) -> tuple[LedgerAttributionRow, ...]:
    normalized_pool = _validated_pool(pool)
    if not path.is_file():
        raise CrossPoolContractError(f"LP ledger CSV does not exist: {path}")

    rows: list[LedgerAttributionRow] = []
    identities: set[tuple[str, int, int]] = set()
    previous_order: tuple[int, int, int] | None = None
    previous_timestamp_ms: int | None = None
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CrossPoolContractError(f"LP ledger CSV has no header: {path}")
        missing = sorted(_REQUIRED_FIELDS.difference(reader.fieldnames))
        if missing:
            raise CrossPoolContractError(f"LP ledger CSV missing required fields {missing}: {path}")

        for row_number, raw_row in enumerate(reader, start=2):
            row = _load_row(normalized_pool, raw_row, row_number=row_number)
            identity = (row.tx_hash.lower(), row.log_index, row.event_order)
            if identity in identities:
                raise _row_error(row_number, f"duplicate ledger identity {identity}")
            identities.add(identity)
            order = (row.block_number, row.log_index, row.event_order)
            if previous_order is not None and order <= previous_order:
                raise _row_error(row_number, "ledger rows must use canonical order")
            if previous_timestamp_ms is not None and row.timestamp_ms < previous_timestamp_ms:
                raise _row_error(
                    row_number,
                    "ledger timestamps must be nondecreasing",
                )
            previous_order = order
            previous_timestamp_ms = row.timestamp_ms
            rows.append(row)
    if not rows:
        raise CrossPoolContractError(f"LP ledger CSV has no rows: {path}")
    inception_block = POOL_ATTRIBUTION_ORIENTATIONS[normalized_pool].inception_block
    if rows[0].block_number != inception_block:
        raise CrossPoolContractError(
            f"{normalized_pool} LP ledger must begin at pool inception block {inception_block}"
        )
    return tuple(rows)


def pool_attribution_orientation(pool: str) -> PoolAttributionOrientation:
    return POOL_ATTRIBUTION_ORIENTATIONS[_validated_pool(pool)]


def _load_row(
    pool: PoolName,
    raw_row: dict[str, str | None],
    *,
    row_number: int,
) -> LedgerAttributionRow:
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[pool]
    chain = _required_cell(raw_row, "chain", row_number).lower()
    pool_id = _required_cell(raw_row, "pool_id", row_number)
    if chain != orientation.chain.lower() or pool_id.lower() != orientation.pool_id.lower():
        raise _row_error(row_number, "chain or pool_id violates pool orientation")

    block_number = _parse_int(raw_row, "block_number", row_number)
    log_index = _parse_int(raw_row, "log_index", row_number)
    event_order = _parse_int(raw_row, "event_order", row_number)
    timestamp_ms = _parse_int(raw_row, "timestamp_ms", row_number)
    token_id = _parse_int(raw_row, "token_id", row_number)
    liquidity_delta = _parse_decimal(raw_row, "liquidity_delta", row_number)
    amount0_actual = _parse_decimal(raw_row, "amount0_actual", row_number)
    amount1_actual = _parse_decimal(raw_row, "amount1_actual", row_number)
    cngn_usd_price = _parse_decimal(
        raw_row,
        "cngn_usd_price_at_event",
        row_number,
    )
    raw_status = _required_cell(raw_row, "amount_attribution_status", row_number)
    attribution_class = _classify_attribution(raw_status)
    owner = _normalize_owner(raw_row.get("lp_owner"))
    capital = (
        opening_capital_usd(
            pool,
            amount0_actual=amount0_actual,
            amount1_actual=amount1_actual,
            cngn_usd_price=cngn_usd_price,
        )
        if liquidity_delta > 0 and attribution_class == "exact"
        else None
    )

    try:
        return LedgerAttributionRow(
            pool=pool,
            pool_id=pool_id,
            block_number=block_number,
            tx_hash=_required_cell(raw_row, "tx_hash", row_number),
            log_index=log_index,
            event_order=event_order,
            timestamp_ms=timestamp_ms,
            token_id=token_id,
            event_type=_required_cell(raw_row, "event_type", row_number),
            owner=owner,
            liquidity_delta=liquidity_delta,
            amount0_actual=amount0_actual,
            amount1_actual=amount1_actual,
            cngn_usd_price=cngn_usd_price,
            amount0_attribution_source=_required_cell(
                raw_row,
                "amount0_attribution_source",
                row_number,
            ),
            amount1_attribution_source=_required_cell(
                raw_row,
                "amount1_attribution_source",
                row_number,
            ),
            raw_attribution_status=raw_status,
            attribution_class=attribution_class,
            opening_capital_usd=capital,
        )
    except CrossPoolContractError as exc:
        raise _row_error(row_number, str(exc)) from exc


def _validated_pool(pool: str) -> PoolName:
    if pool not in _SUPPORTED_POOLS:
        raise CrossPoolContractError(f"unsupported pool {pool!r}")
    return cast(PoolName, pool)


def _classify_attribution(raw_status: str) -> AttributionClass:
    if raw_status == "exact":
        return "exact"
    if raw_status.startswith("ambiguous"):
        return "ambiguous"
    return "other"


def _normalize_owner(value: str | None) -> str | None:
    if value is None:
        return None
    owner = value.strip()
    if not _OWNER_PATTERN.fullmatch(owner):
        return None
    normalized = owner.lower()
    return None if normalized == _ZERO_OWNER else normalized


def _required_cell(
    row: dict[str, str | None],
    field: str,
    row_number: int,
) -> str:
    value = row.get(field)
    if value is None or not value.strip():
        raise _row_error(row_number, f"{field} cannot be empty")
    return value.strip()


def _parse_int(
    row: dict[str, str | None],
    field: str,
    row_number: int,
) -> int:
    value = _required_cell(row, field, row_number)
    try:
        return int(value)
    except ValueError as exc:
        raise _row_error(row_number, f"{field} must be an integer") from exc


def _parse_decimal(
    row: dict[str, str | None],
    field: str,
    row_number: int,
) -> Decimal:
    value = _required_cell(row, field, row_number)
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise _row_error(row_number, f"{field} must be a decimal") from exc
    if not parsed.is_finite():
        raise _row_error(row_number, f"{field} must be finite")
    return parsed


def _require_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CrossPoolContractError(f"{label} must be a positive integer")


def _require_nonnegative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrossPoolContractError(f"{label} must be a nonnegative integer")


def _require_finite_decimal(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CrossPoolContractError(f"{label} must be finite")


def _require_nonnegative_decimal(value: Decimal, label: str) -> None:
    _require_finite_decimal(value, label)
    if value < 0:
        raise CrossPoolContractError(f"{label} must be nonnegative")


def _row_error(row_number: int, message: str) -> CrossPoolContractError:
    return CrossPoolContractError(f"LP ledger row {row_number}: {message}")
