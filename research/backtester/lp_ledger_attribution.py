"""Strict LP-ledger attribution and exact opening-capital accounting."""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias, cast
from urllib.parse import urlsplit

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
    "explicit_candidate_list",
]

LEDGER_COVERAGE_SCHEMA_VERSION = "2.0.0"
UNVERIFIED_LEDGER_COVERAGE_SCHEMA_VERSION = "1.0.0"
FROZEN_REPLAY_PARSER_VERSION = "pool-history-replay-v2"
FROZEN_REPLAY_INPUT_FIELDS = (
    "block_time",
    "chain",
    "pool_id",
    "event_type",
    "tx_hash",
    "log_index",
    "block_number",
    "sqrt_price_x96",
    "tick",
    "active_liquidity",
    "fee_rate",
    "amount0",
    "amount1",
    "amount_usd",
    "cngn_usd_price",
    "token0_symbol",
    "token1_symbol",
    "event_source",
    "sender",
    "recipient",
    "currency0",
    "currency1",
    "hooks",
    "tick_spacing",
    "tick_lower",
    "tick_upper",
    "liquidity_delta",
    "salt",
    "amount0_raw",
    "amount1_raw",
)
FROZEN_REPLAY_EVENT_SOURCES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "initialize": frozenset({"pool_manager_initialize"}),
        "swap": frozenset({"pool_manager_swap"}),
        "mint": frozenset({"pool_manager_modify_liquidity"}),
        "burn": frozenset({"pool_manager_modify_liquidity"}),
        "collect": frozenset(
            {"pool_manager_modify_liquidity", "position_manager_take_pair"}
        ),
    }
)
FROZEN_REPLAY_PRICE_EVENT_TYPES = frozenset(("initialize", "swap"))


@dataclass(frozen=True)
class _FrozenReplayProfile:
    parser_version: str
    header_sha256: str
    parser_contract_sha256: str
    price_semantics_sha256: str


# Never revise a historical profile: sealed evidence selects it by stored version.
_FROZEN_REPLAY_V1_PROFILE = _FrozenReplayProfile(
    parser_version="pool-history-replay-v1",
    header_sha256="f847154f8e83e3db56ea1a7519128cece8832156bf932460b4d5e3eca3616b33",
    parser_contract_sha256=(
        "90b7905bf596f48c52a9e6d3dc957fd496f6f3188d9d48bdc20af1b8666ca462"
    ),
    price_semantics_sha256=(
        "0cef03f965d8879f52faba6dfb7bbfe52be6c998c5e66b7d453ee548d33be885"
    ),
)
_ACTIVE_FROZEN_REPLAY_PROFILE = _FrozenReplayProfile(
    parser_version=FROZEN_REPLAY_PARSER_VERSION,
    header_sha256="f847154f8e83e3db56ea1a7519128cece8832156bf932460b4d5e3eca3616b33",
    parser_contract_sha256=(
        "90b7905bf596f48c52a9e6d3dc957fd496f6f3188d9d48bdc20af1b8666ca462"
    ),
    price_semantics_sha256=(
        "1e42262f314bcbfc3c32f319ca97504511131b964781fe667c788c09cace2d53"
    ),
)
_FROZEN_REPLAY_PROFILES: Mapping[str, _FrozenReplayProfile] = MappingProxyType(
    {
        _FROZEN_REPLAY_V1_PROFILE.parser_version: _FROZEN_REPLAY_V1_PROFILE,
        _ACTIVE_FROZEN_REPLAY_PROFILE.parser_version: _ACTIVE_FROZEN_REPLAY_PROFILE,
    }
)
_FROZEN_REPLAY_PRICE_SEMANTICS_SOURCE_PATHS = (
    "engine/math/v3.py",
    "research/backtester/clmm_math.py",
    "research/backtester/pool_price_semantics.py",
)
_FROZEN_REPLAY_PARSER_SOURCE_SYMBOLS: Mapping[str, tuple[str, ...]] = (
    MappingProxyType(
        {
            "engine/web3_utils.py": ("coerce_hex_str",),
            "research/backtester/v4_event_replay.py": (
                "PoolStateSnapshot",
                "ReplayEvent",
                "ReplayedEvent",
                "_CARRIED_STATE_EVENTS",
                "_PRICE_EVENTS",
                "attach_event_time_state",
            ),
            "research/cross_pool/market_structure.py": (
                "ReplayStreamEvidence",
                "_EPOCH",
                "_POOL_ORDER",
                "_TX_HASH_PATTERN",
                "_load_replay_stream",
                "_parse_decimal",
                "_parse_int",
                "_parse_nonnegative_int",
                "_parse_positive_int",
                "_parse_timestamp_ms",
                "_required_cell",
                "_require_finite_decimal",
                "_require_nonnegative_int",
                "_require_positive_decimal",
                "_require_positive_int",
            ),
            "research/scripts/export_v4_lp_ledger.py": (
                "_PRICE_EVENT_TYPES",
                "_REPLAY_EVENT_SOURCES",
                "_REPLAY_EVENT_TYPES",
                "_REPLAY_INPUT_FIELDS",
                "_REPLAY_INPUT_PARSER_VERSION",
                "_load_frozen_replay_input",
                "_normalize_transaction_hash",
                "_replay_canonical_int",
                "_replay_required_text",
                "_replay_timestamp_ms",
                "_stage_price_replay",
            ),
        }
    )
)
_OWNER_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_ZERO_OWNER = "0x" + "0" * 40
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_BLOCK_HASH_PATTERN = re.compile(r"0x[0-9a-f]{64}\Z")
_TX_HASH_PATTERN = re.compile(r"0x[0-9a-f]{64}\Z")
_SUPPORTED_POOLS = frozenset(("uni-base", "uni-bsc"))
_POSITION_MANAGER_BY_POOL: Mapping[PoolName, str] = MappingProxyType(
    {
        "uni-base": "0x7c5f5a4bbd8fd63184577525326123b519429bdc",
        "uni-bsc": "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b",
    }
)
_POSITION_MANAGER_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa"
    "952ba7f163c4a11628f55a4df523b3ef"
)
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


