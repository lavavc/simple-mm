"""Durable operational state for verified V4 LP-ledger exports."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from web3 import Web3

from research.backtester.v4_event_replay import (
    PoolStateSnapshot,
    ReplayedEvent,
    ReplayEvent,
    attach_event_time_state,
)
from research.backtester.v4_lp_ledger import (
    DecodedLiquidityAction,
    LedgerPositionState,
    OwnershipEvent,
)

CHECKPOINT_APPLICATION_ID = 1_280_330_819
CHECKPOINT_SCHEMA_VERSION = 1
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1
CHECKPOINT_SOURCE_PATHS = (
    "research/backtester/lp_ledger_attribution.py",
    "research/backtester/lp_ledger_checkpoint.py",
    "research/backtester/v4_event_replay.py",
    "research/backtester/v4_export.py",
    "research/backtester/v4_lp_ledger.py",
    "research/scripts/export_v4_lp_ledger.py",
)
_SAFE_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]*", re.ASCII)
_PHASES = (
    "preflight",
    "candidate_discovery",
    "candidate_fetch",
    "chronological_decode",
    "price_event_scan",
    "price_replay",
    "build",
    "publish",
    "succeeded",
)


def _mutation_hook(_stage: Literal["before_commit", "after_commit"]) -> None:
    """Test seam for deterministic crash-boundary assertions."""


Phase = Literal[
    "preflight",
    "candidate_discovery",
    "candidate_fetch",
    "chronological_decode",
    "price_event_scan",
    "price_replay",
    "build",
    "publish",
    "succeeded",
]
RunStatus = Literal["running", "succeeded", "failed", "interrupted"]
DiscoverySource = Literal["pool_modify", "position_transfer"]
ReplayEvidenceType = Literal["initialize", "swap"]
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
    topic0: str
    topic1: str | None


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
class ReplaySeed:
    first_event_block: int
    state: PoolStateSnapshot | None
    state_block_number: int | None
    state_block_hash: str | None


@dataclass(frozen=True)
class ReplayEvidence:
    block_number: int
    log_index: int
    event_order: int
    transaction_index: int
    block_hash: str
    transaction_hash: str
    event_type: ReplayEvidenceType
    sqrt_price_x96: int
    tick: int


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
    actions: tuple[DecodedLiquidityAction, ...]
    ownership_events: tuple[OwnershipEvent, ...]
    replay_events: tuple[ReplayedEvent, ...]
    action_price_bindings: tuple[ActionPriceBinding, ...]
    candidate_hashes: tuple[str, ...]


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
    state_view: str
    token0_address: str
    token1_address: str
    token0_decimals: int
    token1_decimals: int
    fee_rate: str
    invert_price: bool
    start_block: int
    end_block: int
    chunk_size: int
    discovery_topics: tuple[str, ...]
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
    error_code: str | None
    output_published: bool


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
            _validate_checkpoint(connection)
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
        row = self._connection.execute(
            """
            SELECT r.run_id, i.fingerprint_sha256, r.attempt_number,
                   r.generation, r.status, r.phase, r.started_at_utc,
                   r.resumed_at_utc, r.updated_at_utc, r.finished_at_utc,
                   r.error_code, r.output_published
            FROM run_state AS r
            JOIN run_identity AS i ON i.singleton = r.singleton
            WHERE r.singleton = 1
            """
        ).fetchone()
        if row is None:
            raise CheckpointContractError("checkpoint run state is missing")
        return CheckpointSnapshot(
            run_id=cast(str, row["run_id"]),
            identity_sha256=cast(str, row["fingerprint_sha256"]),
            attempt_number=cast(int, row["attempt_number"]),
            generation=cast(int, row["generation"]),
            status=cast(RunStatus, row["status"]),
            phase=cast(Phase, row["phase"]),
            started_at_utc=cast(str, row["started_at_utc"]),
            resumed_at_utc=cast(str, row["resumed_at_utc"]),
            updated_at_utc=cast(str, row["updated_at_utc"]),
            finished_at_utc=cast(str | None, row["finished_at_utc"]),
            error_code=cast(str | None, row["error_code"]),
            output_published=bool(row["output_published"]),
        )

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
            elif phase in ("candidate_fetch", "chronological_decode", "price_replay"):
                _require_phase_evidence_complete(connection, phase)
                if phase == "candidate_fetch":
                    connection.execute(
                        """
                        UPDATE phase_state
                        SET total = ?, updated_generation = ?
                        WHERE phase = 'chronological_decode'
                        """,
                        (_candidate_count(connection), generation),
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

    def incomplete_discovery_chunks(self) -> tuple[BlockRange, ...]:
        return tuple(
            BlockRange(
                index=cast(int, row["chunk_index"]),
                start_block=cast(int, row["start_block"]),
                end_block=cast(int, row["end_block"]),
            )
            for row in self._connection.execute(
                """
                SELECT chunk_index, start_block, end_block
                FROM discovery_chunks
                WHERE status = 'pending'
                ORDER BY chunk_index
                """
            )
        )

    def commit_discovery_chunk(
        self,
        block_range: BlockRange,
        modify: Sequence[DiscoveryWitness],
        transfers: Sequence[DiscoveryWitness],
    ) -> CheckpointSnapshot:
        modify_values = tuple(modify)
        transfer_values = tuple(transfers)
        _validate_discovery_batch(block_range, modify_values, transfer_values)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            _require_pending_range(connection, "discovery_chunks", block_range)
            for witness in (*modify_values, *transfer_values):
                _upsert_chain_block(
                    connection,
                    witness.block_number,
                    witness.block_hash,
                    None,
                    generation,
                )
                connection.execute(
                    """
                    INSERT INTO discovery_witnesses (
                        source, transaction_hash, log_index, chunk_index,
                        block_number, block_hash, transaction_index, address,
                        topic0, topic1
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
                        witness.topic0,
                        witness.topic1,
                    ),
                )
            connection.execute(
                """
                UPDATE discovery_chunks
                SET status = 'completed', modify_witness_count = ?,
                    transfer_witness_count = ?, completed_generation = ?
                WHERE chunk_index = ?
                """,
                (len(modify_values), len(transfer_values), generation, block_range.index),
            )
            completed = _completed_count(connection, "discovery_chunks")
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'candidate_discovery'
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
            if _pending_count(connection, "discovery_chunks") == 0:
                candidate_total = _candidate_count(connection)
                connection.execute(
                    """
                    UPDATE phase_state
                    SET total = ?, updated_generation = ?
                    WHERE phase = 'candidate_fetch'
                    """,
                    (candidate_total, generation),
                )
                _advance_phase(connection, "candidate_discovery", generation)

        return self._mutate("candidate_discovery", operation)

    def candidate_hashes(self) -> tuple[str, ...]:
        return tuple(
            cast(str, row[0])
            for row in self._connection.execute(
                """
                SELECT DISTINCT transaction_hash
                FROM discovery_witnesses
                ORDER BY transaction_hash
                """
            )
        )

    def candidate_witnesses(self, transaction_hash: str) -> tuple[DiscoveryWitness, ...]:
        normalized = _require_lower_hex(transaction_hash, 32, "transaction hash")
        return tuple(
            _witness_from_row(row)
            for row in self._connection.execute(
                """
                SELECT source, block_number, block_hash, transaction_hash,
                       transaction_index, log_index, address, topic0, topic1
                FROM discovery_witnesses
                WHERE transaction_hash = ?
                ORDER BY block_number, log_index, source
                """,
                (normalized,),
            )
        )

    def required_action_witnesses(
        self,
        transaction_hash: str,
    ) -> tuple[DiscoveryWitness, ...]:
        return tuple(
            witness
            for witness in self.candidate_witnesses(transaction_hash)
            if witness.source == "pool_modify"
        )

    def unfetched_candidate_hashes(self) -> tuple[str, ...]:
        return tuple(
            cast(str, row[0])
            for row in self._connection.execute(
                """
                SELECT DISTINCT w.transaction_hash
                FROM discovery_witnesses AS w
                LEFT JOIN candidate_bundles AS b
                  ON b.transaction_hash = w.transaction_hash
                WHERE b.transaction_hash IS NULL
                ORDER BY w.transaction_hash
                """
            )
        )

    def commit_candidate_bundle(self, bundle: CandidateBundle) -> CheckpointSnapshot:
        _validate_candidate_bundle_shape(bundle)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            witnesses = tuple(
                _witness_from_row(row)
                for row in connection.execute(
                    """
                    SELECT source, block_number, block_hash, transaction_hash,
                           transaction_index, log_index, address, topic0, topic1
                    FROM discovery_witnesses
                    WHERE transaction_hash = ?
                    ORDER BY block_number, log_index, source
                    """,
                    (bundle.transaction_hash,),
                )
            )
            if not witnesses:
                raise CheckpointContractError("candidate transaction was not discovered")
            _validate_bundle_against_witnesses(bundle, witnesses)
            _upsert_chain_block(
                connection,
                bundle.block_number,
                bundle.block_hash,
                None,
                generation,
            )
            connection.execute(
                """
                INSERT INTO candidate_bundles (
                    transaction_hash, block_number, block_hash,
                    transaction_index, transaction_json, receipt_json,
                    payload_sha256, committed_generation
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
            completed = cast(
                int,
                connection.execute("SELECT COUNT(*) FROM candidate_bundles").fetchone()[0],
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'candidate_fetch'
                """,
                (
                    completed,
                    _canonical_json({"transaction_hash": bundle.transaction_hash}),
                    generation,
                ),
            )
            if completed == _candidate_count(connection):
                connection.execute(
                    """
                    UPDATE phase_state
                    SET total = ?, updated_generation = ?
                    WHERE phase = 'chronological_decode'
                    """,
                    (completed, generation),
                )
                _advance_phase(connection, "candidate_fetch", generation)

        return self._mutate("candidate_fetch", operation)

    def undecoded_bundles(self) -> tuple[CandidateBundle, ...]:
        return tuple(
            _bundle_from_row(row)
            for row in self._connection.execute(
                """
                SELECT b.transaction_hash, b.block_number, b.block_hash,
                       b.transaction_index, b.transaction_json, b.receipt_json,
                       b.payload_sha256
                FROM candidate_bundles AS b
                LEFT JOIN decoded_transactions AS d
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

    def commit_decoded_transaction(
        self,
        bundle: CandidateBundle,
        headers: Sequence[BlockHeader],
        resolutions: Sequence[PositionResolution],
        state_upserts: Sequence[DecoderStateUpsert],
        state_deletes: Sequence[int],
        actions: Sequence[DecodedLiquidityAction],
        owners: Sequence[OwnershipEvent],
    ) -> CheckpointSnapshot:
        header_values = tuple(headers)
        resolution_values = tuple(resolutions)
        upsert_values = tuple(state_upserts)
        delete_values = tuple(state_deletes)
        action_values = tuple(actions)
        owner_values = tuple(owners)
        _validate_decode_batch(
            bundle,
            header_values,
            resolution_values,
            upsert_values,
            delete_values,
            action_values,
            owner_values,
        )

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            first = connection.execute(
                """
                SELECT b.transaction_hash
                FROM candidate_bundles AS b
                LEFT JOIN decoded_transactions AS d
                  ON d.transaction_hash = b.transaction_hash
                WHERE d.transaction_hash IS NULL
                ORDER BY b.block_number, b.transaction_index, b.transaction_hash
                LIMIT 1
                """
            ).fetchone()
            if first is None or first["transaction_hash"] != bundle.transaction_hash:
                raise CheckpointContractError(
                    "decoded transaction is not the canonical next bundle"
                )
            persisted = connection.execute(
                """
                SELECT transaction_hash, block_number, block_hash,
                       transaction_index, transaction_json, receipt_json,
                       payload_sha256
                FROM candidate_bundles
                WHERE transaction_hash = ?
                """,
                (bundle.transaction_hash,),
            ).fetchone()
            if persisted is None or _bundle_from_row(persisted) != bundle:
                raise CheckpointContractError("decoded candidate bundle does not match checkpoint")
            required_log_indices = tuple(
                cast(int, row[0])
                for row in connection.execute(
                    """
                    SELECT log_index
                    FROM discovery_witnesses
                    WHERE transaction_hash = ? AND source = 'pool_modify'
                    """,
                    (bundle.transaction_hash,),
                )
            )
            _require_modify_action_multiplicity(
                required_log_indices,
                tuple(action.log_index for action in action_values),
            )
            required_transfer_log_indices = tuple(
                cast(int, row[0])
                for row in connection.execute(
                    """
                    SELECT log_index
                    FROM discovery_witnesses
                    WHERE transaction_hash = ? AND source = 'position_transfer'
                    """,
                    (bundle.transaction_hash,),
                )
            )
            _require_transfer_owner_multiplicity(
                required_transfer_log_indices,
                tuple(owner.log_index for owner in owner_values),
            )
            _validate_decoder_state_transition(
                connection,
                resolution_values,
                upsert_values,
                delete_values,
                action_values,
                owner_values,
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
                raise CheckpointContractError("decoded transaction header is missing")
            for resolution in resolution_values:
                _insert_position_resolution(connection, resolution, generation)
            connection.execute(
                """
                INSERT INTO decoded_transactions (
                    transaction_hash, block_number, block_hash,
                    transaction_index, action_count, ownership_count,
                    decoder_state_sha256, committed_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle.transaction_hash,
                    bundle.block_number,
                    bundle.block_hash,
                    bundle.transaction_index,
                    len(action_values),
                    len(owner_values),
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
            state_sha256 = _decoder_state_sha256(connection)
            connection.execute(
                """
                UPDATE decoded_transactions
                SET decoder_state_sha256 = ?
                WHERE transaction_hash = ?
                """,
                (state_sha256, bundle.transaction_hash),
            )
            completed = cast(
                int,
                connection.execute("SELECT COUNT(*) FROM decoded_transactions").fetchone()[0],
            )
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'chronological_decode'
                """,
                (
                    completed,
                    _canonical_json(
                        {
                            "block_number": str(bundle.block_number),
                            "transaction_hash": bundle.transaction_hash,
                            "transaction_index": str(bundle.transaction_index),
                        }
                    ),
                    generation,
                ),
            )
            if completed == _candidate_count(connection):
                _advance_phase(connection, "chronological_decode", generation)

        return self._mutate("chronological_decode", operation)

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

    def incomplete_replay_chunks(self) -> tuple[BlockRange, ...]:
        return tuple(
            BlockRange(
                index=cast(int, row["chunk_index"]),
                start_block=cast(int, row["start_block"]),
                end_block=cast(int, row["end_block"]),
            )
            for row in self._connection.execute(
                """
                SELECT chunk_index, start_block, end_block
                FROM replay_chunks
                WHERE status = 'pending'
                ORDER BY chunk_index
                """
            )
        )

    def commit_replay_chunk(
        self,
        block_range: BlockRange,
        headers: Sequence[BlockHeader],
        events: Sequence[ReplayEvidence],
    ) -> CheckpointSnapshot:
        header_values = tuple(headers)
        event_values = tuple(events)
        _validate_replay_batch(block_range, header_values, event_values)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            _require_pending_range(connection, "replay_chunks", block_range)
            for header in header_values:
                _upsert_chain_block(
                    connection,
                    header.block_number,
                    header.block_hash,
                    header.timestamp_ms,
                    generation,
                )
            for event in event_values:
                _upsert_chain_block(
                    connection,
                    event.block_number,
                    event.block_hash,
                    None,
                    generation,
                )
                connection.execute(
                    """
                    INSERT INTO replay_events (
                        chunk_index, block_number, log_index, event_order,
                        block_hash, transaction_hash, transaction_index,
                        event_type, sqrt_price_x96, tick, committed_generation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        block_range.index,
                        event.block_number,
                        event.log_index,
                        event.event_order,
                        event.block_hash,
                        event.transaction_hash,
                        event.transaction_index,
                        event.event_type,
                        _unsigned_decimal(event.sqrt_price_x96, "sqrt price"),
                        event.tick,
                        generation,
                    ),
                )
            initialize_count = sum(event.event_type == "initialize" for event in event_values)
            swap_count = sum(event.event_type == "swap" for event in event_values)
            connection.execute(
                """
                UPDATE replay_chunks
                SET status = 'completed', initialize_event_count = ?,
                    swap_event_count = ?, completed_generation = ?
                WHERE chunk_index = ?
                """,
                (initialize_count, swap_count, generation, block_range.index),
            )
            completed = _completed_count(connection, "replay_chunks")
            connection.execute(
                """
                UPDATE phase_state
                SET completed = ?, last_durable_json = ?, updated_generation = ?
                WHERE phase = 'price_event_scan'
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
            if _pending_count(connection, "replay_chunks") == 0:
                action_total = cast(
                    int,
                    connection.execute("SELECT COUNT(*) FROM decoded_actions").fetchone()[0],
                )
                connection.execute(
                    """
                    UPDATE phase_state
                    SET total = ?, updated_generation = ?
                    WHERE phase = 'price_replay'
                    """,
                    (action_total, generation),
                )
                _advance_phase(connection, "price_event_scan", generation)

        return self._mutate("price_event_scan", operation)

    def load_replay_events(self) -> tuple[ReplayEvent, ...]:
        return tuple(
            ReplayEvent(
                block_number=cast(int, row["block_number"]),
                log_index=cast(int, row["log_index"]),
                event_order=cast(int, row["event_order"]),
                event_type=cast(str, row["event_type"]),
                sqrt_price_x96=_parse_unsigned_decimal(
                    cast(str, row["sqrt_price_x96"]), "sqrt price"
                ),
                tick=cast(int, row["tick"]),
            )
            for row in self._connection.execute(
                """
                SELECT block_number, log_index, event_order, event_type,
                       sqrt_price_x96, tick
                FROM replay_events
                ORDER BY block_number, log_index, event_order
                """
            )
        )

    def load_replay_seed(self) -> ReplaySeed | None:
        row = self._connection.execute(
            """
            SELECT first_event_block, seed_kind, sqrt_price_x96, tick,
                   state_source, state_block_number, state_block_hash
            FROM replay_seed
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            return None
        state = None
        if row["seed_kind"] == "state":
            state = PoolStateSnapshot(
                sqrt_price_x96=_parse_unsigned_decimal(
                    cast(str, row["sqrt_price_x96"]), "sqrt price"
                ),
                tick=cast(int, row["tick"]),
                source=cast(str, row["state_source"]),
            )
        return ReplaySeed(
            first_event_block=cast(int, row["first_event_block"]),
            state=state,
            state_block_number=cast(int | None, row["state_block_number"]),
            state_block_hash=cast(str | None, row["state_block_hash"]),
        )

    def commit_replay_seed(
        self,
        seed: ReplaySeed,
        header: BlockHeader | None,
    ) -> CheckpointSnapshot:
        _validate_replay_seed(seed, header)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            if connection.execute("SELECT 1 FROM replay_seed").fetchone() is not None:
                raise CheckpointContractError("replay seed is already committed")
            identity_row = connection.execute(
                "SELECT canonical_json FROM run_identity WHERE singleton = 1"
            ).fetchone()
            if identity_row is None:
                raise CheckpointContractError("checkpoint identity is missing")
            identity = cast(
                dict[str, object],
                json.loads(cast(str, identity_row["canonical_json"])),
            )
            earliest_event_block = _earliest_staged_event_block(connection)
            if earliest_event_block is None or seed.first_event_block != earliest_event_block:
                raise CheckpointContractError(
                    "replay seed does not match the earliest staged event"
                )
            if seed.state is None and seed.first_event_block != identity["start_block"]:
                raise CheckpointContractError(
                    "no-state replay seed is permitted only at pool inception"
                )
            if header is not None:
                _upsert_chain_block(
                    connection,
                    header.block_number,
                    header.block_hash,
                    header.timestamp_ms,
                    generation,
                )
            state = seed.state
            connection.execute(
                """
                INSERT INTO replay_seed (
                    singleton, first_event_block, seed_kind, sqrt_price_x96,
                    tick, state_source, state_block_number, state_block_hash,
                    committed_generation
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    seed.first_event_block,
                    "none" if state is None else "state",
                    None
                    if state is None
                    else _unsigned_decimal(state.sqrt_price_x96, "sqrt price"),
                    None if state is None else state.tick,
                    None if state is None else state.source,
                    seed.state_block_number,
                    seed.state_block_hash,
                    generation,
                ),
            )

        return self._mutate("price_replay", operation)

    def commit_action_price_bindings(
        self,
        bindings: Sequence[ActionPriceBinding],
    ) -> CheckpointSnapshot:
        values = tuple(bindings)
        _validate_action_price_bindings(values)

        def operation(connection: sqlite3.Connection, generation: int) -> None:
            action_count = cast(
                int,
                connection.execute("SELECT COUNT(*) FROM decoded_actions").fetchone()[0],
            )
            if action_count and connection.execute("SELECT 1 FROM replay_seed").fetchone() is None:
                raise CheckpointContractError("action price bindings require a replay seed")
            _require_bindings_match_deterministic_replay(connection, values)
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
            if completed > action_count:
                raise CheckpointContractError("price binding count exceeds action count")
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
            if completed == action_count:
                connection.execute(
                    """
                    UPDATE phase_state
                    SET total = ?, updated_generation = ?
                    WHERE phase = 'build'
                    """,
                    (action_count, generation),
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
        prerequisite_statuses = tuple(
            cast(str, row[0])
            for row in self._connection.execute(
                """
                SELECT status
                FROM phase_state
                WHERE phase IN (
                    'preflight','candidate_discovery','candidate_fetch',
                    'chronological_decode','price_event_scan','price_replay'
                )
                ORDER BY phase
                """
            )
        )
        if prerequisite_statuses != ("completed",) * 6:
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
        replayed = tuple(
            ReplayedEvent(
                block_number=action.block_number,
                log_index=action.log_index,
                event_order=action.event_order,
                event_type=action.action_type,
                sqrt_price_x96=None,
                tick=None,
                event_time_sqrt_price_x96=binding_by_key[
                    (action.block_number, action.log_index, action.event_order)
                ].event_time_sqrt_price_x96,
                event_time_tick=binding_by_key[
                    (action.block_number, action.log_index, action.event_order)
                ].event_time_tick,
                event_time_state_source=binding_by_key[
                    (action.block_number, action.log_index, action.event_order)
                ].event_time_state_source,
            )
            for action in actions
        )
        return BuildInputs(
            actions=actions,
            ownership_events=ownership_events,
            replay_events=replayed,
            action_price_bindings=bindings,
            candidate_hashes=self.candidate_hashes(),
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
    if phase == "candidate_fetch":
        if (
            connection.execute(
                """
            SELECT 1
            FROM discovery_witnesses AS w
            LEFT JOIN candidate_bundles AS b ON b.transaction_hash = w.transaction_hash
            WHERE b.transaction_hash IS NULL
            LIMIT 1
            """
            ).fetchone()
            is not None
        ):
            raise CheckpointContractError("candidate fetch evidence is incomplete")
        completed = cast(
            int,
            connection.execute("SELECT COUNT(*) FROM candidate_bundles").fetchone()[0],
        )
    elif phase == "chronological_decode":
        if (
            connection.execute(
                """
            SELECT 1
            FROM candidate_bundles AS b
            LEFT JOIN decoded_transactions AS d ON d.transaction_hash = b.transaction_hash
            WHERE d.transaction_hash IS NULL
            LIMIT 1
            """
            ).fetchone()
            is not None
        ):
            raise CheckpointContractError("chronological decode evidence is incomplete")
        completed = cast(
            int,
            connection.execute("SELECT COUNT(*) FROM decoded_transactions").fetchone()[0],
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


def _validate_discovery_batch(
    block_range: BlockRange,
    modify: tuple[DiscoveryWitness, ...],
    transfers: tuple[DiscoveryWitness, ...],
) -> None:
    _validate_block_range(block_range)
    identities: set[tuple[str, str, int]] = set()
    locations: set[tuple[str, int, int]] = set()
    for expected_source, witnesses in (
        ("pool_modify", modify),
        ("position_transfer", transfers),
    ):
        for witness in witnesses:
            if witness.source != expected_source:
                raise CheckpointContractError("discovery witness source is invalid")
            if not block_range.start_block <= witness.block_number <= block_range.end_block:
                raise CheckpointContractError("discovery witness is outside its chunk")
            _require_nonnegative_int(witness.transaction_index, "transaction index")
            _require_nonnegative_int(witness.log_index, "log index")
            _require_lower_hex(witness.block_hash, 32, "block hash")
            _require_lower_hex(witness.transaction_hash, 32, "transaction hash")
            _require_lower_hex(witness.address, 20, "address")
            _require_lower_hex(witness.topic0, 32, "topic0")
            if witness.topic1 is not None:
                _require_lower_hex(witness.topic1, 32, "topic1")
            identity = (witness.source, witness.transaction_hash, witness.log_index)
            location = (witness.source, witness.block_number, witness.log_index)
            if identity in identities or location in locations:
                raise CheckpointContractError("discovery witness identity is duplicated")
            identities.add(identity)
            locations.add(location)


def _validate_block_range(block_range: BlockRange) -> None:
    _require_nonnegative_int(block_range.index, "chunk index")
    _require_nonnegative_int(block_range.start_block, "chunk start block")
    _require_nonnegative_int(block_range.end_block, "chunk end block")
    if block_range.end_block < block_range.start_block:
        raise CheckpointContractError("chunk range is reversed")


def _require_pending_range(
    connection: sqlite3.Connection,
    table: Literal["discovery_chunks", "replay_chunks"],
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


def _completed_count(
    connection: sqlite3.Connection,
    table: Literal["discovery_chunks", "replay_chunks"],
) -> int:
    return cast(
        int,
        connection.execute(f"SELECT COUNT(*) FROM {table} WHERE status = 'completed'").fetchone()[
            0
        ],
    )


def _pending_count(
    connection: sqlite3.Connection,
    table: Literal["discovery_chunks", "replay_chunks"],
) -> int:
    return cast(
        int,
        connection.execute(f"SELECT COUNT(*) FROM {table} WHERE status = 'pending'").fetchone()[0],
    )


def _candidate_count(connection: sqlite3.Connection) -> int:
    return cast(
        int,
        connection.execute(
            "SELECT COUNT(DISTINCT transaction_hash) FROM discovery_witnesses"
        ).fetchone()[0],
    )


def _earliest_staged_event_block(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        """
        SELECT MIN(block_number)
        FROM (
            SELECT block_number FROM decoded_actions
            UNION ALL
            SELECT block_number FROM replay_events
        )
        """
    ).fetchone()
    return None if row is None or row[0] is None else cast(int, row[0])


def _witness_from_row(row: sqlite3.Row) -> DiscoveryWitness:
    return DiscoveryWitness(
        source=cast(DiscoverySource, row["source"]),
        block_number=cast(int, row["block_number"]),
        block_hash=_require_lower_hex(cast(str, row["block_hash"]), 32, "block hash"),
        transaction_hash=_require_lower_hex(
            cast(str, row["transaction_hash"]), 32, "transaction hash"
        ),
        transaction_index=cast(int, row["transaction_index"]),
        log_index=cast(int, row["log_index"]),
        address=_require_lower_hex(cast(str, row["address"]), 20, "address"),
        topic0=_require_lower_hex(cast(str, row["topic0"]), 32, "topic0"),
        topic1=(
            None
            if row["topic1"] is None
            else _require_lower_hex(cast(str, row["topic1"]), 32, "topic1")
        ),
    )


def _validate_candidate_bundle_shape(bundle: CandidateBundle) -> None:
    _require_lower_hex(bundle.transaction_hash, 32, "transaction hash")
    _require_lower_hex(bundle.block_hash, 32, "block hash")
    _require_nonnegative_int(bundle.block_number, "block number")
    _require_nonnegative_int(bundle.transaction_index, "transaction index")
    _require_lower_hex(bundle.payload_sha256, 32, "candidate payload digest", prefix=False)
    expected_digest = candidate_payload_sha256(bundle.transaction_json, bundle.receipt_json)
    if bundle.payload_sha256 != expected_digest:
        raise CheckpointContractError("candidate payload digest does not match")


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


def _receipt_log_matches_witness(log: object, witness: DiscoveryWitness) -> bool:
    if not isinstance(log, dict):
        return False
    topics = log.get("topics")
    expected_topics = [
        witness.topic0,
        *([] if witness.topic1 is None else [witness.topic1]),
    ]
    return (
        log.get("address") == witness.address
        and log.get("blockHash") == witness.block_hash
        and log.get("transactionHash") == witness.transaction_hash
        and log.get("transactionIndex") == str(witness.transaction_index)
        and log.get("logIndex") == str(witness.log_index)
        and isinstance(topics, list)
        and topics[: len(expected_topics)] == expected_topics
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
    owners: tuple[OwnershipEvent, ...],
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
    ownership_burn_ids = {owner.token_id for owner in owners if owner.new_owner is None}
    for token_id in delete_ids:
        final_action = final_action_by_token.get(token_id)
        has_burn_action = final_action is not None and final_action.action_type in (
            "burn",
            "burn_collect",
        )
        if not has_burn_action and token_id not in ownership_burn_ids:
            raise CheckpointContractError("decoder state delete has no current burn evidence")
    owner_keys: set[tuple[int, int, int]] = set()
    for owner in owners:
        if owner.block_number != bundle.block_number:
            raise CheckpointContractError("ownership event does not match its transaction")
        _require_nonnegative_int(owner.log_index, "ownership log index")
        _require_nonnegative_int(owner.event_order, "ownership event order")
        owner_key = (owner.block_number, owner.log_index, owner.event_order)
        if owner_key in owner_keys:
            raise CheckpointContractError("ownership event identity is duplicated")
        owner_keys.add(owner_key)
        _unsigned_decimal(owner.token_id, "token id")
        _optional_address(owner.previous_owner, "previous owner")
        _optional_address(owner.new_owner, "new owner")


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


def _require_transfer_owner_multiplicity(
    required_log_indices: Sequence[int],
    owner_log_indices: Sequence[int],
) -> None:
    if tuple(sorted(owner_log_indices)) != tuple(sorted(required_log_indices)):
        raise CheckpointContractError(
            "each PositionManager Transfer witness must map to exactly one ownership event"
        )


def _validate_decoder_state_transition(
    connection: sqlite3.Connection,
    resolutions: Sequence[PositionResolution],
    state_upserts: Sequence[DecoderStateUpsert],
    state_deletes: Sequence[int],
    actions: Sequence[DecodedLiquidityAction],
    owners: Sequence[OwnershipEvent],
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
    ownership_burn_ids = {owner.token_id for owner in owners if owner.new_owner is None}
    touched_ids = set(actions_by_token) | set(upsert_by_token) | delete_ids

    for token_id in touched_ids:
        prior = prior_states.get(token_id)
        state = prior if prior is not None else resolved_states.get(token_id)
        for action in actions_by_token.get(token_id, []):
            state = _apply_action_to_decoder_state(state, action)

        if token_id in delete_ids:
            if state is None or state.liquidity_after != 0:
                raise CheckpointContractError("decoder state delete does not end at zero liquidity")
            if token_id not in ownership_burn_ids and not any(
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
    return LedgerPositionState(
        pool_id=state.pool_id,
        tick_lower=state.tick_lower,
        tick_upper=state.tick_upper,
        liquidity_after=max(state.liquidity_after + action.liquidity_delta, 0),
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
    keys: set[tuple[int, int, int]] = set()
    for binding in bindings:
        _require_nonnegative_int(binding.block_number, "binding block number")
        _require_nonnegative_int(binding.log_index, "binding log index")
        _require_nonnegative_int(binding.event_order, "binding event order")
        _unsigned_decimal(binding.event_time_sqrt_price_x96, "event-time sqrt price")
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


def _require_bindings_match_deterministic_replay(
    connection: sqlite3.Connection,
    bindings: Sequence[ActionPriceBinding],
) -> None:
    if not bindings:
        return
    expected = {
        (binding.block_number, binding.log_index, binding.event_order): binding
        for binding in _deterministic_action_price_bindings(connection)
    }
    for binding in bindings:
        key = (binding.block_number, binding.log_index, binding.event_order)
        if expected.get(key) != binding:
            raise CheckpointContractError(
                "action price binding does not match deterministic replay"
            )


def _deterministic_action_price_bindings(
    connection: sqlite3.Connection,
) -> tuple[ActionPriceBinding, ...]:
    seed_row = connection.execute(
        """
        SELECT seed_kind, sqrt_price_x96, tick, state_source
        FROM replay_seed
        WHERE singleton = 1
        """
    ).fetchone()
    if seed_row is None:
        raise CheckpointContractError("deterministic replay requires a staged seed")
    initial_state = None
    if seed_row["seed_kind"] == "state":
        initial_state = PoolStateSnapshot(
            sqrt_price_x96=_parse_unsigned_decimal(
                cast(str, seed_row["sqrt_price_x96"]),
                "sqrt price",
            ),
            tick=cast(int, seed_row["tick"]),
            source=cast(str, seed_row["state_source"]),
        )
    price_events = tuple(
        ReplayEvent(
            block_number=cast(int, row["block_number"]),
            log_index=cast(int, row["log_index"]),
            event_order=cast(int, row["event_order"]),
            event_type=cast(str, row["event_type"]),
            sqrt_price_x96=_parse_unsigned_decimal(
                cast(str, row["sqrt_price_x96"]),
                "sqrt price",
            ),
            tick=cast(int, row["tick"]),
        )
        for row in connection.execute(
            """
            SELECT block_number, log_index, event_order, event_type,
                   sqrt_price_x96, tick
            FROM replay_events
            """
        )
    )
    action_events = tuple(
        ReplayEvent(
            block_number=cast(int, row["block_number"]),
            log_index=cast(int, row["log_index"]),
            event_order=cast(int, row["event_order"]),
            event_type=cast(str, row["action_type"]),
            sqrt_price_x96=None,
            tick=None,
        )
        for row in connection.execute(
            """
            SELECT block_number, log_index, event_order, action_type
            FROM decoded_actions
            """
        )
    )
    action_keys = {
        (event.block_number, event.log_index, event.event_order) for event in action_events
    }
    price_keys = {
        (event.block_number, event.log_index, event.event_order) for event in price_events
    }
    if action_keys & price_keys:
        raise CheckpointContractError("replay and action event identities overlap")
    try:
        replayed = attach_event_time_state((*price_events, *action_events), initial_state)
    except ValueError as exc:
        raise CheckpointContractError("deterministic action price replay failed") from exc
    return tuple(
        ActionPriceBinding(
            block_number=event.block_number,
            log_index=event.log_index,
            event_order=event.event_order,
            event_time_sqrt_price_x96=event.event_time_sqrt_price_x96,
            event_time_tick=event.event_time_tick,
            event_time_state_source=cast(
                EventTimeStateSource,
                event.event_time_state_source,
            ),
        )
        for event in replayed
        if (event.block_number, event.log_index, event.event_order) in action_keys
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


def _validate_replay_batch(
    block_range: BlockRange,
    headers: tuple[BlockHeader, ...],
    events: tuple[ReplayEvidence, ...],
) -> None:
    _validate_block_range(block_range)
    header_by_number: dict[int, BlockHeader] = {}
    for header in headers:
        _validate_header(header)
        if not block_range.start_block <= header.block_number <= block_range.end_block:
            raise CheckpointContractError("replay header is outside its chunk")
        if header.block_number in header_by_number:
            raise CheckpointContractError("replay header block is duplicated")
        header_by_number[header.block_number] = header
    keys: set[tuple[int, int, int]] = set()
    for event in events:
        if not block_range.start_block <= event.block_number <= block_range.end_block:
            raise CheckpointContractError("replay event is outside its chunk")
        if event.event_type not in ("initialize", "swap"):
            raise CheckpointContractError("replay event type is invalid")
        _require_lower_hex(event.block_hash, 32, "block hash")
        _require_lower_hex(event.transaction_hash, 32, "transaction hash")
        _require_nonnegative_int(event.transaction_index, "transaction index")
        _require_nonnegative_int(event.log_index, "log index")
        _require_nonnegative_int(event.event_order, "event order")
        _unsigned_decimal(event.sqrt_price_x96, "sqrt price")
        _require_signed_sqlite_int(event.tick, "replay event tick")
        event_header = header_by_number.get(event.block_number)
        if event_header is None or event_header.block_hash != event.block_hash:
            raise CheckpointContractError("replay event header is missing or conflicting")
        key = (event.block_number, event.log_index, event.event_order)
        if key in keys:
            raise CheckpointContractError("replay event identity is duplicated")
        keys.add(key)


def _validate_replay_seed(seed: ReplaySeed, header: BlockHeader | None) -> None:
    _require_nonnegative_int(seed.first_event_block, "first event block")
    if seed.state is None:
        if (
            header is not None
            or seed.state_block_number is not None
            or seed.state_block_hash is not None
        ):
            raise CheckpointContractError("no-state replay seed cannot include a header")
        return
    if header is None:
        raise CheckpointContractError("state replay seed requires a header")
    _validate_header(header)
    if (
        seed.state.source != "prior_block"
        or seed.state_block_number != seed.first_event_block - 1
        or header.block_number != seed.state_block_number
        or seed.state_block_hash != header.block_hash
    ):
        raise CheckpointContractError("state replay seed header is inconsistent")
    _unsigned_decimal(seed.state.sqrt_price_x96, "sqrt price")
    _require_signed_sqlite_int(seed.state.tick, "replay seed tick")
    _require_lower_hex(seed.state_block_hash, 32, "state block hash")


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
        ("discovery endpoint start hash", identity.endpoint.start.block_hash),
        ("discovery endpoint end hash", identity.endpoint.end.block_hash),
        *(
            (f"discovery topic {index}", topic)
            for index, topic in enumerate(identity.discovery_topics)
        ),
    ):
        _require_lower_hex(value, byte_length=32, label=label)
    for label, value in (
        ("PoolManager", identity.pool_manager),
        ("PositionManager", identity.position_manager),
        ("StateView", identity.state_view),
        ("token0", identity.token0_address),
        ("token1", identity.token1_address),
    ):
        _require_lower_hex(value, byte_length=20, label=label)
    if not identity.discovery_topics:
        raise CheckpointContractError("checkpoint identity discovery topics are empty")
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
        "candidate_discovery": ("chunks", len(chunks)),
        "candidate_fetch": ("transactions", None),
        "chronological_decode": ("transactions", None),
        "price_event_scan": ("chunks", len(chunks)),
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
            values = (index, start_block, end_block, "pending", 0, 0, None)
            connection.execute(
                "INSERT INTO discovery_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            connection.execute(
                "INSERT INTO replay_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
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
            raise CheckpointContractError("checkpoint schema does not match version 1")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CheckpointContractError("checkpoint schema foreign keys are invalid")
        _validate_foundation_rows(connection)
    except CheckpointContractError:
        raise
    except sqlite3.DatabaseError as exc:
        raise CheckpointContractError("checkpoint schema or integrity check failed") from exc


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
    _validate_staged_evidence(connection, payload)


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
    for table in ("discovery_chunks", "replay_chunks"):
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
    _validate_discovery_counts(connection)
    _validate_replay_counts(connection)


def _validate_discovery_counts(connection: sqlite3.Connection) -> None:
    observed = {
        (cast(int, row["chunk_index"]), cast(str, row["source"])): cast(int, row["count"])
        for row in connection.execute(
            """
            SELECT chunk_index, source, COUNT(*) AS count
            FROM discovery_witnesses
            GROUP BY chunk_index, source
            """
        )
    }
    for row in connection.execute(
        """
        SELECT chunk_index, status, modify_witness_count, transfer_witness_count
        FROM discovery_chunks
        """
    ):
        chunk_index = cast(int, row["chunk_index"])
        actual_modify = observed.get((chunk_index, "pool_modify"), 0)
        actual_transfer = observed.get((chunk_index, "position_transfer"), 0)
        if row["status"] == "pending" and (actual_modify or actual_transfer):
            raise CheckpointContractError("checkpoint pending discovery chunk has evidence")
        if (
            row["modify_witness_count"] != actual_modify
            or row["transfer_witness_count"] != actual_transfer
        ):
            raise CheckpointContractError("checkpoint discovery witness count is invalid")


def _validate_replay_counts(connection: sqlite3.Connection) -> None:
    observed = {
        (cast(int, row["chunk_index"]), cast(str, row["event_type"])): cast(int, row["count"])
        for row in connection.execute(
            """
            SELECT chunk_index, event_type, COUNT(*) AS count
            FROM replay_events
            GROUP BY chunk_index, event_type
            """
        )
    }
    for row in connection.execute(
        """
        SELECT chunk_index, status, initialize_event_count, swap_event_count
        FROM replay_chunks
        """
    ):
        chunk_index = cast(int, row["chunk_index"])
        actual_initialize = observed.get((chunk_index, "initialize"), 0)
        actual_swap = observed.get((chunk_index, "swap"), 0)
        if row["status"] == "pending" and (actual_initialize or actual_swap):
            raise CheckpointContractError("checkpoint pending replay chunk has evidence")
        if (
            row["initialize_event_count"] != actual_initialize
            or row["swap_event_count"] != actual_swap
        ):
            raise CheckpointContractError("checkpoint replay event count is invalid")


def _validate_staged_evidence(
    connection: sqlite3.Connection,
    identity_payload: dict[str, object],
) -> None:
    try:
        _validate_staged_evidence_inner(connection, identity_payload)
    except CheckpointContractError as exc:
        raise CheckpointContractError("checkpoint staged evidence is invalid") from exc


def _validate_staged_evidence_inner(
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

    misplaced_discovery = connection.execute(
        """
        SELECT 1
        FROM discovery_witnesses AS w
        JOIN discovery_chunks AS c ON c.chunk_index = w.chunk_index
        WHERE w.block_number < c.start_block OR w.block_number > c.end_block
        LIMIT 1
        """
    ).fetchone()
    if misplaced_discovery is not None:
        raise CheckpointContractError("discovery evidence is outside its chunk")
    for row in connection.execute(
        """
        SELECT source, block_number, block_hash, transaction_hash,
               transaction_index, log_index, address, topic0, topic1
        FROM discovery_witnesses
        ORDER BY source, transaction_hash, log_index
        """
    ):
        witness = _witness_from_row(row)
        if witness.source not in ("pool_modify", "position_transfer"):
            raise CheckpointContractError("discovery source is invalid")
    discovered_hashes = tuple(
        cast(str, row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT transaction_hash
            FROM discovery_witnesses
            ORDER BY transaction_hash
            """
        )
    )

    candidate_rows = tuple(
        connection.execute(
            """
            SELECT transaction_hash, block_number, block_hash,
                   transaction_index, transaction_json, receipt_json,
                   payload_sha256, committed_generation
            FROM candidate_bundles
            ORDER BY block_number, transaction_index, transaction_hash
            """
        )
    )
    candidate_hashes: list[str] = []
    for row in candidate_rows:
        _require_generation(cast(int, row["committed_generation"]), generation)
        bundle = _bundle_from_row(row)
        witnesses = tuple(
            _witness_from_row(witness_row)
            for witness_row in connection.execute(
                """
                SELECT source, block_number, block_hash, transaction_hash,
                       transaction_index, log_index, address, topic0, topic1
                FROM discovery_witnesses
                WHERE transaction_hash = ?
                ORDER BY block_number, log_index, source
                """,
                (bundle.transaction_hash,),
            )
        )
        if not witnesses:
            raise CheckpointContractError("candidate has no discovery evidence")
        _validate_bundle_against_witnesses(bundle, witnesses)
        candidate_hashes.append(bundle.transaction_hash)
    run_phase = cast(
        str,
        connection.execute("SELECT phase FROM run_state WHERE singleton = 1").fetchone()[0],
    )
    if _PHASES.index(run_phase) > _PHASES.index("candidate_fetch") and (
        len(candidate_hashes) != len(discovered_hashes)
        or set(candidate_hashes) != set(discovered_hashes)
    ):
        raise CheckpointContractError("candidate bundle set differs from discovery union")

    decoded_rows = tuple(
        connection.execute(
            """
            SELECT d.transaction_hash, d.block_number, d.block_hash,
                   d.transaction_index, d.action_count, d.ownership_count,
                   d.decoder_state_sha256, d.committed_generation
            FROM decoded_transactions AS d
            ORDER BY d.block_number, d.transaction_index, d.transaction_hash
            """
        )
    )
    decoded_hashes = [cast(str, row["transaction_hash"]) for row in decoded_rows]
    if decoded_hashes != candidate_hashes[: len(decoded_hashes)]:
        raise CheckpointContractError("decoded transaction cursor is not a canonical prefix")
    for row in decoded_rows:
        _require_generation(cast(int, row["committed_generation"]), generation)
        _require_lower_hex(cast(str, row["block_hash"]), 32, "block hash")
        _require_lower_hex(
            cast(str, row["decoder_state_sha256"]),
            32,
            "decoder state digest",
            prefix=False,
        )
        candidate = connection.execute(
            """
            SELECT block_number, block_hash, transaction_index
            FROM candidate_bundles
            WHERE transaction_hash = ?
            """,
            (row["transaction_hash"],),
        ).fetchone()
        if candidate is None or tuple(candidate) != (
            row["block_number"],
            row["block_hash"],
            row["transaction_index"],
        ):
            raise CheckpointContractError("decoded transaction location changed")
        action_count = cast(
            int,
            connection.execute(
                "SELECT COUNT(*) FROM decoded_actions WHERE transaction_hash = ?",
                (row["transaction_hash"],),
            ).fetchone()[0],
        )
        owner_count = cast(
            int,
            connection.execute(
                "SELECT COUNT(*) FROM ownership_events WHERE transaction_hash = ?",
                (row["transaction_hash"],),
            ).fetchone()[0],
        )
        if action_count != row["action_count"] or owner_count != row["ownership_count"]:
            raise CheckpointContractError("decoded transaction child counts changed")
        required_logs = tuple(
            cast(int, witness_row[0])
            for witness_row in connection.execute(
                """
                SELECT log_index
                FROM discovery_witnesses
                WHERE transaction_hash = ? AND source = 'pool_modify'
                """,
                (row["transaction_hash"],),
            )
        )
        represented_logs = tuple(
            cast(int, action_row[0])
            for action_row in connection.execute(
                """
                SELECT log_index
                FROM decoded_actions
                WHERE transaction_hash = ?
                ORDER BY log_index, event_order
                """,
                (row["transaction_hash"],),
            )
        )
        _require_modify_action_multiplicity(required_logs, represented_logs)
        required_transfer_logs = tuple(
            cast(int, witness_row[0])
            for witness_row in connection.execute(
                """
                SELECT log_index
                FROM discovery_witnesses
                WHERE transaction_hash = ? AND source = 'position_transfer'
                """,
                (row["transaction_hash"],),
            )
        )
        represented_owner_logs = tuple(
            cast(int, owner_row[0])
            for owner_row in connection.execute(
                """
                SELECT log_index
                FROM ownership_events
                WHERE transaction_hash = ?
                ORDER BY log_index, event_order
                """,
                (row["transaction_hash"],),
            )
        )
        _require_transfer_owner_multiplicity(
            required_transfer_logs,
            represented_owner_logs,
        )

    _validate_persisted_actions(connection)
    _validate_persisted_ownership(connection)
    _validate_persisted_position_resolutions(connection, generation)
    _validate_persisted_decoder_state(connection, decoded_rows, generation)
    _validate_persisted_replay(connection, identity_payload, generation)
    _validate_persisted_bindings(connection, generation)
    _validate_staged_phase_counts(connection)


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


def _validate_persisted_actions(connection: sqlite3.Connection) -> None:
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


def _validate_persisted_replay(
    connection: sqlite3.Connection,
    identity_payload: dict[str, object],
    generation: int,
) -> None:
    if (
        connection.execute(
            """
        SELECT 1
        FROM replay_events AS e
        JOIN replay_chunks AS c ON c.chunk_index = e.chunk_index
        WHERE e.block_number < c.start_block OR e.block_number > c.end_block
        LIMIT 1
        """
        ).fetchone()
        is not None
    ):
        raise CheckpointContractError("replay evidence is outside its chunk")
    for row in connection.execute(
        """
        SELECT block_number, block_hash, transaction_hash, event_type,
               sqrt_price_x96, committed_generation
        FROM replay_events
        """
    ):
        _require_lower_hex(cast(str, row["block_hash"]), 32, "block hash")
        _require_lower_hex(cast(str, row["transaction_hash"]), 32, "transaction hash")
        if row["event_type"] not in ("initialize", "swap"):
            raise CheckpointContractError("replay event type changed")
        _parse_unsigned_decimal(cast(str, row["sqrt_price_x96"]), "sqrt price")
        _require_generation(cast(int, row["committed_generation"]), generation)
    seed_row = connection.execute(
        """
        SELECT first_event_block, seed_kind, sqrt_price_x96, tick,
               state_source, state_block_number, state_block_hash,
               committed_generation
        FROM replay_seed
        WHERE singleton = 1
        """
    ).fetchone()
    if seed_row is None:
        return
    _require_generation(cast(int, seed_row["committed_generation"]), generation)
    earliest_event_block = _earliest_staged_event_block(connection)
    if earliest_event_block is None or seed_row["first_event_block"] != earliest_event_block:
        raise CheckpointContractError("replay seed moved away from the earliest event")
    if seed_row["seed_kind"] == "none":
        if seed_row["first_event_block"] != identity_payload["start_block"]:
            raise CheckpointContractError("no-state seed moved away from inception")
        return
    seed = ReplaySeed(
        first_event_block=cast(int, seed_row["first_event_block"]),
        state=PoolStateSnapshot(
            sqrt_price_x96=_parse_unsigned_decimal(
                cast(str, seed_row["sqrt_price_x96"]), "sqrt price"
            ),
            tick=cast(int, seed_row["tick"]),
            source=cast(str, seed_row["state_source"]),
        ),
        state_block_number=cast(int, seed_row["state_block_number"]),
        state_block_hash=cast(str, seed_row["state_block_hash"]),
    )
    header_row = connection.execute(
        """
        SELECT block_number, block_hash, timestamp_ms
        FROM chain_blocks
        WHERE block_number = ? AND timestamp_ms IS NOT NULL
        """,
        (seed.state_block_number,),
    ).fetchone()
    if header_row is None:
        raise CheckpointContractError("replay seed header is missing")
    _validate_replay_seed(
        seed,
        BlockHeader(
            cast(int, header_row["block_number"]),
            cast(str, header_row["block_hash"]),
            cast(int, header_row["timestamp_ms"]),
        ),
    )


def _validate_persisted_bindings(
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
    _validate_action_price_bindings(tuple(bindings))
    _require_bindings_match_deterministic_replay(connection, tuple(bindings))
    phase = cast(
        str,
        connection.execute("SELECT phase FROM run_state WHERE singleton = 1").fetchone()[0],
    )
    if _PHASES.index(phase) > _PHASES.index("price_replay"):
        action_keys = {
            tuple(row)
            for row in connection.execute(
                "SELECT block_number, log_index, event_order FROM decoded_actions"
            )
        }
        binding_keys = {
            (binding.block_number, binding.log_index, binding.event_order) for binding in bindings
        }
        if binding_keys != action_keys:
            raise CheckpointContractError("action price bindings are incomplete")


def _validate_staged_phase_counts(connection: sqlite3.Connection) -> None:
    expected = {
        "candidate_discovery": _completed_count(connection, "discovery_chunks"),
        "candidate_fetch": cast(
            int,
            connection.execute("SELECT COUNT(*) FROM candidate_bundles").fetchone()[0],
        ),
        "chronological_decode": cast(
            int,
            connection.execute("SELECT COUNT(*) FROM decoded_transactions").fetchone()[0],
        ),
        "price_event_scan": _completed_count(connection, "replay_chunks"),
        "price_replay": cast(
            int,
            connection.execute("SELECT COUNT(*) FROM action_price_bindings").fetchone()[0],
        ),
    }
    for phase, count in expected.items():
        observed = connection.execute(
            "SELECT completed FROM phase_state WHERE phase = ?",
            (phase,),
        ).fetchone()
        if observed is None or observed["completed"] != count:
            raise CheckpointContractError("staged phase counter changed")
    phase_rows = {
        cast(str, row["phase"]): row
        for row in connection.execute(
            """
            SELECT phase, status, total, updated_generation
            FROM phase_state
            """
        )
    }
    discovered_count = _candidate_count(connection)
    action_count = cast(
        int,
        connection.execute("SELECT COUNT(*) FROM decoded_actions").fetchone()[0],
    )
    dynamic_totals = (
        ("candidate_discovery", "candidate_fetch", discovered_count),
        ("candidate_fetch", "chronological_decode", discovered_count),
        ("price_event_scan", "price_replay", action_count),
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
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
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
        'preflight','candidate_discovery','candidate_fetch',
        'chronological_decode','price_event_scan','price_replay',
        'build','publish','succeeded'
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
        'preflight','candidate_discovery','candidate_fetch',
        'chronological_decode','price_event_scan','price_replay',
        'build','publish','succeeded'
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

CREATE TABLE discovery_chunks (
    chunk_index INTEGER PRIMARY KEY CHECK (chunk_index >= 0),
    start_block INTEGER NOT NULL UNIQUE CHECK (start_block >= 0),
    end_block INTEGER NOT NULL UNIQUE CHECK (end_block >= start_block),
    status TEXT NOT NULL CHECK (status IN ('pending','completed')),
    modify_witness_count INTEGER NOT NULL DEFAULT 0
        CHECK (modify_witness_count >= 0),
    transfer_witness_count INTEGER NOT NULL DEFAULT 0
        CHECK (transfer_witness_count >= 0),
    completed_generation INTEGER
        CHECK (completed_generation IS NULL OR completed_generation >= 1),
    CHECK (
        (status = 'pending' AND modify_witness_count = 0
            AND transfer_witness_count = 0 AND completed_generation IS NULL)
        OR (status = 'completed' AND completed_generation IS NOT NULL)
    )
) STRICT;

CREATE TABLE discovery_witnesses (
    source TEXT NOT NULL CHECK (source IN ('pool_modify','position_transfer')),
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
    topic0 TEXT NOT NULL CHECK (
        length(topic0) = 66 AND substr(topic0, 1, 2) = '0x'
    ),
    topic1 TEXT CHECK (
        topic1 IS NULL
        OR (length(topic1) = 66 AND substr(topic1, 1, 2) = '0x')
    ),
    PRIMARY KEY (source, transaction_hash, log_index),
    UNIQUE (source, block_number, log_index),
    FOREIGN KEY (chunk_index) REFERENCES discovery_chunks(chunk_index),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE candidate_bundles (
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

CREATE TABLE decoded_transactions (
    transaction_hash TEXT PRIMARY KEY,
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    action_count INTEGER NOT NULL CHECK (action_count >= 0),
    ownership_count INTEGER NOT NULL CHECK (ownership_count >= 0),
    decoder_state_sha256 TEXT NOT NULL CHECK (length(decoder_state_sha256) = 64),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    FOREIGN KEY (transaction_hash)
        REFERENCES candidate_bundles(transaction_hash),
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
        REFERENCES decoded_transactions(transaction_hash),
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
        REFERENCES decoded_transactions(transaction_hash),
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

CREATE TABLE replay_chunks (
    chunk_index INTEGER PRIMARY KEY CHECK (chunk_index >= 0),
    start_block INTEGER NOT NULL UNIQUE CHECK (start_block >= 0),
    end_block INTEGER NOT NULL UNIQUE CHECK (end_block >= start_block),
    status TEXT NOT NULL CHECK (status IN ('pending','completed')),
    initialize_event_count INTEGER NOT NULL DEFAULT 0
        CHECK (initialize_event_count >= 0),
    swap_event_count INTEGER NOT NULL DEFAULT 0 CHECK (swap_event_count >= 0),
    completed_generation INTEGER
        CHECK (completed_generation IS NULL OR completed_generation >= 1),
    CHECK (
        (status = 'pending' AND initialize_event_count = 0
            AND swap_event_count = 0 AND completed_generation IS NULL)
        OR (status = 'completed' AND completed_generation IS NOT NULL)
    )
) STRICT;

CREATE TABLE replay_events (
    chunk_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_hash TEXT NOT NULL CHECK (
        length(transaction_hash) = 66
        AND substr(transaction_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    event_type TEXT NOT NULL CHECK (event_type IN ('initialize','swap')),
    sqrt_price_x96 TEXT NOT NULL,
    tick INTEGER NOT NULL,
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    PRIMARY KEY (block_number, log_index, event_order),
    FOREIGN KEY (chunk_index) REFERENCES replay_chunks(chunk_index),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE replay_seed (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    first_event_block INTEGER NOT NULL CHECK (first_event_block >= 0),
    seed_kind TEXT NOT NULL CHECK (seed_kind IN ('none','state')),
    sqrt_price_x96 TEXT,
    tick INTEGER,
    state_source TEXT,
    state_block_number INTEGER,
    state_block_hash TEXT,
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    CHECK (
        (seed_kind = 'none' AND sqrt_price_x96 IS NULL AND tick IS NULL
            AND state_source IS NULL AND state_block_number IS NULL
            AND state_block_hash IS NULL)
        OR
        (seed_kind = 'state' AND sqrt_price_x96 IS NOT NULL AND tick IS NOT NULL
            AND state_source IS NOT NULL AND state_block_number IS NOT NULL
            AND state_block_hash IS NOT NULL)
    ),
    FOREIGN KEY (state_block_number, state_block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
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

CREATE INDEX discovery_chunks_status_order
    ON discovery_chunks(status, chunk_index);
CREATE INDEX discovery_witnesses_candidate_order
    ON discovery_witnesses(transaction_hash, block_number, log_index, source);
CREATE INDEX candidate_bundles_decode_order
    ON candidate_bundles(block_number, transaction_index, transaction_hash);
CREATE INDEX decoded_transactions_location_order
    ON decoded_transactions(block_number, transaction_index, transaction_hash);
CREATE INDEX decoded_actions_transaction_order
    ON decoded_actions(transaction_hash, block_number, log_index, event_order);
CREATE INDEX decoded_actions_token_order
    ON decoded_actions(token_id, block_number, log_index, event_order);
CREATE INDEX ownership_events_token_order
    ON ownership_events(token_id, block_number, log_index, event_order);
CREATE INDEX replay_chunks_status_order
    ON replay_chunks(status, chunk_index);
CREATE INDEX replay_events_canonical_order
    ON replay_events(chunk_index, block_number, log_index, event_order);
CREATE INDEX phase_state_status
    ON phase_state(status, phase);
"""
