"""Durable operational state for verified V4 LP-ledger exports."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from web3 import Web3

from research.backtester.v4_lp_ledger import (
    DecodedLiquidityAction,
    LedgerPositionState,
    OwnershipEvent,
)

CHECKPOINT_APPLICATION_ID = 1_280_330_819
CHECKPOINT_SCHEMA_VERSION = 2
PROGRESS_SCHEMA_VERSION = 2
_MAX_PROGRESS_BYTES = 1_048_576
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1
CHECKPOINT_SOURCE_PATHS = (
    "research/backtester/lp_ledger_attribution.py",
    "research/backtester/lp_ledger_checkpoint.py",
    "research/backtester/pool_price_semantics.py",
    "research/backtester/v4_event_replay.py",
    "research/backtester/v4_export.py",
    "research/backtester/v4_lp_ledger.py",
    "research/scripts/export_v4_lp_ledger.py",
)
_SAFE_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]*", re.ASCII)
_PHASES = (
    "preflight",
    "action_discovery",
    "action_fetch",
    "action_decode",
    "token_set_freeze",
    "full_transfer_scan",
    "relevant_transfer_fetch",
    "relevant_transfer_decode",
    "replay_input_bind",
    "price_replay",
    "build",
    "publish",
    "succeeded",
)


def _mutation_hook(_stage: Literal["before_commit", "after_commit"]) -> None:
    """Test seam for deterministic crash-boundary assertions."""


Phase = Literal[
    "preflight",
    "action_discovery",
    "action_fetch",
    "action_decode",
    "token_set_freeze",
    "full_transfer_scan",
    "relevant_transfer_fetch",
    "relevant_transfer_decode",
    "replay_input_bind",
    "price_replay",
    "build",
    "publish",
    "succeeded",
]
RunStatus = Literal["running", "succeeded", "failed", "interrupted"]
PhaseStatus = Literal["pending", "running", "completed"]
ErrorCode = Literal[
    "rpc_error",
    "checkpoint_incompatible",
    "checkpoint_corrupt",
    "decode_error",
    "replay_error",
    "publication_error",
    "checkpoint_maintenance_error",
    "interrupted",
    "unknown_error",
]
ProgressMode = Literal["rpc_verified", "candidate_list_unverified", "fixture"]
ProgressState = Literal["current", "progress_unavailable"]
ExportStatusState = Literal[
    "active",
    "interrupted",
    "failed",
    "succeeded",
    "blocked",
    "non_resumable",
    "unknown",
]
_ERROR_CODES: tuple[ErrorCode, ...] = (
    "rpc_error",
    "checkpoint_incompatible",
    "checkpoint_corrupt",
    "decode_error",
    "replay_error",
    "publication_error",
    "checkpoint_maintenance_error",
    "interrupted",
    "unknown_error",
)
_PROGRESS_MODES: tuple[ProgressMode, ...] = (
    "rpc_verified",
    "candidate_list_unverified",
    "fixture",
)
_PUBLIC_DURABLE_KEYS = frozenset(
    {
        "block_number",
        "bound_actions",
        "checks",
        "chunk_index",
        "durable_end_block",
        "files",
        "output",
        "rows",
        "token_set_sha256",
        "unfiltered_transfer_sha256",
        "replay_input_sha256",
        "transaction_hash",
        "transaction_index",
    }
)
_INTERNAL_DURABLE_COUNTER_KEYS = frozenset({"action_count", "event_count", "ownership_count"})
DiscoverySource = Literal["pool_modify", "position_transfer"]
EventTimeStateSource = Literal[
    "self_event",
    "prior_event",
    "prior_block",
    "same_block_prior_event",
]


class CheckpointContractError(ValueError):
    """Raised when a checkpoint path or persisted contract is invalid."""


@dataclass(frozen=True)
class BlockRange:
    index: int
    start_block: int
    end_block: int


@dataclass(frozen=True)
class DiscoveryWitness:
    source: DiscoverySource
    block_number: int
    block_hash: str
    transaction_hash: str
    transaction_index: int
    log_index: int
    address: str
    topics: tuple[str, ...]
    data: str


@dataclass(frozen=True)
class PositionKeyMapping:
    token_id: int
    pool_id: str
    tick_lower: int
    tick_upper: int
    salt: str
    mint_block_number: int
    mint_log_index: int
    mint_event_order: int


@dataclass(frozen=True)
class ReplayInputEvidence:
    path: str
    sha256: str
    byte_length: int
    row_count: int
    header_sha256: str
    parser_version: str
    price_semantics_sha256: str
    chain: str
    pool_id: str
    first_block: int
    last_block: int
    first_timestamp_ms: int
    last_timestamp_ms: int
    price_event_count: int
    price_events_sha256: str


@dataclass(frozen=True)
class TransferChunkAttestation:
    index: int
    start_block: int
    end_block: int
    unfiltered_count: int
    unfiltered_sha256: str
    relevant_count: int


@dataclass(frozen=True)
class CandidateBundle:
    transaction_hash: str
    block_number: int
    block_hash: str
    transaction_index: int
    transaction_json: str
    receipt_json: str
    payload_sha256: str


@dataclass(frozen=True)
class PositionResolution:
    token_id: int
    query_block: int
    found: bool
    state: LedgerPositionState | None


@dataclass(frozen=True)
class DecoderStateUpsert:
    token_id: int
    state: LedgerPositionState
    last_block_number: int
    last_log_index: int
    last_event_order: int


@dataclass(frozen=True)
class ActionPriceBinding:
    block_number: int
    log_index: int
    event_order: int
    event_time_sqrt_price_x96: int
    event_time_tick: int
    event_time_state_source: EventTimeStateSource


@dataclass(frozen=True)
class PublicationState:
    state: Literal["not_started", "started", "published"]
    expected_ledger_sha256: str | None
    expected_sidecar_sha256: str | None
    started_generation: int | None
    published_generation: int | None


@dataclass(frozen=True)
class BuildInputs:
    action_witnesses: tuple[DiscoveryWitness, ...]
    action_transaction_hashes: tuple[str, ...]
    frozen_token_ids: tuple[int, ...]
    position_keys: tuple[PositionKeyMapping, ...]
    transfer_chunk_attestations: tuple[TransferChunkAttestation, ...]
    relevant_transfer_witnesses: tuple[DiscoveryWitness, ...]
    relevant_transfer_transaction_hashes: tuple[str, ...]
    eligible_bundles: tuple[CandidateBundle, ...]
    replay_input: ReplayInputEvidence
    actions: tuple[DecodedLiquidityAction, ...]
    ownership_events: tuple[OwnershipEvent, ...]
    action_price_bindings: tuple[ActionPriceBinding, ...]


@dataclass(frozen=True)
class CheckpointPaths:
    output: Path
    database: Path
    wal: Path
    shm: Path
    progress: Path
    run_lock: Path
    publish_lock: Path

    @classmethod
    def from_output(cls, output: Path) -> CheckpointPaths:
        normalized = Path(os.path.abspath(os.fspath(output)))
        if normalized.suffix != ".csv":
            raise CheckpointContractError("LP ledger checkpoint output must end in .csv")
        database = normalized.with_name(f"{normalized.name}.checkpoint.sqlite3")
        return cls(
            output=normalized,
            database=database,
            wal=Path(f"{database}-wal"),
            shm=Path(f"{database}-shm"),
            progress=normalized.with_name(f"{normalized.name}.progress.json"),
            run_lock=normalized.with_name(f"{normalized.name}.run.lock"),
            publish_lock=normalized.with_name(f"{normalized.name}.publish.lock"),
        )


@dataclass(frozen=True)
class BlockHeader:
    block_number: int
    block_hash: str
    timestamp_ms: int


@dataclass(frozen=True)
class EndpointSnapshot:
    start: BlockHeader
    end: BlockHeader


@dataclass(frozen=True)
class RunIdentity:
    schema_version: int
    exporter_version: str
    verification_mode: Literal["rpc_verified"]
    pool: str
    chain: str
    chain_id: int
    pool_id: str
    pool_manager: str
    position_manager: str
    wrapper_entrypoint: str
    token0_address: str
    token1_address: str
    token0_decimals: int
    token1_decimals: int
    fee_rate: str
    invert_price: bool
    start_block: int
    end_block: int
    chunk_size: int
    action_topic: str
    transfer_topic: str
    replay_input: ReplayInputEvidence
    output_path: str
    endpoint: EndpointSnapshot
    python_version: str
    web3_version: str
    source_sha256: tuple[tuple[str, str], ...]

    def canonical_bytes(self) -> bytes:
        _validate_identity(self)
        return json.dumps(
            asdict(self),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class ActionDecodeIdentity:
    pool: str
    chain: str
    pool_id: str
    pool_manager: str
    position_manager: str
    wrapper_entrypoint: str


@dataclass(frozen=True)
class PhaseProgress:
    phase: Phase
    status: PhaseStatus
    unit: str
    completed: int
    total: int | None


@dataclass(frozen=True)
class CheckpointSnapshot:
    run_id: str
    identity_sha256: str
    attempt_number: int
    generation: int
    status: RunStatus
    phase: Phase
    started_at_utc: str
    resumed_at_utc: str
    updated_at_utc: str
    finished_at_utc: str | None
    error_code: ErrorCode | None
    output_published: bool
    pool: str
    mode: ProgressMode
    start_block: int
    end_block: int
    output_filename: str
    phases: tuple[PhaseProgress, ...]
    last_durable: tuple[tuple[str, str], ...] | None
    action_candidate_transaction_count: int
    action_count: int
    frozen_token_count: int
    full_transfer_log_count: int
    relevant_transfer_witness_count: int
    relevant_transfer_transaction_count: int
    ownership_event_count: int
    bound_action_count: int
    ledger_row_count: int


@dataclass(frozen=True)
class ProgressSnapshot:
    schema_version: int
    run_id: str
    attempt_number: int
    checkpoint_generation: int | None
    status: RunStatus
    pool: str
    mode: ProgressMode
    start_block: int
    end_block: int
    output_filename: str
    phase: Phase
    phases: tuple[PhaseProgress, ...]
    last_durable: tuple[tuple[str, str], ...] | None
    action_candidate_transaction_count: int
    action_count: int
    frozen_token_count: int
    full_transfer_log_count: int
    relevant_transfer_witness_count: int
    relevant_transfer_transaction_count: int
    ownership_event_count: int
    bound_action_count: int
    ledger_row_count: int
    rate_per_second: str | None
    eta_seconds: int | None
    started_at: str
    resumed_at: str
    updated_at: str
    finished_at: str | None
    output_published: bool
    error_code: ErrorCode | None
    sample_monotonic_seconds: float | None = dataclass_field(compare=False, repr=False)

    def canonical_bytes(self) -> bytes:
        return _progress_canonical_bytes(self)


@dataclass(frozen=True)
class ExportStatus:
    output: str
    pool: str | None
    mode: ProgressMode | None
    start_block: int | None
    end_block: int | None
    state: ExportStatusState
    phase: Phase | None
    unit: str | None
    completed: int | None
    total: int | None
    last_durable: tuple[tuple[str, str], ...] | None
    action_candidate_transaction_count: int | None
    action_count: int | None
    frozen_token_count: int | None
    full_transfer_log_count: int | None
    relevant_transfer_witness_count: int | None
    relevant_transfer_transaction_count: int | None
    ownership_event_count: int | None
    bound_action_count: int | None
    ledger_row_count: int | None
    run_id: str | None
    attempt_number: int | None
    checkpoint_generation: int | None
    updated_at: str | None
    updated_age_seconds: int | None
    rate_per_second: str | None
    eta_seconds: int | None
    progress_state: ProgressState
    lock_held: bool
    locally_compatible: bool
    resumable: bool
    output_published: bool
    error_code: ErrorCode | None

    def as_dict(self) -> dict[str, object]:
        return _export_status_payload(self)


@dataclass
class RunLock:
    path: Path
    descriptor: int | None

    def close(self) -> None:
        if self.descriptor is None:
            return
        descriptor = self.descriptor
        self.descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def acquire_run_lock(paths: CheckpointPaths) -> Iterator[RunLock]:
    _ensure_secure_parent(paths.run_lock)
    descriptor = _open_regular_leaf(
        paths.run_lock,
        os.O_RDWR | os.O_CREAT,
        mode=0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CheckpointContractError("LP ledger export is already active") from exc
        lock = RunLock(path=paths.run_lock, descriptor=descriptor)
        descriptor = -1
        try:
            yield lock
        finally:
            lock.close()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class LPLedgerCheckpoint:
    def __init__(
        self,
        paths: CheckpointPaths,
        connection: sqlite3.Connection,
    ) -> None:
        self.paths = paths
        self._connection = connection

    @classmethod
    def create_or_resume(
        cls,
        paths: CheckpointPaths,
        identity: RunIdentity,
        *,
        fresh: bool,
    ) -> LPLedgerCheckpoint:
        _validate_paths_match_identity(paths, identity)
        _ensure_checkpoint_namespace_secure(paths)
        if fresh:
            _remove_fresh_namespace(paths)

        created = not paths.database.exists()
        if created:
            descriptor = _open_regular_leaf(
                paths.database,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                mode=0o600,
            )
            os.close(descriptor)
            _fsync_directory(paths.database.parent)
        else:
            _require_regular_mode(paths.database, 0o600)

        connection = _connect_read_write(paths)
        try:
            if created:
                _initialize_checkpoint(connection, identity)
            else:
                _validate_checkpoint(connection)
                _require_compatible_identity(connection, identity)
                _resume_checkpoint(connection)
            _validate_checkpoint(connection)
        except BaseException:
            connection.close()
            raise
        return cls(paths, connection)

    @classmethod
    def open_status(cls, paths: CheckpointPaths) -> LPLedgerCheckpoint:
        _validate_existing_parent(paths.database)
        if not paths.database.exists():
            raise CheckpointContractError("LP ledger checkpoint does not exist")
        _require_sqlite_namespace(paths)
        connection = sqlite3.connect(
            f"{paths.database.as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA busy_timeout = 5000")
            _require_sqlite_namespace(paths)
            _validate_checkpoint_status(connection, paths)
        except BaseException:
            connection.close()
            raise
        return cls(paths, connection)

    def __enter__(self) -> LPLedgerCheckpoint:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def snapshot(self) -> CheckpointSnapshot:
        return _snapshot_from_connection(self._connection)

    def action_decode_identity(self) -> ActionDecodeIdentity:
        payload = _identity_payload(self._connection)
        identity = ActionDecodeIdentity(
            pool=_required_identity_str(payload, "pool"),
            chain=_required_identity_str(payload, "chain"),
            pool_id=_required_identity_str(payload, "pool_id"),
            pool_manager=_required_identity_str(payload, "pool_manager"),
            position_manager=_required_identity_str(payload, "position_manager"),
            wrapper_entrypoint=_required_identity_str(
                payload,
                "wrapper_entrypoint",
            ),
        )
        _validate_action_decode_identity(identity)
        return identity

    def complete_phase(self, phase: Phase) -> CheckpointSnapshot:
        """Complete a phase that has no evidence-bearing final commit."""

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            if phase == "preflight":
                connection.execute(
                    """
                    UPDATE phase_state
                    SET completed = 1, last_durable_json = ?, updated_generation = ?
                    WHERE phase = 'preflight'
                    """,
                    (_canonical_json({"checks": "1"}), generation),
                )
            elif phase in (
                "action_fetch",
                "action_decode",
                "relevant_transfer_fetch",
                "relevant_transfer_decode",
                "price_replay",
            ):
                _require_phase_evidence_complete(connection, phase)
                if phase == "action_fetch":
                    connection.execute(
                        """
                        UPDATE phase_state
                        SET total = ?, updated_generation = ?
                        WHERE phase = 'action_decode'
                        """,
                        (_action_candidate_count(connection), generation),
                    )
                elif phase == "action_decode":
                    token_count = cast(
                        int,
                        connection.execute(
                            "SELECT COUNT(DISTINCT token_id) FROM decoded_actions"
                        ).fetchone()[0],
                    )
                    connection.execute(
                        """
                        UPDATE phase_state
                        SET total = ?, updated_generation = ?
                        WHERE phase = 'token_set_freeze'
                        """,
                        (token_count, generation),
                    )
                elif phase == "relevant_transfer_fetch":
                    connection.execute(
                        """
                        UPDATE phase_state
                        SET total = ?, updated_generation = ?
                        WHERE phase = 'relevant_transfer_decode'
                        """,
                        (_relevant_transfer_transaction_count(connection), generation),
                    )
                elif phase == "price_replay":
                    action_count = cast(
                        int,
                        connection.execute("SELECT COUNT(*) FROM decoded_actions").fetchone()[0],
                    )
                    connection.execute(
                        """
                        UPDATE phase_state
                        SET total = ?, updated_generation = ?
                        WHERE phase = 'build'
                        """,
                        (action_count, generation),
                    )
            else:
                raise CheckpointContractError(
                    "phase requires an evidence-bearing completion operation"
                )
            _advance_phase(connection, phase, generation)

        return self._mutate(phase, operation)

    def incomplete_action_chunks(self) -> tuple[BlockRange, ...]:
        return tuple(
            BlockRange(
                index=cast(int, row["chunk_index"]),
                start_block=cast(int, row["start_block"]),
                end_block=cast(int, row["end_block"]),
            )
            for row in self._connection.execute(
                """
                SELECT chunk_index, start_block, end_block
                FROM action_chunks
                WHERE status = 'pending'
                ORDER BY chunk_index
                """
            )
        )

    def commit_action_chunk(
        self,
        block_range: BlockRange,
        witnesses: Sequence[DiscoveryWitness],
    ) -> CheckpointSnapshot:
        values = tuple(witnesses)
        _validate_action_witness_batch(block_range, values)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            _require_pending_range(connection, "action_chunks", block_range)
            identity = _identity_payload(connection)
            expected_address = _required_identity_str(identity, "pool_manager")
            expected_topic = _required_identity_str(identity, "action_topic")
            expected_pool = _required_identity_str(identity, "pool_id")
            for witness in values:
                if (
                    witness.address != expected_address
                    or witness.topics[0] != expected_topic
                    or len(witness.topics) < 2
                    or witness.topics[1] != expected_pool
                ):
                    raise CheckpointContractError("action witness target identity is invalid")
                _upsert_chain_block(
                    connection,
                    witness.block_number,
                    witness.block_hash,
                    None,
                    generation,
                )
                connection.execute(
                    """
                    INSERT INTO action_witnesses (
                        source, transaction_hash, log_index, chunk_index,
                        block_number, block_hash, transaction_index, address,
                        topics_json, data
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        witness.source,
                        witness.transaction_hash,
                        witness.log_index,
                        block_range.index,
                        witness.block_number,
                        witness.block_hash,
                        witness.transaction_index,
                        witness.address,
                        _canonical_json(list(witness.topics)),
                        witness.data,
                    ),
                )
            connection.execute(
                """
                UPDATE action_chunks
                SET status = 'completed', witness_count = ?,
                    completed_generation = ?
                WHERE chunk_index = ?
                """,
                (len(values), generation, block_range.index),
            )
            completed = _completed_count(connection, "action_chunks")
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'action_discovery'
                """,
                (
                    completed,
                    _canonical_json(
                        {
                            "chunk_index": str(block_range.index),
                            "durable_end_block": str(block_range.end_block),
                        }
                    ),
                    generation,
                ),
            )
            if _pending_count(connection, "action_chunks") == 0:
                connection.execute(
                    """
                    UPDATE phase_state
                    SET total = ?, updated_generation = ?
                    WHERE phase = 'action_fetch'
                    """,
                    (_action_candidate_count(connection), generation),
                )
                _advance_phase(connection, "action_discovery", generation)

        return self._mutate("action_discovery", operation)

    def action_candidate_hashes(self) -> tuple[str, ...]:
        return tuple(
            cast(str, row[0])
            for row in self._connection.execute(
                """
                SELECT DISTINCT transaction_hash
                FROM action_witnesses
                ORDER BY transaction_hash
                """
            )
        )

    def action_witnesses(self, transaction_hash: str) -> tuple[DiscoveryWitness, ...]:
        normalized = _require_lower_hex(transaction_hash, 32, "transaction hash")
        return tuple(
            _witness_from_row(row)
            for row in self._connection.execute(
                """
                SELECT source, block_number, block_hash, transaction_hash,
                       transaction_index, log_index, address, topics_json, data
                FROM action_witnesses
                WHERE transaction_hash = ?
                ORDER BY block_number, transaction_index, log_index
                """,
                (normalized,),
            )
        )

    def unfetched_action_hashes(self) -> tuple[str, ...]:
        return tuple(
            cast(str, row[0])
            for row in self._connection.execute(
                """
                SELECT DISTINCT w.transaction_hash
                FROM action_witnesses AS w
                LEFT JOIN action_bundle_fetches AS f
                  ON f.transaction_hash = w.transaction_hash
                WHERE f.transaction_hash IS NULL
                ORDER BY w.transaction_hash
                """
            )
        )

    def commit_action_bundle(self, bundle: CandidateBundle) -> CheckpointSnapshot:
        _validate_candidate_bundle_shape(bundle)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            witnesses = tuple(
                _witness_from_row(row)
                for row in connection.execute(
                    """
                    SELECT source, block_number, block_hash, transaction_hash,
                           transaction_index, log_index, address,
                           topics_json, data
                    FROM action_witnesses
                    WHERE transaction_hash = ?
                    ORDER BY block_number, transaction_index, log_index
                    """,
                    (bundle.transaction_hash,),
                )
            )
            if not witnesses:
                raise CheckpointContractError("action transaction was not discovered")
            _validate_bundle_against_witnesses(bundle, witnesses)
            _upsert_chain_block(
                connection,
                bundle.block_number,
                bundle.block_hash,
                None,
                generation,
            )
            _insert_or_match_transaction_bundle(connection, bundle, generation)
            connection.execute(
                """
                INSERT INTO action_bundle_fetches (
                    transaction_hash, committed_generation
                ) VALUES (?, ?)
                """,
                (bundle.transaction_hash, generation),
            )
            completed = cast(
                int,
                connection.execute("SELECT COUNT(*) FROM action_bundle_fetches").fetchone()[0],
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'action_fetch'
                """,
                (
                    completed,
                    _canonical_json({"transaction_hash": bundle.transaction_hash}),
                    generation,
                ),
            )
            if completed == _action_candidate_count(connection):
                connection.execute(
                    """
                    UPDATE phase_state
                    SET total = ?, updated_generation = ?
                    WHERE phase = 'action_decode'
                    """,
                    (completed, generation),
                )
                _advance_phase(connection, "action_fetch", generation)

        return self._mutate("action_fetch", operation)

    def undecoded_action_bundles(self) -> tuple[CandidateBundle, ...]:
        return tuple(
            _bundle_from_row(row)
            for row in self._connection.execute(
                """
                SELECT b.transaction_hash, b.block_number, b.block_hash,
                       b.transaction_index, b.transaction_json, b.receipt_json,
                       b.payload_sha256
                FROM transaction_bundles AS b
                JOIN action_bundle_fetches AS f
                  ON f.transaction_hash = b.transaction_hash
                LEFT JOIN decoded_action_transactions AS d
                  ON d.transaction_hash = b.transaction_hash
                WHERE d.transaction_hash IS NULL
                ORDER BY b.block_number, b.transaction_index, b.transaction_hash
                """
            )
        )

    def load_header(self, block_number: int) -> BlockHeader | None:
        _require_nonnegative_int(block_number, "block number")
        row = self._connection.execute(
            """
            SELECT block_number, block_hash, timestamp_ms
            FROM chain_blocks
            WHERE block_number = ? AND timestamp_ms IS NOT NULL
            """,
            (block_number,),
        ).fetchone()
        if row is None:
            return None
        return BlockHeader(
            block_number=cast(int, row["block_number"]),
            block_hash=_require_lower_hex(cast(str, row["block_hash"]), 32, "block hash"),
            timestamp_ms=cast(int, row["timestamp_ms"]),
        )

    def load_decoder_state(self) -> dict[int, LedgerPositionState]:
        result: dict[int, LedgerPositionState] = {}
        for row in self._connection.execute(
            """
            SELECT token_id, pool_id, tick_lower, tick_upper, liquidity_after
            FROM decoder_token_state
            ORDER BY length(token_id), token_id
            """
        ):
            token_id = _parse_unsigned_decimal(cast(str, row["token_id"]), "token id")
            result[token_id] = LedgerPositionState(
                pool_id=_require_lower_hex(cast(str, row["pool_id"]), 32, "pool id"),
                tick_lower=cast(int, row["tick_lower"]),
                tick_upper=cast(int, row["tick_upper"]),
                liquidity_after=_parse_unsigned_decimal(
                    cast(str, row["liquidity_after"]), "liquidity"
                ),
            )
        return result

    def load_position_resolution(
        self,
        token_id: int,
        query_block: int,
    ) -> PositionResolution | None:
        token_text = _unsigned_decimal(token_id, "token id")
        _require_nonnegative_int(query_block, "query block")
        row = self._connection.execute(
            """
            SELECT found, pool_id, tick_lower, tick_upper, liquidity_after
            FROM position_resolutions
            WHERE token_id = ? AND query_block = ?
            """,
            (token_text, query_block),
        ).fetchone()
        if row is None:
            return None
        state = None
        if row["found"] == 1:
            state = LedgerPositionState(
                pool_id=_require_lower_hex(cast(str, row["pool_id"]), 32, "pool id"),
                tick_lower=cast(int, row["tick_lower"]),
                tick_upper=cast(int, row["tick_upper"]),
                liquidity_after=_parse_unsigned_decimal(
                    cast(str, row["liquidity_after"]), "liquidity"
                ),
            )
        return PositionResolution(token_id, query_block, bool(row["found"]), state)

    def commit_decoded_action_transaction(
        self,
        bundle: CandidateBundle,
        headers: Sequence[BlockHeader],
        resolutions: Sequence[PositionResolution],
        state_upserts: Sequence[DecoderStateUpsert],
        state_deletes: Sequence[int],
        actions: Sequence[DecodedLiquidityAction],
        position_keys: Sequence[PositionKeyMapping],
    ) -> CheckpointSnapshot:
        header_values = tuple(headers)
        resolution_values = tuple(resolutions)
        upsert_values = tuple(state_upserts)
        delete_values = tuple(state_deletes)
        action_values = tuple(actions)
        position_key_values = tuple(position_keys)
        _validate_decode_batch(
            bundle,
            header_values,
            resolution_values,
            upsert_values,
            delete_values,
            action_values,
        )

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            first = connection.execute(
                """
                SELECT b.transaction_hash
                FROM transaction_bundles AS b
                JOIN action_bundle_fetches AS f
                  ON f.transaction_hash = b.transaction_hash
                LEFT JOIN decoded_action_transactions AS d
                  ON d.transaction_hash = b.transaction_hash
                WHERE d.transaction_hash IS NULL
                ORDER BY b.block_number, b.transaction_index, b.transaction_hash
                LIMIT 1
                """
            ).fetchone()
            if first is None or first["transaction_hash"] != bundle.transaction_hash:
                raise CheckpointContractError(
                    "decoded action transaction is not the canonical next bundle"
                )
            persisted = connection.execute(
                """
                SELECT transaction_hash, block_number, block_hash,
                       transaction_index, transaction_json, receipt_json,
                       payload_sha256
                FROM transaction_bundles
                WHERE transaction_hash = ?
                """,
                (bundle.transaction_hash,),
            ).fetchone()
            if persisted is None or _bundle_from_row(persisted) != bundle:
                raise CheckpointContractError("decoded action bundle does not match checkpoint")
            identity = _identity_payload(connection)
            for action in action_values:
                _validate_action_run_identity(action, identity)
            existing_position_key_ids = {
                _parse_unsigned_decimal(cast(str, row[0]), "token id")
                for row in connection.execute(
                    "SELECT token_id FROM token_position_keys"
                )
            }
            _validate_position_key_mappings(
                position_key_values,
                action_values,
                existing_position_key_ids,
            )
            required_log_indices = tuple(
                cast(int, row[0])
                for row in connection.execute(
                    """
                    SELECT log_index
                    FROM action_witnesses
                    WHERE transaction_hash = ?
                    ORDER BY log_index
                    """,
                    (bundle.transaction_hash,),
                )
            )
            _require_modify_action_multiplicity(
                required_log_indices,
                tuple(action.log_index for action in action_values),
            )
            _validate_decoder_state_transition(
                connection,
                resolution_values,
                upsert_values,
                delete_values,
                action_values,
            )
            for header in header_values:
                _upsert_chain_block(
                    connection,
                    header.block_number,
                    header.block_hash,
                    header.timestamp_ms,
                    generation,
                )
            if self.load_header(bundle.block_number) is None:
                raise CheckpointContractError("decoded action transaction header is missing")
            for resolution in resolution_values:
                _insert_position_resolution(connection, resolution, generation)
            connection.execute(
                """
                INSERT INTO decoded_action_transactions (
                    transaction_hash, block_number, block_hash,
                    transaction_index, action_count, decoder_state_sha256,
                    committed_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle.transaction_hash,
                    bundle.block_number,
                    bundle.block_hash,
                    bundle.transaction_index,
                    len(action_values),
                    "0" * 64,
                    generation,
                ),
            )
            for action in action_values:
                action_json = _action_json(action)
                connection.execute(
                    """
                    INSERT INTO decoded_actions (
                        block_number, log_index, event_order, block_hash,
                        transaction_hash, token_id, action_type, action_json,
                        payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action.block_number,
                        action.log_index,
                        action.event_order,
                        bundle.block_hash,
                        bundle.transaction_hash,
                        _unsigned_decimal(action.token_id, "token id"),
                        action.action_type,
                        action_json,
                        hashlib.sha256(action_json.encode("utf-8")).hexdigest(),
                    ),
                )
            for mapping in position_key_values:
                connection.execute(
                    """
                    INSERT INTO token_position_keys (
                        token_id, pool_id, tick_lower, tick_upper, salt,
                        mint_block_number, mint_log_index, mint_event_order,
                        committed_generation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _unsigned_decimal(mapping.token_id, "token id"),
                        mapping.pool_id,
                        mapping.tick_lower,
                        mapping.tick_upper,
                        mapping.salt,
                        mapping.mint_block_number,
                        mapping.mint_log_index,
                        mapping.mint_event_order,
                        generation,
                    ),
                )
            for token_id in delete_values:
                cursor = connection.execute(
                    "DELETE FROM decoder_token_state WHERE token_id = ?",
                    (_unsigned_decimal(token_id, "token id"),),
                )
                if cursor.rowcount != 1:
                    raise CheckpointContractError("decoder state delete did not match a token")
            for upsert in upsert_values:
                connection.execute(
                    """
                    INSERT INTO decoder_token_state (
                        token_id, pool_id, tick_lower, tick_upper,
                        liquidity_after, last_block_number, last_log_index,
                        last_event_order, updated_generation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(token_id) DO UPDATE SET
                        pool_id = excluded.pool_id,
                        tick_lower = excluded.tick_lower,
                        tick_upper = excluded.tick_upper,
                        liquidity_after = excluded.liquidity_after,
                        last_block_number = excluded.last_block_number,
                        last_log_index = excluded.last_log_index,
                        last_event_order = excluded.last_event_order,
                        updated_generation = excluded.updated_generation
                    """,
                    (
                        _unsigned_decimal(upsert.token_id, "token id"),
                        _require_lower_hex(upsert.state.pool_id, 32, "pool id"),
                        upsert.state.tick_lower,
                        upsert.state.tick_upper,
                        _unsigned_decimal(upsert.state.liquidity_after, "liquidity"),
                        upsert.last_block_number,
                        upsert.last_log_index,
                        upsert.last_event_order,
                        generation,
                    ),
                )
            connection.execute(
                """
                UPDATE decoded_action_transactions
                SET decoder_state_sha256 = ?
                WHERE transaction_hash = ?
                """,
                (_decoder_state_sha256(connection), bundle.transaction_hash),
            )
            completed = cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM decoded_action_transactions"
                ).fetchone()[0],
            )
            action_count = cast(
                int,
                connection.execute("SELECT COUNT(*) FROM decoded_actions").fetchone()[0],
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'action_decode'
                """,
                (
                    completed,
                    _canonical_json(
                        {
                            "block_number": str(bundle.block_number),
                            "transaction_hash": bundle.transaction_hash,
                            "transaction_index": str(bundle.transaction_index),
                            "action_count": str(action_count),
                        }
                    ),
                    generation,
                ),
            )
            if completed == _action_candidate_count(connection):
                token_count = cast(
                    int,
                    connection.execute(
                        "SELECT COUNT(DISTINCT token_id) FROM decoded_actions"
                    ).fetchone()[0],
                )
                connection.execute(
                    """
                    UPDATE phase_state
                    SET total = ?, updated_generation = ?
                    WHERE phase = 'token_set_freeze'
                    """,
                    (token_count, generation),
                )
                _advance_phase(connection, "action_decode", generation)

        return self._mutate("action_decode", operation)

    def load_position_keys(self) -> tuple[PositionKeyMapping, ...]:
        return tuple(
            _position_key_from_row(row)
            for row in self._connection.execute(
                """
                SELECT token_id, pool_id, tick_lower, tick_upper, salt,
                       mint_block_number, mint_log_index, mint_event_order
                FROM token_position_keys
                ORDER BY length(token_id), token_id
                """
            )
        )

    def frozen_token_ids(self) -> tuple[int, ...]:
        return tuple(
            _parse_unsigned_decimal(cast(str, row[0]), "token id")
            for row in self._connection.execute(
                "SELECT token_id FROM frozen_token_ids ORDER BY ordinal"
            )
        )

    def freeze_action_token_set(self) -> CheckpointSnapshot:
        def operation(connection: sqlite3.Connection, generation: int) -> None:
            if connection.execute("SELECT 1 FROM frozen_token_set").fetchone() is not None:
                raise CheckpointContractError("action token set is already frozen")
            token_ids = tuple(
                sorted(
                    {
                        _parse_unsigned_decimal(cast(str, row[0]), "token id")
                        for row in connection.execute(
                            "SELECT token_id FROM decoded_actions"
                        )
                    }
                )
            )
            token_digest = _token_ids_sha256(token_ids)
            connection.execute(
                """
                INSERT INTO frozen_token_set (
                    singleton, token_count, token_ids_sha256,
                    committed_generation
                ) VALUES (1, ?, ?, ?)
                """,
                (len(token_ids), token_digest, generation),
            )
            for ordinal, token_id in enumerate(token_ids):
                connection.execute(
                    """
                    INSERT INTO frozen_token_ids (
                        token_id, ordinal, frozen_set_singleton
                    ) VALUES (?, ?, 1)
                    """,
                    (_unsigned_decimal(token_id, "token id"), ordinal),
                )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'token_set_freeze'
                """,
                (
                    len(token_ids),
                    _canonical_json({"token_set_sha256": token_digest}),
                    generation,
                ),
            )
            _advance_phase(connection, "token_set_freeze", generation)

        return self._mutate("token_set_freeze", operation)

    def incomplete_transfer_chunks(self) -> tuple[BlockRange, ...]:
        return tuple(
            BlockRange(
                index=cast(int, row["chunk_index"]),
                start_block=cast(int, row["start_block"]),
                end_block=cast(int, row["end_block"]),
            )
            for row in self._connection.execute(
                """
                SELECT chunk_index, start_block, end_block
                FROM transfer_chunks
                WHERE status = 'pending'
                ORDER BY chunk_index
                """
            )
        )

    def commit_transfer_chunk(
        self,
        block_range: BlockRange,
        *,
        unfiltered_count: int,
        unfiltered_sha256: str,
        relevant_witnesses: Sequence[DiscoveryWitness],
    ) -> CheckpointSnapshot:
        values = tuple(relevant_witnesses)
        _require_nonnegative_int(unfiltered_count, "unfiltered transfer count")
        _require_lower_hex(
            unfiltered_sha256,
            32,
            "unfiltered transfer digest",
            prefix=False,
        )
        if unfiltered_count < len(values):
            raise CheckpointContractError("unfiltered transfer count is below relevant count")
        _validate_transfer_witness_batch(block_range, values)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            _require_pending_range(connection, "transfer_chunks", block_range)
            identity = _identity_payload(connection)
            expected_address = _required_identity_str(identity, "position_manager")
            expected_topic = _required_identity_str(identity, "transfer_topic")
            frozen_ids = {
                _parse_unsigned_decimal(cast(str, row[0]), "token id")
                for row in connection.execute("SELECT token_id FROM frozen_token_ids")
            }
            if connection.execute("SELECT 1 FROM frozen_token_set").fetchone() is None:
                raise CheckpointContractError("transfer scan requires a frozen token set")
            for witness in values:
                token_id = _transfer_witness_token_id(witness)
                if (
                    witness.address != expected_address
                    or witness.topics[0] != expected_topic
                    or token_id not in frozen_ids
                ):
                    raise CheckpointContractError("relevant transfer witness is invalid")
                _upsert_chain_block(
                    connection,
                    witness.block_number,
                    witness.block_hash,
                    None,
                    generation,
                )
                connection.execute(
                    """
                    INSERT INTO relevant_transfer_witnesses (
                        source, transaction_hash, log_index, chunk_index,
                        block_number, block_hash, transaction_index, address,
                        topics_json, data, token_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        witness.source,
                        witness.transaction_hash,
                        witness.log_index,
                        block_range.index,
                        witness.block_number,
                        witness.block_hash,
                        witness.transaction_index,
                        witness.address,
                        _canonical_json(list(witness.topics)),
                        witness.data,
                        _unsigned_decimal(token_id, "token id"),
                    ),
                )
            connection.execute(
                """
                UPDATE transfer_chunks
                SET status = 'completed', unfiltered_count = ?,
                    unfiltered_sha256 = ?, relevant_count = ?,
                    completed_generation = ?
                WHERE chunk_index = ?
                """,
                (
                    unfiltered_count,
                    unfiltered_sha256,
                    len(values),
                    generation,
                    block_range.index,
                ),
            )
            completed = _completed_count(connection, "transfer_chunks")
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'full_transfer_scan'
                """,
                (
                    completed,
                    _canonical_json(
                        {
                            "chunk_index": str(block_range.index),
                            "durable_end_block": str(block_range.end_block),
                            "unfiltered_transfer_sha256": unfiltered_sha256,
                        }
                    ),
                    generation,
                ),
            )
            if _pending_count(connection, "transfer_chunks") == 0:
                relevant_total = _relevant_transfer_transaction_count(connection)
                connection.execute(
                    """
                    UPDATE phase_state
                    SET total = ?, updated_generation = ?
                    WHERE phase = 'relevant_transfer_fetch'
                    """,
                    (relevant_total, generation),
                )
                _advance_phase(connection, "full_transfer_scan", generation)

        return self._mutate("full_transfer_scan", operation)

    def relevant_transfer_transaction_hashes(self) -> tuple[str, ...]:
        return tuple(
            cast(str, row[0])
            for row in self._connection.execute(
                """
                SELECT DISTINCT transaction_hash
                FROM relevant_transfer_witnesses
                ORDER BY transaction_hash
                """
            )
        )

    def relevant_transfer_witnesses(
        self,
        transaction_hash: str,
    ) -> tuple[DiscoveryWitness, ...]:
        normalized = _require_lower_hex(transaction_hash, 32, "transaction hash")
        return tuple(
            _witness_from_row(row)
            for row in self._connection.execute(
                """
                SELECT source, block_number, block_hash, transaction_hash,
                       transaction_index, log_index, address, topics_json, data
                FROM relevant_transfer_witnesses
                WHERE transaction_hash = ?
                ORDER BY block_number, transaction_index, log_index
                """,
                (normalized,),
            )
        )

    def unfetched_relevant_transfer_hashes(self) -> tuple[str, ...]:
        return tuple(
            cast(str, row[0])
            for row in self._connection.execute(
                """
                SELECT DISTINCT w.transaction_hash
                FROM relevant_transfer_witnesses AS w
                LEFT JOIN relevant_transfer_bundle_fetches AS f
                  ON f.transaction_hash = w.transaction_hash
                WHERE f.transaction_hash IS NULL
                ORDER BY w.transaction_hash
                """
            )
        )

    def commit_relevant_transfer_bundle(
        self,
        bundle: CandidateBundle,
    ) -> CheckpointSnapshot:
        _validate_candidate_bundle_shape(bundle)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            witnesses = tuple(
                _witness_from_row(row)
                for row in connection.execute(
                    """
                    SELECT source, block_number, block_hash, transaction_hash,
                           transaction_index, log_index, address,
                           topics_json, data
                    FROM relevant_transfer_witnesses
                    WHERE transaction_hash = ?
                    ORDER BY block_number, transaction_index, log_index
                    """,
                    (bundle.transaction_hash,),
                )
            )
            if not witnesses:
                raise CheckpointContractError("relevant transfer was not discovered")
            _validate_bundle_against_witnesses(bundle, witnesses)
            _upsert_chain_block(
                connection,
                bundle.block_number,
                bundle.block_hash,
                None,
                generation,
            )
            _insert_or_match_transaction_bundle(connection, bundle, generation)
            connection.execute(
                """
                INSERT INTO relevant_transfer_bundle_fetches (
                    transaction_hash, committed_generation
                ) VALUES (?, ?)
                """,
                (bundle.transaction_hash, generation),
            )
            completed = cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM relevant_transfer_bundle_fetches"
                ).fetchone()[0],
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'relevant_transfer_fetch'
                """,
                (
                    completed,
                    _canonical_json({"transaction_hash": bundle.transaction_hash}),
                    generation,
                ),
            )
            if completed == _relevant_transfer_transaction_count(connection):
                connection.execute(
                    """
                    UPDATE phase_state
                    SET total = ?, updated_generation = ?
                    WHERE phase = 'relevant_transfer_decode'
                    """,
                    (completed, generation),
                )
                _advance_phase(connection, "relevant_transfer_fetch", generation)

        return self._mutate("relevant_transfer_fetch", operation)

    def undecoded_relevant_transfer_bundles(self) -> tuple[CandidateBundle, ...]:
        return tuple(
            _bundle_from_row(row)
            for row in self._connection.execute(
                """
                SELECT b.transaction_hash, b.block_number, b.block_hash,
                       b.transaction_index, b.transaction_json, b.receipt_json,
                       b.payload_sha256
                FROM transaction_bundles AS b
                JOIN relevant_transfer_bundle_fetches AS f
                  ON f.transaction_hash = b.transaction_hash
                LEFT JOIN decoded_relevant_transfer_transactions AS d
                  ON d.transaction_hash = b.transaction_hash
                WHERE d.transaction_hash IS NULL
                ORDER BY b.block_number, b.transaction_index, b.transaction_hash
                """
            )
        )

    def commit_decoded_relevant_transfer_transaction(
        self,
        bundle: CandidateBundle,
        *,
        owners: Sequence[OwnershipEvent],
    ) -> CheckpointSnapshot:
        owner_values = tuple(owners)
        _validate_candidate_bundle_shape(bundle)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            first = connection.execute(
                """
                SELECT b.transaction_hash
                FROM transaction_bundles AS b
                JOIN relevant_transfer_bundle_fetches AS f
                  ON f.transaction_hash = b.transaction_hash
                LEFT JOIN decoded_relevant_transfer_transactions AS d
                  ON d.transaction_hash = b.transaction_hash
                WHERE d.transaction_hash IS NULL
                ORDER BY b.block_number, b.transaction_index, b.transaction_hash
                LIMIT 1
                """
            ).fetchone()
            if first is None or first["transaction_hash"] != bundle.transaction_hash:
                raise CheckpointContractError(
                    "ownership transaction is not the canonical next bundle"
                )
            persisted = connection.execute(
                """
                SELECT transaction_hash, block_number, block_hash,
                       transaction_index, transaction_json, receipt_json,
                       payload_sha256
                FROM transaction_bundles
                WHERE transaction_hash = ?
                """,
                (bundle.transaction_hash,),
            ).fetchone()
            if persisted is None or _bundle_from_row(persisted) != bundle:
                raise CheckpointContractError("ownership bundle does not match checkpoint")
            witnesses = tuple(
                _witness_from_row(row)
                for row in connection.execute(
                    """
                    SELECT source, block_number, block_hash, transaction_hash,
                           transaction_index, log_index, address,
                           topics_json, data
                    FROM relevant_transfer_witnesses
                    WHERE transaction_hash = ?
                    ORDER BY block_number, transaction_index, log_index
                    """,
                    (bundle.transaction_hash,),
                )
            )
            frozen_ids = {
                _parse_unsigned_decimal(cast(str, row[0]), "token id")
                for row in connection.execute("SELECT token_id FROM frozen_token_ids")
            }
            _validate_ownership_reconciliation(
                bundle,
                witnesses,
                owner_values,
                frozen_ids,
            )
            connection.execute(
                """
                INSERT INTO decoded_relevant_transfer_transactions (
                    transaction_hash, block_number, block_hash,
                    transaction_index, ownership_count, committed_generation
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle.transaction_hash,
                    bundle.block_number,
                    bundle.block_hash,
                    bundle.transaction_index,
                    len(owner_values),
                    generation,
                ),
            )
            for owner in owner_values:
                connection.execute(
                    """
                    INSERT INTO ownership_events (
                        block_number, log_index, event_order, block_hash,
                        transaction_hash, token_id, previous_owner, new_owner
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner.block_number,
                        owner.log_index,
                        owner.event_order,
                        bundle.block_hash,
                        bundle.transaction_hash,
                        _unsigned_decimal(owner.token_id, "token id"),
                        _optional_address(owner.previous_owner, "previous owner"),
                        _optional_address(owner.new_owner, "new owner"),
                    ),
                )
            completed = cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM decoded_relevant_transfer_transactions"
                ).fetchone()[0],
            )
            owner_count = cast(
                int,
                connection.execute("SELECT COUNT(*) FROM ownership_events").fetchone()[0],
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'relevant_transfer_decode'
                """,
                (
                    completed,
                    _canonical_json(
                        {
                            "transaction_hash": bundle.transaction_hash,
                            "ownership_count": str(owner_count),
                        }
                    ),
                    generation,
                ),
            )
            if completed == _relevant_transfer_transaction_count(connection):
                _advance_phase(connection, "relevant_transfer_decode", generation)

        return self._mutate("relevant_transfer_decode", operation)

    def commit_replay_input(
        self,
        evidence: ReplayInputEvidence,
    ) -> CheckpointSnapshot:
        _validate_replay_input_evidence(evidence)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            if connection.execute(
                "SELECT 1 FROM replay_input WHERE singleton = 1"
            ).fetchone() is not None:
                raise CheckpointContractError("replay input is already committed")
            expected = _identity_payload(connection).get("replay_input")
            if not isinstance(expected, dict) or asdict(evidence) != expected:
                raise CheckpointContractError("replay input does not match run identity")
            connection.execute(
                """
                INSERT INTO replay_input (
                    singleton, path, sha256, byte_length, row_count,
                    header_sha256, parser_version, price_semantics_sha256,
                    chain, pool_id, first_block, last_block,
                    first_timestamp_ms, last_timestamp_ms,
                    price_event_count, price_events_sha256,
                    committed_generation
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.path,
                    evidence.sha256,
                    evidence.byte_length,
                    evidence.row_count,
                    evidence.header_sha256,
                    evidence.parser_version,
                    evidence.price_semantics_sha256,
                    evidence.chain,
                    evidence.pool_id,
                    evidence.first_block,
                    evidence.last_block,
                    evidence.first_timestamp_ms,
                    evidence.last_timestamp_ms,
                    evidence.price_event_count,
                    evidence.price_events_sha256,
                    generation,
                ),
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = 1, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'replay_input_bind'
                """,
                (
                    _canonical_json({"replay_input_sha256": evidence.sha256}),
                    generation,
                ),
            )
            action_count = cast(
                int,
                connection.execute("SELECT COUNT(*) FROM decoded_actions").fetchone()[0],
            )
            connection.execute(
                """
                UPDATE phase_state
                SET total = ?, updated_generation = ?
                WHERE phase = 'price_replay'
                """,
                (action_count, generation),
            )
            _advance_phase(connection, "replay_input_bind", generation)

        return self._mutate("replay_input_bind", operation)

    def load_replay_input(self) -> ReplayInputEvidence:
        row = self._connection.execute(
            "SELECT * FROM replay_input WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise CheckpointContractError("replay input is not committed")
        return _replay_input_from_row(row)

    def load_decoded_actions(self) -> tuple[DecodedLiquidityAction, ...]:
        actions: list[DecodedLiquidityAction] = []
        for row in self._connection.execute(
            """
            SELECT action_json, payload_sha256
            FROM decoded_actions
            ORDER BY block_number, log_index, event_order
            """
        ):
            action_json = cast(str, row["action_json"])
            digest = _require_lower_hex(
                cast(str, row["payload_sha256"]),
                32,
                "action payload digest",
                prefix=False,
            )
            if hashlib.sha256(action_json.encode("utf-8")).hexdigest() != digest:
                raise CheckpointContractError("decoded action payload digest does not match")
            actions.append(_action_from_json(action_json))
        return tuple(actions)

    def load_ownership_events(self) -> tuple[OwnershipEvent, ...]:
        return tuple(
            OwnershipEvent(
                block_number=cast(int, row["block_number"]),
                log_index=cast(int, row["log_index"]),
                token_id=_parse_unsigned_decimal(cast(str, row["token_id"]), "token id"),
                previous_owner=(
                    None
                    if row["previous_owner"] is None
                    else _semantic_address(cast(str, row["previous_owner"]), "previous owner")
                ),
                new_owner=(
                    None
                    if row["new_owner"] is None
                    else _semantic_address(cast(str, row["new_owner"]), "new owner")
                ),
                event_order=cast(int, row["event_order"]),
            )
            for row in self._connection.execute(
                """
                SELECT block_number, log_index, event_order, token_id,
                       previous_owner, new_owner
                FROM ownership_events
                ORDER BY block_number, log_index, event_order
                """
            )
        )

    def commit_action_price_bindings(
        self,
        bindings: Sequence[ActionPriceBinding],
    ) -> CheckpointSnapshot:
        values = tuple(bindings)
        _validate_action_price_bindings(values)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            if connection.execute(
                "SELECT 1 FROM replay_input WHERE singleton = 1"
            ).fetchone() is None:
                raise CheckpointContractError("action price bindings require a replay input")
            expected_keys = tuple(
                (cast(int, row[0]), cast(int, row[1]), cast(int, row[2]))
                for row in connection.execute(
                    """
                    SELECT block_number, log_index, event_order
                    FROM decoded_actions
                    ORDER BY block_number, log_index, event_order
                    """
                )
            )
            binding_keys = tuple(
                (value.block_number, value.log_index, value.event_order) for value in values
            )
            if binding_keys != expected_keys:
                raise CheckpointContractError(
                    "action price binding set does not equal decoded actions"
                )
            for binding in values:
                connection.execute(
                    """
                    INSERT INTO action_price_bindings (
                        block_number, log_index, event_order,
                        event_time_sqrt_price_x96, event_time_tick,
                        event_time_state_source, committed_generation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding.block_number,
                        binding.log_index,
                        binding.event_order,
                        _unsigned_decimal(
                            binding.event_time_sqrt_price_x96,
                            "event-time sqrt price",
                        ),
                        binding.event_time_tick,
                        binding.event_time_state_source,
                        generation,
                    ),
                )
            completed = cast(
                int,
                connection.execute("SELECT COUNT(*) FROM action_price_bindings").fetchone()[0],
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'price_replay'
                """,
                (
                    completed,
                    _canonical_json({"bound_actions": str(completed)}),
                    generation,
                ),
            )
            connection.execute(
                """
                UPDATE phase_state
                SET total = ?, updated_generation = ?
                WHERE phase = 'build'
                """,
                (len(expected_keys), generation),
            )
            _advance_phase(connection, "price_replay", generation)

        return self._mutate("price_replay", operation)

    def load_action_price_bindings(self) -> tuple[ActionPriceBinding, ...]:
        return tuple(
            _binding_from_row(row)
            for row in self._connection.execute(
                """
                SELECT block_number, log_index, event_order,
                       event_time_sqrt_price_x96, event_time_tick,
                       event_time_state_source
                FROM action_price_bindings
                ORDER BY block_number, log_index, event_order
                """
            )
        )

    def load_build_inputs(self) -> BuildInputs:
        run = self._connection.execute("SELECT phase FROM run_state WHERE singleton = 1").fetchone()
        if run is None or _PHASES.index(cast(str, run["phase"])) < _PHASES.index("build"):
            raise CheckpointContractError("checkpoint is not ready for deterministic build")
        prerequisite_phases = _PHASES[: _PHASES.index("build")]
        prerequisite_statuses = {
            cast(str, row["phase"]): cast(str, row["status"])
            for row in self._connection.execute(
                """
                SELECT phase, status
                FROM phase_state
                WHERE phase != 'build' AND phase != 'publish' AND phase != 'succeeded'
                """
            )
        }
        if prerequisite_statuses != {phase: "completed" for phase in prerequisite_phases}:
            raise CheckpointContractError("checkpoint build prerequisites are incomplete")
        actions = self.load_decoded_actions()
        ownership_events = self.load_ownership_events()
        bindings = self.load_action_price_bindings()
        action_keys = tuple(
            (action.block_number, action.log_index, action.event_order) for action in actions
        )
        binding_by_key = {
            (binding.block_number, binding.log_index, binding.event_order): binding
            for binding in bindings
        }
        if tuple(sorted(binding_by_key)) != action_keys or len(binding_by_key) != len(bindings):
            raise CheckpointContractError("action price bindings are not total and unique")
        action_witnesses = tuple(
            _witness_from_row(row)
            for row in self._connection.execute(
                """
                SELECT source, block_number, block_hash, transaction_hash,
                       transaction_index, log_index, address, topics_json, data
                FROM action_witnesses
                ORDER BY block_number, transaction_index, log_index, transaction_hash
                """
            )
        )
        transfer_attestations = tuple(
            TransferChunkAttestation(
                index=cast(int, row["chunk_index"]),
                start_block=cast(int, row["start_block"]),
                end_block=cast(int, row["end_block"]),
                unfiltered_count=cast(int, row["unfiltered_count"]),
                unfiltered_sha256=cast(str, row["unfiltered_sha256"]),
                relevant_count=cast(int, row["relevant_count"]),
            )
            for row in self._connection.execute(
                """
                SELECT chunk_index, start_block, end_block, unfiltered_count,
                       unfiltered_sha256, relevant_count
                FROM transfer_chunks
                WHERE status = 'completed'
                ORDER BY chunk_index
                """
            )
        )
        relevant_witnesses = tuple(
            _witness_from_row(row)
            for row in self._connection.execute(
                """
                SELECT source, block_number, block_hash, transaction_hash,
                       transaction_index, log_index, address, topics_json, data
                FROM relevant_transfer_witnesses
                ORDER BY block_number, transaction_index, log_index, transaction_hash
                """
            )
        )
        eligible_bundles = tuple(
            _bundle_from_row(row)
            for row in self._connection.execute(
                """
                SELECT b.transaction_hash, b.block_number, b.block_hash,
                       b.transaction_index, b.transaction_json, b.receipt_json,
                       b.payload_sha256
                FROM transaction_bundles AS b
                WHERE EXISTS (
                    SELECT 1 FROM action_bundle_fetches AS a
                    WHERE a.transaction_hash = b.transaction_hash
                ) OR EXISTS (
                    SELECT 1 FROM relevant_transfer_bundle_fetches AS t
                    WHERE t.transaction_hash = b.transaction_hash
                )
                ORDER BY b.block_number, b.transaction_index, b.transaction_hash
                """
            )
        )
        return BuildInputs(
            action_witnesses=action_witnesses,
            action_transaction_hashes=self.action_candidate_hashes(),
            frozen_token_ids=self.frozen_token_ids(),
            position_keys=self.load_position_keys(),
            transfer_chunk_attestations=transfer_attestations,
            relevant_transfer_witnesses=relevant_witnesses,
            relevant_transfer_transaction_hashes=(
                self.relevant_transfer_transaction_hashes()
            ),
            eligible_bundles=eligible_bundles,
            replay_input=self.load_replay_input(),
            actions=actions,
            ownership_events=ownership_events,
            action_price_bindings=bindings,
        )

    def load_publication_state(self) -> PublicationState:
        row = self._connection.execute(
            """
            SELECT state, expected_ledger_sha256, expected_sidecar_sha256,
                   started_generation, published_generation
            FROM publication_state
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise CheckpointContractError("checkpoint publication state is missing")
        expected_ledger = cast(str | None, row["expected_ledger_sha256"])
        expected_sidecar = cast(str | None, row["expected_sidecar_sha256"])
        if expected_ledger is not None:
            _require_lower_hex(expected_ledger, 32, "ledger digest", prefix=False)
        if expected_sidecar is not None:
            _require_lower_hex(expected_sidecar, 32, "sidecar digest", prefix=False)
        return PublicationState(
            state=cast(Literal["not_started", "started", "published"], row["state"]),
            expected_ledger_sha256=expected_ledger,
            expected_sidecar_sha256=expected_sidecar,
            started_generation=cast(int | None, row["started_generation"]),
            published_generation=cast(int | None, row["published_generation"]),
        )

    def begin_publication(
        self,
        expected_ledger_sha256: str,
        expected_sidecar_sha256: str,
    ) -> CheckpointSnapshot:
        ledger_digest = _require_lower_hex(
            expected_ledger_sha256,
            32,
            "ledger digest",
            prefix=False,
        )
        sidecar_digest = _require_lower_hex(
            expected_sidecar_sha256,
            32,
            "sidecar digest",
            prefix=False,
        )

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            publication = connection.execute(
                "SELECT state FROM publication_state WHERE singleton = 1"
            ).fetchone()
            if publication is None or publication["state"] != "not_started":
                raise CheckpointContractError("publication has already started")
            build_total = cast(
                int,
                connection.execute("SELECT COUNT(*) FROM decoded_actions").fetchone()[0],
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, total = ?, last_durable_json = ?,
                    updated_generation = ?
                WHERE phase = 'build'
                """,
                (
                    build_total,
                    build_total,
                    _canonical_json({"rows": str(build_total)}),
                    generation,
                ),
            )
            _advance_phase(connection, "build", generation)
            connection.execute(
                """
                UPDATE publication_state
                SET state = 'started', expected_ledger_sha256 = ?,
                    expected_sidecar_sha256 = ?, started_generation = ?
                WHERE singleton = 1
                """,
                (ledger_digest, sidecar_digest, generation),
            )

        return self._mutate("build", operation)

    def mark_published(self) -> CheckpointSnapshot:
        def operation(connection: sqlite3.Connection, generation: int) -> None:
            publication = connection.execute(
                "SELECT state FROM publication_state WHERE singleton = 1"
            ).fetchone()
            if publication is None or publication["state"] != "started":
                raise CheckpointContractError("publication was not durably started")
            connection.execute(
                """
                UPDATE publication_state
                SET state = 'published', published_generation = ?
                WHERE singleton = 1
                """,
                (generation,),
            )
            connection.execute(
                """
                UPDATE phase_state
                SET status = 'completed', completed = 2,
                    last_durable_json = ?, updated_generation = ?
                WHERE phase = 'publish'
                """,
                (_canonical_json({"files": "2"}), generation),
            )
            connection.execute(
                """
                UPDATE phase_state
                SET status = 'completed', completed = 1, total = 1,
                    last_durable_json = ?, updated_generation = ?
                WHERE phase = 'succeeded'
                """,
                (_canonical_json({"output": "1"}), generation),
            )
            now = _utc_now()
            connection.execute(
                """
                UPDATE run_state
                SET status = 'succeeded', phase = 'succeeded',
                    finished_at_utc = ?, error_code = NULL,
                    output_published = 1
                WHERE singleton = 1
                """,
                (now,),
            )

        return self._mutate("publish", operation)

    def checkpoint_terminal_wal(self) -> Literal["truncated", "busy"]:
        if self.load_publication_state().state != "published":
            raise CheckpointContractError("checkpoint WAL maintenance requires publication")
        row = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or len(row) != 3:
            raise CheckpointContractError("checkpoint WAL result is invalid")
        return "busy" if cast(int, row[0]) else "truncated"

    def mark_checkpoint_maintenance_failed(self) -> CheckpointSnapshot:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """
                SELECT r.generation, r.status, r.phase, r.output_published,
                       p.state AS publication_state
                FROM run_state AS r
                JOIN publication_state AS p ON p.singleton = r.singleton
                WHERE r.singleton = 1
                """
            ).fetchone()
            if (
                row is None
                or row["status"] != "succeeded"
                or row["phase"] != "succeeded"
                or row["output_published"] != 1
                or row["publication_state"] != "published"
            ):
                raise CheckpointContractError(
                    "checkpoint maintenance failure requires durable publication"
                )
            generation = cast(int, row["generation"]) + 1
            now = _utc_now()
            self._connection.execute(
                """
                UPDATE run_state
                SET generation = ?, status = 'failed', updated_at_utc = ?,
                    finished_at_utc = ?, error_code = 'checkpoint_maintenance_error'
                WHERE singleton = 1
                """,
                (generation, now, now),
            )
            _mutation_hook("before_commit")
            self._connection.commit()
            _mutation_hook("after_commit")
        except CheckpointContractError:
            self._connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            self._connection.rollback()
            raise CheckpointContractError(
                "checkpoint maintenance failure could not be persisted"
            ) from exc
        except BaseException:
            self._connection.rollback()
            raise
        return self.snapshot()

    def mark_attempt_failed(self, error_code: ErrorCode) -> CheckpointSnapshot:
        code = _require_error_code(error_code)
        status: RunStatus = "interrupted" if code == "interrupted" else "failed"
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """
                SELECT generation, status, phase, output_published
                FROM run_state
                WHERE singleton = 1
                """
            ).fetchone()
            if (
                row is None
                or row["status"] != "running"
                or row["phase"] == "succeeded"
                or row["output_published"] != 0
            ):
                raise CheckpointContractError(
                    "checkpoint attempt failure requires a running attempt"
                )
            generation = cast(int, row["generation"]) + 1
            now = _utc_now()
            cursor = self._connection.execute(
                """
                UPDATE run_state
                SET generation = ?, status = ?, updated_at_utc = ?,
                    finished_at_utc = ?, error_code = ?
                WHERE singleton = 1
                """,
                (generation, status, now, now, code),
            )
            if cursor.rowcount != 1:
                raise CheckpointContractError("checkpoint run state is missing")
            _mutation_hook("before_commit")
            self._connection.commit()
            _mutation_hook("after_commit")
        except CheckpointContractError:
            self._connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            self._connection.rollback()
            raise CheckpointContractError(
                "checkpoint attempt failure could not be persisted"
            ) from exc
        except BaseException:
            self._connection.rollback()
            raise
        return self.snapshot()

    def _mutate(
        self,
        expected_phase: Phase,
        operation: Callable[[sqlite3.Connection, int], None],
    ) -> CheckpointSnapshot:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT generation, status, phase FROM run_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise CheckpointContractError("checkpoint run state is missing")
            if row["status"] != "running" or row["phase"] != expected_phase:
                raise CheckpointContractError("checkpoint phase does not accept this operation")
            generation = cast(int, row["generation"]) + 1
            operation(self._connection, generation)
            now = _utc_now()
            cursor = self._connection.execute(
                """
                UPDATE run_state
                SET generation = ?, updated_at_utc = ?
                WHERE singleton = 1
                """,
                (generation, now),
            )
            if cursor.rowcount != 1:
                raise CheckpointContractError("checkpoint run state is missing")
            _mutation_hook("before_commit")
            self._connection.commit()
            _mutation_hook("after_commit")
        except CheckpointContractError:
            self._connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            self._connection.rollback()
            raise CheckpointContractError("checkpoint evidence commit failed") from exc
        except BaseException:
            self._connection.rollback()
            raise
        return self.snapshot()


def progress_from_snapshot(
    snapshot: CheckpointSnapshot,
    now: datetime,
    monotonic_seconds: float,
    previous: ProgressSnapshot | None,
) -> ProgressSnapshot:
    """Build one progress record from a single transactional checkpoint view."""
    updated_at = _format_utc_datetime(now)
    if not math.isfinite(monotonic_seconds):
        raise CheckpointContractError("progress monotonic clock is invalid")
    current_phase = _phase_progress(snapshot.phases, snapshot.phase)
    rate: Decimal | None = None
    eta_seconds: int | None = None
    if (
        snapshot.status == "running"
        and previous is not None
        and previous.run_id == snapshot.run_id
        and previous.attempt_number == snapshot.attempt_number
        and previous.phase == snapshot.phase
        and previous.status == "running"
        and previous.sample_monotonic_seconds is not None
    ):
        prior_phase = _phase_progress(previous.phases, previous.phase)
        elapsed = Decimal(str(monotonic_seconds - previous.sample_monotonic_seconds))
        durable_delta = current_phase.completed - prior_phase.completed
        if elapsed > 0 and durable_delta > 0:
            rate = Decimal(durable_delta) / elapsed
            if current_phase.total is not None and current_phase.completed < current_phase.total:
                remaining = Decimal(current_phase.total - current_phase.completed)
                eta_seconds = int((remaining / rate).to_integral_value(rounding=ROUND_CEILING))
    progress = ProgressSnapshot(
        schema_version=PROGRESS_SCHEMA_VERSION,
        run_id=snapshot.run_id,
        attempt_number=snapshot.attempt_number,
        checkpoint_generation=snapshot.generation,
        status=snapshot.status,
        pool=snapshot.pool,
        mode=snapshot.mode,
        start_block=snapshot.start_block,
        end_block=snapshot.end_block,
        output_filename=snapshot.output_filename,
        phase=snapshot.phase,
        phases=snapshot.phases,
        last_durable=snapshot.last_durable,
        action_candidate_transaction_count=snapshot.action_candidate_transaction_count,
        action_count=snapshot.action_count,
        frozen_token_count=snapshot.frozen_token_count,
        full_transfer_log_count=snapshot.full_transfer_log_count,
        relevant_transfer_witness_count=snapshot.relevant_transfer_witness_count,
        relevant_transfer_transaction_count=snapshot.relevant_transfer_transaction_count,
        ownership_event_count=snapshot.ownership_event_count,
        bound_action_count=snapshot.bound_action_count,
        ledger_row_count=snapshot.ledger_row_count,
        rate_per_second=None if rate is None else _canonical_decimal(rate),
        eta_seconds=eta_seconds,
        started_at=snapshot.started_at_utc,
        resumed_at=snapshot.resumed_at_utc,
        updated_at=updated_at,
        finished_at=snapshot.finished_at_utc,
        output_published=snapshot.output_published,
        error_code=snapshot.error_code,
        sample_monotonic_seconds=monotonic_seconds,
    )
    _validate_progress(progress)
    return progress


def should_write_progress(
    previous: ProgressSnapshot | None,
    current: ProgressSnapshot,
) -> bool:
    """Apply the frozen lifecycle, time, and durable-unit write cadence."""
    if previous is None:
        return True
    if (
        previous.run_id != current.run_id
        or previous.attempt_number != current.attempt_number
        or previous.phase != current.phase
        or previous.status != current.status
        or previous.output_published != current.output_published
        or previous.error_code != current.error_code
    ):
        return True
    if previous.sample_monotonic_seconds is None or current.sample_monotonic_seconds is None:
        return False
    elapsed = current.sample_monotonic_seconds - previous.sample_monotonic_seconds
    if elapsed < 5:
        return False
    previous_phase = _phase_progress(previous.phases, previous.phase)
    current_phase = _phase_progress(current.phases, current.phase)
    durable_delta = current_phase.completed - previous_phase.completed
    return elapsed >= 30 or durable_delta >= 25


def write_progress_atomically(
    paths: CheckpointPaths,
    progress: ProgressSnapshot,
) -> None:
    """Durably replace progress without exposing a partial JSON record."""
    payload = progress.canonical_bytes()
    _ensure_secure_parent(paths.progress)
    _validate_regular_leaf(paths.progress, allow_missing=True)
    temporary = paths.progress.with_name(f"{paths.progress.name}.{uuid.uuid4().hex}.tmp")
    descriptor = -1
    replaced = False
    try:
        descriptor = _open_regular_leaf(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode=0o600,
        )
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("progress write returned no bytes")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _validate_regular_leaf(paths.progress, allow_missing=True)
        _validate_regular_leaf(temporary, allow_missing=False)
        os.replace(temporary, paths.progress)
        replaced = True
        _require_regular_mode(paths.progress, 0o600)
        _fsync_directory(paths.progress.parent)
    except CheckpointContractError:
        raise
    except OSError as exc:
        raise CheckpointContractError("progress file could not be replaced") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            try:
                if _validate_regular_leaf(temporary, allow_missing=True):
                    os.unlink(temporary)
            except (CheckpointContractError, OSError):
                pass


def read_export_status(paths: CheckpointPaths) -> ExportStatus:
    """Read normalized local status without creating files or calling RPC."""
    try:
        lock_held = _probe_existing_run_lock(paths)
    except (CheckpointContractError, OSError):
        return _blocked_export_status(paths)
    try:
        database_exists = _validate_regular_leaf(paths.database, allow_missing=True)
    except CheckpointContractError:
        return _blocked_export_status(paths, lock_held=lock_held)
    if database_exists:
        return _read_checkpoint_export_status(paths, lock_held)
    return _read_progress_only_status(paths, lock_held)


def render_safe_failure(
    pool: str,
    phase: str,
    error_code: ErrorCode,
    exception: BaseException,
) -> str:
    """Render only closed labels; arbitrary exception material is never observed."""
    del exception
    if _SAFE_LABEL.fullmatch(pool) is None:
        raise CheckpointContractError("failure pool label is invalid")
    if _SAFE_LABEL.fullmatch(phase) is None:
        raise CheckpointContractError("failure phase label is invalid")
    code = _require_error_code(error_code)
    return f"[lp-ledger][{pool}] phase={phase} error={code}"


def _snapshot_from_connection(connection: sqlite3.Connection) -> CheckpointSnapshot:
    row = connection.execute(
        """
        SELECT r.run_id, i.fingerprint_sha256, i.canonical_json,
               r.attempt_number, r.generation, r.status, r.phase,
               r.started_at_utc, r.resumed_at_utc, r.updated_at_utc,
               r.finished_at_utc, r.error_code, r.output_published
        FROM run_state AS r
        JOIN run_identity AS i ON i.singleton = r.singleton
        WHERE r.singleton = 1
        """
    ).fetchone()
    if row is None:
        raise CheckpointContractError("checkpoint run state is missing")
    identity = _parse_identity_json(cast(str, row["canonical_json"]))
    phase_rows = {
        cast(str, phase_row["phase"]): phase_row
        for phase_row in connection.execute(
            """
            SELECT phase, status, unit, completed, total, last_durable_json
            FROM phase_state
            """
        )
    }
    if tuple(sorted(phase_rows)) != tuple(sorted(_PHASES)):
        raise CheckpointContractError("checkpoint phase state is invalid")
    phases = tuple(
        PhaseProgress(
            phase=cast(Phase, phase),
            status=cast(PhaseStatus, phase_rows[phase]["status"]),
            unit=cast(str, phase_rows[phase]["unit"]),
            completed=cast(int, phase_rows[phase]["completed"]),
            total=cast(int | None, phase_rows[phase]["total"]),
        )
        for phase in _PHASES
    )
    current_phase = cast(Phase, row["phase"])
    last_durable: tuple[tuple[str, str], ...] | None = None
    for phase in reversed(_PHASES[: _PHASES.index(current_phase) + 1]):
        raw = cast(str | None, phase_rows[phase]["last_durable_json"])
        if raw is not None:
            public_durable = _public_durable_mapping(_parse_durable_mapping(raw))
            last_durable = public_durable or None
            break
    counters = connection.execute(
        """
        SELECT
          (SELECT COUNT(DISTINCT transaction_hash) FROM action_witnesses),
          (SELECT COUNT(*) FROM decoded_actions),
          (SELECT COUNT(*) FROM frozen_token_ids),
          (SELECT COALESCE(SUM(unfiltered_count), 0)
             FROM transfer_chunks WHERE status = 'completed'),
          (SELECT COUNT(*) FROM relevant_transfer_witnesses),
          (SELECT COUNT(DISTINCT transaction_hash)
             FROM relevant_transfer_witnesses),
          (SELECT COUNT(*) FROM ownership_events),
          (SELECT COUNT(*) FROM action_price_bindings)
        """
    ).fetchone()
    if counters is None:
        raise CheckpointContractError("checkpoint counters are missing")
    error_raw = cast(str | None, row["error_code"])
    error_code = None if error_raw is None else _require_error_code(error_raw)
    pool = _required_identity_str(identity, "pool")
    if _SAFE_LABEL.fullmatch(pool) is None:
        raise CheckpointContractError("checkpoint identity pool is invalid")
    mode_raw = _required_identity_str(identity, "verification_mode")
    if mode_raw not in _PROGRESS_MODES:
        raise CheckpointContractError("checkpoint identity mode is invalid")
    output_path = _required_identity_str(identity, "output_path")
    return CheckpointSnapshot(
        run_id=cast(str, row["run_id"]),
        identity_sha256=cast(str, row["fingerprint_sha256"]),
        attempt_number=cast(int, row["attempt_number"]),
        generation=cast(int, row["generation"]),
        status=cast(RunStatus, row["status"]),
        phase=current_phase,
        started_at_utc=cast(str, row["started_at_utc"]),
        resumed_at_utc=cast(str, row["resumed_at_utc"]),
        updated_at_utc=cast(str, row["updated_at_utc"]),
        finished_at_utc=cast(str | None, row["finished_at_utc"]),
        error_code=error_code,
        output_published=bool(row["output_published"]),
        pool=pool,
        mode=mode_raw,
        start_block=_required_identity_int(identity, "start_block"),
        end_block=_required_identity_int(identity, "end_block"),
        output_filename=Path(output_path).name,
        phases=phases,
        last_durable=last_durable,
        action_candidate_transaction_count=cast(int, counters[0]),
        action_count=cast(int, counters[1]),
        frozen_token_count=cast(int, counters[2]),
        full_transfer_log_count=cast(int, counters[3]),
        relevant_transfer_witness_count=cast(int, counters[4]),
        relevant_transfer_transaction_count=cast(int, counters[5]),
        ownership_event_count=cast(int, counters[6]),
        bound_action_count=cast(int, counters[7]),
        ledger_row_count=cast(int, phase_rows["build"]["completed"]),
    )


def _read_checkpoint_export_status(
    paths: CheckpointPaths,
    lock_held: bool,
) -> ExportStatus:
    try:
        with LPLedgerCheckpoint.open_status(paths) as run:
            run._connection.execute("BEGIN")
            try:
                _validate_checkpoint_status(run._connection, paths)
                snapshot = run.snapshot()
                run._connection.commit()
            except BaseException:
                run._connection.rollback()
                raise
    except (CheckpointContractError, sqlite3.DatabaseError, OSError):
        return _blocked_export_status(paths, lock_held=lock_held)
    progress = _read_progress_if_valid(paths)
    progress_current = (
        progress is not None
        and progress.run_id == snapshot.run_id
        and progress.attempt_number == snapshot.attempt_number
        and progress.checkpoint_generation == snapshot.generation
    )
    if lock_held:
        state: ExportStatusState = "active"
    elif snapshot.status == "running":
        state = "interrupted"
    elif snapshot.status == "succeeded":
        state = "succeeded"
    elif snapshot.status == "failed":
        state = "failed"
    else:
        state = "interrupted"
    current = _phase_progress(snapshot.phases, snapshot.phase)
    updated_at = (
        progress.updated_at
        if progress_current and progress is not None
        else snapshot.updated_at_utc
    )
    resumable = (
        snapshot.phase != "succeeded"
        and snapshot.status != "succeeded"
        and snapshot.error_code not in ("checkpoint_incompatible", "checkpoint_corrupt")
    )
    return ExportStatus(
        output=os.fspath(paths.output),
        pool=snapshot.pool,
        mode=snapshot.mode,
        start_block=snapshot.start_block,
        end_block=snapshot.end_block,
        state=state,
        phase=snapshot.phase,
        unit=current.unit,
        completed=current.completed,
        total=current.total,
        last_durable=snapshot.last_durable,
        action_candidate_transaction_count=snapshot.action_candidate_transaction_count,
        action_count=snapshot.action_count,
        frozen_token_count=snapshot.frozen_token_count,
        full_transfer_log_count=snapshot.full_transfer_log_count,
        relevant_transfer_witness_count=snapshot.relevant_transfer_witness_count,
        relevant_transfer_transaction_count=snapshot.relevant_transfer_transaction_count,
        ownership_event_count=snapshot.ownership_event_count,
        bound_action_count=snapshot.bound_action_count,
        ledger_row_count=snapshot.ledger_row_count,
        run_id=snapshot.run_id,
        attempt_number=snapshot.attempt_number,
        checkpoint_generation=snapshot.generation,
        updated_at=updated_at,
        updated_age_seconds=_timestamp_age_seconds(updated_at),
        rate_per_second=(
            progress.rate_per_second if progress_current and progress is not None else None
        ),
        eta_seconds=progress.eta_seconds if progress_current and progress is not None else None,
        progress_state="current" if progress_current else "progress_unavailable",
        lock_held=lock_held,
        locally_compatible=True,
        resumable=resumable,
        output_published=snapshot.output_published,
        error_code=snapshot.error_code,
    )


def _read_progress_only_status(
    paths: CheckpointPaths,
    lock_held: bool,
) -> ExportStatus:
    progress = _read_progress_if_valid(paths)
    if (
        progress is None
        or progress.checkpoint_generation is not None
        or progress.mode == "rpc_verified"
        or progress.output_filename != paths.output.name
    ):
        return _unknown_export_status(paths, lock_held=lock_held)
    current = _phase_progress(progress.phases, progress.phase)
    return ExportStatus(
        output=os.fspath(paths.output),
        pool=progress.pool,
        mode=progress.mode,
        start_block=progress.start_block,
        end_block=progress.end_block,
        state="active" if lock_held else "non_resumable",
        phase=progress.phase,
        unit=current.unit,
        completed=current.completed,
        total=current.total,
        last_durable=progress.last_durable,
        action_candidate_transaction_count=progress.action_candidate_transaction_count,
        action_count=progress.action_count,
        frozen_token_count=progress.frozen_token_count,
        full_transfer_log_count=progress.full_transfer_log_count,
        relevant_transfer_witness_count=progress.relevant_transfer_witness_count,
        relevant_transfer_transaction_count=progress.relevant_transfer_transaction_count,
        ownership_event_count=progress.ownership_event_count,
        bound_action_count=progress.bound_action_count,
        ledger_row_count=progress.ledger_row_count,
        run_id=progress.run_id,
        attempt_number=progress.attempt_number,
        checkpoint_generation=None,
        updated_at=progress.updated_at,
        updated_age_seconds=_timestamp_age_seconds(progress.updated_at),
        rate_per_second=progress.rate_per_second,
        eta_seconds=progress.eta_seconds,
        progress_state="current",
        lock_held=lock_held,
        locally_compatible=False,
        resumable=False,
        output_published=progress.output_published,
        error_code=progress.error_code,
    )


def _blocked_export_status(
    paths: CheckpointPaths,
    *,
    lock_held: bool = False,
) -> ExportStatus:
    return _empty_export_status(paths, "blocked", lock_held)


def _unknown_export_status(
    paths: CheckpointPaths,
    *,
    lock_held: bool,
) -> ExportStatus:
    return _empty_export_status(paths, "unknown", lock_held)


def _empty_export_status(
    paths: CheckpointPaths,
    state: Literal["blocked", "unknown"],
    lock_held: bool,
) -> ExportStatus:
    return ExportStatus(
        output=os.fspath(paths.output),
        pool=None,
        mode=None,
        start_block=None,
        end_block=None,
        state=state,
        phase=None,
        unit=None,
        completed=None,
        total=None,
        last_durable=None,
        action_candidate_transaction_count=None,
        action_count=None,
        frozen_token_count=None,
        full_transfer_log_count=None,
        relevant_transfer_witness_count=None,
        relevant_transfer_transaction_count=None,
        ownership_event_count=None,
        bound_action_count=None,
        ledger_row_count=None,
        run_id=None,
        attempt_number=None,
        checkpoint_generation=None,
        updated_at=None,
        updated_age_seconds=None,
        rate_per_second=None,
        eta_seconds=None,
        progress_state="progress_unavailable",
        lock_held=lock_held,
        locally_compatible=False,
        resumable=False,
        output_published=False,
        error_code=None,
    )


def _probe_existing_run_lock(paths: CheckpointPaths) -> bool:
    _validate_existing_parent(paths.run_lock)
    if not _validate_regular_leaf(paths.run_lock, allow_missing=True):
        return False
    _require_regular_mode(paths.run_lock, 0o600)
    descriptor = _open_regular_leaf(paths.run_lock, os.O_RDONLY, mode=0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as exc:
            raise CheckpointContractError("run lock could not be released") from exc
        return False
    finally:
        os.close(descriptor)


def _read_progress_if_valid(paths: CheckpointPaths) -> ProgressSnapshot | None:
    try:
        _validate_existing_parent(paths.progress)
        if not _validate_regular_leaf(paths.progress, allow_missing=True):
            return None
        _require_regular_mode(paths.progress, 0o600)
        descriptor = _open_regular_leaf(paths.progress, os.O_RDONLY, mode=0o600)
        try:
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_PROGRESS_BYTES:
                    raise CheckpointContractError("progress JSON is too large")
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        return _parse_progress_bytes(b"".join(chunks))
    except (CheckpointContractError, OSError, UnicodeError, ValueError):
        return None


def _progress_canonical_bytes(progress: ProgressSnapshot) -> bytes:
    _validate_progress(progress)
    payload: dict[str, object] = {
        "schema_version": progress.schema_version,
        "run_id": progress.run_id,
        "attempt_number": progress.attempt_number,
        "checkpoint_generation": progress.checkpoint_generation,
        "status": progress.status,
        "pool": progress.pool,
        "mode": progress.mode,
        "start_block": progress.start_block,
        "end_block": progress.end_block,
        "output_filename": progress.output_filename,
        "phase": progress.phase,
        "phases": {
            phase.phase: {
                "status": phase.status,
                "unit": phase.unit,
                "completed": phase.completed,
                "total": phase.total,
            }
            for phase in progress.phases
        },
        "last_durable": (None if progress.last_durable is None else dict(progress.last_durable)),
        "action_candidate_transaction_count": progress.action_candidate_transaction_count,
        "action_count": progress.action_count,
        "frozen_token_count": progress.frozen_token_count,
        "full_transfer_log_count": progress.full_transfer_log_count,
        "relevant_transfer_witness_count": progress.relevant_transfer_witness_count,
        "relevant_transfer_transaction_count": progress.relevant_transfer_transaction_count,
        "ownership_event_count": progress.ownership_event_count,
        "bound_action_count": progress.bound_action_count,
        "ledger_row_count": progress.ledger_row_count,
        "rate_per_second": progress.rate_per_second,
        "eta_seconds": progress.eta_seconds,
        "started_at": progress.started_at,
        "resumed_at": progress.resumed_at,
        "updated_at": progress.updated_at,
        "finished_at": progress.finished_at,
        "output_published": progress.output_published,
        "error_code": progress.error_code,
    }
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _parse_progress_bytes(payload: bytes) -> ProgressSnapshot:
    if not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        raise CheckpointContractError("progress JSON newline is invalid")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CheckpointContractError("progress JSON has a duplicate key")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload[:-1].decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except CheckpointContractError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise CheckpointContractError("progress JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise CheckpointContractError("progress JSON root is invalid")
    expected_keys = {
        "schema_version",
        "run_id",
        "attempt_number",
        "checkpoint_generation",
        "status",
        "pool",
        "mode",
        "start_block",
        "end_block",
        "output_filename",
        "phase",
        "phases",
        "last_durable",
        "action_candidate_transaction_count",
        "action_count",
        "frozen_token_count",
        "full_transfer_log_count",
        "relevant_transfer_witness_count",
        "relevant_transfer_transaction_count",
        "ownership_event_count",
        "bound_action_count",
        "ledger_row_count",
        "rate_per_second",
        "eta_seconds",
        "started_at",
        "resumed_at",
        "updated_at",
        "finished_at",
        "output_published",
        "error_code",
    }
    if set(decoded) != expected_keys:
        raise CheckpointContractError("progress JSON fields are invalid")
    raw_phases = decoded["phases"]
    if not isinstance(raw_phases, dict) or tuple(sorted(raw_phases)) != tuple(sorted(_PHASES)):
        raise CheckpointContractError("progress phase mapping is invalid")
    phases: list[PhaseProgress] = []
    for phase in _PHASES:
        raw_phase = raw_phases[phase]
        if not isinstance(raw_phase, dict) or set(raw_phase) != {
            "status",
            "unit",
            "completed",
            "total",
        }:
            raise CheckpointContractError("progress phase fields are invalid")
        phases.append(
            PhaseProgress(
                phase=cast(Phase, phase),
                status=cast(PhaseStatus, _required_json_str(raw_phase, "status")),
                unit=_required_json_str(raw_phase, "unit"),
                completed=_required_json_int(raw_phase, "completed"),
                total=_optional_json_int(raw_phase, "total"),
            )
        )
    raw_last = decoded["last_durable"]
    if raw_last is None:
        last_durable = None
    elif isinstance(raw_last, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_last.items()
    ):
        last_durable = tuple(sorted(cast(dict[str, str], raw_last).items()))
    else:
        raise CheckpointContractError("progress durable identity is invalid")
    generation = _optional_json_int(decoded, "checkpoint_generation")
    finished_at = _optional_json_str(decoded, "finished_at")
    rate = _optional_json_str(decoded, "rate_per_second")
    error_raw = _optional_json_str(decoded, "error_code")
    progress = ProgressSnapshot(
        schema_version=_required_json_int(decoded, "schema_version"),
        run_id=_required_json_str(decoded, "run_id"),
        attempt_number=_required_json_int(decoded, "attempt_number"),
        checkpoint_generation=generation,
        status=cast(RunStatus, _required_json_str(decoded, "status")),
        pool=_required_json_str(decoded, "pool"),
        mode=cast(ProgressMode, _required_json_str(decoded, "mode")),
        start_block=_required_json_int(decoded, "start_block"),
        end_block=_required_json_int(decoded, "end_block"),
        output_filename=_required_json_str(decoded, "output_filename"),
        phase=cast(Phase, _required_json_str(decoded, "phase")),
        phases=tuple(phases),
        last_durable=last_durable,
        action_candidate_transaction_count=_required_json_int(
            decoded, "action_candidate_transaction_count"
        ),
        action_count=_required_json_int(decoded, "action_count"),
        frozen_token_count=_required_json_int(decoded, "frozen_token_count"),
        full_transfer_log_count=_required_json_int(decoded, "full_transfer_log_count"),
        relevant_transfer_witness_count=_required_json_int(
            decoded, "relevant_transfer_witness_count"
        ),
        relevant_transfer_transaction_count=_required_json_int(
            decoded, "relevant_transfer_transaction_count"
        ),
        ownership_event_count=_required_json_int(decoded, "ownership_event_count"),
        bound_action_count=_required_json_int(decoded, "bound_action_count"),
        ledger_row_count=_required_json_int(decoded, "ledger_row_count"),
        rate_per_second=rate,
        eta_seconds=_optional_json_int(decoded, "eta_seconds"),
        started_at=_required_json_str(decoded, "started_at"),
        resumed_at=_required_json_str(decoded, "resumed_at"),
        updated_at=_required_json_str(decoded, "updated_at"),
        finished_at=finished_at,
        output_published=_required_json_bool(decoded, "output_published"),
        error_code=None if error_raw is None else _require_error_code(error_raw),
        sample_monotonic_seconds=None,
    )
    if progress.canonical_bytes() != payload:
        raise CheckpointContractError("progress JSON is not canonical")
    return progress


def _validate_progress(progress: ProgressSnapshot) -> None:
    if progress.schema_version != PROGRESS_SCHEMA_VERSION:
        raise CheckpointContractError("progress schema version is invalid")
    try:
        if str(uuid.UUID(progress.run_id)) != progress.run_id:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise CheckpointContractError("progress run ID is invalid") from exc
    attempt_number = _require_nonnegative_int(
        progress.attempt_number,
        "progress attempt number",
    )
    if attempt_number < 1:
        raise CheckpointContractError("progress attempt number is invalid")
    if progress.checkpoint_generation is not None:
        generation = _require_nonnegative_int(
            progress.checkpoint_generation,
            "progress generation",
        )
        if generation < 1:
            raise CheckpointContractError("progress generation is invalid")
    if progress.status not in ("running", "succeeded", "failed", "interrupted"):
        raise CheckpointContractError("progress status is invalid")
    if progress.phase not in _PHASES:
        raise CheckpointContractError("progress phase is invalid")
    if progress.mode not in _PROGRESS_MODES:
        raise CheckpointContractError("progress mode is invalid")
    if _SAFE_LABEL.fullmatch(progress.pool) is None:
        raise CheckpointContractError("progress pool is invalid")
    if (
        not progress.output_filename
        or Path(progress.output_filename).name != progress.output_filename
    ):
        raise CheckpointContractError("progress output filename is invalid")
    start_block = _require_nonnegative_int(progress.start_block, "progress start block")
    end_block = _require_nonnegative_int(progress.end_block, "progress end block")
    if end_block < start_block:
        raise CheckpointContractError("progress block range is invalid")
    if tuple(phase.phase for phase in progress.phases) != _PHASES:
        raise CheckpointContractError("progress phases are invalid")
    for phase in progress.phases:
        if phase.status not in ("pending", "running", "completed"):
            raise CheckpointContractError("progress phase status is invalid")
        completed = _require_nonnegative_int(
            phase.completed,
            "progress phase completed counter",
        )
        if not phase.unit:
            raise CheckpointContractError("progress phase counter is invalid")
        if phase.total is not None:
            total = _require_nonnegative_int(
                phase.total,
                "progress phase total",
            )
            if total < completed:
                raise CheckpointContractError("progress phase total is invalid")
    for counter in (
        progress.action_candidate_transaction_count,
        progress.action_count,
        progress.frozen_token_count,
        progress.full_transfer_log_count,
        progress.relevant_transfer_witness_count,
        progress.relevant_transfer_transaction_count,
        progress.ownership_event_count,
        progress.bound_action_count,
        progress.ledger_row_count,
    ):
        _require_nonnegative_int(counter, "progress counter")
    if progress.last_durable is not None:
        if tuple(sorted(progress.last_durable)) != progress.last_durable:
            raise CheckpointContractError("progress durable identity is not canonical")
        _validate_public_durable(progress.last_durable)
    if progress.rate_per_second is not None:
        try:
            rate = Decimal(progress.rate_per_second)
        except InvalidOperation as exc:
            raise CheckpointContractError("progress rate is invalid") from exc
        if (
            not rate.is_finite()
            or rate <= 0
            or _canonical_decimal(rate) != progress.rate_per_second
        ):
            raise CheckpointContractError("progress rate is invalid")
    if progress.eta_seconds is not None:
        _require_nonnegative_int(progress.eta_seconds, "progress ETA")
    if type(progress.output_published) is not bool:
        raise CheckpointContractError("progress output publication flag is invalid")
    for timestamp in (
        progress.started_at,
        progress.resumed_at,
        progress.updated_at,
    ):
        _parse_utc_datetime(timestamp)
    if progress.finished_at is not None:
        _parse_utc_datetime(progress.finished_at)
    if progress.error_code is not None:
        _require_error_code(progress.error_code)
    _validate_progress_state(progress)


def _validate_progress_state(progress: ProgressSnapshot) -> None:
    current_index = _PHASES.index(progress.phase)
    expected_phase_statuses = tuple(
        "completed" if index < current_index else "running" if index == current_index else "pending"
        for index in range(len(_PHASES))
    )
    if progress.phase == "succeeded":
        expected_phase_statuses = tuple("completed" for _phase in _PHASES)
    if tuple(phase.status for phase in progress.phases) != expected_phase_statuses:
        raise CheckpointContractError("progress phase cursor is inconsistent")
    if progress.status == "running":
        valid = (
            progress.phase != "succeeded"
            and progress.finished_at is None
            and progress.error_code is None
            and not progress.output_published
        )
    elif progress.status == "interrupted":
        valid = (
            progress.phase != "succeeded"
            and progress.finished_at is not None
            and progress.error_code == "interrupted"
            and not progress.output_published
        )
    elif progress.status == "failed" and progress.phase == "succeeded":
        valid = (
            progress.finished_at is not None
            and progress.error_code == "checkpoint_maintenance_error"
            and progress.output_published
        )
    elif progress.status == "failed":
        valid = (
            progress.finished_at is not None
            and progress.error_code is not None
            and not progress.output_published
        )
    else:
        valid = (
            progress.phase == "succeeded"
            and progress.finished_at is not None
            and progress.error_code is None
            and progress.output_published
        )
    if not valid:
        raise CheckpointContractError("progress run state is inconsistent")


def _export_status_payload(status: ExportStatus) -> dict[str, object]:
    return {
        "output": status.output,
        "pool": status.pool,
        "mode": status.mode,
        "start_block": status.start_block,
        "end_block": status.end_block,
        "state": status.state,
        "phase": status.phase,
        "unit": status.unit,
        "completed": status.completed,
        "total": status.total,
        "last_durable": None if status.last_durable is None else dict(status.last_durable),
        "action_candidate_transaction_count": status.action_candidate_transaction_count,
        "action_count": status.action_count,
        "frozen_token_count": status.frozen_token_count,
        "full_transfer_log_count": status.full_transfer_log_count,
        "relevant_transfer_witness_count": status.relevant_transfer_witness_count,
        "relevant_transfer_transaction_count": status.relevant_transfer_transaction_count,
        "ownership_event_count": status.ownership_event_count,
        "bound_action_count": status.bound_action_count,
        "ledger_row_count": status.ledger_row_count,
        "run_id": status.run_id,
        "attempt_number": status.attempt_number,
        "checkpoint_generation": status.checkpoint_generation,
        "updated_at": status.updated_at,
        "updated_age_seconds": status.updated_age_seconds,
        "rate_per_second": status.rate_per_second,
        "eta_seconds": status.eta_seconds,
        "progress_state": status.progress_state,
        "lock_held": status.lock_held,
        "locally_compatible": status.locally_compatible,
        "resumable": status.resumable,
        "output_published": status.output_published,
        "error_code": status.error_code,
    }


def _phase_progress(
    phases: tuple[PhaseProgress, ...],
    phase: Phase,
) -> PhaseProgress:
    for candidate in phases:
        if candidate.phase == phase:
            return candidate
    raise CheckpointContractError("progress current phase is missing")


def _phase_durable_counter(
    connection: sqlite3.Connection,
    phase: Phase,
    key: str,
) -> int | None:
    row = connection.execute(
        "SELECT last_durable_json FROM phase_state WHERE phase = ?",
        (phase,),
    ).fetchone()
    if row is None:
        raise CheckpointContractError("checkpoint phase state is missing")
    durable = _optional_durable_mapping(cast(str | None, row[0]))
    return _durable_mapping_counter(durable, key)


def _optional_durable_mapping(
    payload: str | None,
) -> tuple[tuple[str, str], ...] | None:
    return None if payload is None else _parse_durable_mapping(payload)


def _parse_durable_mapping(payload: str) -> tuple[tuple[str, str], ...]:
    value = _parse_canonical_json(payload, "checkpoint durable identity")
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise CheckpointContractError("checkpoint durable identity is invalid")
    return tuple(sorted(cast(dict[str, str], value).items()))


def _public_durable_mapping(
    durable: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    keys = {key for key, _value in durable}
    if not keys <= _PUBLIC_DURABLE_KEYS | _INTERNAL_DURABLE_COUNTER_KEYS:
        raise CheckpointContractError("checkpoint durable identity field is invalid")
    public = tuple(item for item in durable if item[0] in _PUBLIC_DURABLE_KEYS)
    _validate_public_durable(public)
    return public


def _validate_public_durable(durable: tuple[tuple[str, str], ...]) -> None:
    for key, value in durable:
        if key not in _PUBLIC_DURABLE_KEYS:
            raise CheckpointContractError("progress durable identity field is invalid")
        if key == "transaction_hash":
            _require_lower_hex(value, 32, "durable transaction hash")
            continue
        if key in {
            "token_set_sha256",
            "unfiltered_transfer_sha256",
            "replay_input_sha256",
        }:
            _require_lower_hex(value, 32, f"durable {key}", prefix=False)
            continue
        try:
            parsed = int(value)
        except ValueError as exc:
            raise CheckpointContractError("progress durable identity value is invalid") from exc
        if parsed < 0 or str(parsed) != value:
            raise CheckpointContractError("progress durable identity value is invalid")


def _durable_mapping_counter(
    durable: tuple[tuple[str, str], ...] | None,
    key: str,
) -> int | None:
    if durable is None:
        return None
    raw = dict(durable).get(key)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise CheckpointContractError("checkpoint durable counter is invalid") from exc
    if value < 0 or str(value) != raw:
        raise CheckpointContractError("checkpoint durable counter is invalid")
    return value


def _parse_identity_json(payload: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
        canonical = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CheckpointContractError("checkpoint identity JSON is invalid") from exc
    if not isinstance(value, dict) or canonical != payload:
        raise CheckpointContractError("checkpoint identity JSON is not canonical")
    return cast(dict[str, object], value)


def _required_identity_str(identity: dict[str, object], key: str) -> str:
    value = identity.get(key)
    if not isinstance(value, str) or not value:
        raise CheckpointContractError(f"checkpoint identity {key} is invalid")
    return value


def _required_identity_int(identity: dict[str, object], key: str) -> int:
    value = identity.get(key)
    if type(value) is not int or value < 0:
        raise CheckpointContractError(f"checkpoint identity {key} is invalid")
    return value


def _required_json_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise CheckpointContractError(f"progress {key} is invalid")
    return value


def _optional_json_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CheckpointContractError(f"progress {key} is invalid")
    return value


def _required_json_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int:
        raise CheckpointContractError(f"progress {key} is invalid")
    return value


def _optional_json_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise CheckpointContractError(f"progress {key} is invalid")
    return value


def _required_json_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if type(value) is not bool:
        raise CheckpointContractError(f"progress {key} is invalid")
    return value


def _require_error_code(value: str) -> ErrorCode:
    if value not in _ERROR_CODES:
        raise CheckpointContractError("checkpoint error code is invalid")
    return value


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise CheckpointContractError("progress decimal is not finite")
    result = format(value.normalize(), "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in ("-0", "") else result


def _format_utc_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CheckpointContractError("progress UTC timestamp is naive")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc_datetime(value: str) -> datetime:
    if not value.endswith("Z"):
        raise CheckpointContractError("progress UTC timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CheckpointContractError("progress UTC timestamp is invalid") from exc
    if _format_utc_datetime(parsed) != value:
        raise CheckpointContractError("progress UTC timestamp is not canonical")
    return parsed


def _timestamp_age_seconds(value: str) -> int:
    age = datetime.now(timezone.utc) - _parse_utc_datetime(value)
    return max(0, int(age.total_seconds()))


def candidate_payload_sha256(transaction_json: str, receipt_json: str) -> str:
    transaction = _parse_canonical_json(transaction_json, "transaction JSON")
    receipt = _parse_canonical_json(receipt_json, "receipt JSON")
    envelope = _canonical_json({"receipt": receipt, "transaction": transaction})
    return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CheckpointContractError("checkpoint JSON value is invalid") from exc


def _parse_canonical_json(payload: str, label: str) -> object:
    if not isinstance(payload, str) or not payload:
        raise CheckpointContractError(f"{label} is missing")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CheckpointContractError(f"{label} has a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=unique_object)
    except CheckpointContractError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CheckpointContractError(f"{label} is invalid") from exc
    _reject_json_numbers(value, label)
    if _canonical_json(value) != payload:
        raise CheckpointContractError(f"{label} is not canonical")
    return value


def _reject_json_numbers(value: object, label: str) -> None:
    if type(value) in (int, float):
        raise CheckpointContractError(f"{label} contains a JSON number")
    if isinstance(value, list):
        for item in value:
            _reject_json_numbers(item, label)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_json_numbers(item, label)


def _advance_phase(
    connection: sqlite3.Connection,
    phase: Phase,
    generation: int,
) -> None:
    index = _PHASES.index(phase)
    if index >= len(_PHASES) - 1:
        raise CheckpointContractError("terminal phase cannot advance through staged evidence")
    row = connection.execute(
        "SELECT status, completed, total FROM phase_state WHERE phase = ?",
        (phase,),
    ).fetchone()
    if (
        row is None
        or row["status"] != "running"
        or row["total"] is None
        or row["completed"] != row["total"]
    ):
        raise CheckpointContractError("checkpoint phase evidence is incomplete")
    next_phase = _PHASES[index + 1]
    connection.execute(
        """
        UPDATE phase_state
        SET status = 'completed', updated_generation = ?
        WHERE phase = ?
        """,
        (generation, phase),
    )
    cursor = connection.execute(
        """
        UPDATE phase_state
        SET status = 'running', updated_generation = ?
        WHERE phase = ? AND status = 'pending'
        """,
        (generation, next_phase),
    )
    if cursor.rowcount != 1:
        raise CheckpointContractError("checkpoint next phase is not pending")
    connection.execute(
        "UPDATE run_state SET phase = ? WHERE singleton = 1",
        (next_phase,),
    )


def _require_phase_evidence_complete(
    connection: sqlite3.Connection,
    phase: Phase,
) -> None:
    if phase == "action_fetch":
        if (
            connection.execute(
                """
            SELECT 1
            FROM action_witnesses AS w
            LEFT JOIN action_bundle_fetches AS f
              ON f.transaction_hash = w.transaction_hash
            WHERE f.transaction_hash IS NULL
            LIMIT 1
            """
            ).fetchone()
            is not None
        ):
            raise CheckpointContractError("action fetch evidence is incomplete")
        completed = cast(
            int,
            connection.execute("SELECT COUNT(*) FROM action_bundle_fetches").fetchone()[0],
        )
    elif phase == "action_decode":
        if (
            connection.execute(
                """
                SELECT 1
                FROM action_bundle_fetches AS f
                LEFT JOIN decoded_action_transactions AS d
                  ON d.transaction_hash = f.transaction_hash
                WHERE d.transaction_hash IS NULL
                LIMIT 1
                """
            ).fetchone()
            is not None
        ):
            raise CheckpointContractError("action decode evidence is incomplete")
        completed = cast(
            int,
            connection.execute(
                "SELECT COUNT(*) FROM decoded_action_transactions"
            ).fetchone()[0],
        )
    elif phase == "relevant_transfer_fetch":
        if (
            connection.execute(
                """
                SELECT 1
                FROM relevant_transfer_witnesses AS w
                LEFT JOIN relevant_transfer_bundle_fetches AS f
                  ON f.transaction_hash = w.transaction_hash
                WHERE f.transaction_hash IS NULL
                LIMIT 1
                """
            ).fetchone()
            is not None
        ):
            raise CheckpointContractError("relevant transfer fetch evidence is incomplete")
        completed = cast(
            int,
            connection.execute(
                "SELECT COUNT(*) FROM relevant_transfer_bundle_fetches"
            ).fetchone()[0],
        )
    elif phase == "relevant_transfer_decode":
        if (
            connection.execute(
                """
                SELECT 1
                FROM relevant_transfer_bundle_fetches AS f
                LEFT JOIN decoded_relevant_transfer_transactions AS d
                  ON d.transaction_hash = f.transaction_hash
                WHERE d.transaction_hash IS NULL
                LIMIT 1
                """
            ).fetchone()
            is not None
        ):
            raise CheckpointContractError("relevant transfer decode evidence is incomplete")
        completed = cast(
            int,
            connection.execute(
                "SELECT COUNT(*) FROM decoded_relevant_transfer_transactions"
            ).fetchone()[0],
        )
    elif phase == "price_replay":
        action_count = cast(
            int,
            connection.execute("SELECT COUNT(*) FROM decoded_actions").fetchone()[0],
        )
        binding_count = cast(
            int,
            connection.execute("SELECT COUNT(*) FROM action_price_bindings").fetchone()[0],
        )
        if binding_count != action_count:
            raise CheckpointContractError("price replay evidence is incomplete")
        completed = binding_count
    else:
        raise CheckpointContractError("phase has no evidence completion rule")
    connection.execute(
        """
        UPDATE phase_state
        SET completed = ?, updated_generation = updated_generation
        WHERE phase = ?
        """,
        (completed, phase),
    )


def _validate_action_witness_batch(
    block_range: BlockRange,
    witnesses: tuple[DiscoveryWitness, ...],
) -> None:
    _validate_block_range(block_range)
    canonical = tuple(
        sorted(
            witnesses,
            key=lambda witness: (
                witness.block_number,
                witness.transaction_index,
                witness.log_index,
                witness.transaction_hash,
            ),
        )
    )
    if witnesses != canonical:
        raise CheckpointContractError("action witnesses are not canonically ordered")
    identities: set[tuple[str, int]] = set()
    locations: set[tuple[int, int]] = set()
    for witness in witnesses:
        _validate_witness_shape(witness, expected_source="pool_modify")
        if not block_range.start_block <= witness.block_number <= block_range.end_block:
            raise CheckpointContractError("action witness is outside its chunk")
        identity = (witness.transaction_hash, witness.log_index)
        location = (witness.block_number, witness.log_index)
        if identity in identities or location in locations:
            raise CheckpointContractError("action witness identity is duplicated")
        identities.add(identity)
        locations.add(location)


def _validate_transfer_witness_batch(
    block_range: BlockRange,
    witnesses: tuple[DiscoveryWitness, ...],
) -> None:
    _validate_block_range(block_range)
    canonical = tuple(
        sorted(
            witnesses,
            key=lambda witness: (
                witness.block_number,
                witness.transaction_index,
                witness.log_index,
                witness.transaction_hash,
            ),
        )
    )
    if witnesses != canonical:
        raise CheckpointContractError("transfer witnesses are not canonically ordered")
    identities: set[tuple[str, int]] = set()
    locations: set[tuple[int, int]] = set()
    for witness in witnesses:
        _validate_witness_shape(witness, expected_source="position_transfer")
        if not block_range.start_block <= witness.block_number <= block_range.end_block:
            raise CheckpointContractError("transfer witness is outside its chunk")
        _transfer_witness_token_id(witness)
        _transfer_witness_owners(witness)
        identity = (witness.transaction_hash, witness.log_index)
        location = (witness.block_number, witness.log_index)
        if identity in identities or location in locations:
            raise CheckpointContractError("transfer witness identity is duplicated")
        identities.add(identity)
        locations.add(location)


def _transfer_witness_token_id(witness: DiscoveryWitness) -> int:
    if len(witness.topics) != 4 or witness.data != "0x":
        raise CheckpointContractError("transfer witness is not an indexed ERC-721 Transfer")
    return int(witness.topics[3][2:], 16)


def _transfer_witness_owners(
    witness: DiscoveryWitness,
) -> tuple[str | None, str | None]:
    if len(witness.topics) != 4:
        raise CheckpointContractError("transfer witness topics are invalid")
    return (
        _optional_transfer_topic_address(witness.topics[1], "transfer from topic"),
        _optional_transfer_topic_address(witness.topics[2], "transfer to topic"),
    )


def _optional_transfer_topic_address(topic: str, label: str) -> str | None:
    normalized = _require_lower_hex(topic, 32, label)
    if normalized[2:26] != "0" * 24:
        raise CheckpointContractError(f"{label} is not a canonical indexed address")
    address = f"0x{normalized[-40:]}"
    return None if address == f"0x{'0' * 40}" else address


def _validate_witness_shape(
    witness: DiscoveryWitness,
    *,
    expected_source: DiscoverySource,
) -> None:
    if witness.source != expected_source:
        raise CheckpointContractError("witness source is invalid")
    _require_nonnegative_int(witness.block_number, "block number")
    _require_nonnegative_int(witness.transaction_index, "transaction index")
    _require_nonnegative_int(witness.log_index, "log index")
    _require_lower_hex(witness.block_hash, 32, "block hash")
    _require_lower_hex(witness.transaction_hash, 32, "transaction hash")
    _require_lower_hex(witness.address, 20, "address")
    if not witness.topics:
        raise CheckpointContractError("witness topics are empty")
    for index, topic in enumerate(witness.topics):
        _require_lower_hex(topic, 32, f"topic {index}")
    _require_hex_data(witness.data, "log data")


def _require_hex_data(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("0x")
        or len(value) % 2 != 0
        or re.fullmatch(r"0x[0-9a-f]*", value, flags=re.ASCII) is None
    ):
        raise CheckpointContractError(f"{label} is not canonical hex data")
    return value


def _validate_block_range(block_range: BlockRange) -> None:
    _require_nonnegative_int(block_range.index, "chunk index")
    _require_nonnegative_int(block_range.start_block, "chunk start block")
    _require_nonnegative_int(block_range.end_block, "chunk end block")
    if block_range.end_block < block_range.start_block:
        raise CheckpointContractError("chunk range is reversed")


def _require_pending_range(
    connection: sqlite3.Connection,
    table: Literal["action_chunks", "transfer_chunks"],
    block_range: BlockRange,
) -> None:
    row = connection.execute(
        f"""
        SELECT start_block, end_block, status
        FROM {table}
        WHERE chunk_index = ?
        """,
        (block_range.index,),
    ).fetchone()
    if (
        row is None
        or row["start_block"] != block_range.start_block
        or row["end_block"] != block_range.end_block
        or row["status"] != "pending"
    ):
        raise CheckpointContractError("checkpoint chunk is not the expected pending range")


def _upsert_chain_block(
    connection: sqlite3.Connection,
    block_number: int,
    block_hash: str,
    timestamp_ms: int | None,
    generation: int,
) -> None:
    _require_nonnegative_int(block_number, "block number")
    normalized_hash = _require_lower_hex(block_hash, 32, "block hash")
    if timestamp_ms is not None:
        _require_nonnegative_int(timestamp_ms, "block timestamp")
    _require_endpoint_observation(
        connection,
        block_number,
        normalized_hash,
        timestamp_ms,
    )
    row = connection.execute(
        "SELECT block_hash, timestamp_ms FROM chain_blocks WHERE block_number = ?",
        (block_number,),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO chain_blocks (
                block_number, block_hash, timestamp_ms, committed_generation
            ) VALUES (?, ?, ?, ?)
            """,
            (block_number, normalized_hash, timestamp_ms, generation),
        )
        return
    if row["block_hash"] != normalized_hash:
        raise CheckpointContractError("block number has conflicting hashes")
    persisted_timestamp = cast(int | None, row["timestamp_ms"])
    if timestamp_ms is None or persisted_timestamp == timestamp_ms:
        return
    if persisted_timestamp is not None:
        raise CheckpointContractError("block header has conflicting timestamps")
    connection.execute(
        """
        UPDATE chain_blocks
        SET timestamp_ms = ?, committed_generation = ?
        WHERE block_number = ?
        """,
        (timestamp_ms, generation, block_number),
    )


def _require_endpoint_observation(
    connection: sqlite3.Connection,
    block_number: int,
    block_hash: str,
    timestamp_ms: int | None,
) -> None:
    row = connection.execute(
        "SELECT canonical_json FROM run_identity WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise CheckpointContractError("checkpoint identity is missing")
    payload = cast(dict[str, object], json.loads(cast(str, row["canonical_json"])))
    endpoint = cast(dict[str, object], payload["endpoint"])
    for key in ("start", "end"):
        expected = cast(dict[str, object], endpoint[key])
        if expected["block_number"] != block_number:
            continue
        if expected["block_hash"] != block_hash:
            raise CheckpointContractError("endpoint block hash changed")
        if timestamp_ms is not None and expected["timestamp_ms"] != timestamp_ms:
            raise CheckpointContractError("endpoint block timestamp changed")


def _identity_payload(connection: sqlite3.Connection) -> dict[str, object]:
    row = connection.execute(
        "SELECT canonical_json FROM run_identity WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise CheckpointContractError("checkpoint identity is missing")
    payload = json.loads(cast(str, row["canonical_json"]))
    if not isinstance(payload, dict):
        raise CheckpointContractError("checkpoint identity payload is invalid")
    return cast(dict[str, object], payload)


def _completed_count(
    connection: sqlite3.Connection,
    table: Literal["action_chunks", "transfer_chunks"],
) -> int:
    return cast(
        int,
        connection.execute(f"SELECT COUNT(*) FROM {table} WHERE status = 'completed'").fetchone()[
            0
        ],
    )


def _pending_count(
    connection: sqlite3.Connection,
    table: Literal["action_chunks", "transfer_chunks"],
) -> int:
    return cast(
        int,
        connection.execute(f"SELECT COUNT(*) FROM {table} WHERE status = 'pending'").fetchone()[0],
    )


def _action_candidate_count(connection: sqlite3.Connection) -> int:
    return cast(
        int,
        connection.execute(
            "SELECT COUNT(DISTINCT transaction_hash) FROM action_witnesses"
        ).fetchone()[0],
    )


def _relevant_transfer_transaction_count(connection: sqlite3.Connection) -> int:
    return cast(
        int,
        connection.execute(
            "SELECT COUNT(DISTINCT transaction_hash) FROM relevant_transfer_witnesses"
        ).fetchone()[0],
    )


def _witness_from_row(row: sqlite3.Row) -> DiscoveryWitness:
    raw_topics = _parse_canonical_json(cast(str, row["topics_json"]), "witness topics")
    if not isinstance(raw_topics, list) or not all(
        isinstance(topic, str) for topic in raw_topics
    ):
        raise CheckpointContractError("witness topics are invalid")
    witness = DiscoveryWitness(
        source=cast(DiscoverySource, row["source"]),
        block_number=cast(int, row["block_number"]),
        block_hash=_require_lower_hex(cast(str, row["block_hash"]), 32, "block hash"),
        transaction_hash=_require_lower_hex(
            cast(str, row["transaction_hash"]), 32, "transaction hash"
        ),
        transaction_index=cast(int, row["transaction_index"]),
        log_index=cast(int, row["log_index"]),
        address=_require_lower_hex(cast(str, row["address"]), 20, "address"),
        topics=tuple(cast(list[str], raw_topics)),
        data=cast(str, row["data"]),
    )
    _validate_witness_shape(witness, expected_source=witness.source)
    return witness


def _validate_candidate_bundle_shape(bundle: CandidateBundle) -> None:
    _require_lower_hex(bundle.transaction_hash, 32, "transaction hash")
    _require_lower_hex(bundle.block_hash, 32, "block hash")
    _require_nonnegative_int(bundle.block_number, "block number")
    _require_nonnegative_int(bundle.transaction_index, "transaction index")
    _require_lower_hex(bundle.payload_sha256, 32, "candidate payload digest", prefix=False)
    expected_digest = candidate_payload_sha256(bundle.transaction_json, bundle.receipt_json)
    if bundle.payload_sha256 != expected_digest:
        raise CheckpointContractError("candidate payload digest does not match")


def _insert_or_match_transaction_bundle(
    connection: sqlite3.Connection,
    bundle: CandidateBundle,
    generation: int,
) -> None:
    row = connection.execute(
        """
        SELECT transaction_hash, block_number, block_hash, transaction_index,
               transaction_json, receipt_json, payload_sha256
        FROM transaction_bundles
        WHERE transaction_hash = ?
        """,
        (bundle.transaction_hash,),
    ).fetchone()
    if row is not None:
        if _bundle_from_row(row) != bundle:
            raise CheckpointContractError("transaction bundle cache entry changed")
        return
    connection.execute(
        """
        INSERT INTO transaction_bundles (
            transaction_hash, block_number, block_hash, transaction_index,
            transaction_json, receipt_json, payload_sha256,
            committed_generation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bundle.transaction_hash,
            bundle.block_number,
            bundle.block_hash,
            bundle.transaction_index,
            bundle.transaction_json,
            bundle.receipt_json,
            bundle.payload_sha256,
            generation,
        ),
    )


def _validate_bundle_against_witnesses(
    bundle: CandidateBundle,
    witnesses: tuple[DiscoveryWitness, ...],
) -> None:
    transaction = _require_json_mapping(
        _parse_canonical_json(bundle.transaction_json, "transaction JSON"),
        "transaction JSON",
    )
    receipt = _require_json_mapping(
        _parse_canonical_json(bundle.receipt_json, "receipt JSON"),
        "receipt JSON",
    )
    expected = {
        "blockHash": bundle.block_hash,
        "blockNumber": str(bundle.block_number),
        "transactionIndex": str(bundle.transaction_index),
    }
    for label, payload in (("transaction", transaction), ("receipt", receipt)):
        for field, value in expected.items():
            if payload.get(field) != value:
                raise CheckpointContractError(f"candidate {label} location does not match")
    if transaction.get("hash") != bundle.transaction_hash:
        raise CheckpointContractError("candidate transaction hash does not match")
    if receipt.get("transactionHash") != bundle.transaction_hash:
        raise CheckpointContractError("candidate receipt hash does not match")
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise CheckpointContractError("candidate receipt logs are invalid")
    for witness in witnesses:
        if (
            witness.block_number != bundle.block_number
            or witness.block_hash != bundle.block_hash
            or witness.transaction_index != bundle.transaction_index
        ):
            raise CheckpointContractError("candidate witness location does not match")
        if not any(_receipt_log_matches_witness(log, witness) for log in logs):
            raise CheckpointContractError("candidate receipt is missing a discovery witness")


def _validate_ownership_reconciliation(
    bundle: CandidateBundle,
    witnesses: tuple[DiscoveryWitness, ...],
    owners: tuple[OwnershipEvent, ...],
    frozen_token_ids: set[int],
) -> None:
    canonical_owners = tuple(
        sorted(
            owners,
            key=lambda owner: (
                owner.block_number,
                owner.log_index,
                owner.event_order,
                owner.token_id,
            ),
        )
    )
    if owners != canonical_owners:
        raise CheckpointContractError("ownership events are not canonically ordered")
    expected: list[tuple[int, int, int, str | None, str | None]] = []
    for witness in witnesses:
        if (
            witness.transaction_hash != bundle.transaction_hash
            or witness.block_number != bundle.block_number
            or witness.block_hash != bundle.block_hash
            or witness.transaction_index != bundle.transaction_index
        ):
            raise CheckpointContractError("ownership witness does not match its bundle")
        token_id = _transfer_witness_token_id(witness)
        if token_id not in frozen_token_ids:
            raise CheckpointContractError("ownership witness token is not frozen")
        previous_owner, new_owner = _transfer_witness_owners(witness)
        expected.append(
            (
                witness.block_number,
                witness.log_index,
                token_id,
                previous_owner,
                new_owner,
            )
        )
    actual: list[tuple[int, int, int, str | None, str | None]] = []
    identities: set[tuple[int, int, int]] = set()
    for owner in owners:
        _require_nonnegative_int(owner.block_number, "ownership block number")
        _require_nonnegative_int(owner.log_index, "ownership log index")
        _require_nonnegative_int(owner.event_order, "ownership event order")
        _unsigned_decimal(owner.token_id, "ownership token id")
        identity = (owner.block_number, owner.log_index, owner.event_order)
        if identity in identities:
            raise CheckpointContractError("ownership event identity is duplicated")
        identities.add(identity)
        if owner.event_order != 0:
            raise CheckpointContractError("ERC-721 ownership event order must be zero")
        actual.append(
            (
                owner.block_number,
                owner.log_index,
                owner.token_id,
                _optional_address(owner.previous_owner, "previous owner"),
                _optional_address(owner.new_owner, "new owner"),
            )
        )
    if tuple(actual) != tuple(expected):
        raise CheckpointContractError(
            "ownership events do not reconcile exactly to relevant transfer witnesses"
        )


def _receipt_log_matches_witness(log: object, witness: DiscoveryWitness) -> bool:
    if not isinstance(log, dict):
        return False
    topics = log.get("topics")
    return (
        log.get("address") == witness.address
        and log.get("blockHash") == witness.block_hash
        and log.get("data") == witness.data
        and log.get("transactionHash") == witness.transaction_hash
        and log.get("transactionIndex") == str(witness.transaction_index)
        and log.get("logIndex") == str(witness.log_index)
        and isinstance(topics, list)
        and topics == list(witness.topics)
    )


def _require_json_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CheckpointContractError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _bundle_from_row(row: sqlite3.Row) -> CandidateBundle:
    bundle = CandidateBundle(
        transaction_hash=cast(str, row["transaction_hash"]),
        block_number=cast(int, row["block_number"]),
        block_hash=cast(str, row["block_hash"]),
        transaction_index=cast(int, row["transaction_index"]),
        transaction_json=cast(str, row["transaction_json"]),
        receipt_json=cast(str, row["receipt_json"]),
        payload_sha256=cast(str, row["payload_sha256"]),
    )
    _validate_candidate_bundle_shape(bundle)
    return bundle


def _validate_decode_batch(
    bundle: CandidateBundle,
    headers: tuple[BlockHeader, ...],
    resolutions: tuple[PositionResolution, ...],
    state_upserts: tuple[DecoderStateUpsert, ...],
    state_deletes: tuple[int, ...],
    actions: tuple[DecodedLiquidityAction, ...],
) -> None:
    _validate_candidate_bundle_shape(bundle)
    header_numbers: set[int] = set()
    for header in headers:
        _validate_header(header)
        if header.block_number in header_numbers:
            raise CheckpointContractError("decode headers contain a duplicate block")
        header_numbers.add(header.block_number)
    if bundle.block_number not in header_numbers:
        raise CheckpointContractError("decoded transaction header is missing")
    resolution_keys: set[tuple[int, int]] = set()
    for resolution in resolutions:
        _validate_position_resolution(resolution)
        key = (resolution.token_id, resolution.query_block)
        if key in resolution_keys or resolution.query_block not in header_numbers:
            raise CheckpointContractError("position resolution header or identity is invalid")
        resolution_keys.add(key)
    upsert_ids: set[int] = set()
    for upsert in state_upserts:
        _validate_state_upsert(upsert)
        if upsert.token_id in upsert_ids:
            raise CheckpointContractError("decoder state upsert is duplicated")
        upsert_ids.add(upsert.token_id)
    delete_ids = {int(_unsigned_decimal(value, "token id")) for value in state_deletes}
    if len(delete_ids) != len(state_deletes) or upsert_ids & delete_ids:
        raise CheckpointContractError("decoder state changes are duplicated or crossed")
    action_keys: set[tuple[int, int, int]] = set()
    for action in actions:
        if (
            action.block_number != bundle.block_number
            or action.tx_hash.lower() != bundle.transaction_hash
        ):
            raise CheckpointContractError("decoded action does not match its transaction")
        action_key = (action.block_number, action.log_index, action.event_order)
        if action_key in action_keys:
            raise CheckpointContractError("decoded action identity is duplicated")
        action_keys.add(action_key)
        _action_json(action)
    final_action_by_token: dict[int, DecodedLiquidityAction] = {}
    for action in sorted(
        actions,
        key=lambda value: (value.block_number, value.log_index, value.event_order),
    ):
        final_action_by_token[action.token_id] = action
    for upsert in state_upserts:
        final_action = final_action_by_token.get(upsert.token_id)
        if final_action is None or (
            upsert.last_block_number,
            upsert.last_log_index,
            upsert.last_event_order,
        ) != (
            final_action.block_number,
            final_action.log_index,
            final_action.event_order,
        ):
            raise CheckpointContractError(
                "decoder state upsert does not match the token's final action"
            )
    for token_id in delete_ids:
        final_action = final_action_by_token.get(token_id)
        has_burn_action = final_action is not None and final_action.action_type in (
            "burn",
            "burn_collect",
        )
        if not has_burn_action:
            raise CheckpointContractError("decoder state delete has no current burn evidence")


def _validate_position_key_mappings(
    mappings: tuple[PositionKeyMapping, ...],
    actions: tuple[DecodedLiquidityAction, ...],
    existing_token_ids: set[int],
) -> None:
    mint_actions = tuple(
        action
        for action in actions
        if action.action_type == "mint"
    )
    mint_actions_by_location = {
        (action.block_number, action.log_index, action.event_order): action
        for action in mint_actions
    }
    if len(mint_actions_by_location) != len(mint_actions):
        raise CheckpointContractError("mint action identity is duplicated")
    first_mint_by_token: dict[int, DecodedLiquidityAction] = {}
    for action in sorted(
        mint_actions,
        key=lambda value: (value.block_number, value.log_index, value.event_order),
    ):
        first_mint_by_token.setdefault(action.token_id, action)
    seen_tokens: set[int] = set()
    seen_locations: set[tuple[int, int, int]] = set()
    for mapping in mappings:
        _validate_position_key_shape(mapping)
        location = _position_key_location(mapping)
        action = mint_actions_by_location.get(location)
        if action is None or (
            action.token_id != mapping.token_id
            or action.pool_id != mapping.pool_id
            or action.tick_lower != mapping.tick_lower
            or action.tick_upper != mapping.tick_upper
        ):
            raise CheckpointContractError("position key does not match a mint action")
        if (
            mapping.token_id in existing_token_ids
            or mapping.token_id in seen_tokens
            or location in seen_locations
        ):
            raise CheckpointContractError("position key mapping is duplicated")
        first_action = first_mint_by_token[mapping.token_id]
        first_location = (
            first_action.block_number,
            first_action.log_index,
            first_action.event_order,
        )
        if location != first_location:
            raise CheckpointContractError(
                "position key does not anchor the token's first mint action"
            )
        seen_tokens.add(mapping.token_id)
        seen_locations.add(location)
    new_mint_tokens = set(first_mint_by_token).difference(existing_token_ids)
    if seen_tokens != new_mint_tokens:
        raise CheckpointContractError("new mint token is missing a position key")


def _validate_position_key_shape(mapping: PositionKeyMapping) -> None:
    _unsigned_decimal(mapping.token_id, "token id")
    _require_lower_hex(mapping.pool_id, 32, "pool id")
    _require_signed_sqlite_int(mapping.tick_lower, "position lower tick")
    _require_signed_sqlite_int(mapping.tick_upper, "position upper tick")
    if mapping.tick_lower >= mapping.tick_upper:
        raise CheckpointContractError("position key tick range is invalid")
    _require_lower_hex(mapping.salt, 32, "position salt")
    _position_key_location(mapping)


def _position_key_location(mapping: PositionKeyMapping) -> tuple[int, int, int]:
    return (
        _require_nonnegative_int(mapping.mint_block_number, "mint block number"),
        _require_nonnegative_int(mapping.mint_log_index, "mint log index"),
        _require_nonnegative_int(mapping.mint_event_order, "mint event order"),
    )


def _position_key_from_row(row: sqlite3.Row) -> PositionKeyMapping:
    mapping = PositionKeyMapping(
        token_id=_parse_unsigned_decimal(cast(str, row["token_id"]), "token id"),
        pool_id=cast(str, row["pool_id"]),
        tick_lower=cast(int, row["tick_lower"]),
        tick_upper=cast(int, row["tick_upper"]),
        salt=cast(str, row["salt"]),
        mint_block_number=cast(int, row["mint_block_number"]),
        mint_log_index=cast(int, row["mint_log_index"]),
        mint_event_order=cast(int, row["mint_event_order"]),
    )
    _validate_position_key_shape(mapping)
    return mapping


def _token_ids_sha256(token_ids: tuple[int, ...]) -> str:
    if token_ids != tuple(sorted(set(token_ids))):
        raise CheckpointContractError("token IDs are not canonical")
    payload = _canonical_json([_unsigned_decimal(token_id, "token id") for token_id in token_ids])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_header(header: BlockHeader) -> None:
    _require_nonnegative_int(header.block_number, "block number")
    _require_lower_hex(header.block_hash, 32, "block hash")
    _require_nonnegative_int(header.timestamp_ms, "block timestamp")


def _validate_position_resolution(resolution: PositionResolution) -> None:
    _unsigned_decimal(resolution.token_id, "token id")
    _require_nonnegative_int(resolution.query_block, "query block")
    if type(resolution.found) is not bool or resolution.found != (resolution.state is not None):
        raise CheckpointContractError("position resolution found flag is inconsistent")
    if resolution.state is not None:
        _validate_position_state(resolution.state)


def _validate_position_state(state: LedgerPositionState) -> None:
    _require_lower_hex(state.pool_id, 32, "pool id")
    _require_signed_sqlite_int(state.tick_lower, "position lower tick")
    _require_signed_sqlite_int(state.tick_upper, "position upper tick")
    _unsigned_decimal(state.liquidity_after, "liquidity")


def _validate_state_upsert(upsert: DecoderStateUpsert) -> None:
    _unsigned_decimal(upsert.token_id, "token id")
    _validate_position_state(upsert.state)
    _require_nonnegative_int(upsert.last_block_number, "last block number")
    _require_nonnegative_int(upsert.last_log_index, "last log index")
    _require_nonnegative_int(upsert.last_event_order, "last event order")


def _require_modify_action_multiplicity(
    required_log_indices: Sequence[int],
    action_log_indices: Sequence[int],
) -> None:
    required = tuple(sorted(required_log_indices))
    required_set = frozenset(required)
    represented = tuple(
        sorted(log_index for log_index in action_log_indices if log_index in required_set)
    )
    if represented != required:
        raise CheckpointContractError("each ModifyLiquidity witness must map to exactly one action")


def _validate_decoder_state_transition(
    connection: sqlite3.Connection,
    resolutions: Sequence[PositionResolution],
    state_upserts: Sequence[DecoderStateUpsert],
    state_deletes: Sequence[int],
    actions: Sequence[DecodedLiquidityAction],
) -> None:
    prior_states = {
        _parse_unsigned_decimal(cast(str, row["token_id"]), "token id"): (
            LedgerPositionState(
                pool_id=_require_lower_hex(cast(str, row["pool_id"]), 32, "pool id"),
                tick_lower=cast(int, row["tick_lower"]),
                tick_upper=cast(int, row["tick_upper"]),
                liquidity_after=_parse_unsigned_decimal(
                    cast(str, row["liquidity_after"]),
                    "liquidity",
                ),
            )
        )
        for row in connection.execute(
            """
            SELECT token_id, pool_id, tick_lower, tick_upper, liquidity_after
            FROM decoder_token_state
            """
        )
    }
    resolved_states: dict[int, LedgerPositionState] = {}
    for resolution in sorted(resolutions, key=lambda value: value.query_block):
        if resolution.state is not None:
            resolved_states[resolution.token_id] = resolution.state
    actions_by_token: dict[int, list[DecodedLiquidityAction]] = {}
    for action in sorted(
        actions,
        key=lambda value: (value.block_number, value.log_index, value.event_order),
    ):
        actions_by_token.setdefault(action.token_id, []).append(action)
    upsert_by_token = {upsert.token_id: upsert for upsert in state_upserts}
    delete_ids = set(state_deletes)
    touched_ids = set(actions_by_token) | set(upsert_by_token) | delete_ids

    for token_id in touched_ids:
        prior = prior_states.get(token_id)
        state = prior if prior is not None else resolved_states.get(token_id)
        for action in actions_by_token.get(token_id, []):
            state = _apply_action_to_decoder_state(state, action)

        if token_id in delete_ids:
            if state is None or state.liquidity_after != 0:
                raise CheckpointContractError("decoder state delete does not end at zero liquidity")
            if not any(
                action.action_type in ("burn", "burn_collect")
                for action in actions_by_token.get(token_id, [])
            ):
                raise CheckpointContractError("decoder state delete has no burn evidence")
            continue

        upsert = upsert_by_token.get(token_id)
        if upsert is not None:
            if state != upsert.state:
                raise CheckpointContractError(
                    "decoder state upsert does not match action-derived state"
                )
            continue

        if state != prior:
            raise CheckpointContractError("decoder state transition was not persisted")


def _apply_action_to_decoder_state(
    state: LedgerPositionState | None,
    action: DecodedLiquidityAction,
) -> LedgerPositionState | None:
    if action.action_type == "collect":
        if action.liquidity_delta != 0:
            raise CheckpointContractError("collect action changes decoder liquidity")
        if state is not None:
            _require_action_matches_position(action, state)
        return state
    if action.action_type not in ("mint", "burn", "burn_collect"):
        raise CheckpointContractError("decoded action type cannot update decoder state")
    if action.action_type == "mint" and action.liquidity_delta < 0:
        raise CheckpointContractError("mint action has negative liquidity")
    if action.action_type in ("burn", "burn_collect") and action.liquidity_delta > 0:
        raise CheckpointContractError("burn action has positive liquidity")
    if state is None:
        if action.action_type != "mint" or action.tick_lower is None or action.tick_upper is None:
            raise CheckpointContractError("decoded action has no prior position state")
        state = LedgerPositionState(
            pool_id=action.pool_id.lower(),
            tick_lower=action.tick_lower,
            tick_upper=action.tick_upper,
            liquidity_after=0,
        )
    else:
        _require_action_matches_position(action, state)
    next_liquidity = state.liquidity_after + action.liquidity_delta
    if next_liquidity < 0:
        raise CheckpointContractError("decoded action exceeds position liquidity")
    return LedgerPositionState(
        pool_id=state.pool_id,
        tick_lower=state.tick_lower,
        tick_upper=state.tick_upper,
        liquidity_after=next_liquidity,
    )


def _require_action_matches_position(
    action: DecodedLiquidityAction,
    state: LedgerPositionState,
) -> None:
    if (
        action.pool_id.lower() != state.pool_id
        or action.tick_lower is not None
        and action.tick_lower != state.tick_lower
        or action.tick_upper is not None
        and action.tick_upper != state.tick_upper
    ):
        raise CheckpointContractError("decoded action does not match its position state")


def _insert_position_resolution(
    connection: sqlite3.Connection,
    resolution: PositionResolution,
    generation: int,
) -> None:
    row = connection.execute(
        "SELECT block_hash, timestamp_ms FROM chain_blocks WHERE block_number = ?",
        (resolution.query_block,),
    ).fetchone()
    if row is None or row["timestamp_ms"] is None:
        raise CheckpointContractError("position resolution block header is missing")
    state = resolution.state
    connection.execute(
        """
        INSERT INTO position_resolutions (
            token_id, query_block, query_block_hash, found, pool_id,
            tick_lower, tick_upper, liquidity_after, committed_generation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _unsigned_decimal(resolution.token_id, "token id"),
            resolution.query_block,
            row["block_hash"],
            int(resolution.found),
            None if state is None else _require_lower_hex(state.pool_id, 32, "pool id"),
            None if state is None else state.tick_lower,
            None if state is None else state.tick_upper,
            None if state is None else _unsigned_decimal(state.liquidity_after, "liquidity"),
            generation,
        ),
    )


def _action_json(action: DecodedLiquidityAction) -> str:
    if not action.action_type:
        raise CheckpointContractError("decoded action type is missing")
    _require_nonnegative_int(action.block_number, "action block number")
    _require_nonnegative_int(action.log_index, "action log index")
    _require_nonnegative_int(action.event_order, "action event order")
    tx_hash = _require_lower_hex(action.tx_hash.lower(), 32, "action transaction hash")
    pool_id = _require_lower_hex(action.pool_id.lower(), 32, "action pool id")
    position_manager = _require_lower_hex(
        action.position_manager.lower(), 20, "action position manager"
    )
    if action.tick_lower is not None:
        _require_signed_sqlite_int(action.tick_lower, "action lower tick")
    if action.tick_upper is not None:
        _require_signed_sqlite_int(action.tick_upper, "action upper tick")
    return _canonical_json(
        {
            "action_type": action.action_type,
            "amount0": _decimal_text(action.amount0, "amount0"),
            "amount0_actual": _optional_decimal_text(action.amount0_actual, "amount0 actual"),
            "amount0_attribution_source": action.amount0_attribution_source,
            "amount0_raw": _canonical_unsigned_text(action.amount0_raw, "amount0 raw"),
            "amount1": _decimal_text(action.amount1, "amount1"),
            "amount1_actual": _optional_decimal_text(action.amount1_actual, "amount1 actual"),
            "amount1_attribution_source": action.amount1_attribution_source,
            "amount1_raw": _canonical_unsigned_text(action.amount1_raw, "amount1 raw"),
            "amount_attribution_status": action.amount_attribution_status,
            "block_number": str(action.block_number),
            "block_time": action.block_time,
            "chain": action.chain,
            "collect_amount0": _decimal_text(action.collect_amount0, "collect amount0"),
            "collect_amount1": _decimal_text(action.collect_amount1, "collect amount1"),
            "event_order": str(action.event_order),
            "liquidity_delta": _signed_decimal(action.liquidity_delta, "liquidity delta"),
            "log_index": str(action.log_index),
            "lp_owner": _optional_address(action.lp_owner, "LP owner"),
            "pool_id": pool_id,
            "position_manager": position_manager,
            "tick_lower": None if action.tick_lower is None else str(action.tick_lower),
            "tick_upper": None if action.tick_upper is None else str(action.tick_upper),
            "timestamp_ms": (
                None
                if action.timestamp_ms is None
                else str(_require_nonnegative_int(action.timestamp_ms, "timestamp"))
            ),
            "token_id": _unsigned_decimal(action.token_id, "token id"),
            "tx_hash": tx_hash,
        }
    )


def _action_from_json(action_json: str) -> DecodedLiquidityAction:
    payload = _require_json_mapping(
        _parse_canonical_json(action_json, "decoded action JSON"),
        "decoded action JSON",
    )
    expected_fields = {
        "action_type",
        "amount0",
        "amount0_actual",
        "amount0_attribution_source",
        "amount0_raw",
        "amount1",
        "amount1_actual",
        "amount1_attribution_source",
        "amount1_raw",
        "amount_attribution_status",
        "block_number",
        "block_time",
        "chain",
        "collect_amount0",
        "collect_amount1",
        "event_order",
        "liquidity_delta",
        "log_index",
        "lp_owner",
        "pool_id",
        "position_manager",
        "tick_lower",
        "tick_upper",
        "timestamp_ms",
        "token_id",
        "tx_hash",
    }
    if set(payload) != expected_fields:
        raise CheckpointContractError("decoded action JSON fields are invalid")
    lp_owner_value = payload["lp_owner"]
    if lp_owner_value is not None and not isinstance(lp_owner_value, str):
        raise CheckpointContractError("decoded action LP owner is invalid")
    return DecodedLiquidityAction(
        action_type=_required_string(payload, "action_type"),
        block_number=_parse_unsigned_decimal(
            _required_string(payload, "block_number"), "action block number"
        ),
        log_index=_parse_unsigned_decimal(
            _required_string(payload, "log_index"), "action log index"
        ),
        event_order=_parse_unsigned_decimal(
            _required_string(payload, "event_order"), "action event order"
        ),
        token_id=_parse_unsigned_decimal(_required_string(payload, "token_id"), "token id"),
        lp_owner=(
            None if lp_owner_value is None else _semantic_address(lp_owner_value, "LP owner")
        ),
        tick_lower=_optional_signed_payload_int(payload, "tick_lower"),
        tick_upper=_optional_signed_payload_int(payload, "tick_upper"),
        liquidity_delta=_parse_signed_decimal(
            _required_string(payload, "liquidity_delta"), "liquidity delta"
        ),
        amount0=_payload_decimal(payload, "amount0"),
        amount1=_payload_decimal(payload, "amount1"),
        collect_amount0=_payload_decimal(payload, "collect_amount0"),
        collect_amount1=_payload_decimal(payload, "collect_amount1"),
        chain=_required_string(payload, "chain", allow_empty=True),
        pool_id=_require_lower_hex(_required_string(payload, "pool_id"), 32, "action pool id"),
        block_time=_required_string(payload, "block_time", allow_empty=True),
        tx_hash=_require_lower_hex(
            _required_string(payload, "tx_hash"), 32, "action transaction hash"
        ),
        position_manager=_require_lower_hex(
            _required_string(payload, "position_manager"), 20, "action position manager"
        ),
        amount0_raw=_canonical_unsigned_text(
            _required_string(payload, "amount0_raw"), "amount0 raw"
        ),
        amount1_raw=_canonical_unsigned_text(
            _required_string(payload, "amount1_raw"), "amount1 raw"
        ),
        timestamp_ms=_optional_unsigned_payload_int(payload, "timestamp_ms"),
        amount0_actual=_optional_payload_decimal(payload, "amount0_actual"),
        amount1_actual=_optional_payload_decimal(payload, "amount1_actual"),
        amount0_attribution_source=_required_string(
            payload, "amount0_attribution_source", allow_empty=True
        ),
        amount1_attribution_source=_required_string(
            payload, "amount1_attribution_source", allow_empty=True
        ),
        amount_attribution_status=_required_string(
            payload, "amount_attribution_status", allow_empty=True
        ),
    )


def _required_string(
    payload: dict[str, object],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not allow_empty and not value:
        raise CheckpointContractError(f"decoded action {key} is invalid")
    return value


def _payload_decimal(payload: dict[str, object], key: str) -> Decimal:
    value = _required_string(payload, key)
    canonical = _decimal_text(value, key)
    if canonical != value:
        raise CheckpointContractError(f"decoded action {key} is not canonical")
    return Decimal(value)


def _optional_payload_decimal(
    payload: dict[str, object],
    key: str,
) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CheckpointContractError(f"decoded action {key} is invalid")
    canonical = _decimal_text(value, key)
    if canonical != value:
        raise CheckpointContractError(f"decoded action {key} is not canonical")
    return Decimal(value)


def _optional_signed_payload_int(
    payload: dict[str, object],
    key: str,
) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CheckpointContractError(f"decoded action {key} is invalid")
    return _require_signed_sqlite_int(_parse_signed_decimal(value, key), key)


def _optional_unsigned_payload_int(
    payload: dict[str, object],
    key: str,
) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CheckpointContractError(f"decoded action {key} is invalid")
    return _parse_unsigned_decimal(value, key)


def _validate_action_price_bindings(
    bindings: tuple[ActionPriceBinding, ...],
) -> None:
    canonical = tuple(
        sorted(
            bindings,
            key=lambda binding: (
                binding.block_number,
                binding.log_index,
                binding.event_order,
            ),
        )
    )
    if bindings != canonical:
        raise CheckpointContractError("action price bindings are not canonically ordered")
    keys: set[tuple[int, int, int]] = set()
    for binding in bindings:
        _require_nonnegative_int(binding.block_number, "binding block number")
        _require_nonnegative_int(binding.log_index, "binding log index")
        _require_nonnegative_int(binding.event_order, "binding event order")
        _unsigned_decimal(binding.event_time_sqrt_price_x96, "event-time sqrt price")
        if binding.event_time_sqrt_price_x96 == 0:
            raise CheckpointContractError("event-time sqrt price must be positive")
        _require_signed_sqlite_int(binding.event_time_tick, "event-time tick")
        if binding.event_time_state_source not in (
            "self_event",
            "prior_event",
            "prior_block",
            "same_block_prior_event",
        ):
            raise CheckpointContractError("event-time state source is invalid")
        key = (binding.block_number, binding.log_index, binding.event_order)
        if key in keys:
            raise CheckpointContractError("action price binding identity is duplicated")
        keys.add(key)


def _binding_from_row(row: sqlite3.Row) -> ActionPriceBinding:
    source = cast(str, row["event_time_state_source"])
    if source not in (
        "self_event",
        "prior_event",
        "prior_block",
        "same_block_prior_event",
    ):
        raise CheckpointContractError("persisted event-time state source is invalid")
    return ActionPriceBinding(
        block_number=cast(int, row["block_number"]),
        log_index=cast(int, row["log_index"]),
        event_order=cast(int, row["event_order"]),
        event_time_sqrt_price_x96=_parse_unsigned_decimal(
            cast(str, row["event_time_sqrt_price_x96"]),
            "event-time sqrt price",
        ),
        event_time_tick=cast(int, row["event_time_tick"]),
        event_time_state_source=cast(EventTimeStateSource, source),
    )


def _decoder_state_sha256(connection: sqlite3.Connection) -> str:
    payload = [
        {
            "last_block_number": str(row["last_block_number"]),
            "last_event_order": str(row["last_event_order"]),
            "last_log_index": str(row["last_log_index"]),
            "liquidity_after": cast(str, row["liquidity_after"]),
            "pool_id": cast(str, row["pool_id"]),
            "tick_lower": str(row["tick_lower"]),
            "tick_upper": str(row["tick_upper"]),
            "token_id": cast(str, row["token_id"]),
        }
        for row in connection.execute(
            """
            SELECT token_id, pool_id, tick_lower, tick_upper, liquidity_after,
                   last_block_number, last_log_index, last_event_order
            FROM decoder_token_state
            ORDER BY length(token_id), token_id
            """
        )
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _unsigned_decimal(value: object, label: str) -> str:
    if type(value) is not int or value < 0:
        raise CheckpointContractError(f"{label} is not a nonnegative integer")
    return str(value)


def _parse_unsigned_decimal(value: str, label: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", value, flags=re.ASCII):
        raise CheckpointContractError(f"{label} is not canonical unsigned decimal text")
    return int(value)


def _canonical_unsigned_text(value: object, label: str) -> str:
    if type(value) is int:
        if value < 0:
            raise CheckpointContractError(f"{label} is not canonical unsigned decimal text")
        return str(value)
    if not isinstance(value, str):
        raise CheckpointContractError(f"{label} is not canonical unsigned decimal text")
    _parse_unsigned_decimal(value, label)
    return value


def _signed_decimal(value: object, label: str) -> str:
    if type(value) is int:
        return str(value)
    if not isinstance(value, str) or not re.fullmatch(r"0|-?[1-9][0-9]*", value, flags=re.ASCII):
        raise CheckpointContractError(f"{label} is not canonical signed decimal text")
    return value


def _parse_signed_decimal(value: str, label: str) -> int:
    canonical = _signed_decimal(value, label)
    return int(canonical)


def _decimal_text(value: object, label: str) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise CheckpointContractError(f"{label} is not an exact decimal")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CheckpointContractError(f"{label} is not an exact decimal") from exc
    if not decimal_value.is_finite() or decimal_value.is_zero() and decimal_value.is_signed():
        raise CheckpointContractError(f"{label} is not a finite canonical decimal")
    return format(decimal_value, "f")


def _optional_decimal_text(value: object | None, label: str) -> str | None:
    return None if value is None else _decimal_text(value, label)


def _optional_address(value: str | None, label: str) -> str | None:
    return None if value is None else _require_lower_hex(value.lower(), 20, label)


def _semantic_address(value: str, label: str) -> str:
    return cast(str, Web3.to_checksum_address(_require_lower_hex(value, 20, label)))


def _require_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _SQLITE_INT_MAX:
        raise CheckpointContractError(f"{label} is not a nonnegative signed-64 SQLite integer")
    return value


def _require_signed_sqlite_int(value: object, label: str) -> int:
    if type(value) is not int or not _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX:
        raise CheckpointContractError(f"{label} is not a signed-64 SQLite integer")
    return value


def _validate_identity(identity: RunIdentity) -> None:
    if identity.schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointContractError("checkpoint identity schema version is invalid")
    for label, value in (
        ("exporter version", identity.exporter_version),
        ("pool", identity.pool),
        ("chain", identity.chain),
        ("Python version", identity.python_version),
        ("Web3 version", identity.web3_version),
    ):
        if _SAFE_LABEL.fullmatch(value) is None:
            raise CheckpointContractError(f"checkpoint identity {label} is invalid")
    if identity.verification_mode != "rpc_verified":
        raise CheckpointContractError("checkpoint identity verification mode is invalid")
    if identity.chain_id <= 0:
        raise CheckpointContractError("checkpoint identity chain ID must be positive")
    for label, value in (
        ("pool ID", identity.pool_id),
        ("endpoint start hash", identity.endpoint.start.block_hash),
        ("endpoint end hash", identity.endpoint.end.block_hash),
        ("action topic", identity.action_topic),
        ("transfer topic", identity.transfer_topic),
    ):
        _require_lower_hex(value, byte_length=32, label=label)
    for label, value in (
        ("PoolManager", identity.pool_manager),
        ("PositionManager", identity.position_manager),
        ("wrapper EntryPoint", identity.wrapper_entrypoint),
        ("token0", identity.token0_address),
        ("token1", identity.token1_address),
    ):
        _require_lower_hex(value, byte_length=20, label=label)
    if not 0 <= identity.token0_decimals <= 255:
        raise CheckpointContractError("checkpoint identity token0 decimals are invalid")
    if not 0 <= identity.token1_decimals <= 255:
        raise CheckpointContractError("checkpoint identity token1 decimals are invalid")
    _require_nonnegative_int(identity.start_block, "identity start block")
    _require_nonnegative_int(identity.end_block, "identity end block")
    if identity.end_block < identity.start_block:
        raise CheckpointContractError("checkpoint identity block range is invalid")
    _require_nonnegative_int(identity.chunk_size, "identity chunk size")
    if identity.chunk_size == 0:
        raise CheckpointContractError("checkpoint identity chunk size must be positive")
    if identity.endpoint.start.block_number != identity.start_block:
        raise CheckpointContractError("checkpoint identity endpoint start block is invalid")
    if identity.endpoint.end.block_number != identity.end_block:
        raise CheckpointContractError("checkpoint identity endpoint end block is invalid")
    _validate_header(identity.endpoint.start)
    _validate_header(identity.endpoint.end)
    _validate_replay_input_evidence(identity.replay_input)
    if (
        identity.replay_input.chain != identity.chain
        or identity.replay_input.pool_id != identity.pool_id
    ):
        raise CheckpointContractError("checkpoint replay input pool identity is invalid")
    if (
        identity.replay_input.first_block != identity.start_block
        or identity.replay_input.last_block != identity.end_block
    ):
        raise CheckpointContractError("checkpoint range must equal replay input range")
    if (
        identity.replay_input.first_timestamp_ms != identity.endpoint.start.timestamp_ms
        or identity.replay_input.last_timestamp_ms != identity.endpoint.end.timestamp_ms
    ):
        raise CheckpointContractError("checkpoint replay input timestamps are invalid")
    if identity.output_path != os.path.abspath(identity.output_path):
        raise CheckpointContractError("checkpoint identity output path is not normalized")
    try:
        fee_rate = Decimal(identity.fee_rate)
    except InvalidOperation as exc:
        raise CheckpointContractError("checkpoint identity fee rate is invalid") from exc
    if (
        not fee_rate.is_finite()
        or fee_rate < 0
        or format(fee_rate, "f") != identity.fee_rate
        or identity.fee_rate == "-0"
    ):
        raise CheckpointContractError("checkpoint identity fee rate is not canonical")
    if not identity.source_sha256:
        raise CheckpointContractError("checkpoint identity source digests are empty")
    source_names = tuple(name for name, _digest in identity.source_sha256)
    if source_names != CHECKPOINT_SOURCE_PATHS:
        raise CheckpointContractError("checkpoint identity source manifest is invalid")
    for source_name, digest in identity.source_sha256:
        if not source_name:
            raise CheckpointContractError("checkpoint identity source name is empty")
        _require_lower_hex(
            digest,
            byte_length=32,
            label=f"source digest {source_name}",
            prefix=False,
        )


def _validate_action_decode_identity(identity: ActionDecodeIdentity) -> None:
    for label, value in (("pool", identity.pool), ("chain", identity.chain)):
        if _SAFE_LABEL.fullmatch(value) is None:
            raise CheckpointContractError(f"action decode {label} is invalid")
    _require_lower_hex(identity.pool_id, 32, "action decode pool ID")
    for label, value in (
        ("PoolManager", identity.pool_manager),
        ("PositionManager", identity.position_manager),
        ("wrapper EntryPoint", identity.wrapper_entrypoint),
    ):
        _require_lower_hex(value, 20, f"action decode {label}")


def _validate_replay_input_evidence(evidence: ReplayInputEvidence) -> None:
    if evidence.path != os.path.abspath(evidence.path):
        raise CheckpointContractError("replay input path is not normalized")
    for label, digest in (
        ("replay input digest", evidence.sha256),
        ("replay header digest", evidence.header_sha256),
        ("price semantics digest", evidence.price_semantics_sha256),
        ("price events digest", evidence.price_events_sha256),
    ):
        _require_lower_hex(digest, 32, label, prefix=False)
    if _SAFE_LABEL.fullmatch(evidence.parser_version) is None:
        raise CheckpointContractError("replay parser version is invalid")
    if _SAFE_LABEL.fullmatch(evidence.chain) is None:
        raise CheckpointContractError("replay input chain is invalid")
    _require_lower_hex(evidence.pool_id, 32, "replay input pool ID")
    for label, value in (
        ("replay input byte length", evidence.byte_length),
        ("replay input row count", evidence.row_count),
        ("replay input first block", evidence.first_block),
        ("replay input last block", evidence.last_block),
        ("replay input first timestamp", evidence.first_timestamp_ms),
        ("replay input last timestamp", evidence.last_timestamp_ms),
        ("replay price event count", evidence.price_event_count),
    ):
        _require_nonnegative_int(value, label)
    if evidence.byte_length == 0 or evidence.row_count == 0:
        raise CheckpointContractError("replay input cannot be empty")
    if evidence.last_block < evidence.first_block:
        raise CheckpointContractError("replay input block range is invalid")
    if evidence.last_timestamp_ms < evidence.first_timestamp_ms:
        raise CheckpointContractError("replay input timestamp range is invalid")
    if evidence.price_event_count == 0 or evidence.price_event_count > evidence.row_count:
        raise CheckpointContractError("replay price event count is invalid")


def _replay_input_from_row(row: sqlite3.Row) -> ReplayInputEvidence:
    evidence = ReplayInputEvidence(
        path=cast(str, row["path"]),
        sha256=cast(str, row["sha256"]),
        byte_length=cast(int, row["byte_length"]),
        row_count=cast(int, row["row_count"]),
        header_sha256=cast(str, row["header_sha256"]),
        parser_version=cast(str, row["parser_version"]),
        price_semantics_sha256=cast(str, row["price_semantics_sha256"]),
        chain=cast(str, row["chain"]),
        pool_id=cast(str, row["pool_id"]),
        first_block=cast(int, row["first_block"]),
        last_block=cast(int, row["last_block"]),
        first_timestamp_ms=cast(int, row["first_timestamp_ms"]),
        last_timestamp_ms=cast(int, row["last_timestamp_ms"]),
        price_event_count=cast(int, row["price_event_count"]),
        price_events_sha256=cast(str, row["price_events_sha256"]),
    )
    _validate_replay_input_evidence(evidence)
    return evidence


def _validate_paths_match_identity(paths: CheckpointPaths, identity: RunIdentity) -> None:
    _validate_identity(identity)
    if identity.output_path != os.fspath(paths.output):
        raise CheckpointContractError("checkpoint identity output path does not match")


def _require_lower_hex(
    value: str,
    byte_length: int,
    label: str,
    prefix: bool = True,
) -> str:
    expected_length = byte_length * 2 + (2 if prefix else 0)
    if len(value) != expected_length or value != value.lower():
        raise CheckpointContractError(f"checkpoint identity {label} is not canonical hex")
    payload = value[2:] if prefix else value
    if prefix and not value.startswith("0x"):
        raise CheckpointContractError(f"checkpoint identity {label} is not canonical hex")
    try:
        bytes.fromhex(payload)
    except ValueError as exc:
        raise CheckpointContractError(f"checkpoint identity {label} is not canonical hex") from exc
    return value


def _ensure_secure_parent(path: Path) -> None:
    _walk_parent_components(path, create=True)


def _validate_existing_parent(path: Path) -> None:
    _walk_parent_components(path, create=False)


def _walk_parent_components(path: Path, *, create: bool) -> None:
    if not path.is_absolute():
        raise CheckpointContractError("checkpoint path is not absolute")
    current = Path(path.anchor)
    for component in path.parent.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if not create:
                raise CheckpointContractError("checkpoint parent directory does not exist")
            try:
                os.mkdir(current, mode=0o700)
            except FileExistsError:
                pass
            metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode):
            raise CheckpointContractError("checkpoint path contains a symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise CheckpointContractError("checkpoint parent component is not a directory")


def _validate_regular_leaf(path: Path, *, allow_missing: bool) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if allow_missing:
            return False
        raise CheckpointContractError("checkpoint path does not exist")
    if stat.S_ISLNK(metadata.st_mode):
        raise CheckpointContractError("checkpoint path is a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise CheckpointContractError("checkpoint path is not a regular file")
    return True


def _open_regular_leaf(path: Path, flags: int, *, mode: int) -> int:
    _validate_regular_leaf(path, allow_missing=True)
    secure_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, secure_flags, mode)
    except OSError as exc:
        raise CheckpointContractError("checkpoint path could not be opened safely") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CheckpointContractError("checkpoint path is not a regular file")
    return descriptor


def _require_regular_mode(path: Path, expected_mode: int) -> None:
    if not _validate_regular_leaf(path, allow_missing=False):
        raise AssertionError("unreachable")
    actual_mode = stat.S_IMODE(os.lstat(path).st_mode)
    if actual_mode != expected_mode:
        raise CheckpointContractError("checkpoint file permissions are invalid")


def _coverage_path(output: Path) -> Path:
    return Path(f"{output}.coverage.json")


def _ensure_checkpoint_namespace_secure(paths: CheckpointPaths) -> None:
    _ensure_secure_parent(paths.database)
    for path in (
        paths.output,
        _coverage_path(paths.output),
        paths.database,
        paths.wal,
        paths.shm,
        paths.progress,
        paths.run_lock,
        paths.publish_lock,
    ):
        _validate_regular_leaf(path, allow_missing=True)


def _remove_fresh_namespace(paths: CheckpointPaths) -> None:
    removed = False
    for path in (paths.database, paths.wal, paths.shm, paths.progress):
        if _validate_regular_leaf(path, allow_missing=True):
            os.unlink(path)
            removed = True
    if removed:
        _fsync_directory(paths.database.parent)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _connect_read_write(paths: CheckpointPaths) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        _require_sqlite_namespace(paths)
        connection = sqlite3.connect(paths.database, isolation_level=None, timeout=5.0)
        connection.row_factory = sqlite3.Row
        journal_mode = cast(
            str,
            connection.execute("PRAGMA journal_mode = WAL").fetchone()[0],
        )
        if journal_mode.lower() != "wal":
            raise CheckpointContractError("checkpoint database did not enter WAL mode")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA busy_timeout = 5000")
        _require_sqlite_namespace(paths)
        return connection
    except CheckpointContractError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.DatabaseError as exc:
        if connection is not None:
            connection.close()
        raise CheckpointContractError("checkpoint database could not be opened") from exc


def _require_sqlite_namespace(paths: CheckpointPaths) -> None:
    _validate_existing_parent(paths.database)
    _require_regular_mode(paths.database, 0o600)
    for sibling in (paths.wal, paths.shm):
        if _validate_regular_leaf(sibling, allow_missing=True):
            _require_regular_mode(sibling, 0o600)


def _initialize_checkpoint(
    connection: sqlite3.Connection,
    identity: RunIdentity,
) -> None:
    now = _utc_now()
    run_id = str(uuid.uuid4())
    chunks = _block_ranges(identity.start_block, identity.end_block, identity.chunk_size)
    phase_units = {
        "preflight": ("checks", 1),
        "action_discovery": ("chunks", len(chunks)),
        "action_fetch": ("transactions", None),
        "action_decode": ("transactions", None),
        "token_set_freeze": ("token_ids", None),
        "full_transfer_scan": ("chunks", len(chunks)),
        "relevant_transfer_fetch": ("transactions", None),
        "relevant_transfer_decode": ("transactions", None),
        "replay_input_bind": ("input", 1),
        "price_replay": ("actions", None),
        "build": ("rows", None),
        "publish": ("files", 2),
        "succeeded": ("output", 1),
    }
    try:
        connection.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA_SQL}")
        connection.execute(f"PRAGMA application_id = {CHECKPOINT_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {CHECKPOINT_SCHEMA_VERSION}")
        connection.execute(
            "INSERT INTO schema_meta VALUES (1, ?, ?)",
            (CHECKPOINT_SCHEMA_VERSION, now),
        )
        connection.execute(
            "INSERT INTO run_identity VALUES (1, ?, ?)",
            (identity.sha256(), identity.canonical_bytes().decode("utf-8")),
        )
        connection.execute(
            """
            INSERT INTO run_state (
                singleton, run_id, attempt_number, generation, status, phase,
                started_at_utc, resumed_at_utc, updated_at_utc,
                finished_at_utc, error_code, output_published
            ) VALUES (1, ?, 1, 1, 'running', 'preflight', ?, ?, ?, NULL, NULL, 0)
            """,
            (run_id, now, now, now),
        )
        for phase in _PHASES:
            unit, total = phase_units[phase]
            connection.execute(
                """
                INSERT INTO phase_state (
                    phase, status, unit, completed, total,
                    last_durable_json, updated_generation
                ) VALUES (?, ?, ?, 0, ?, NULL, 1)
                """,
                (phase, "running" if phase == "preflight" else "pending", unit, total),
            )
        connection.execute(
            "INSERT INTO publication_state VALUES (1, 'not_started', NULL, NULL, NULL, NULL)"
        )
        for index, start_block, end_block in chunks:
            connection.execute(
                "INSERT INTO action_chunks VALUES (?, ?, ?, 'pending', 0, NULL)",
                (index, start_block, end_block),
            )
            connection.execute(
                """
                INSERT INTO transfer_chunks (
                    chunk_index, start_block, end_block, status,
                    unfiltered_count, unfiltered_sha256, relevant_count,
                    completed_generation
                ) VALUES (?, ?, ?, 'pending', 0, NULL, 0, NULL)
                """,
                (index, start_block, end_block),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _resume_checkpoint(connection: sqlite3.Connection) -> None:
    current = connection.execute(
        "SELECT status, phase, output_published FROM run_state WHERE singleton = 1"
    ).fetchone()
    if current is None:
        raise CheckpointContractError("checkpoint run state is missing")
    if (
        current["status"] in ("succeeded", "failed")
        and current["phase"] == "succeeded"
        and current["output_published"] == 1
    ):
        return
    now = _utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE run_state
            SET attempt_number = attempt_number + 1,
                generation = generation + 1,
                status = 'running',
                resumed_at_utc = ?,
                updated_at_utc = ?,
                finished_at_utc = NULL,
                error_code = NULL
            WHERE singleton = 1
            """,
            (now, now),
        )
        if cursor.rowcount != 1:
            raise CheckpointContractError("checkpoint run state is missing")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _require_compatible_identity(
    connection: sqlite3.Connection,
    identity: RunIdentity,
) -> None:
    row = connection.execute(
        "SELECT fingerprint_sha256, canonical_json FROM run_identity WHERE singleton = 1"
    ).fetchone()
    expected_bytes = identity.canonical_bytes()
    if (
        row is None
        or row["fingerprint_sha256"] != hashlib.sha256(expected_bytes).hexdigest()
        or row["canonical_json"].encode("utf-8") != expected_bytes
    ):
        raise CheckpointContractError("checkpoint identity is incompatible")


def _validate_checkpoint(connection: sqlite3.Connection) -> None:
    try:
        if _pragma_int(connection, "application_id") != CHECKPOINT_APPLICATION_ID:
            raise CheckpointContractError("checkpoint schema application ID is invalid")
        if _pragma_int(connection, "user_version") != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointContractError("checkpoint schema user version is invalid")
        integrity_rows = tuple(
            cast(str, row[0]) for row in connection.execute("PRAGMA integrity_check")
        )
        if integrity_rows != ("ok",):
            raise CheckpointContractError("checkpoint integrity check failed")
        if _schema_manifest(connection) != _expected_schema_manifest():
            raise CheckpointContractError("checkpoint schema does not match version 2")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CheckpointContractError("checkpoint schema foreign keys are invalid")
        _validate_foundation_rows(connection)
    except CheckpointContractError:
        raise
    except sqlite3.DatabaseError as exc:
        raise CheckpointContractError("checkpoint schema or integrity check failed") from exc


def _validate_checkpoint_status(
    connection: sqlite3.Connection,
    paths: CheckpointPaths,
) -> None:
    """Validate the bounded metadata needed for a short read-only status view."""
    try:
        if _pragma_int(connection, "application_id") != CHECKPOINT_APPLICATION_ID:
            raise CheckpointContractError("checkpoint schema application ID is invalid")
        if _pragma_int(connection, "user_version") != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointContractError("checkpoint schema user version is invalid")
        if _schema_manifest(connection) != _expected_schema_manifest():
            raise CheckpointContractError("checkpoint schema does not match version 2")
        for table in ("schema_meta", "run_identity", "run_state", "publication_state"):
            count = cast(
                int,
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
            )
            if count != 1:
                raise CheckpointContractError("checkpoint singleton state is invalid")
        phases = tuple(
            cast(str, row[0])
            for row in connection.execute("SELECT phase FROM phase_state ORDER BY phase")
        )
        if phases != tuple(sorted(_PHASES)):
            raise CheckpointContractError("checkpoint phase state is invalid")
        identity_row = connection.execute(
            """
            SELECT fingerprint_sha256, canonical_json
            FROM run_identity
            WHERE singleton = 1
            """
        ).fetchone()
        if identity_row is None:
            raise CheckpointContractError("checkpoint identity is missing")
        canonical_json = cast(str, identity_row["canonical_json"])
        identity = _parse_identity_json(canonical_json)
        if (
            hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
            != identity_row["fingerprint_sha256"]
        ):
            raise CheckpointContractError("checkpoint identity digest is invalid")
        if _required_identity_str(identity, "output_path") != os.fspath(paths.output):
            raise CheckpointContractError("checkpoint identity output path does not match")
        _validate_phase_consistency(connection)
    except CheckpointContractError:
        raise
    except sqlite3.DatabaseError as exc:
        raise CheckpointContractError("checkpoint status metadata is invalid") from exc


def _validate_foundation_rows(connection: sqlite3.Connection) -> None:
    for table in ("schema_meta", "run_identity", "run_state", "publication_state"):
        count = cast(int, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if count != 1:
            raise CheckpointContractError("checkpoint singleton state is invalid")
    phases = tuple(
        cast(str, row[0])
        for row in connection.execute("SELECT phase FROM phase_state ORDER BY phase")
    )
    if phases != tuple(sorted(_PHASES)):
        raise CheckpointContractError("checkpoint phase state is invalid")
    row = connection.execute(
        "SELECT fingerprint_sha256, canonical_json FROM run_identity WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise CheckpointContractError("checkpoint identity is missing")
    canonical_json = cast(str, row["canonical_json"])
    try:
        payload = json.loads(canonical_json)
        canonical_round_trip = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CheckpointContractError("checkpoint identity JSON is invalid") from exc
    if canonical_round_trip != canonical_json:
        raise CheckpointContractError("checkpoint identity JSON is not canonical")
    if hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() != row["fingerprint_sha256"]:
        raise CheckpointContractError("checkpoint identity digest is invalid")
    if not isinstance(payload, dict):
        raise CheckpointContractError("checkpoint identity payload is invalid")
    _validate_chunk_partitions(connection, payload)
    _validate_phase_consistency(connection)
    try:
        _validate_staged_evidence_v2(connection, payload)
    except CheckpointContractError as exc:
        raise CheckpointContractError("checkpoint staged evidence is invalid") from exc


def _validate_chunk_partitions(
    connection: sqlite3.Connection,
    identity_payload: dict[str, object],
) -> None:
    expected = _block_ranges(
        _required_payload_int(identity_payload, "start_block"),
        _required_payload_int(identity_payload, "end_block"),
        _required_payload_int(identity_payload, "chunk_size"),
    )
    run_generation = cast(
        int,
        connection.execute("SELECT generation FROM run_state WHERE singleton = 1").fetchone()[0],
    )
    for table in ("action_chunks", "transfer_chunks"):
        rows = tuple(
            connection.execute(
                f"""
                SELECT chunk_index, start_block, end_block, status,
                       completed_generation
                FROM {table}
                ORDER BY chunk_index
                """
            )
        )
        locations = tuple(
            (row["chunk_index"], row["start_block"], row["end_block"]) for row in rows
        )
        if locations != expected:
            raise CheckpointContractError(f"checkpoint {table} partition is invalid")
        for row in rows:
            completed_generation = cast(int | None, row["completed_generation"])
            if completed_generation is not None and completed_generation > run_generation:
                raise CheckpointContractError(f"checkpoint {table} generation is invalid")
    _validate_action_chunk_counts(connection)
    _validate_transfer_chunk_counts(connection)


def _validate_action_chunk_counts(connection: sqlite3.Connection) -> None:
    observed = {
        cast(int, row["chunk_index"]): cast(int, row["count"])
        for row in connection.execute(
            """
            SELECT chunk_index, COUNT(*) AS count
            FROM action_witnesses
            GROUP BY chunk_index
            """
        )
    }
    for row in connection.execute(
        "SELECT chunk_index, status, witness_count FROM action_chunks"
    ):
        actual = observed.get(cast(int, row["chunk_index"]), 0)
        if row["status"] == "pending" and actual:
            raise CheckpointContractError("pending action chunk has evidence")
        if row["witness_count"] != actual:
            raise CheckpointContractError("action witness count is invalid")


def _validate_transfer_chunk_counts(connection: sqlite3.Connection) -> None:
    observed = {
        cast(int, row["chunk_index"]): cast(int, row["count"])
        for row in connection.execute(
            """
            SELECT chunk_index, COUNT(*) AS count
            FROM relevant_transfer_witnesses
            GROUP BY chunk_index
            """
        )
    }
    for row in connection.execute(
        """
        SELECT chunk_index, status, unfiltered_count, unfiltered_sha256,
               relevant_count
        FROM transfer_chunks
        """
    ):
        actual = observed.get(cast(int, row["chunk_index"]), 0)
        if row["status"] == "pending" and actual:
            raise CheckpointContractError("pending transfer chunk has evidence")
        if row["relevant_count"] != actual:
            raise CheckpointContractError("relevant transfer witness count is invalid")
        if row["status"] == "completed":
            _require_lower_hex(
                cast(str, row["unfiltered_sha256"]),
                32,
                "unfiltered transfer digest",
                prefix=False,
            )
            if row["unfiltered_count"] < actual:
                raise CheckpointContractError("unfiltered transfer count is invalid")


def _validate_staged_evidence_v2(
    connection: sqlite3.Connection,
    identity_payload: dict[str, object],
) -> None:
    generation = cast(
        int,
        connection.execute("SELECT generation FROM run_state WHERE singleton = 1").fetchone()[0],
    )
    for row in connection.execute(
        "SELECT block_number, block_hash, timestamp_ms, committed_generation FROM chain_blocks"
    ):
        _require_nonnegative_int(row["block_number"], "block number")
        _require_lower_hex(cast(str, row["block_hash"]), 32, "block hash")
        if row["timestamp_ms"] is not None:
            _require_nonnegative_int(row["timestamp_ms"], "block timestamp")
        _require_generation(cast(int, row["committed_generation"]), generation)
        _require_endpoint_payload_observation(identity_payload, row)

    expected_pool_manager = _required_identity_str(identity_payload, "pool_manager")
    expected_action_topic = _required_identity_str(identity_payload, "action_topic")
    expected_pool_id = _required_identity_str(identity_payload, "pool_id")
    for witness_table, chunk_table in (
        ("action_witnesses", "action_chunks"),
        ("relevant_transfer_witnesses", "transfer_chunks"),
    ):
        misplaced = connection.execute(
            f"""
            SELECT 1
            FROM {witness_table} AS w
            JOIN {chunk_table} AS c ON c.chunk_index = w.chunk_index
            WHERE w.block_number < c.start_block OR w.block_number > c.end_block
            LIMIT 1
            """
        ).fetchone()
        if misplaced is not None:
            raise CheckpointContractError("staged witness is outside its chunk")
        for row in connection.execute(
            f"""
            SELECT source, block_number, block_hash, transaction_hash,
                   transaction_index, log_index, address, topics_json, data
            FROM {witness_table}
            ORDER BY block_number, transaction_index, log_index, transaction_hash
            """
        ):
            witness = _witness_from_row(row)
            expected_source: DiscoverySource = (
                "pool_modify"
                if witness_table == "action_witnesses"
                else "position_transfer"
            )
            _validate_witness_shape(witness, expected_source=expected_source)
            if witness_table == "action_witnesses" and (
                witness.address != expected_pool_manager
                or witness.topics[0] != expected_action_topic
                or len(witness.topics) < 2
                or witness.topics[1] != expected_pool_id
            ):
                raise CheckpointContractError("persisted action witness target changed")

    allowed_hashes = {
        cast(str, row[0])
        for row in connection.execute(
            "SELECT transaction_hash FROM action_witnesses"
        )
    } | {
        cast(str, row[0])
        for row in connection.execute(
            "SELECT transaction_hash FROM relevant_transfer_witnesses"
        )
    }
    for row in connection.execute(
        """
        SELECT transaction_hash, block_number, block_hash, transaction_index,
               transaction_json, receipt_json, payload_sha256,
               committed_generation
        FROM transaction_bundles
        ORDER BY block_number, transaction_index, transaction_hash
        """
    ):
        _require_generation(cast(int, row["committed_generation"]), generation)
        bundle = _bundle_from_row(row)
        if bundle.transaction_hash not in allowed_hashes:
            raise CheckpointContractError("bundle has no eligible witness")

    for table in (
        "action_bundle_fetches",
        "relevant_transfer_bundle_fetches",
        "decoded_action_transactions",
        "decoded_relevant_transfer_transactions",
    ):
        for row in connection.execute(
            f"SELECT committed_generation FROM {table}"
        ):
            _require_generation(cast(int, row[0]), generation)

    action_hashes = tuple(
        cast(str, row[0])
        for row in connection.execute(
            "SELECT DISTINCT transaction_hash FROM action_witnesses ORDER BY transaction_hash"
        )
    )
    fetched_action_hashes = tuple(
        cast(str, row[0])
        for row in connection.execute(
            "SELECT transaction_hash FROM action_bundle_fetches ORDER BY transaction_hash"
        )
    )
    if not set(fetched_action_hashes) <= set(action_hashes):
        raise CheckpointContractError("action fetch relation has no action witness")

    action_bundle_order = tuple(
        cast(str, row[0])
        for row in connection.execute(
            """
            SELECT b.transaction_hash
            FROM transaction_bundles AS b
            JOIN action_bundle_fetches AS f
              ON f.transaction_hash = b.transaction_hash
            ORDER BY b.block_number, b.transaction_index, b.transaction_hash
            """
        )
    )
    decoded_rows = tuple(
        connection.execute(
            """
            SELECT transaction_hash, block_number, block_hash,
                   transaction_index, action_count, decoder_state_sha256,
                   committed_generation
            FROM decoded_action_transactions
            ORDER BY block_number, transaction_index, transaction_hash
            """
        )
    )
    decoded_hashes = tuple(cast(str, row["transaction_hash"]) for row in decoded_rows)
    if decoded_hashes != action_bundle_order[: len(decoded_hashes)]:
        raise CheckpointContractError("action decode cursor is not a canonical prefix")
    for row in decoded_rows:
        _require_generation(cast(int, row["committed_generation"]), generation)
        _require_lower_hex(
            cast(str, row["decoder_state_sha256"]),
            32,
            "decoder state digest",
            prefix=False,
        )
        child_count = cast(
            int,
            connection.execute(
                "SELECT COUNT(*) FROM decoded_actions WHERE transaction_hash = ?",
                (row["transaction_hash"],),
            ).fetchone()[0],
        )
        if child_count != row["action_count"]:
            raise CheckpointContractError("decoded action child count changed")

    _validate_persisted_actions(connection, identity_payload)
    _validate_persisted_position_resolutions(connection, generation)
    _validate_persisted_decoder_state(connection, decoded_rows, generation)

    mappings = tuple(
        _position_key_from_row(row)
        for row in connection.execute(
            """
            SELECT token_id, pool_id, tick_lower, tick_upper, salt,
                   mint_block_number, mint_log_index, mint_event_order,
                   committed_generation
            FROM token_position_keys
            ORDER BY length(token_id), token_id
            """
        )
    )
    for mapping, row in zip(
        mappings,
        connection.execute(
            """
            SELECT committed_generation FROM token_position_keys
            ORDER BY length(token_id), token_id
            """
        ),
        strict=True,
    ):
        del mapping
        _require_generation(cast(int, row[0]), generation)
    mint_actions = tuple(
        action
        for action in (
            _action_from_json(cast(str, row[0]))
            for row in connection.execute(
                """
                SELECT action_json FROM decoded_actions
                WHERE action_type = 'mint'
                ORDER BY block_number, log_index, event_order
                """
            )
        )
    )
    _validate_position_key_mappings(mappings, mint_actions, set())

    frozen_row = connection.execute(
        """
        SELECT token_count, token_ids_sha256, committed_generation
        FROM frozen_token_set WHERE singleton = 1
        """
    ).fetchone()
    frozen_ids = tuple(
        _parse_unsigned_decimal(cast(str, row["token_id"]), "token id")
        for row in connection.execute(
            "SELECT token_id, ordinal FROM frozen_token_ids ORDER BY ordinal"
        )
    )
    frozen_phase_completed = (
        connection.execute(
            "SELECT status FROM phase_state WHERE phase = 'token_set_freeze'"
        ).fetchone()[0]
        == "completed"
    )
    if (frozen_row is not None) != frozen_phase_completed:
        raise CheckpointContractError("frozen token-set phase evidence is inconsistent")
    if frozen_row is None:
        if frozen_ids:
            raise CheckpointContractError("frozen token rows lack a set record")
    else:
        _require_generation(cast(int, frozen_row["committed_generation"]), generation)
        if (
            frozen_ids != tuple(sorted(set(frozen_ids)))
            or frozen_row["token_count"] != len(frozen_ids)
            or frozen_row["token_ids_sha256"] != _token_ids_sha256(frozen_ids)
        ):
            raise CheckpointContractError("frozen token set changed")
        decoded_token_ids = tuple(
            sorted(
                {
                    _parse_unsigned_decimal(cast(str, row[0]), "token id")
                    for row in connection.execute("SELECT token_id FROM decoded_actions")
                }
            )
        )
        if frozen_ids != decoded_token_ids:
            raise CheckpointContractError("frozen token set differs from decoded actions")

    replay_row = connection.execute(
        "SELECT * FROM replay_input WHERE singleton = 1"
    ).fetchone()
    replay_phase_completed = (
        connection.execute(
            "SELECT status FROM phase_state WHERE phase = 'replay_input_bind'"
        ).fetchone()[0]
        == "completed"
    )
    if (replay_row is not None) != replay_phase_completed:
        raise CheckpointContractError("replay-input phase evidence is inconsistent")
    if replay_row is not None:
        evidence = _replay_input_from_row(replay_row)
        _validate_replay_input_evidence(evidence)
        expected_replay = cast(dict[str, object], identity_payload["replay_input"])
        if asdict(evidence) != expected_replay:
            raise CheckpointContractError("replay input differs from run identity")
        _require_generation(cast(int, replay_row["committed_generation"]), generation)

    _validate_v2_bundle_and_decode_relations(
        connection,
        identity_payload,
        generation,
        frozen_ids,
    )
    _validate_persisted_bindings_v2(connection, generation)
    _validate_staged_phase_counts_v2(connection)


def _validate_v2_bundle_and_decode_relations(
    connection: sqlite3.Connection,
    identity_payload: dict[str, object],
    generation: int,
    frozen_ids: tuple[int, ...],
) -> None:
    run_phase = cast(
        str,
        connection.execute("SELECT phase FROM run_state WHERE singleton = 1").fetchone()[0],
    )
    phase_index = _PHASES.index(run_phase)
    action_hashes = {
        cast(str, row[0])
        for row in connection.execute(
            "SELECT DISTINCT transaction_hash FROM action_witnesses"
        )
    }
    fetched_action_hashes = {
        cast(str, row[0])
        for row in connection.execute(
            "SELECT transaction_hash FROM action_bundle_fetches"
        )
    }
    if not fetched_action_hashes <= action_hashes:
        raise CheckpointContractError("action fetch relation has no action witness")
    if phase_index > _PHASES.index("action_fetch") and fetched_action_hashes != action_hashes:
        raise CheckpointContractError("action bundle set differs from action witnesses")
    for transaction_hash in sorted(fetched_action_hashes):
        bundle_row = connection.execute(
            """
            SELECT transaction_hash, block_number, block_hash, transaction_index,
                   transaction_json, receipt_json, payload_sha256
            FROM transaction_bundles WHERE transaction_hash = ?
            """,
            (transaction_hash,),
        ).fetchone()
        if bundle_row is None:
            raise CheckpointContractError("action fetch relation lacks a bundle")
        bundle = _bundle_from_row(bundle_row)
        witnesses = tuple(
            _witness_from_row(row)
            for row in connection.execute(
                """
                SELECT source, block_number, block_hash, transaction_hash,
                       transaction_index, log_index, address, topics_json, data
                FROM action_witnesses WHERE transaction_hash = ?
                ORDER BY block_number, transaction_index, log_index
                """,
                (transaction_hash,),
            )
        )
        _validate_bundle_against_witnesses(bundle, witnesses)

    decoded_action_order = tuple(
        cast(str, row[0])
        for row in connection.execute(
            """
            SELECT d.transaction_hash
            FROM decoded_action_transactions AS d
            ORDER BY d.block_number, d.transaction_index, d.transaction_hash
            """
        )
    )
    action_bundle_order = tuple(
        cast(str, row[0])
        for row in connection.execute(
            """
            SELECT b.transaction_hash
            FROM transaction_bundles AS b
            JOIN action_bundle_fetches AS f
              ON f.transaction_hash = b.transaction_hash
            ORDER BY b.block_number, b.transaction_index, b.transaction_hash
            """
        )
    )
    if decoded_action_order != action_bundle_order[: len(decoded_action_order)]:
        raise CheckpointContractError("action decode cursor is not a canonical prefix")
    if (
        phase_index > _PHASES.index("action_decode")
        and decoded_action_order != action_bundle_order
    ):
        raise CheckpointContractError("action decode set differs from action bundles")
    for transaction_hash in decoded_action_order:
        required_logs = tuple(
            cast(int, row[0])
            for row in connection.execute(
                """
                SELECT log_index FROM action_witnesses
                WHERE transaction_hash = ? ORDER BY log_index
                """,
                (transaction_hash,),
            )
        )
        represented_logs = tuple(
            cast(int, row[0])
            for row in connection.execute(
                """
                SELECT log_index FROM decoded_actions
                WHERE transaction_hash = ? ORDER BY log_index, event_order
                """,
                (transaction_hash,),
            )
        )
        _require_modify_action_multiplicity(required_logs, represented_logs)

    frozen_set = set(frozen_ids)
    expected_manager = _required_identity_str(identity_payload, "position_manager")
    expected_topic = _required_identity_str(identity_payload, "transfer_topic")
    for row in connection.execute(
        """
        SELECT source, block_number, block_hash, transaction_hash,
               transaction_index, log_index, address, topics_json, data, token_id
        FROM relevant_transfer_witnesses
        ORDER BY block_number, transaction_index, log_index, transaction_hash
        """
    ):
        witness = _witness_from_row(row)
        token_id = _transfer_witness_token_id(witness)
        _transfer_witness_owners(witness)
        if (
            witness.address != expected_manager
            or witness.topics[0] != expected_topic
            or token_id not in frozen_set
            or cast(str, row["token_id"]) != _unsigned_decimal(token_id, "token id")
        ):
            raise CheckpointContractError("persisted relevant transfer witness changed")

    relevant_hashes = {
        cast(str, row[0])
        for row in connection.execute(
            "SELECT DISTINCT transaction_hash FROM relevant_transfer_witnesses"
        )
    }
    fetched_transfer_hashes = {
        cast(str, row[0])
        for row in connection.execute(
            "SELECT transaction_hash FROM relevant_transfer_bundle_fetches"
        )
    }
    if not fetched_transfer_hashes <= relevant_hashes:
        raise CheckpointContractError("relevant transfer fetch has no witness")
    if (
        phase_index > _PHASES.index("relevant_transfer_fetch")
        and fetched_transfer_hashes != relevant_hashes
    ):
        raise CheckpointContractError(
            "relevant transfer bundle set differs from retained witnesses"
        )
    for transaction_hash in sorted(fetched_transfer_hashes):
        bundle_row = connection.execute(
            """
            SELECT transaction_hash, block_number, block_hash, transaction_index,
                   transaction_json, receipt_json, payload_sha256
            FROM transaction_bundles WHERE transaction_hash = ?
            """,
            (transaction_hash,),
        ).fetchone()
        if bundle_row is None:
            raise CheckpointContractError("relevant transfer fetch lacks a bundle")
        bundle = _bundle_from_row(bundle_row)
        witnesses = tuple(
            _witness_from_row(row)
            for row in connection.execute(
                """
                SELECT source, block_number, block_hash, transaction_hash,
                       transaction_index, log_index, address, topics_json, data
                FROM relevant_transfer_witnesses WHERE transaction_hash = ?
                ORDER BY block_number, transaction_index, log_index
                """,
                (transaction_hash,),
            )
        )
        _validate_bundle_against_witnesses(bundle, witnesses)

    transfer_bundle_order = tuple(
        cast(str, row[0])
        for row in connection.execute(
            """
            SELECT b.transaction_hash
            FROM transaction_bundles AS b
            JOIN relevant_transfer_bundle_fetches AS f
              ON f.transaction_hash = b.transaction_hash
            ORDER BY b.block_number, b.transaction_index, b.transaction_hash
            """
        )
    )
    decoded_transfer_rows = tuple(
        connection.execute(
            """
            SELECT transaction_hash, block_number, block_hash, transaction_index,
                   ownership_count, committed_generation
            FROM decoded_relevant_transfer_transactions
            ORDER BY block_number, transaction_index, transaction_hash
            """
        )
    )
    decoded_transfer_order = tuple(
        cast(str, row["transaction_hash"]) for row in decoded_transfer_rows
    )
    if decoded_transfer_order != transfer_bundle_order[: len(decoded_transfer_order)]:
        raise CheckpointContractError("ownership decode cursor is not a canonical prefix")
    if (
        phase_index > _PHASES.index("relevant_transfer_decode")
        and decoded_transfer_order != transfer_bundle_order
    ):
        raise CheckpointContractError("ownership decode set differs from transfer bundles")
    for row in decoded_transfer_rows:
        _require_generation(cast(int, row["committed_generation"]), generation)
        transaction_hash = cast(str, row["transaction_hash"])
        bundle_row = connection.execute(
            """
            SELECT transaction_hash, block_number, block_hash, transaction_index,
                   transaction_json, receipt_json, payload_sha256
            FROM transaction_bundles WHERE transaction_hash = ?
            """,
            (transaction_hash,),
        ).fetchone()
        if bundle_row is None:
            raise CheckpointContractError("ownership decode bundle is missing")
        witnesses = tuple(
            _witness_from_row(witness_row)
            for witness_row in connection.execute(
                """
                SELECT source, block_number, block_hash, transaction_hash,
                       transaction_index, log_index, address, topics_json, data
                FROM relevant_transfer_witnesses WHERE transaction_hash = ?
                ORDER BY block_number, transaction_index, log_index
                """,
                (transaction_hash,),
            )
        )
        owners = tuple(
            OwnershipEvent(
                block_number=cast(int, owner_row["block_number"]),
                log_index=cast(int, owner_row["log_index"]),
                token_id=_parse_unsigned_decimal(
                    cast(str, owner_row["token_id"]), "token id"
                ),
                previous_owner=cast(str | None, owner_row["previous_owner"]),
                new_owner=cast(str | None, owner_row["new_owner"]),
                event_order=cast(int, owner_row["event_order"]),
            )
            for owner_row in connection.execute(
                """
                SELECT block_number, log_index, event_order, token_id,
                       previous_owner, new_owner
                FROM ownership_events WHERE transaction_hash = ?
                ORDER BY block_number, log_index, event_order
                """,
                (transaction_hash,),
            )
        )
        if len(owners) != row["ownership_count"]:
            raise CheckpointContractError("decoded ownership child count changed")
        _validate_ownership_reconciliation(
            _bundle_from_row(bundle_row),
            witnesses,
            owners,
            frozen_set,
        )
    _validate_persisted_ownership(connection)


def _validate_persisted_bindings_v2(
    connection: sqlite3.Connection,
    generation: int,
) -> None:
    bindings: list[ActionPriceBinding] = []
    for row in connection.execute(
        """
        SELECT block_number, log_index, event_order,
               event_time_sqrt_price_x96, event_time_tick,
               event_time_state_source, committed_generation
        FROM action_price_bindings
        ORDER BY block_number, log_index, event_order
        """
    ):
        bindings.append(_binding_from_row(row))
        _require_generation(cast(int, row["committed_generation"]), generation)
    binding_values = tuple(bindings)
    _validate_action_price_bindings(binding_values)
    phase = cast(
        str,
        connection.execute("SELECT phase FROM run_state WHERE singleton = 1").fetchone()[0],
    )
    action_keys = tuple(
        tuple(cast(int, value) for value in row)
        for row in connection.execute(
            """
            SELECT block_number, log_index, event_order FROM decoded_actions
            ORDER BY block_number, log_index, event_order
            """
        )
    )
    binding_keys = tuple(
        (binding.block_number, binding.log_index, binding.event_order)
        for binding in binding_values
    )
    if _PHASES.index(phase) > _PHASES.index("price_replay"):
        if binding_keys != action_keys:
            raise CheckpointContractError("action price bindings are incomplete")
    elif binding_keys:
        raise CheckpointContractError("action price bindings precede phase completion")


def _validate_staged_phase_counts_v2(connection: sqlite3.Connection) -> None:
    evidence_counts = {
        "action_discovery": _completed_count(connection, "action_chunks"),
        "action_fetch": cast(
            int,
            connection.execute("SELECT COUNT(*) FROM action_bundle_fetches").fetchone()[0],
        ),
        "action_decode": cast(
            int,
            connection.execute(
                "SELECT COUNT(*) FROM decoded_action_transactions"
            ).fetchone()[0],
        ),
        "token_set_freeze": cast(
            int,
            connection.execute("SELECT COUNT(*) FROM frozen_token_ids").fetchone()[0],
        ),
        "full_transfer_scan": _completed_count(connection, "transfer_chunks"),
        "relevant_transfer_fetch": cast(
            int,
            connection.execute(
                "SELECT COUNT(*) FROM relevant_transfer_bundle_fetches"
            ).fetchone()[0],
        ),
        "relevant_transfer_decode": cast(
            int,
            connection.execute(
                "SELECT COUNT(*) FROM decoded_relevant_transfer_transactions"
            ).fetchone()[0],
        ),
        "replay_input_bind": cast(
            int,
            connection.execute("SELECT COUNT(*) FROM replay_input").fetchone()[0],
        ),
        "price_replay": cast(
            int,
            connection.execute("SELECT COUNT(*) FROM action_price_bindings").fetchone()[0],
        ),
    }
    phase_rows = {
        cast(str, row["phase"]): row
        for row in connection.execute(
            """
            SELECT phase, status, completed, total, updated_generation
            FROM phase_state
            """
        )
    }
    for phase, expected_completed in evidence_counts.items():
        if phase_rows[phase]["completed"] != expected_completed:
            raise CheckpointContractError("staged phase counter changed")

    action_count = cast(
        int,
        connection.execute("SELECT COUNT(*) FROM decoded_actions").fetchone()[0],
    )
    decoded_token_count = cast(
        int,
        connection.execute(
            "SELECT COUNT(DISTINCT token_id) FROM decoded_actions"
        ).fetchone()[0],
    )
    dynamic_totals = (
        ("action_discovery", "action_fetch", _action_candidate_count(connection)),
        ("action_fetch", "action_decode", _action_candidate_count(connection)),
        ("action_decode", "token_set_freeze", decoded_token_count),
        (
            "full_transfer_scan",
            "relevant_transfer_fetch",
            _relevant_transfer_transaction_count(connection),
        ),
        (
            "relevant_transfer_fetch",
            "relevant_transfer_decode",
            _relevant_transfer_transaction_count(connection),
        ),
        ("replay_input_bind", "price_replay", action_count),
        ("price_replay", "build", action_count),
    )
    for source_phase, target_phase, expected_total in dynamic_totals:
        source = phase_rows[source_phase]
        target = phase_rows[target_phase]
        if source["status"] != "completed":
            continue
        if (
            target["total"] != expected_total
            or target["updated_generation"] < source["updated_generation"]
        ):
            raise CheckpointContractError("staged phase total or generation changed")


def _require_endpoint_payload_observation(
    identity_payload: dict[str, object],
    row: sqlite3.Row,
) -> None:
    endpoint = cast(dict[str, object], identity_payload["endpoint"])
    for name in ("start", "end"):
        expected = cast(dict[str, object], endpoint[name])
        if expected["block_number"] != row["block_number"]:
            continue
        if expected["block_hash"] != row["block_hash"]:
            raise CheckpointContractError("endpoint block hash changed")
        if row["timestamp_ms"] is not None and expected["timestamp_ms"] != row["timestamp_ms"]:
            raise CheckpointContractError("endpoint block timestamp changed")


def _require_generation(observed: int, current: int) -> None:
    if observed < 1 or observed > current:
        raise CheckpointContractError("staged evidence generation is invalid")


def _validate_persisted_actions(
    connection: sqlite3.Connection,
    identity_payload: dict[str, object],
) -> None:
    for row in connection.execute(
        """
        SELECT block_number, log_index, event_order, block_hash,
               transaction_hash, token_id, action_type, action_json,
               payload_sha256
        FROM decoded_actions
        ORDER BY block_number, log_index, event_order
        """
    ):
        action_json = cast(str, row["action_json"])
        digest = _require_lower_hex(
            cast(str, row["payload_sha256"]), 32, "action payload digest", prefix=False
        )
        if hashlib.sha256(action_json.encode("utf-8")).hexdigest() != digest:
            raise CheckpointContractError("decoded action digest changed")
        action = _action_from_json(action_json)
        _validate_action_run_identity(action, identity_payload)
        if (
            action.block_number != row["block_number"]
            or action.log_index != row["log_index"]
            or action.event_order != row["event_order"]
            or action.tx_hash != row["transaction_hash"]
            or _unsigned_decimal(action.token_id, "token id") != row["token_id"]
            or action.action_type != row["action_type"]
        ):
            raise CheckpointContractError("decoded action columns changed")
        block = connection.execute(
            "SELECT block_hash FROM chain_blocks WHERE block_number = ?",
            (action.block_number,),
        ).fetchone()
        if block is None or block["block_hash"] != row["block_hash"]:
            raise CheckpointContractError("decoded action block changed")


def _validate_action_run_identity(
    action: DecodedLiquidityAction,
    identity_payload: dict[str, object],
) -> None:
    if (
        action.chain != _required_identity_str(identity_payload, "chain")
        or action.pool_id.lower() != _required_identity_str(identity_payload, "pool_id")
        or action.position_manager.lower()
        != _required_identity_str(identity_payload, "position_manager")
    ):
        raise CheckpointContractError("decoded action run identity is invalid")


def _validate_persisted_ownership(connection: sqlite3.Connection) -> None:
    for row in connection.execute(
        """
        SELECT block_number, block_hash, transaction_hash, token_id,
               previous_owner, new_owner
        FROM ownership_events
        """
    ):
        _parse_unsigned_decimal(cast(str, row["token_id"]), "token id")
        _require_lower_hex(cast(str, row["transaction_hash"]), 32, "transaction hash")
        if row["previous_owner"] is not None:
            _require_lower_hex(cast(str, row["previous_owner"]), 20, "previous owner")
        if row["new_owner"] is not None:
            _require_lower_hex(cast(str, row["new_owner"]), 20, "new owner")
        block = connection.execute(
            "SELECT block_hash FROM chain_blocks WHERE block_number = ?",
            (row["block_number"],),
        ).fetchone()
        if block is None or block["block_hash"] != row["block_hash"]:
            raise CheckpointContractError("ownership event block changed")


def _validate_persisted_position_resolutions(
    connection: sqlite3.Connection,
    generation: int,
) -> None:
    for row in connection.execute(
        """
        SELECT token_id, query_block, query_block_hash, found, pool_id,
               tick_lower, tick_upper, liquidity_after, committed_generation
        FROM position_resolutions
        """
    ):
        token_id = _parse_unsigned_decimal(cast(str, row["token_id"]), "token id")
        state = None
        if row["found"] == 1:
            state = LedgerPositionState(
                pool_id=_require_lower_hex(cast(str, row["pool_id"]), 32, "pool id"),
                tick_lower=cast(int, row["tick_lower"]),
                tick_upper=cast(int, row["tick_upper"]),
                liquidity_after=_parse_unsigned_decimal(
                    cast(str, row["liquidity_after"]), "liquidity"
                ),
            )
        _validate_position_resolution(
            PositionResolution(
                token_id=token_id,
                query_block=cast(int, row["query_block"]),
                found=bool(row["found"]),
                state=state,
            )
        )
        block = connection.execute(
            "SELECT block_hash, timestamp_ms FROM chain_blocks WHERE block_number = ?",
            (row["query_block"],),
        ).fetchone()
        if (
            block is None
            or block["timestamp_ms"] is None
            or block["block_hash"] != row["query_block_hash"]
        ):
            raise CheckpointContractError("position resolution block changed")
        _require_generation(cast(int, row["committed_generation"]), generation)


def _validate_persisted_decoder_state(
    connection: sqlite3.Connection,
    decoded_rows: tuple[sqlite3.Row, ...],
    generation: int,
) -> None:
    for row in connection.execute(
        """
        SELECT token_id, pool_id, liquidity_after, updated_generation
        FROM decoder_token_state
        """
    ):
        _parse_unsigned_decimal(cast(str, row["token_id"]), "token id")
        _require_lower_hex(cast(str, row["pool_id"]), 32, "pool id")
        _parse_unsigned_decimal(cast(str, row["liquidity_after"]), "liquidity")
        _require_generation(cast(int, row["updated_generation"]), generation)
    if not decoded_rows:
        if connection.execute("SELECT 1 FROM decoder_token_state LIMIT 1").fetchone() is not None:
            raise CheckpointContractError("decoder state exists before its cursor")
        return
    if decoded_rows[-1]["decoder_state_sha256"] != _decoder_state_sha256(connection):
        raise CheckpointContractError("decoder state digest changed")


def _validate_phase_consistency(connection: sqlite3.Connection) -> None:
    run = connection.execute(
        """
        SELECT status, phase, generation, output_published, error_code
        FROM run_state
        WHERE singleton = 1
        """
    ).fetchone()
    publication = connection.execute(
        "SELECT state FROM publication_state WHERE singleton = 1"
    ).fetchone()
    if run is None or publication is None:
        raise CheckpointContractError("checkpoint run phase state is missing")
    phase_rows = {
        cast(str, row["phase"]): row
        for row in connection.execute(
            """
            SELECT phase, status, completed, total, updated_generation
            FROM phase_state
            """
        )
    }
    generation = cast(int, run["generation"])
    for phase, row in phase_rows.items():
        completed = cast(int, row["completed"])
        total = cast(int | None, row["total"])
        if row["updated_generation"] > generation:
            raise CheckpointContractError("checkpoint phase generation is invalid")
        if row["status"] == "pending" and completed != 0:
            raise CheckpointContractError("checkpoint pending phase has durable units")
        if row["status"] == "completed" and total is not None and completed != total:
            raise CheckpointContractError(f"checkpoint completed phase {phase} is incomplete")

    current_phase = cast(str, run["phase"])
    if current_phase == "succeeded":
        terminal_error_is_valid = (run["status"] == "succeeded" and run["error_code"] is None) or (
            run["status"] == "failed" and run["error_code"] == "checkpoint_maintenance_error"
        )
        if (
            not terminal_error_is_valid
            or run["output_published"] != 1
            or publication["state"] != "published"
            or any(row["status"] != "completed" for row in phase_rows.values())
        ):
            raise CheckpointContractError("checkpoint terminal phase is inconsistent")
        return

    if (
        run["status"] == "succeeded"
        or run["output_published"] != 0
        or (run["status"] == "running" and run["error_code"] is not None)
        or (run["status"] == "failed" and run["error_code"] is None)
        or (run["status"] == "interrupted" and run["error_code"] != "interrupted")
    ):
        raise CheckpointContractError("checkpoint nonterminal phase is inconsistent")
    current_index = _PHASES.index(current_phase)
    for index, phase in enumerate(_PHASES):
        expected_status = (
            "completed"
            if index < current_index
            else "running"
            if index == current_index
            else "pending"
        )
        if phase_rows[phase]["status"] != expected_status:
            raise CheckpointContractError("checkpoint phase cursor is inconsistent")


def _required_payload_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int:
        raise CheckpointContractError(f"checkpoint identity {key} is invalid")
    return value


def _block_ranges(
    start_block: int,
    end_block: int,
    chunk_size: int,
) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (index, block, min(block + chunk_size - 1, end_block))
        for index, block in enumerate(range(start_block, end_block + 1, chunk_size))
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pragma_int(connection: sqlite3.Connection, name: str) -> int:
    return cast(int, connection.execute(f"PRAGMA {name}").fetchone()[0])


def _schema_manifest(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'view', 'trigger')
            ORDER BY type, name
            """
        )
    )
    tables = tuple(
        cast(str, row[1])
        for row in objects
        if row[0] == "table" and not cast(str, row[1]).startswith("sqlite_")
    )
    details: list[object] = []
    for table in tables:
        columns = tuple(tuple(row) for row in connection.execute(f'PRAGMA table_xinfo("{table}")'))
        details.append((table, "columns", columns))
        foreign_keys = tuple(
            tuple(row) for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        )
        details.append((table, "foreign_keys", foreign_keys))
        index_rows = tuple(
            tuple(row) for row in connection.execute(f'PRAGMA index_list("{table}")')
        )
        details.append((table, "indexes", index_rows))
        for index_row in index_rows:
            index_name = cast(str, index_row[1])
            details.append(
                (
                    table,
                    "index_xinfo",
                    index_name,
                    tuple(
                        tuple(row)
                        for row in connection.execute(f'PRAGMA index_xinfo("{index_name}")')
                    ),
                )
            )
    return objects + tuple(details)


@lru_cache(maxsize=1)
def _expected_schema_manifest() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA_SQL)
        return _schema_manifest(connection)
    finally:
        connection.close()


_SCHEMA_SQL = """
CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 2),
    created_at_utc TEXT NOT NULL CHECK (length(created_at_utc) > 0)
) STRICT;

CREATE TABLE run_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    fingerprint_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(fingerprint_sha256) = 64),
    canonical_json TEXT NOT NULL CHECK (length(canonical_json) > 0),
    FOREIGN KEY (singleton) REFERENCES schema_meta(singleton)
) STRICT;

CREATE TABLE run_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    run_id TEXT NOT NULL UNIQUE CHECK (length(run_id) = 36),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    status TEXT NOT NULL
        CHECK (status IN ('running','succeeded','failed','interrupted')),
    phase TEXT NOT NULL CHECK (phase IN (
        'preflight','action_discovery','action_fetch','action_decode',
        'token_set_freeze','full_transfer_scan','relevant_transfer_fetch',
        'relevant_transfer_decode','replay_input_bind','price_replay','build',
        'publish','succeeded'
    )),
    started_at_utc TEXT NOT NULL CHECK (length(started_at_utc) > 0),
    resumed_at_utc TEXT NOT NULL CHECK (length(resumed_at_utc) > 0),
    updated_at_utc TEXT NOT NULL CHECK (length(updated_at_utc) > 0),
    finished_at_utc TEXT,
    error_code TEXT CHECK (error_code IS NULL OR error_code IN (
        'rpc_error','checkpoint_incompatible','checkpoint_corrupt',
        'decode_error','replay_error','publication_error',
        'checkpoint_maintenance_error','interrupted','unknown_error'
    )),
    output_published INTEGER NOT NULL DEFAULT 0
        CHECK (output_published IN (0,1)),
    FOREIGN KEY (singleton) REFERENCES schema_meta(singleton)
) STRICT;

CREATE TABLE phase_state (
    phase TEXT PRIMARY KEY CHECK (phase IN (
        'preflight','action_discovery','action_fetch','action_decode',
        'token_set_freeze','full_transfer_scan','relevant_transfer_fetch',
        'relevant_transfer_decode','replay_input_bind','price_replay','build',
        'publish','succeeded'
    )),
    status TEXT NOT NULL CHECK (status IN ('pending','running','completed')),
    unit TEXT NOT NULL CHECK (length(unit) > 0),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed >= 0),
    total INTEGER CHECK (total IS NULL OR total >= completed),
    last_durable_json TEXT,
    updated_generation INTEGER NOT NULL CHECK (updated_generation >= 1)
) STRICT;

CREATE TABLE publication_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state TEXT NOT NULL CHECK (state IN ('not_started','started','published')),
    expected_ledger_sha256 TEXT
        CHECK (expected_ledger_sha256 IS NULL OR length(expected_ledger_sha256) = 64),
    expected_sidecar_sha256 TEXT
        CHECK (expected_sidecar_sha256 IS NULL OR length(expected_sidecar_sha256) = 64),
    started_generation INTEGER
        CHECK (started_generation IS NULL OR started_generation >= 1),
    published_generation INTEGER
        CHECK (published_generation IS NULL OR published_generation >= 1),
    CHECK (
        (state = 'not_started' AND expected_ledger_sha256 IS NULL
            AND expected_sidecar_sha256 IS NULL AND started_generation IS NULL
            AND published_generation IS NULL)
        OR
        (state = 'started' AND expected_ledger_sha256 IS NOT NULL
            AND expected_sidecar_sha256 IS NOT NULL
            AND started_generation IS NOT NULL AND published_generation IS NULL)
        OR
        (state = 'published' AND expected_ledger_sha256 IS NOT NULL
            AND expected_sidecar_sha256 IS NOT NULL
            AND started_generation IS NOT NULL
            AND published_generation IS NOT NULL)
    ),
    FOREIGN KEY (singleton) REFERENCES schema_meta(singleton)
) STRICT;

CREATE TABLE chain_blocks (
    block_number INTEGER PRIMARY KEY CHECK (block_number >= 0),
    block_hash TEXT NOT NULL UNIQUE CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    timestamp_ms INTEGER CHECK (timestamp_ms IS NULL OR timestamp_ms >= 0),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    UNIQUE (block_number, block_hash)
) STRICT;

CREATE TABLE action_chunks (
    chunk_index INTEGER PRIMARY KEY CHECK (chunk_index >= 0),
    start_block INTEGER NOT NULL UNIQUE CHECK (start_block >= 0),
    end_block INTEGER NOT NULL UNIQUE CHECK (end_block >= start_block),
    status TEXT NOT NULL CHECK (status IN ('pending','completed')),
    witness_count INTEGER NOT NULL DEFAULT 0 CHECK (witness_count >= 0),
    completed_generation INTEGER
        CHECK (completed_generation IS NULL OR completed_generation >= 1),
    CHECK (
        (status = 'pending' AND witness_count = 0 AND completed_generation IS NULL)
        OR (status = 'completed' AND completed_generation IS NOT NULL)
    )
) STRICT;

CREATE TABLE action_witnesses (
    source TEXT NOT NULL CHECK (source = 'pool_modify'),
    transaction_hash TEXT NOT NULL CHECK (
        length(transaction_hash) = 66
        AND substr(transaction_hash, 1, 2) = '0x'
    ),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    chunk_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    address TEXT NOT NULL CHECK (
        length(address) = 42 AND substr(address, 1, 2) = '0x'
    ),
    topics_json TEXT NOT NULL CHECK (length(topics_json) > 0),
    data TEXT NOT NULL CHECK (
        length(data) >= 2 AND substr(data, 1, 2) = '0x'
    ),
    PRIMARY KEY (transaction_hash, log_index),
    UNIQUE (block_number, log_index),
    FOREIGN KEY (chunk_index) REFERENCES action_chunks(chunk_index),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE transaction_bundles (
    transaction_hash TEXT PRIMARY KEY CHECK (
        length(transaction_hash) = 66
        AND substr(transaction_hash, 1, 2) = '0x'
    ),
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    transaction_json TEXT NOT NULL CHECK (length(transaction_json) > 0),
    receipt_json TEXT NOT NULL CHECK (length(receipt_json) > 0),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    UNIQUE (block_number, transaction_index),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE action_bundle_fetches (
    transaction_hash TEXT PRIMARY KEY,
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    FOREIGN KEY (transaction_hash) REFERENCES transaction_bundles(transaction_hash)
) STRICT;

CREATE TABLE relevant_transfer_bundle_fetches (
    transaction_hash TEXT PRIMARY KEY,
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    FOREIGN KEY (transaction_hash) REFERENCES transaction_bundles(transaction_hash)
) STRICT;

CREATE TABLE position_resolutions (
    token_id TEXT NOT NULL,
    query_block INTEGER NOT NULL CHECK (query_block >= 0),
    query_block_hash TEXT NOT NULL CHECK (
        length(query_block_hash) = 66
        AND substr(query_block_hash, 1, 2) = '0x'
    ),
    found INTEGER NOT NULL CHECK (found IN (0,1)),
    pool_id TEXT,
    tick_lower INTEGER,
    tick_upper INTEGER,
    liquidity_after TEXT,
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    PRIMARY KEY (token_id, query_block),
    CHECK (
        (found = 0 AND pool_id IS NULL AND tick_lower IS NULL
            AND tick_upper IS NULL AND liquidity_after IS NULL)
        OR
        (found = 1 AND pool_id IS NOT NULL AND tick_lower IS NOT NULL
            AND tick_upper IS NOT NULL AND liquidity_after IS NOT NULL)
    ),
    FOREIGN KEY (query_block, query_block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE decoded_action_transactions (
    transaction_hash TEXT PRIMARY KEY,
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    action_count INTEGER NOT NULL CHECK (action_count >= 0),
    decoder_state_sha256 TEXT NOT NULL CHECK (length(decoder_state_sha256) = 64),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    FOREIGN KEY (transaction_hash)
        REFERENCES action_bundle_fetches(transaction_hash),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash),
    UNIQUE (block_number, transaction_index)
) STRICT;

CREATE TABLE decoded_relevant_transfer_transactions (
    transaction_hash TEXT PRIMARY KEY,
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    ownership_count INTEGER NOT NULL CHECK (ownership_count >= 0),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    FOREIGN KEY (transaction_hash)
        REFERENCES relevant_transfer_bundle_fetches(transaction_hash),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash),
    UNIQUE (block_number, transaction_index)
) STRICT;

CREATE TABLE decoded_actions (
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_hash TEXT NOT NULL,
    token_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (length(action_type) > 0),
    action_json TEXT NOT NULL CHECK (length(action_json) > 0),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    PRIMARY KEY (block_number, log_index, event_order),
    FOREIGN KEY (transaction_hash)
        REFERENCES decoded_action_transactions(transaction_hash),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE ownership_events (
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_hash TEXT NOT NULL,
    token_id TEXT NOT NULL,
    previous_owner TEXT CHECK (
        previous_owner IS NULL
        OR (length(previous_owner) = 42 AND substr(previous_owner, 1, 2) = '0x')
    ),
    new_owner TEXT CHECK (
        new_owner IS NULL
        OR (length(new_owner) = 42 AND substr(new_owner, 1, 2) = '0x')
    ),
    PRIMARY KEY (block_number, log_index, event_order),
    FOREIGN KEY (transaction_hash)
        REFERENCES decoded_relevant_transfer_transactions(transaction_hash),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE decoder_token_state (
    token_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL,
    tick_lower INTEGER NOT NULL,
    tick_upper INTEGER NOT NULL,
    liquidity_after TEXT NOT NULL,
    last_block_number INTEGER NOT NULL CHECK (last_block_number >= 0),
    last_log_index INTEGER NOT NULL CHECK (last_log_index >= 0),
    last_event_order INTEGER NOT NULL CHECK (last_event_order >= 0),
    updated_generation INTEGER NOT NULL CHECK (updated_generation >= 1)
) STRICT;

CREATE TABLE token_position_keys (
    token_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL CHECK (
        length(pool_id) = 66 AND substr(pool_id, 1, 2) = '0x'
    ),
    tick_lower INTEGER NOT NULL,
    tick_upper INTEGER NOT NULL,
    salt TEXT NOT NULL CHECK (
        length(salt) = 66 AND substr(salt, 1, 2) = '0x'
    ),
    mint_block_number INTEGER NOT NULL CHECK (mint_block_number >= 0),
    mint_log_index INTEGER NOT NULL CHECK (mint_log_index >= 0),
    mint_event_order INTEGER NOT NULL CHECK (mint_event_order >= 0),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    FOREIGN KEY (mint_block_number, mint_log_index, mint_event_order)
        REFERENCES decoded_actions(block_number, log_index, event_order)
) STRICT;

CREATE TABLE frozen_token_set (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    token_ids_sha256 TEXT NOT NULL CHECK (length(token_ids_sha256) = 64),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1)
) STRICT;

CREATE TABLE frozen_token_ids (
    token_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL UNIQUE CHECK (ordinal >= 0),
    frozen_set_singleton INTEGER NOT NULL DEFAULT 1 CHECK (frozen_set_singleton = 1),
    FOREIGN KEY (frozen_set_singleton) REFERENCES frozen_token_set(singleton)
) STRICT;

CREATE TABLE transfer_chunks (
    chunk_index INTEGER PRIMARY KEY CHECK (chunk_index >= 0),
    start_block INTEGER NOT NULL UNIQUE CHECK (start_block >= 0),
    end_block INTEGER NOT NULL UNIQUE CHECK (end_block >= start_block),
    status TEXT NOT NULL CHECK (status IN ('pending','completed')),
    unfiltered_count INTEGER NOT NULL DEFAULT 0 CHECK (unfiltered_count >= 0),
    unfiltered_sha256 TEXT,
    relevant_count INTEGER NOT NULL DEFAULT 0 CHECK (relevant_count >= 0),
    completed_generation INTEGER
        CHECK (completed_generation IS NULL OR completed_generation >= 1),
    CHECK (
        (status = 'pending' AND unfiltered_count = 0
            AND unfiltered_sha256 IS NULL AND relevant_count = 0
            AND completed_generation IS NULL)
        OR (status = 'completed' AND unfiltered_sha256 IS NOT NULL
            AND length(unfiltered_sha256) = 64
            AND relevant_count <= unfiltered_count
            AND completed_generation IS NOT NULL)
    )
) STRICT;

CREATE TABLE relevant_transfer_witnesses (
    source TEXT NOT NULL CHECK (source = 'position_transfer'),
    transaction_hash TEXT NOT NULL CHECK (
        length(transaction_hash) = 66
        AND substr(transaction_hash, 1, 2) = '0x'
    ),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    chunk_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    address TEXT NOT NULL CHECK (
        length(address) = 42 AND substr(address, 1, 2) = '0x'
    ),
    topics_json TEXT NOT NULL CHECK (length(topics_json) > 0),
    data TEXT NOT NULL CHECK (
        length(data) >= 2 AND substr(data, 1, 2) = '0x'
    ),
    token_id TEXT NOT NULL,
    PRIMARY KEY (transaction_hash, log_index),
    UNIQUE (block_number, log_index),
    FOREIGN KEY (chunk_index) REFERENCES transfer_chunks(chunk_index),
    FOREIGN KEY (token_id) REFERENCES frozen_token_ids(token_id),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE replay_input (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    path TEXT NOT NULL CHECK (length(path) > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    byte_length INTEGER NOT NULL CHECK (byte_length > 0),
    row_count INTEGER NOT NULL CHECK (row_count > 0),
    header_sha256 TEXT NOT NULL CHECK (length(header_sha256) = 64),
    parser_version TEXT NOT NULL CHECK (length(parser_version) > 0),
    price_semantics_sha256 TEXT NOT NULL CHECK (length(price_semantics_sha256) = 64),
    chain TEXT NOT NULL CHECK (length(chain) > 0),
    pool_id TEXT NOT NULL CHECK (
        length(pool_id) = 66 AND substr(pool_id, 1, 2) = '0x'
    ),
    first_block INTEGER NOT NULL CHECK (first_block >= 0),
    last_block INTEGER NOT NULL CHECK (last_block >= first_block),
    first_timestamp_ms INTEGER NOT NULL CHECK (first_timestamp_ms >= 0),
    last_timestamp_ms INTEGER NOT NULL CHECK (last_timestamp_ms >= first_timestamp_ms),
    price_event_count INTEGER NOT NULL CHECK (
        price_event_count > 0 AND price_event_count <= row_count
    ),
    price_events_sha256 TEXT NOT NULL CHECK (length(price_events_sha256) = 64),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1)
) STRICT;

CREATE TABLE action_price_bindings (
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    event_time_sqrt_price_x96 TEXT NOT NULL,
    event_time_tick INTEGER NOT NULL,
    event_time_state_source TEXT NOT NULL CHECK (
        event_time_state_source IN (
            'self_event','prior_event','prior_block','same_block_prior_event'
        )
    ),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    PRIMARY KEY (block_number, log_index, event_order),
    FOREIGN KEY (block_number, log_index, event_order)
        REFERENCES decoded_actions(block_number, log_index, event_order)
) STRICT;

CREATE INDEX action_chunks_status_order
    ON action_chunks(status, chunk_index);
CREATE INDEX action_witnesses_candidate_order
    ON action_witnesses(transaction_hash, block_number, log_index);
CREATE INDEX transaction_bundles_decode_order
    ON transaction_bundles(block_number, transaction_index, transaction_hash);
CREATE INDEX decoded_action_transactions_location_order
    ON decoded_action_transactions(block_number, transaction_index, transaction_hash);
CREATE INDEX decoded_relevant_transfer_transactions_location_order
    ON decoded_relevant_transfer_transactions(
        block_number, transaction_index, transaction_hash
    );
CREATE INDEX decoded_actions_transaction_order
    ON decoded_actions(transaction_hash, block_number, log_index, event_order);
CREATE INDEX decoded_actions_token_order
    ON decoded_actions(token_id, block_number, log_index, event_order);
CREATE INDEX ownership_events_token_order
    ON ownership_events(token_id, block_number, log_index, event_order);
CREATE INDEX transfer_chunks_status_order
    ON transfer_chunks(status, chunk_index);
CREATE INDEX relevant_transfer_witnesses_candidate_order
    ON relevant_transfer_witnesses(transaction_hash, block_number, log_index);
CREATE INDEX phase_state_status
    ON phase_state(status, phase);
"""