def frozen_replay_evidence_sha256(value: object) -> str:
    """Hash one replay-evidence preimage using the frozen canonical JSON form."""
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _registered_frozen_replay_profile(parser_version: str) -> _FrozenReplayProfile:
    profile = _FROZEN_REPLAY_PROFILES.get(parser_version)
    if profile is None:
        raise CrossPoolContractError("replay parser profile is unsupported")
    return profile


def _active_frozen_replay_parser_contract_sha256() -> str:
    return frozen_replay_evidence_sha256(
        {
            "event_sources": {
                event_type: sorted(sources)
                for event_type, sources in FROZEN_REPLAY_EVENT_SOURCES.items()
            },
            "fields": list(FROZEN_REPLAY_INPUT_FIELDS),
            "implementation_sha256": _frozen_replay_parser_implementation_sha256(),
            "pool_orientations": {
                pool: {
                    "chain": orientation.chain,
                    "fee_rate": str(orientation.fee_rate),
                    "inception_block": orientation.inception_block,
                    "pool_id": orientation.pool_id,
                    "token0_decimals": orientation.token0_decimals,
                    "token0_symbol": orientation.token0_symbol,
                    "token1_decimals": orientation.token1_decimals,
                    "token1_symbol": orientation.token1_symbol,
                }
                for pool, orientation in POOL_ATTRIBUTION_ORIENTATIONS.items()
            },
            "price_event_types": sorted(FROZEN_REPLAY_PRICE_EVENT_TYPES),
        }
    )


def _frozen_replay_parser_implementation_sha256() -> dict[str, dict[str, str]]:
    repository_root = Path(__file__).resolve().parents[2]
    implementation: dict[str, dict[str, str]] = {}
    for relative_path, expected_symbols in _FROZEN_REPLAY_PARSER_SOURCE_SYMBOLS.items():
        source = (repository_root / relative_path).read_text(encoding="utf-8")
        source_lines = source.splitlines(keepends=True)
        tree = ast.parse(source, filename=relative_path)
        selected: dict[str, str] = {}
        for node in tree.body:
            names: tuple[str, ...]
            decorator_lines: tuple[int, ...]
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = (node.name,)
                decorator_lines = tuple(item.lineno for item in node.decorator_list)
            elif isinstance(node, ast.Assign):
                names = tuple(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
                decorator_lines = ()
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = (node.target.id,)
                decorator_lines = ()
            else:
                continue
            matched_names = tuple(name for name in names if name in expected_symbols)
            if not matched_names:
                continue
            if node.end_lineno is None:
                raise CrossPoolContractError(
                    f"frozen replay parser source is unreadable: {relative_path}"
                )
            start_line = min((node.lineno, *decorator_lines))
            segment = "".join(source_lines[start_line - 1 : node.end_lineno])
            digest = hashlib.sha256(segment.encode("utf-8")).hexdigest()
            for name in matched_names:
                selected[name] = digest
        if set(selected) != set(expected_symbols):
            raise CrossPoolContractError(
                f"frozen replay parser source is incomplete: {relative_path}"
            )
        implementation[relative_path] = {
            symbol: selected[symbol] for symbol in sorted(selected)
        }
    return implementation


def _active_frozen_replay_price_semantics_sha256() -> str:
    repository_root = Path(__file__).resolve().parents[2]
    return frozen_replay_evidence_sha256(
        {
            relative_path: hashlib.sha256(
                (repository_root / relative_path).read_bytes()
            ).hexdigest()
            for relative_path in _FROZEN_REPLAY_PRICE_SEMANTICS_SOURCE_PATHS
        }
    )


@lru_cache(maxsize=1)
def _active_frozen_replay_profile() -> _FrozenReplayProfile:
    profile = _registered_frozen_replay_profile(FROZEN_REPLAY_PARSER_VERSION)
    observed = (
        frozen_replay_evidence_sha256(list(FROZEN_REPLAY_INPUT_FIELDS)),
        _active_frozen_replay_parser_contract_sha256(),
        _active_frozen_replay_price_semantics_sha256(),
    )
    expected = (
        profile.header_sha256,
        profile.parser_contract_sha256,
        profile.price_semantics_sha256,
    )
    if observed != expected:
        raise CrossPoolContractError("active frozen replay profile does not match source")
    return profile


def frozen_replay_header_sha256() -> str:
    return _active_frozen_replay_profile().header_sha256


def frozen_replay_parser_contract_sha256() -> str:
    return _active_frozen_replay_profile().parser_contract_sha256


def frozen_replay_price_semantics_sha256() -> str:
    return _active_frozen_replay_profile().price_semantics_sha256


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
class WitnessSetCoverage:
    witness_count: int
    witnesses_sha256: str
    transaction_count: int
    transactions_sha256: str
    transaction_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.witness_count, "witness count")
        _require_sha256(self.witnesses_sha256, "witness set")
        hashes = _canonical_transaction_hashes(self.transaction_hashes)
        if hashes != self.transaction_hashes:
            raise CrossPoolContractError("witness transaction hashes are not canonical")
        if self.transaction_count != len(hashes):
            raise CrossPoolContractError("witness transaction count is inconsistent")
        if self.transactions_sha256 != _candidate_set_sha256(hashes):
            raise CrossPoolContractError("witness transaction digest is inconsistent")
        if self.witness_count < self.transaction_count:
            raise CrossPoolContractError("witness count is below transaction count")


