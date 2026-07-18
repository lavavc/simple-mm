"""Strict LP-ledger attribution and exact opening-capital accounting."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Sequence
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
RpcVerificationMode: TypeAlias = Literal[
    "rpc_verified",
    "candidate_list_unverified",
]
CandidateScanSource: TypeAlias = Literal[
    "pool_manager_modify_liquidity_plus_position_manager_transfer_logs",
    "explicit_candidate_list",
]

LEDGER_COVERAGE_SCHEMA_VERSION = "1.0.0"
_OWNER_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_ZERO_OWNER = "0x" + "0" * 40
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_BLOCK_HASH_PATTERN = re.compile(r"0x[0-9a-f]{64}\Z")
_TX_HASH_PATTERN = re.compile(r"0x[0-9a-f]{64}\Z")
_SUPPORTED_POOLS = frozenset(("uni-base", "uni-bsc"))
_FIXTURE_INPUT_KEYS = frozenset(("decoded_actions", "ownership_events", "price_events"))
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
    chain_id: int
    pool_id: str
    inception_block: int
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    fee_rate: Decimal


@dataclass(frozen=True)
class RpcLedgerCoverage:
    schema_version: str
    verification_mode: RpcVerificationMode
    pool: PoolName
    chain: str
    chain_id: int
    pool_id: str
    covered_start_block: int
    covered_start_block_hash: str
    covered_start_timestamp_ms: int
    covered_end_block: int
    covered_end_block_hash: str
    covered_end_timestamp_ms: int
    ledger_sha256: str
    ledger_rows: int
    ledger_first_block: int
    ledger_last_block: int
    candidate_transaction_count: int
    candidate_transactions_sha256: str
    candidate_transaction_hashes: tuple[str, ...]
    candidate_scan_source: CandidateScanSource
    candidate_scan_status: Literal["producer_attested"]

    def __post_init__(self) -> None:
        orientation = POOL_ATTRIBUTION_ORIENTATIONS[_validated_pool(self.pool)]
        if self.schema_version != LEDGER_COVERAGE_SCHEMA_VERSION:
            raise CrossPoolContractError("unsupported LP ledger coverage schema version")
        if self.verification_mode not in (
            "rpc_verified",
            "candidate_list_unverified",
        ):
            raise CrossPoolContractError("unsupported LP ledger RPC verification mode")
        if (
            self.chain != orientation.chain
            or self.chain_id != orientation.chain_id
            or self.pool_id.lower() != orientation.pool_id.lower()
        ):
            raise CrossPoolContractError("LP ledger coverage violates pool orientation")
        for value, label in (
            (self.covered_start_block, "covered_start_block"),
            (self.covered_start_timestamp_ms, "covered_start_timestamp_ms"),
            (self.covered_end_block, "covered_end_block"),
            (self.covered_end_timestamp_ms, "covered_end_timestamp_ms"),
            (self.ledger_rows, "ledger_rows"),
            (self.ledger_first_block, "ledger_first_block"),
            (self.ledger_last_block, "ledger_last_block"),
        ):
            _require_positive_int(value, f"LP ledger coverage {label}")
        _require_nonnegative_int(
            self.candidate_transaction_count,
            "LP ledger coverage candidate_transaction_count",
        )
        if self.candidate_transaction_count == 0:
            raise CrossPoolContractError(
                "nonempty LP ledger coverage requires candidate transactions"
            )
        if self.covered_start_block > self.covered_end_block:
            raise CrossPoolContractError("LP ledger coverage block range is inverted")
        if self.covered_start_timestamp_ms > self.covered_end_timestamp_ms:
            raise CrossPoolContractError("LP ledger coverage timestamp range is inverted")
        if not (
            self.covered_start_block
            <= self.ledger_first_block
            <= self.ledger_last_block
            <= self.covered_end_block
        ):
            raise CrossPoolContractError("LP ledger rows fall outside the covered block range")
        _require_sha256(self.ledger_sha256, "LP ledger coverage ledger_sha256")
        _require_sha256(
            self.candidate_transactions_sha256,
            "LP ledger coverage candidate_transactions_sha256",
        )
        candidate_hashes = _canonical_transaction_hashes(
            self.candidate_transaction_hashes
        )
        if candidate_hashes != self.candidate_transaction_hashes:
            raise CrossPoolContractError(
                "LP ledger coverage candidate hashes must use canonical order"
            )
        if self.candidate_transaction_count != len(candidate_hashes):
            raise CrossPoolContractError(
                "LP ledger coverage candidate transaction count is inconsistent"
            )
        if self.candidate_transactions_sha256 != _candidate_set_sha256(
            candidate_hashes
        ):
            raise CrossPoolContractError(
                "LP ledger coverage candidate transaction digest is inconsistent"
            )
        expected_source: CandidateScanSource = (
            "pool_manager_modify_liquidity_plus_position_manager_transfer_logs"
            if self.verification_mode == "rpc_verified"
            else "explicit_candidate_list"
        )
        if self.candidate_scan_source != expected_source:
            raise CrossPoolContractError(
                "LP ledger coverage candidate scan source is inconsistent"
            )
        if self.candidate_scan_status != "producer_attested":
            raise CrossPoolContractError(
                "LP ledger coverage candidate scan must be producer-attested"
            )
        _require_block_hash(
            self.covered_start_block_hash,
            "LP ledger coverage covered_start_block_hash",
        )
        _require_block_hash(
            self.covered_end_block_hash,
            "LP ledger coverage covered_end_block_hash",
        )


@dataclass(frozen=True)
class FixtureLedgerCoverage:
    schema_version: str
    verification_mode: Literal["fixture_unverified"]
    pool: PoolName
    chain: str
    chain_id: int
    pool_id: str
    requested_start_block: int
    requested_end_block: int
    ledger_sha256: str
    ledger_rows: int
    ledger_first_block: int
    ledger_last_block: int
    fixture_input_sha256: Mapping[str, str]

    def __post_init__(self) -> None:
        orientation = POOL_ATTRIBUTION_ORIENTATIONS[_validated_pool(self.pool)]
        if self.schema_version != LEDGER_COVERAGE_SCHEMA_VERSION:
            raise CrossPoolContractError("unsupported LP ledger coverage schema version")
        if self.verification_mode != "fixture_unverified":
            raise CrossPoolContractError("unsupported LP ledger fixture verification mode")
        if (
            self.chain != orientation.chain
            or self.chain_id != orientation.chain_id
            or self.pool_id.lower() != orientation.pool_id.lower()
        ):
            raise CrossPoolContractError("LP ledger coverage violates pool orientation")
        for value, label in (
            (self.requested_start_block, "requested_start_block"),
            (self.requested_end_block, "requested_end_block"),
            (self.ledger_rows, "ledger_rows"),
            (self.ledger_first_block, "ledger_first_block"),
            (self.ledger_last_block, "ledger_last_block"),
        ):
            _require_positive_int(value, f"LP ledger coverage {label}")
        if self.requested_start_block > self.requested_end_block:
            raise CrossPoolContractError("LP ledger fixture range is inverted")
        if not (
            self.requested_start_block
            <= self.ledger_first_block
            <= self.ledger_last_block
            <= self.requested_end_block
        ):
            raise CrossPoolContractError(
                "LP ledger rows fall outside the requested block range"
            )
        _require_sha256(self.ledger_sha256, "LP ledger coverage ledger_sha256")
        if set(self.fixture_input_sha256) != _FIXTURE_INPUT_KEYS:
            raise CrossPoolContractError(
                "LP ledger fixture coverage requires exact fixture input hashes"
            )
        normalized_hashes: dict[str, str] = {}
        for key in sorted(self.fixture_input_sha256):
            digest = self.fixture_input_sha256[key]
            _require_sha256(digest, f"LP ledger fixture {key} SHA-256")
            normalized_hashes[key] = digest
        object.__setattr__(
            self,
            "fixture_input_sha256",
            MappingProxyType(normalized_hashes),
        )


LedgerCoverage: TypeAlias = RpcLedgerCoverage | FixtureLedgerCoverage


@dataclass(frozen=True)
class VerifiedLedgerCoverageEvidence:
    schema_version: str
    sidecar_sha256: str
    ledger_sha256: str
    pool: PoolName
    chain: str
    chain_id: int
    pool_id: str
    covered_start_block: int
    covered_start_block_hash: str
    covered_start_timestamp_ms: int
    covered_end_block: int
    covered_end_block_hash: str
    covered_end_timestamp_ms: int
    required_end_block: int
    required_end_timestamp_ms: int
    candidate_transaction_count: int
    candidate_transactions_sha256: str
    candidate_scan_source: CandidateScanSource
    candidate_scan_status: Literal["producer_attested"]


@dataclass(frozen=True)
class VerifiedLedgerLoad:
    rows: tuple[LedgerAttributionRow, ...]
    coverage: VerifiedLedgerCoverageEvidence


@dataclass(frozen=True)
class _LedgerSnapshot:
    raw_bytes: bytes
    sha256: str
    row_count: int
    first_block: int
    last_block: int


@dataclass(frozen=True)
class _CoverageSnapshot:
    coverage: LedgerCoverage
    sidecar_sha256: str


POOL_ATTRIBUTION_ORIENTATIONS: Mapping[PoolName, PoolAttributionOrientation] = MappingProxyType(
    {
        "uni-base": PoolAttributionOrientation(
            chain="base",
            chain_id=8453,
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
            chain_id=56,
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
    return _load_ledger_attribution_rows_from_bytes(
        normalized_pool,
        _read_path_bytes(path, label="LP ledger CSV"),
        source=path,
    )


def _load_ledger_attribution_rows_from_bytes(
    pool: PoolName,
    raw_bytes: bytes,
    *,
    source: Path,
) -> tuple[LedgerAttributionRow, ...]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError(
            f"LP ledger CSV must be valid UTF-8: {source}"
        ) from exc

    rows: list[LedgerAttributionRow] = []
    identities: set[tuple[str, int, int]] = set()
    previous_order: tuple[int, int, int] | None = None
    previous_timestamp_ms: int | None = None
    with io.StringIO(text, newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CrossPoolContractError(f"LP ledger CSV has no header: {source}")
        missing = sorted(_REQUIRED_FIELDS.difference(reader.fieldnames))
        if missing:
            raise CrossPoolContractError(
                f"LP ledger CSV missing required fields {missing}: {source}"
            )

        for row_number, raw_row in enumerate(reader, start=2):
            row = _load_row(pool, raw_row, row_number=row_number)
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
        raise CrossPoolContractError(f"LP ledger CSV has no rows: {source}")
    inception_block = POOL_ATTRIBUTION_ORIENTATIONS[pool].inception_block
    if rows[0].block_number != inception_block:
        raise CrossPoolContractError(
            f"{pool} LP ledger must begin at pool inception block {inception_block}"
        )
    return tuple(rows)


def pool_attribution_orientation(pool: str) -> PoolAttributionOrientation:
    return POOL_ATTRIBUTION_ORIENTATIONS[_validated_pool(pool)]


def ledger_coverage_path(ledger_path: Path) -> Path:
    if ledger_path.suffix.lower() != ".csv":
        raise CrossPoolContractError("LP ledger coverage requires a .csv ledger path")
    return ledger_path.with_suffix(ledger_path.suffix + ".coverage.json")


def write_rpc_ledger_coverage(
    pool: str,
    ledger_path: Path,
    *,
    chain_id: int,
    covered_start_block: int,
    covered_start_block_hash: str,
    covered_start_timestamp_ms: int,
    covered_end_block: int,
    covered_end_block_hash: str,
    covered_end_timestamp_ms: int,
    candidate_transaction_hashes: Sequence[str],
    verification_mode: RpcVerificationMode,
) -> Path:
    serialized = build_rpc_ledger_coverage_bytes(
        pool,
        _read_path_bytes(ledger_path, label="LP ledger CSV"),
        chain_id=chain_id,
        covered_start_block=covered_start_block,
        covered_start_block_hash=covered_start_block_hash,
        covered_start_timestamp_ms=covered_start_timestamp_ms,
        covered_end_block=covered_end_block,
        covered_end_block_hash=covered_end_block_hash,
        covered_end_timestamp_ms=covered_end_timestamp_ms,
        candidate_transaction_hashes=candidate_transaction_hashes,
        verification_mode=verification_mode,
    )
    sidecar_path = ledger_coverage_path(ledger_path)
    sidecar_path.write_bytes(serialized)
    return sidecar_path


def build_rpc_ledger_coverage_bytes(
    pool: str,
    ledger_bytes: bytes,
    *,
    chain_id: int,
    covered_start_block: int,
    covered_start_block_hash: str,
    covered_start_timestamp_ms: int,
    covered_end_block: int,
    covered_end_block_hash: str,
    covered_end_timestamp_ms: int,
    candidate_transaction_hashes: Sequence[str],
    verification_mode: RpcVerificationMode,
) -> bytes:
    normalized_pool = _validated_pool(pool)
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[normalized_pool]
    snapshot = _ledger_snapshot_from_bytes(
        normalized_pool,
        ledger_bytes,
        source=Path("<ledger-bytes>"),
    )
    candidate_hashes = _canonical_transaction_hashes(candidate_transaction_hashes)
    coverage = RpcLedgerCoverage(
        schema_version=LEDGER_COVERAGE_SCHEMA_VERSION,
        verification_mode=verification_mode,
        pool=normalized_pool,
        chain=orientation.chain,
        chain_id=chain_id,
        pool_id=orientation.pool_id,
        covered_start_block=covered_start_block,
        covered_start_block_hash=covered_start_block_hash.lower(),
        covered_start_timestamp_ms=covered_start_timestamp_ms,
        covered_end_block=covered_end_block,
        covered_end_block_hash=covered_end_block_hash.lower(),
        covered_end_timestamp_ms=covered_end_timestamp_ms,
        ledger_sha256=snapshot.sha256,
        ledger_rows=snapshot.row_count,
        ledger_first_block=snapshot.first_block,
        ledger_last_block=snapshot.last_block,
        candidate_transaction_count=len(candidate_hashes),
        candidate_transactions_sha256=_candidate_set_sha256(candidate_hashes),
        candidate_transaction_hashes=candidate_hashes,
        candidate_scan_source=(
            "pool_manager_modify_liquidity_plus_position_manager_transfer_logs"
            if verification_mode == "rpc_verified"
            else "explicit_candidate_list"
        ),
        candidate_scan_status="producer_attested",
    )
    return _canonical_json_bytes(_rpc_coverage_payload(coverage))


def write_fixture_ledger_coverage(
    pool: str,
    ledger_path: Path,
    *,
    requested_start_block: int,
    requested_end_block: int,
    fixture_input_sha256: Mapping[str, str],
) -> Path:
    serialized = build_fixture_ledger_coverage_bytes(
        pool,
        _read_path_bytes(ledger_path, label="LP ledger CSV"),
        requested_start_block=requested_start_block,
        requested_end_block=requested_end_block,
        fixture_input_sha256=fixture_input_sha256,
    )
    sidecar_path = ledger_coverage_path(ledger_path)
    sidecar_path.write_bytes(serialized)
    return sidecar_path


def build_fixture_ledger_coverage_bytes(
    pool: str,
    ledger_bytes: bytes,
    *,
    requested_start_block: int,
    requested_end_block: int,
    fixture_input_sha256: Mapping[str, str],
) -> bytes:
    normalized_pool = _validated_pool(pool)
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[normalized_pool]
    snapshot = _ledger_snapshot_from_bytes(
        normalized_pool,
        ledger_bytes,
        source=Path("<ledger-bytes>"),
    )
    coverage = FixtureLedgerCoverage(
        schema_version=LEDGER_COVERAGE_SCHEMA_VERSION,
        verification_mode="fixture_unverified",
        pool=normalized_pool,
        chain=orientation.chain,
        chain_id=orientation.chain_id,
        pool_id=orientation.pool_id,
        requested_start_block=requested_start_block,
        requested_end_block=requested_end_block,
        ledger_sha256=snapshot.sha256,
        ledger_rows=snapshot.row_count,
        ledger_first_block=snapshot.first_block,
        ledger_last_block=snapshot.last_block,
        fixture_input_sha256=fixture_input_sha256,
    )
    return _canonical_json_bytes(_fixture_coverage_payload(coverage))


def load_ledger_coverage(pool: str, ledger_path: Path) -> LedgerCoverage:
    normalized_pool = _validated_pool(pool)
    ledger_snapshot = _read_ledger_snapshot(
        normalized_pool,
        ledger_path,
    )
    return _load_coverage_snapshot(
        normalized_pool,
        ledger_path,
        ledger_snapshot,
    ).coverage


def require_verified_ledger_coverage(
    pool: str,
    ledger_path: Path,
    *,
    required_end_block: int,
    required_end_timestamp_ms: int,
) -> VerifiedLedgerCoverageEvidence:
    normalized_pool = _validated_pool(pool)
    _require_positive_int(required_end_block, "required LP coverage end block")
    _require_positive_int(required_end_timestamp_ms, "required LP coverage end timestamp")
    ledger_snapshot = _read_ledger_snapshot(normalized_pool, ledger_path)
    coverage_snapshot = _load_coverage_snapshot(
        normalized_pool,
        ledger_path,
        ledger_snapshot,
    )
    return _verified_coverage_evidence(
        normalized_pool,
        coverage_snapshot,
        required_end_block=required_end_block,
        required_end_timestamp_ms=required_end_timestamp_ms,
    )


def load_verified_ledger_attribution_rows(
    pool: str,
    ledger_path: Path,
    *,
    required_end_block: int,
    required_end_timestamp_ms: int,
) -> VerifiedLedgerLoad:
    normalized_pool = _validated_pool(pool)
    _require_positive_int(required_end_block, "required LP coverage end block")
    _require_positive_int(required_end_timestamp_ms, "required LP coverage end timestamp")
    ledger_snapshot = _read_ledger_snapshot(normalized_pool, ledger_path)
    coverage_snapshot = _load_coverage_snapshot(
        normalized_pool,
        ledger_path,
        ledger_snapshot,
    )
    evidence = _verified_coverage_evidence(
        normalized_pool,
        coverage_snapshot,
        required_end_block=required_end_block,
        required_end_timestamp_ms=required_end_timestamp_ms,
    )
    rows = _load_ledger_attribution_rows_from_bytes(
        normalized_pool,
        ledger_snapshot.raw_bytes,
        source=ledger_path,
    )
    return VerifiedLedgerLoad(rows=rows, coverage=evidence)


def _verified_coverage_evidence(
    pool: PoolName,
    coverage_snapshot: _CoverageSnapshot,
    *,
    required_end_block: int,
    required_end_timestamp_ms: int,
) -> VerifiedLedgerCoverageEvidence:
    coverage = coverage_snapshot.coverage
    if not isinstance(coverage, RpcLedgerCoverage) or coverage.verification_mode != "rpc_verified":
        raise CrossPoolContractError(
            f"{pool} LP ledger coverage requires a full RPC scan"
        )
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[pool]
    if coverage.covered_start_block != orientation.inception_block:
        raise CrossPoolContractError(
            f"{pool} LP ledger coverage must begin at pool inception"
        )
    if coverage.covered_end_block < required_end_block:
        raise CrossPoolContractError(
            f"{pool} LP ledger coverage ends before its last common-interval swap block"
        )
    if coverage.covered_end_timestamp_ms < required_end_timestamp_ms:
        raise CrossPoolContractError(
            f"{pool} LP ledger coverage ends before the common activity end"
        )
    return VerifiedLedgerCoverageEvidence(
        schema_version=coverage.schema_version,
        sidecar_sha256=coverage_snapshot.sidecar_sha256,
        ledger_sha256=coverage.ledger_sha256,
        pool=coverage.pool,
        chain=coverage.chain,
        chain_id=coverage.chain_id,
        pool_id=coverage.pool_id,
        covered_start_block=coverage.covered_start_block,
        covered_start_block_hash=coverage.covered_start_block_hash,
        covered_start_timestamp_ms=coverage.covered_start_timestamp_ms,
        covered_end_block=coverage.covered_end_block,
        covered_end_block_hash=coverage.covered_end_block_hash,
        covered_end_timestamp_ms=coverage.covered_end_timestamp_ms,
        required_end_block=required_end_block,
        required_end_timestamp_ms=required_end_timestamp_ms,
        candidate_transaction_count=coverage.candidate_transaction_count,
        candidate_transactions_sha256=coverage.candidate_transactions_sha256,
        candidate_scan_source=coverage.candidate_scan_source,
        candidate_scan_status=coverage.candidate_scan_status,
    )


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


def _read_path_bytes(path: Path, *, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CrossPoolContractError(f"{label} does not exist or is unreadable: {path}") from exc


def _read_ledger_snapshot(pool: PoolName, path: Path) -> _LedgerSnapshot:
    return _ledger_snapshot_from_bytes(
        pool,
        _read_path_bytes(path, label="LP ledger CSV"),
        source=path,
    )


def _ledger_snapshot_from_bytes(
    pool: PoolName,
    raw_bytes: bytes,
    *,
    source: Path,
) -> _LedgerSnapshot:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError(
            f"LP ledger CSV must be valid UTF-8: {source}"
        ) from exc
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[pool]
    blocks: list[int] = []
    with io.StringIO(text, newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CrossPoolContractError(f"LP ledger CSV has no header: {source}")
        missing = sorted({"chain", "pool_id", "block_number"}.difference(reader.fieldnames))
        if missing:
            raise CrossPoolContractError(
                f"LP ledger CSV missing coverage fields {missing}: {source}"
            )
        for row_number, row in enumerate(reader, start=2):
            chain = _required_cell(row, "chain", row_number).lower()
            pool_id = _required_cell(row, "pool_id", row_number)
            if chain != orientation.chain or pool_id.lower() != orientation.pool_id.lower():
                raise _row_error(row_number, "chain or pool_id violates pool orientation")
            block_number = _parse_int(row, "block_number", row_number)
            _require_positive_int(block_number, "LP ledger block_number")
            if blocks and block_number < blocks[-1]:
                raise _row_error(row_number, "ledger block numbers must be nondecreasing")
            blocks.append(block_number)
    if not blocks:
        raise CrossPoolContractError(f"LP ledger CSV has no rows: {source}")
    return _LedgerSnapshot(
        raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        row_count=len(blocks),
        first_block=blocks[0],
        last_block=blocks[-1],
    )


def _load_coverage_snapshot(
    pool: PoolName,
    ledger_path: Path,
    ledger_snapshot: _LedgerSnapshot,
) -> _CoverageSnapshot:
    sidecar_path = ledger_coverage_path(ledger_path)
    sidecar_bytes = _read_path_bytes(
        sidecar_path,
        label="LP ledger coverage sidecar",
    )
    coverage = _coverage_from_bytes(pool, sidecar_bytes)
    if coverage.ledger_sha256 != ledger_snapshot.sha256:
        raise CrossPoolContractError("LP ledger coverage ledger SHA-256 does not match")
    if (
        coverage.ledger_rows,
        coverage.ledger_first_block,
        coverage.ledger_last_block,
    ) != (
        ledger_snapshot.row_count,
        ledger_snapshot.first_block,
        ledger_snapshot.last_block,
    ):
        raise CrossPoolContractError(
            "LP ledger coverage row facts do not match the ledger"
        )
    return _CoverageSnapshot(
        coverage=coverage,
        sidecar_sha256=hashlib.sha256(sidecar_bytes).hexdigest(),
    )


def _coverage_from_bytes(pool: PoolName, sidecar_bytes: bytes) -> LedgerCoverage:
    try:
        text = sidecar_bytes.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CrossPoolContractError(
            "LP ledger coverage sidecar must be valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise CrossPoolContractError("LP ledger coverage sidecar must contain one object")
    if _canonical_json_bytes(payload) != sidecar_bytes:
        raise CrossPoolContractError(
            "LP ledger coverage sidecar must use canonical JSON bytes"
        )

    mode = _json_string(payload, "verification_mode")
    if mode in ("rpc_verified", "candidate_list_unverified"):
        return _rpc_coverage_from_payload(pool, payload, mode)
    if mode == "fixture_unverified":
        return _fixture_coverage_from_payload(pool, payload)
    raise CrossPoolContractError("unsupported LP ledger coverage verification mode")


def _rpc_coverage_from_payload(
    pool: PoolName,
    payload: Mapping[str, object],
    mode: str,
) -> RpcLedgerCoverage:
    expected_fields = {
        "schema_version",
        "verification_mode",
        "pool",
        "chain",
        "chain_id",
        "pool_id",
        "covered_start_block",
        "covered_start_block_hash",
        "covered_start_timestamp_ms",
        "covered_end_block",
        "covered_end_block_hash",
        "covered_end_timestamp_ms",
        "ledger_sha256",
        "ledger_rows",
        "ledger_first_block",
        "ledger_last_block",
        "candidate_transaction_count",
        "candidate_transactions_sha256",
        "candidate_transaction_hashes",
        "candidate_scan_source",
        "candidate_scan_status",
    }
    _require_exact_json_fields(payload, expected_fields)
    candidate_hashes = payload.get("candidate_transaction_hashes")
    if not isinstance(candidate_hashes, list) or not all(
        isinstance(value, str) for value in candidate_hashes
    ):
        raise CrossPoolContractError(
            "LP ledger coverage candidate_transaction_hashes must be a string list"
        )
    source = _json_string(payload, "candidate_scan_source")
    if source not in (
        "pool_manager_modify_liquidity_plus_position_manager_transfer_logs",
        "explicit_candidate_list",
    ):
        raise CrossPoolContractError(
            "LP ledger coverage has an unsupported candidate scan source"
        )
    status = _json_string(payload, "candidate_scan_status")
    if status != "producer_attested":
        raise CrossPoolContractError(
            "LP ledger coverage candidate scan must be producer-attested"
        )
    return RpcLedgerCoverage(
        schema_version=_json_string(payload, "schema_version"),
        verification_mode=cast(RpcVerificationMode, mode),
        pool=_json_pool(payload, pool),
        chain=_json_string(payload, "chain"),
        chain_id=_json_int(payload, "chain_id"),
        pool_id=_json_string(payload, "pool_id"),
        covered_start_block=_json_int(payload, "covered_start_block"),
        covered_start_block_hash=_json_string(payload, "covered_start_block_hash"),
        covered_start_timestamp_ms=_json_int(payload, "covered_start_timestamp_ms"),
        covered_end_block=_json_int(payload, "covered_end_block"),
        covered_end_block_hash=_json_string(payload, "covered_end_block_hash"),
        covered_end_timestamp_ms=_json_int(payload, "covered_end_timestamp_ms"),
        ledger_sha256=_json_string(payload, "ledger_sha256"),
        ledger_rows=_json_int(payload, "ledger_rows"),
        ledger_first_block=_json_int(payload, "ledger_first_block"),
        ledger_last_block=_json_int(payload, "ledger_last_block"),
        candidate_transaction_count=_json_int(
            payload,
            "candidate_transaction_count",
        ),
        candidate_transactions_sha256=_json_string(
            payload,
            "candidate_transactions_sha256",
        ),
        candidate_transaction_hashes=tuple(candidate_hashes),
        candidate_scan_source=cast(CandidateScanSource, source),
        candidate_scan_status="producer_attested",
    )


def _fixture_coverage_from_payload(
    pool: PoolName,
    payload: Mapping[str, object],
) -> FixtureLedgerCoverage:
    expected_fields = {
        "schema_version",
        "verification_mode",
        "pool",
        "chain",
        "chain_id",
        "pool_id",
        "requested_start_block",
        "requested_end_block",
        "ledger_sha256",
        "ledger_rows",
        "ledger_first_block",
        "ledger_last_block",
        "fixture_input_sha256",
    }
    _require_exact_json_fields(payload, expected_fields)
    fixture_hashes = payload.get("fixture_input_sha256")
    if not isinstance(fixture_hashes, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in fixture_hashes.items()
    ):
        raise CrossPoolContractError(
            "LP ledger coverage fixture_input_sha256 must be a string mapping"
        )
    return FixtureLedgerCoverage(
        schema_version=_json_string(payload, "schema_version"),
        verification_mode="fixture_unverified",
        pool=_json_pool(payload, pool),
        chain=_json_string(payload, "chain"),
        chain_id=_json_int(payload, "chain_id"),
        pool_id=_json_string(payload, "pool_id"),
        requested_start_block=_json_int(payload, "requested_start_block"),
        requested_end_block=_json_int(payload, "requested_end_block"),
        ledger_sha256=_json_string(payload, "ledger_sha256"),
        ledger_rows=_json_int(payload, "ledger_rows"),
        ledger_first_block=_json_int(payload, "ledger_first_block"),
        ledger_last_block=_json_int(payload, "ledger_last_block"),
        fixture_input_sha256=cast(dict[str, str], fixture_hashes),
    )


def _rpc_coverage_payload(coverage: RpcLedgerCoverage) -> Mapping[str, object]:
    return {
        "schema_version": coverage.schema_version,
        "verification_mode": coverage.verification_mode,
        "pool": coverage.pool,
        "chain": coverage.chain,
        "chain_id": coverage.chain_id,
        "pool_id": coverage.pool_id,
        "covered_start_block": coverage.covered_start_block,
        "covered_start_block_hash": coverage.covered_start_block_hash,
        "covered_start_timestamp_ms": coverage.covered_start_timestamp_ms,
        "covered_end_block": coverage.covered_end_block,
        "covered_end_block_hash": coverage.covered_end_block_hash,
        "covered_end_timestamp_ms": coverage.covered_end_timestamp_ms,
        "ledger_sha256": coverage.ledger_sha256,
        "ledger_rows": coverage.ledger_rows,
        "ledger_first_block": coverage.ledger_first_block,
        "ledger_last_block": coverage.ledger_last_block,
        "candidate_transaction_count": coverage.candidate_transaction_count,
        "candidate_transactions_sha256": coverage.candidate_transactions_sha256,
        "candidate_transaction_hashes": list(coverage.candidate_transaction_hashes),
        "candidate_scan_source": coverage.candidate_scan_source,
        "candidate_scan_status": coverage.candidate_scan_status,
    }


def _fixture_coverage_payload(
    coverage: FixtureLedgerCoverage,
) -> Mapping[str, object]:
    return {
        "schema_version": coverage.schema_version,
        "verification_mode": coverage.verification_mode,
        "pool": coverage.pool,
        "chain": coverage.chain,
        "chain_id": coverage.chain_id,
        "pool_id": coverage.pool_id,
        "requested_start_block": coverage.requested_start_block,
        "requested_end_block": coverage.requested_end_block,
        "ledger_sha256": coverage.ledger_sha256,
        "ledger_rows": coverage.ledger_rows,
        "ledger_first_block": coverage.ledger_first_block,
        "ledger_last_block": coverage.ledger_last_block,
        "fixture_input_sha256": dict(coverage.fixture_input_sha256),
    }


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_transaction_hashes(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise CrossPoolContractError("candidate transaction hashes must be strings")
        candidate = value.strip().lower()
        if not _TX_HASH_PATTERN.fullmatch(candidate):
            raise CrossPoolContractError("candidate transaction hash must be 32-byte hex")
        normalized.append(candidate)
    if len(set(normalized)) != len(normalized):
        raise CrossPoolContractError("candidate transaction hashes must be unique")
    return tuple(sorted(normalized))


def _candidate_set_sha256(values: Sequence[str]) -> str:
    payload = json.dumps(
        tuple(values),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise CrossPoolContractError(f"LP ledger coverage has duplicate JSON key {key!r}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(value: str) -> object:
    raise CrossPoolContractError(
        f"LP ledger coverage has non-finite JSON constant {value!r}"
    )


def _require_exact_json_fields(
    payload: Mapping[str, object],
    expected_fields: set[str],
) -> None:
    missing = sorted(expected_fields.difference(payload))
    unknown = sorted(set(payload).difference(expected_fields))
    if missing:
        raise CrossPoolContractError(f"LP ledger coverage missing fields {missing}")
    if unknown:
        raise CrossPoolContractError(f"LP ledger coverage has unknown fields {unknown}")


def _json_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise CrossPoolContractError(f"LP ledger coverage {field} must be a nonempty string")
    return value


def _json_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CrossPoolContractError(f"LP ledger coverage {field} must be an integer")
    return value


def _json_pool(payload: Mapping[str, object], expected_pool: PoolName) -> PoolName:
    value = _json_string(payload, "pool")
    if value != expected_pool:
        raise CrossPoolContractError("LP ledger coverage names an unexpected pool")
    return expected_pool


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise CrossPoolContractError(f"{label} must be lowercase SHA-256 hex")


def _require_block_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or not _BLOCK_HASH_PATTERN.fullmatch(value):
        raise CrossPoolContractError(f"{label} must be a lowercase 32-byte hex hash")


def _row_error(row_number: int, message: str) -> CrossPoolContractError:
    return CrossPoolContractError(f"LP ledger row {row_number}: {message}")
