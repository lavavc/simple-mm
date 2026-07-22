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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

CHECKPOINT_APPLICATION_ID = 1_280_330_819
CHECKPOINT_SCHEMA_VERSION = 1
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


class CheckpointContractError(ValueError):
    """Raised when a checkpoint path or persisted contract is invalid."""


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
    if identity.start_block < 0 or identity.end_block < identity.start_block:
        raise CheckpointContractError("checkpoint identity block range is invalid")
    if identity.chunk_size <= 0:
        raise CheckpointContractError("checkpoint identity chunk size must be positive")
    if identity.endpoint.start.block_number != identity.start_block:
        raise CheckpointContractError("checkpoint identity endpoint start block is invalid")
    if identity.endpoint.end.block_number != identity.end_block:
        raise CheckpointContractError("checkpoint identity endpoint end block is invalid")
    if identity.endpoint.start.timestamp_ms < 0 or identity.endpoint.end.timestamp_ms < 0:
        raise CheckpointContractError("checkpoint identity endpoint timestamp is invalid")
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
    *,
    byte_length: int,
    label: str,
    prefix: bool = True,
) -> None:
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