@dataclass(frozen=True)
class FrozenTokenCoverage:
    token_count: int
    token_ids_sha256: str
    token_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        canonical = _canonical_token_ids(self.token_ids)
        if canonical != self.token_ids:
            raise CrossPoolContractError("frozen token IDs are not canonical")
        if self.token_count != len(canonical):
            raise CrossPoolContractError("frozen token count is inconsistent")
        if self.token_ids_sha256 != _canonical_set_sha256(list(canonical)):
            raise CrossPoolContractError("frozen token digest is inconsistent")


@dataclass(frozen=True)
class TransferChunkCoverage:
    index: int
    start_block: int
    end_block: int
    unfiltered_count: int
    unfiltered_sha256: str

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.index, "transfer chunk index")
        _require_positive_int(self.start_block, "transfer chunk start block")
        _require_positive_int(self.end_block, "transfer chunk end block")
        _require_nonnegative_int(self.unfiltered_count, "transfer chunk log count")
        _require_sha256(self.unfiltered_sha256, "transfer chunk")
        if self.end_block < self.start_block:
            raise CrossPoolContractError("transfer chunk range is inverted")


@dataclass(frozen=True)
class FullTransferCoverage:
    position_manager: str
    transfer_topic: str
    start_block: int
    end_block: int
    log_count: int
    chunks_sha256: str
    chunks: tuple[TransferChunkCoverage, ...]

    def __post_init__(self) -> None:
        _require_address(self.position_manager, "full transfer PositionManager")
        _require_block_hash(self.transfer_topic, "full transfer topic")
        _require_positive_int(self.start_block, "full transfer start block")
        _require_positive_int(self.end_block, "full transfer end block")
        _require_nonnegative_int(self.log_count, "full transfer log count")
        _require_sha256(self.chunks_sha256, "full transfer chunks")
        if not self.chunks:
            raise CrossPoolContractError("full transfer coverage requires parent chunks")
        expected_start = self.start_block
        for expected_index, chunk in enumerate(self.chunks):
            if chunk.index != expected_index or chunk.start_block != expected_start:
                raise CrossPoolContractError("full transfer chunks are not contiguous")
            expected_start = chunk.end_block + 1
        if expected_start - 1 != self.end_block:
            raise CrossPoolContractError("full transfer chunks do not cover the frozen range")
        if self.log_count != sum(chunk.unfiltered_count for chunk in self.chunks):
            raise CrossPoolContractError("full transfer log count is inconsistent")
        if self.chunks_sha256 != _canonical_set_sha256(
            [asdict(chunk) for chunk in self.chunks]
        ):
            raise CrossPoolContractError("full transfer chunk digest is inconsistent")


@dataclass(frozen=True)
class BundleDigestCoverage:
    transaction_hash: str
    payload_sha256: str

    def __post_init__(self) -> None:
        _require_transaction_hash(self.transaction_hash, "eligible bundle transaction")
        _require_sha256(self.payload_sha256, "eligible bundle payload")


@dataclass(frozen=True)
class EligibleBundleCoverage:
    transaction_count: int
    transactions_sha256: str
    bundles_sha256: str
    transaction_hashes: tuple[str, ...]
    bundles: tuple[BundleDigestCoverage, ...]

    def __post_init__(self) -> None:
        hashes = _canonical_transaction_hashes(self.transaction_hashes)
        if hashes != self.transaction_hashes:
            raise CrossPoolContractError("eligible bundle hashes are not canonical")
        canonical_bundles = tuple(
            sorted(self.bundles, key=lambda value: value.transaction_hash)
        )
        if canonical_bundles != self.bundles:
            raise CrossPoolContractError("eligible bundles are not canonical")
        bundle_hashes = tuple(bundle.transaction_hash for bundle in self.bundles)
        if hashes != bundle_hashes or self.transaction_count != len(hashes):
            raise CrossPoolContractError("eligible bundle transaction set is inconsistent")
        if self.transactions_sha256 != _candidate_set_sha256(hashes):
            raise CrossPoolContractError("eligible bundle transaction digest is inconsistent")
        if self.bundles_sha256 != _canonical_set_sha256(
            [asdict(bundle) for bundle in self.bundles]
        ):
            raise CrossPoolContractError("eligible bundle payload digest is inconsistent")


@dataclass(frozen=True)
class ReplayCoverage:
    sha256: str
    byte_length: int
    row_count: int
    header_sha256: str
    parser_version: str
    parser_contract_sha256: str
    price_semantics_sha256: str
    chain: str
    pool_id: str
    first_block: int
    last_block: int
    first_timestamp_ms: int
    last_timestamp_ms: int
    price_event_count: int
    price_events_sha256: str

    def __post_init__(self) -> None:
        for digest, label in (
            (self.sha256, "replay input"),
            (self.header_sha256, "replay header"),
            (self.parser_contract_sha256, "replay parser contract"),
            (self.price_semantics_sha256, "replay price semantics"),
            (self.price_events_sha256, "replay price events"),
        ):
            _require_sha256(digest, label)
        for numeric_value, label in (
            (self.byte_length, "replay byte length"),
            (self.row_count, "replay row count"),
            (self.first_block, "replay first block"),
            (self.last_block, "replay last block"),
            (self.first_timestamp_ms, "replay first timestamp"),
            (self.last_timestamp_ms, "replay last timestamp"),
            (self.price_event_count, "replay price event count"),
        ):
            _require_positive_int(numeric_value, label)
        profile = _registered_frozen_replay_profile(self.parser_version)
        if (
            self.header_sha256 != profile.header_sha256
            or self.parser_contract_sha256 != profile.parser_contract_sha256
            or self.price_semantics_sha256 != profile.price_semantics_sha256
        ):
            raise CrossPoolContractError("replay parser contract is inconsistent")
        if not self.chain:
            raise CrossPoolContractError("replay chain must be nonempty")
        _require_pool_id(self.pool_id, "replay pool ID")
        if self.last_block < self.first_block:
            raise CrossPoolContractError("replay block range is inverted")
        if self.last_timestamp_ms < self.first_timestamp_ms:
            raise CrossPoolContractError("replay timestamp range is inverted")
        if self.price_event_count > self.row_count:
            raise CrossPoolContractError("replay price event count exceeds row count")


@dataclass(frozen=True)
class ReconciliationCoverage:
    status: Literal["exact_success"]
    count: int
    sha256: str

    def __post_init__(self) -> None:
        if self.status != "exact_success":
            raise CrossPoolContractError("ledger reconciliation is not exact")
        _require_nonnegative_int(self.count, "reconciliation count")
        _require_sha256(self.sha256, "reconciliation")


@dataclass(frozen=True)
class AcquisitionPolicyCoverage:
    max_blocks_per_log_query: int
    retry_attempts: int
    subdivision: Literal["sequential_binary"]
    hidden_provider_retries: Literal["disabled"]
    completeness: Literal["provider_conditioned"]

    def __post_init__(self) -> None:
        if (
            self.max_blocks_per_log_query != 2_000
            or self.retry_attempts != 3
            or self.subdivision != "sequential_binary"
            or self.hidden_provider_retries != "disabled"
            or self.completeness != "provider_conditioned"
        ):
            raise CrossPoolContractError("unsupported RPC acquisition policy")


@dataclass(frozen=True)
class RpcLedgerEvidence:
    rpc_provider_origin: str
    acquisition_policy: AcquisitionPolicyCoverage
    action_witnesses: WitnessSetCoverage
    frozen_token_ids: FrozenTokenCoverage
    full_transfer_scan: FullTransferCoverage
    relevant_transfer_witnesses: WitnessSetCoverage
    eligible_bundles: EligibleBundleCoverage
    replay_input: ReplayCoverage
    action_reconciliation: ReconciliationCoverage
    ownership_reconciliation: ReconciliationCoverage

    def __post_init__(self) -> None:
        _require_provider_origin(self.rpc_provider_origin)


@dataclass(frozen=True)
class RpcLedgerCoverage:
    schema_version: Literal["2.0.0"]
    verification_mode: Literal["rpc_verified"]
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
    evidence: RpcLedgerEvidence
    attestation_sha256: str

    def __post_init__(self) -> None:
        _validate_rpc_coverage(self)


@dataclass(frozen=True)
class CandidateListLedgerCoverage:
    schema_version: Literal["1.0.0"]
    verification_mode: Literal["candidate_list_unverified"]
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
        _validate_unverified_candidate_coverage(self)


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
        if self.schema_version != UNVERIFIED_LEDGER_COVERAGE_SCHEMA_VERSION:
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


LedgerCoverage: TypeAlias = (
    RpcLedgerCoverage | CandidateListLedgerCoverage | FixtureLedgerCoverage
)


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
    ledger_rows: int
    ledger_first_block: int
    ledger_last_block: int
    evidence: RpcLedgerEvidence
    attestation_sha256: str


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
    evidence: RpcLedgerEvidence,
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
        evidence=evidence,
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
    evidence: RpcLedgerEvidence,
) -> bytes:
    normalized_pool = _validated_pool(pool)
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[normalized_pool]
    snapshot = _ledger_snapshot_from_bytes(
        normalized_pool,
        ledger_bytes,
        source=Path("<ledger-bytes>"),
    )
    common = {
        "schema_version": LEDGER_COVERAGE_SCHEMA_VERSION,
        "verification_mode": "rpc_verified",
        "pool": normalized_pool,
        "chain": orientation.chain,
        "chain_id": chain_id,
        "pool_id": orientation.pool_id,
        "covered_start_block": covered_start_block,
        "covered_start_block_hash": covered_start_block_hash.lower(),
        "covered_start_timestamp_ms": covered_start_timestamp_ms,
        "covered_end_block": covered_end_block,
        "covered_end_block_hash": covered_end_block_hash.lower(),
        "covered_end_timestamp_ms": covered_end_timestamp_ms,
        "ledger_sha256": snapshot.sha256,
        "ledger_rows": snapshot.row_count,
        "ledger_first_block": snapshot.first_block,
        "ledger_last_block": snapshot.last_block,
    }
    attestation_sha256 = _canonical_set_sha256(
        {**common, "evidence": _rpc_evidence_payload(evidence)}
    )
    coverage = RpcLedgerCoverage(
        schema_version="2.0.0",
        verification_mode="rpc_verified",
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
        evidence=evidence,
        attestation_sha256=attestation_sha256,
    )
    return rpc_ledger_coverage_bytes(coverage)


def rpc_ledger_coverage_bytes(coverage: RpcLedgerCoverage) -> bytes:
    """Serialize one validated verified sidecar using canonical bytes."""
    return _canonical_json_bytes(rpc_ledger_coverage_payload(coverage))


def rpc_ledger_coverage_payload(
    coverage: RpcLedgerCoverage,
) -> dict[str, object]:
    """Return the exact JSON-compatible payload attested by a verified sidecar."""
    if not isinstance(coverage, RpcLedgerCoverage):
        raise CrossPoolContractError("verified coverage payload requires RPC evidence")
    return dict(_rpc_coverage_payload(coverage))


def rpc_ledger_coverage_from_payload(
    pool: PoolName,
    payload: Mapping[str, object],
) -> RpcLedgerCoverage:
    """Parse and validate one exact verified sidecar payload mapping."""
    return _rpc_coverage_from_payload(pool, payload)


def write_candidate_list_ledger_coverage(
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
) -> Path:
    serialized = build_candidate_list_ledger_coverage_bytes(
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
    )
    sidecar_path = ledger_coverage_path(ledger_path)
    sidecar_path.write_bytes(serialized)
    return sidecar_path


def build_candidate_list_ledger_coverage_bytes(
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
) -> bytes:
    normalized_pool = _validated_pool(pool)
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[normalized_pool]
    snapshot = _ledger_snapshot_from_bytes(
        normalized_pool,
        ledger_bytes,
        source=Path("<ledger-bytes>"),
    )
    candidate_hashes = _canonical_transaction_hashes(candidate_transaction_hashes)
    coverage = CandidateListLedgerCoverage(
        schema_version="1.0.0",
        verification_mode="candidate_list_unverified",
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
        candidate_scan_source="explicit_candidate_list",
        candidate_scan_status="producer_attested",
    )
    return _canonical_json_bytes(_candidate_coverage_payload(coverage))


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
        schema_version=UNVERIFIED_LEDGER_COVERAGE_SCHEMA_VERSION,
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
        ledger_rows=coverage.ledger_rows,
        ledger_first_block=coverage.ledger_first_block,
        ledger_last_block=coverage.ledger_last_block,
        evidence=coverage.evidence,
        attestation_sha256=coverage.attestation_sha256,
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
    if mode == "rpc_verified":
        return _rpc_coverage_from_payload(pool, payload)
    if mode == "candidate_list_unverified":
        return _candidate_coverage_from_payload(pool, payload)
    if mode == "fixture_unverified":
        return _fixture_coverage_from_payload(pool, payload)
    raise CrossPoolContractError("unsupported LP ledger coverage verification mode")


def _rpc_coverage_from_payload(
    pool: PoolName,
    payload: Mapping[str, object],
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
        "evidence",
        "attestation_sha256",
    }
    _require_exact_json_fields(payload, expected_fields)
    return RpcLedgerCoverage(
        schema_version=cast(Literal["2.0.0"], _json_string(payload, "schema_version")),
        verification_mode=cast(
            Literal["rpc_verified"],
            _json_string(payload, "verification_mode"),
        ),
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
        evidence=_rpc_evidence_from_payload(_json_mapping(payload, "evidence")),
        attestation_sha256=_json_string(payload, "attestation_sha256"),
    )


def _candidate_coverage_from_payload(
    pool: PoolName,
    payload: Mapping[str, object],
) -> CandidateListLedgerCoverage:
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
    if source != "explicit_candidate_list":
        raise CrossPoolContractError(
            "LP ledger coverage has an unsupported candidate scan source"
        )
    status = _json_string(payload, "candidate_scan_status")
    if status != "producer_attested":
        raise CrossPoolContractError(
            "LP ledger coverage candidate scan must be producer-attested"
        )
    return CandidateListLedgerCoverage(
        schema_version=cast(Literal["1.0.0"], _json_string(payload, "schema_version")),
        verification_mode=cast(
            Literal["candidate_list_unverified"],
            _json_string(payload, "verification_mode"),
        ),
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


def _rpc_evidence_from_payload(payload: Mapping[str, object]) -> RpcLedgerEvidence:
    _require_exact_json_fields(
        payload,
        {
            "rpc_provider_origin",
            "acquisition_policy",
            "action_witnesses",
            "frozen_token_ids",
            "full_transfer_scan",
            "relevant_transfer_witnesses",
            "eligible_bundles",
            "replay_input",
            "action_reconciliation",
            "ownership_reconciliation",
        },
    )
    return RpcLedgerEvidence(
        rpc_provider_origin=_json_string(payload, "rpc_provider_origin"),
        acquisition_policy=_acquisition_policy_from_payload(
            _json_mapping(payload, "acquisition_policy")
        ),
        action_witnesses=_witness_set_from_payload(
            _json_mapping(payload, "action_witnesses")
        ),
        frozen_token_ids=_frozen_tokens_from_payload(
            _json_mapping(payload, "frozen_token_ids")
        ),
        full_transfer_scan=_full_transfer_from_payload(
            _json_mapping(payload, "full_transfer_scan")
        ),
        relevant_transfer_witnesses=_witness_set_from_payload(
            _json_mapping(payload, "relevant_transfer_witnesses")
        ),
        eligible_bundles=_eligible_bundles_from_payload(
            _json_mapping(payload, "eligible_bundles")
        ),
        replay_input=_replay_coverage_from_payload(
            _json_mapping(payload, "replay_input")
        ),
        action_reconciliation=_reconciliation_from_payload(
            _json_mapping(payload, "action_reconciliation")
        ),
        ownership_reconciliation=_reconciliation_from_payload(
            _json_mapping(payload, "ownership_reconciliation")
        ),
    )


def _witness_set_from_payload(payload: Mapping[str, object]) -> WitnessSetCoverage:
    _require_exact_json_fields(
        payload,
        {
            "witness_count",
            "witnesses_sha256",
            "transaction_count",
            "transactions_sha256",
            "transaction_hashes",
        },
    )
    return WitnessSetCoverage(
        witness_count=_json_int(payload, "witness_count"),
        witnesses_sha256=_json_string(payload, "witnesses_sha256"),
        transaction_count=_json_int(payload, "transaction_count"),
        transactions_sha256=_json_string(payload, "transactions_sha256"),
        transaction_hashes=_json_string_list(payload, "transaction_hashes"),
    )


def _frozen_tokens_from_payload(payload: Mapping[str, object]) -> FrozenTokenCoverage:
    _require_exact_json_fields(
        payload,
        {"token_count", "token_ids_sha256", "token_ids"},
    )
    return FrozenTokenCoverage(
        token_count=_json_int(payload, "token_count"),
        token_ids_sha256=_json_string(payload, "token_ids_sha256"),
        token_ids=_json_string_list(payload, "token_ids"),
    )


def _full_transfer_from_payload(payload: Mapping[str, object]) -> FullTransferCoverage:
    _require_exact_json_fields(
        payload,
        {
            "position_manager",
            "transfer_topic",
            "start_block",
            "end_block",
            "log_count",
            "chunks_sha256",
            "chunks",
        },
    )
    chunks = tuple(
        _transfer_chunk_from_payload(item)
        for item in _json_mapping_list(payload, "chunks")
    )
    return FullTransferCoverage(
        position_manager=_json_string(payload, "position_manager"),
        transfer_topic=_json_string(payload, "transfer_topic"),
        start_block=_json_int(payload, "start_block"),
        end_block=_json_int(payload, "end_block"),
        log_count=_json_int(payload, "log_count"),
        chunks_sha256=_json_string(payload, "chunks_sha256"),
        chunks=chunks,
    )


def _transfer_chunk_from_payload(
    payload: Mapping[str, object],
) -> TransferChunkCoverage:
    _require_exact_json_fields(
        payload,
        {
            "index",
            "start_block",
            "end_block",
            "unfiltered_count",
            "unfiltered_sha256",
        },
    )
    return TransferChunkCoverage(
        index=_json_int(payload, "index"),
        start_block=_json_int(payload, "start_block"),
        end_block=_json_int(payload, "end_block"),
        unfiltered_count=_json_int(payload, "unfiltered_count"),
        unfiltered_sha256=_json_string(payload, "unfiltered_sha256"),
    )


def _eligible_bundles_from_payload(
    payload: Mapping[str, object],
) -> EligibleBundleCoverage:
    _require_exact_json_fields(
        payload,
        {
            "transaction_count",
            "transactions_sha256",
            "bundles_sha256",
            "transaction_hashes",
            "bundles",
        },
    )
    bundles = tuple(
        _bundle_digest_from_payload(item)
        for item in _json_mapping_list(payload, "bundles")
    )
    return EligibleBundleCoverage(
        transaction_count=_json_int(payload, "transaction_count"),
        transactions_sha256=_json_string(payload, "transactions_sha256"),
        bundles_sha256=_json_string(payload, "bundles_sha256"),
        transaction_hashes=_json_string_list(payload, "transaction_hashes"),
        bundles=bundles,
    )


def _bundle_digest_from_payload(
    payload: Mapping[str, object],
) -> BundleDigestCoverage:
    _require_exact_json_fields(payload, {"transaction_hash", "payload_sha256"})
    return BundleDigestCoverage(
        transaction_hash=_json_string(payload, "transaction_hash"),
        payload_sha256=_json_string(payload, "payload_sha256"),
    )


def _replay_coverage_from_payload(payload: Mapping[str, object]) -> ReplayCoverage:
    _require_exact_json_fields(
        payload,
        {
            "sha256",
            "byte_length",
            "row_count",
            "header_sha256",
            "parser_version",
            "parser_contract_sha256",
            "price_semantics_sha256",
            "chain",
            "pool_id",
            "first_block",
            "last_block",
            "first_timestamp_ms",
            "last_timestamp_ms",
            "price_event_count",
            "price_events_sha256",
        },
    )
    return ReplayCoverage(
        sha256=_json_string(payload, "sha256"),
        byte_length=_json_int(payload, "byte_length"),
        row_count=_json_int(payload, "row_count"),
        header_sha256=_json_string(payload, "header_sha256"),
        parser_version=_json_string(payload, "parser_version"),
        parser_contract_sha256=_json_string(payload, "parser_contract_sha256"),
        price_semantics_sha256=_json_string(payload, "price_semantics_sha256"),
        chain=_json_string(payload, "chain"),
        pool_id=_json_string(payload, "pool_id"),
        first_block=_json_int(payload, "first_block"),
        last_block=_json_int(payload, "last_block"),
        first_timestamp_ms=_json_int(payload, "first_timestamp_ms"),
        last_timestamp_ms=_json_int(payload, "last_timestamp_ms"),
        price_event_count=_json_int(payload, "price_event_count"),
        price_events_sha256=_json_string(payload, "price_events_sha256"),
    )


def _reconciliation_from_payload(
    payload: Mapping[str, object],
) -> ReconciliationCoverage:
    _require_exact_json_fields(payload, {"status", "count", "sha256"})
    return ReconciliationCoverage(
        status=cast(Literal["exact_success"], _json_string(payload, "status")),
        count=_json_int(payload, "count"),
        sha256=_json_string(payload, "sha256"),
    )


def _acquisition_policy_from_payload(
    payload: Mapping[str, object],
) -> AcquisitionPolicyCoverage:
    _require_exact_json_fields(
        payload,
        {
            "max_blocks_per_log_query",
            "retry_attempts",
            "subdivision",
            "hidden_provider_retries",
            "completeness",
        },
    )
    return AcquisitionPolicyCoverage(
        max_blocks_per_log_query=_json_int(payload, "max_blocks_per_log_query"),
        retry_attempts=_json_int(payload, "retry_attempts"),
        subdivision=cast(
            Literal["sequential_binary"],
            _json_string(payload, "subdivision"),
        ),
        hidden_provider_retries=cast(
            Literal["disabled"],
            _json_string(payload, "hidden_provider_retries"),
        ),
        completeness=cast(
            Literal["provider_conditioned"],
            _json_string(payload, "completeness"),
        ),
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
        **_rpc_coverage_attestation_payload(coverage),
        "attestation_sha256": coverage.attestation_sha256,
    }


def _rpc_coverage_attestation_payload(
    coverage: RpcLedgerCoverage,
) -> dict[str, object]:
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
        "evidence": _rpc_evidence_payload(coverage.evidence),
    }


def _rpc_coverage_attestation_sha256(coverage: RpcLedgerCoverage) -> str:
    return _canonical_set_sha256(_rpc_coverage_attestation_payload(coverage))


def _rpc_evidence_payload(evidence: RpcLedgerEvidence) -> dict[str, object]:
    encoded = json.dumps(
        asdict(evidence),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    payload = json.loads(encoded)
    if not isinstance(payload, dict):  # pragma: no cover - dataclass root is fixed.
        raise CrossPoolContractError("RPC ledger evidence must encode as an object")
    return cast(dict[str, object], payload)


def _candidate_coverage_payload(
    coverage: CandidateListLedgerCoverage,
) -> Mapping[str, object]:
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


def _canonical_set_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_token_ids(values: Sequence[str]) -> tuple[str, ...]:
    parsed: list[tuple[int, str]] = []
    for value in values:
        if not isinstance(value, str):
            raise CrossPoolContractError("frozen token IDs must be strings")
        try:
            token_id = int(value)
        except ValueError as exc:
            raise CrossPoolContractError("frozen token ID is not decimal") from exc
        if str(token_id) != value or not 0 <= token_id < 2**256:
            raise CrossPoolContractError("frozen token ID is not canonical uint256")
        parsed.append((token_id, value))
    if len({item[0] for item in parsed}) != len(parsed):
        raise CrossPoolContractError("frozen token IDs must be unique")
    return tuple(value for _, value in sorted(parsed))


def _require_address(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or value != value.lower()
        or not _OWNER_PATTERN.fullmatch(value)
    ):
        raise CrossPoolContractError(f"{label} must be a lowercase address")


def _require_pool_id(value: str, label: str) -> None:
    _require_block_hash(value, label)


def _require_transaction_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or not _TX_HASH_PATTERN.fullmatch(value):
        raise CrossPoolContractError(f"{label} must be a lowercase transaction hash")


def _require_provider_origin(value: str) -> None:
    if not isinstance(value, str):
        raise CrossPoolContractError("RPC provider origin must be a string")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CrossPoolContractError("RPC provider origin is invalid") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme not in ("http", "https")
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or ":" in hostname
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise CrossPoolContractError("RPC provider origin is invalid")
    expected = f"{parsed.scheme}://{hostname.lower()}"
    if port is not None:
        expected = f"{expected}:{port}"
    if value != expected:
        raise CrossPoolContractError("RPC provider origin is not canonical")


def _validate_common_rpc_coverage(
    coverage: RpcLedgerCoverage | CandidateListLedgerCoverage,
) -> None:
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[_validated_pool(coverage.pool)]
    if (
        coverage.chain != orientation.chain
        or coverage.chain_id != orientation.chain_id
        or coverage.pool_id != orientation.pool_id
    ):
        raise CrossPoolContractError("LP ledger coverage violates pool orientation")
    for value, label in (
        (coverage.covered_start_block, "covered_start_block"),
        (coverage.covered_start_timestamp_ms, "covered_start_timestamp_ms"),
        (coverage.covered_end_block, "covered_end_block"),
        (coverage.covered_end_timestamp_ms, "covered_end_timestamp_ms"),
        (coverage.ledger_rows, "ledger_rows"),
        (coverage.ledger_first_block, "ledger_first_block"),
        (coverage.ledger_last_block, "ledger_last_block"),
    ):
        _require_positive_int(value, f"LP ledger coverage {label}")
    if coverage.covered_start_block > coverage.covered_end_block:
        raise CrossPoolContractError("LP ledger coverage block range is inverted")
    if coverage.covered_start_timestamp_ms > coverage.covered_end_timestamp_ms:
        raise CrossPoolContractError("LP ledger coverage timestamp range is inverted")
    if not (
        coverage.covered_start_block
        <= coverage.ledger_first_block
        <= coverage.ledger_last_block
        <= coverage.covered_end_block
    ):
        raise CrossPoolContractError("LP ledger rows fall outside the covered block range")
    _require_sha256(coverage.ledger_sha256, "LP ledger coverage ledger SHA-256")
    _require_block_hash(
        coverage.covered_start_block_hash,
        "LP ledger coverage start block hash",
    )
    _require_block_hash(
        coverage.covered_end_block_hash,
        "LP ledger coverage end block hash",
    )


def _validate_rpc_coverage(coverage: RpcLedgerCoverage) -> None:
    if (
        coverage.schema_version != LEDGER_COVERAGE_SCHEMA_VERSION
        or coverage.verification_mode != "rpc_verified"
    ):
        raise CrossPoolContractError("unsupported verified LP ledger coverage schema")
    _validate_common_rpc_coverage(coverage)
    evidence = coverage.evidence
    if (
        evidence.replay_input.chain != coverage.chain
        or evidence.replay_input.pool_id != coverage.pool_id
        or evidence.replay_input.first_block != coverage.covered_start_block
        or evidence.replay_input.last_block != coverage.covered_end_block
        or evidence.replay_input.first_timestamp_ms
        != coverage.covered_start_timestamp_ms
        or evidence.replay_input.last_timestamp_ms
        != coverage.covered_end_timestamp_ms
        or evidence.full_transfer_scan.start_block != coverage.covered_start_block
        or evidence.full_transfer_scan.end_block != coverage.covered_end_block
        or evidence.full_transfer_scan.position_manager
        != _POSITION_MANAGER_BY_POOL[coverage.pool]
        or evidence.full_transfer_scan.transfer_topic
        != _POSITION_MANAGER_TRANSFER_TOPIC
    ):
        raise CrossPoolContractError(
            "verified coverage evidence identity or range is inconsistent"
        )
    expected_eligible = tuple(
        sorted(
            set(evidence.action_witnesses.transaction_hashes).union(
                evidence.relevant_transfer_witnesses.transaction_hashes
            )
        )
    )
    if evidence.eligible_bundles.transaction_hashes != expected_eligible:
        raise CrossPoolContractError("eligible bundles differ from attested transactions")
    if evidence.frozen_token_ids.token_count == 0:
        raise CrossPoolContractError("verified coverage has an empty frozen token set")
    if (
        evidence.relevant_transfer_witnesses.witness_count
        > evidence.full_transfer_scan.log_count
    ):
        raise CrossPoolContractError(
            "relevant transfer witnesses exceed the full transfer scan"
        )
    if evidence.ownership_reconciliation.count != (
        evidence.relevant_transfer_witnesses.witness_count
    ):
        raise CrossPoolContractError("ownership reconciliation count is inconsistent")
    if not (
        evidence.action_reconciliation.count
        == coverage.ledger_rows
        == evidence.action_witnesses.witness_count
    ):
        raise CrossPoolContractError("action reconciliation count is inconsistent")
    _require_sha256(coverage.attestation_sha256, "verified coverage attestation")
    if coverage.attestation_sha256 != _rpc_coverage_attestation_sha256(coverage):
        raise CrossPoolContractError("verified coverage attestation digest is inconsistent")


def _validate_unverified_candidate_coverage(
    coverage: CandidateListLedgerCoverage,
) -> None:
    if (
        coverage.schema_version != UNVERIFIED_LEDGER_COVERAGE_SCHEMA_VERSION
        or coverage.verification_mode != "candidate_list_unverified"
    ):
        raise CrossPoolContractError("unsupported candidate-list coverage schema")
    _validate_common_rpc_coverage(coverage)
    hashes = _canonical_transaction_hashes(coverage.candidate_transaction_hashes)
    if hashes != coverage.candidate_transaction_hashes:
        raise CrossPoolContractError("candidate transaction hashes are not canonical")
    if coverage.candidate_transaction_count != len(hashes) or not hashes:
        raise CrossPoolContractError("candidate transaction count is inconsistent")
    if coverage.candidate_transactions_sha256 != _candidate_set_sha256(hashes):
        raise CrossPoolContractError("candidate transaction digest is inconsistent")
    if (
        coverage.candidate_scan_source != "explicit_candidate_list"
        or coverage.candidate_scan_status != "producer_attested"
    ):
        raise CrossPoolContractError("candidate-list scan evidence is invalid")


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


def _json_mapping(
    payload: Mapping[str, object],
    field: str,
) -> Mapping[str, object]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise CrossPoolContractError(f"LP ledger coverage {field} must be an object")
    return cast(dict[str, object], value)


def _json_string_list(
    payload: Mapping[str, object],
    field: str,
) -> tuple[str, ...]:
    value = payload.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CrossPoolContractError(
            f"LP ledger coverage {field} must be a string list"
        )
    return tuple(cast(list[str], value))


def _json_mapping_list(
    payload: Mapping[str, object],
    field: str,
) -> tuple[Mapping[str, object], ...]:
    value = payload.get(field)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CrossPoolContractError(
            f"LP ledger coverage {field} must be an object list"
        )
    return tuple(cast(list[dict[str, object]], value))


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
