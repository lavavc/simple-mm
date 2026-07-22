import json
import os
import sqlite3
import stat
from dataclasses import fields, replace
from pathlib import Path

import pytest

import research.backtester.lp_ledger_checkpoint as checkpoint_module
from research.backtester.lp_ledger_checkpoint import (
    CHECKPOINT_SOURCE_PATHS,
    BlockHeader,
    CheckpointContractError,
    CheckpointPaths,
    EndpointSnapshot,
    LPLedgerCheckpoint,
    RunIdentity,
    acquire_run_lock,
)

_HASH_A = f"0x{'a' * 64}"
_HASH_B = f"0x{'b' * 64}"
_ADDRESS_A = f"0x{'1' * 40}"
_ADDRESS_B = f"0x{'2' * 40}"
_ADDRESS_C = f"0x{'3' * 40}"
_ADDRESS_D = f"0x{'4' * 40}"
_ADDRESS_E = f"0x{'5' * 40}"


def test_checkpoint_paths_are_output_derived(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    assert paths.output == tmp_path / "ledger.csv"
    assert paths.database == tmp_path / "ledger.csv.checkpoint.sqlite3"
    assert paths.wal == tmp_path / "ledger.csv.checkpoint.sqlite3-wal"
    assert paths.shm == tmp_path / "ledger.csv.checkpoint.sqlite3-shm"
    assert paths.progress == tmp_path / "ledger.csv.progress.json"
    assert paths.run_lock == tmp_path / "ledger.csv.run.lock"
    assert paths.publish_lock == tmp_path / "ledger.csv.publish.lock"


def test_checkpoint_paths_normalize_absolute_output_and_reject_non_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert CheckpointPaths.from_output(Path("nested/../ledger.csv")).output == (
        tmp_path / "ledger.csv"
    )
    with pytest.raises(CheckpointContractError, match=r"\.csv"):
        CheckpointPaths.from_output(tmp_path / "ledger.json")


def test_run_identity_is_canonical_and_contains_no_rpc_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "alchemy-secret-that-must-not-be-persisted"
    monkeypatch.setenv("ALCHEMY_KEY", secret)
    identity = _identity(tmp_path / "ledger.csv")

    payload = identity.canonical_bytes()
    assert payload == identity.canonical_bytes()
    assert json.loads(payload) == json.loads(identity.canonical_bytes())
    assert secret.encode() not in payload
    assert identity.sha256() == identity.sha256()
    assert len(identity.sha256()) == 64
    assert {field.name for field in fields(RunIdentity)}.isdisjoint(
        {"rpc_url", "api_key", "headers", "command_line", "exception"}
    )
    for unsafe in (
        replace(identity, exporter_version=f"https://provider.invalid/v2/{secret}"),
        replace(identity, pool=f"Authorization: Bearer {secret}"),
        replace(identity, python_version=f"X-API-Key={secret}"),
    ):
        with pytest.raises(CheckpointContractError, match="identity"):
            unsafe.canonical_bytes()


@pytest.mark.parametrize("mutation", ("missing", "extra", "reordered"))
def test_run_identity_requires_exact_semantic_source_manifest(
    tmp_path: Path,
    mutation: str,
) -> None:
    identity = _identity(tmp_path / "ledger.csv")
    sources = identity.source_sha256
    if mutation == "missing":
        mutated = sources[:-1]
    elif mutation == "extra":
        mutated = tuple(sorted((*sources, ("unexpected.py", "f" * 64))))
    else:
        mutated = tuple(reversed(sources))

    with pytest.raises(CheckpointContractError, match="source"):
        replace(identity, source_sha256=mutated).canonical_bytes()


def test_compatible_resume_increments_attempt_and_generation(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            first = run.snapshot()
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            resumed = run.snapshot()

    assert resumed.run_id == first.run_id
    assert resumed.attempt_number == first.attempt_number + 1
    assert resumed.generation == first.generation + 1


@pytest.mark.parametrize("mutation", ("range", "endpoint", "source", "output"))
def test_incompatible_resume_fails_without_mutating_state(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    original = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, original, fresh=False) as run:
            before = run.snapshot()
        with pytest.raises(CheckpointContractError, match="identity"):
            LPLedgerCheckpoint.create_or_resume(
                paths,
                _mutated_identity(original, mutation),
                fresh=False,
            )
        with LPLedgerCheckpoint.open_status(paths) as run:
            assert run.snapshot() == before


def test_run_lock_contention_precedes_checkpoint_and_progress_mutation(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="already active"):
            with acquire_run_lock(paths):
                raise AssertionError("unreachable")
        assert not paths.database.exists()
        assert not paths.progress.exists()

    assert stat.S_IMODE(paths.run_lock.stat().st_mode) == 0o600


def test_checkpoint_creation_uses_frozen_schema_and_durable_pragmas(
    tmp_path: Path,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(
            paths,
            _identity(paths.output),
            fresh=False,
        ) as run:
            connection = run._connection
            pragmas = {
                name: connection.execute(f"PRAGMA {name}").fetchone()[0]
                for name in (
                    "application_id",
                    "foreign_keys",
                    "journal_mode",
                    "synchronous",
                    "trusted_schema",
                    "user_version",
                )
            }
            objects = tuple(
                connection.execute(
                    """
                    SELECT type, name
                    FROM sqlite_master
                    WHERE type IN ('table', 'view', 'trigger')
                      AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
            )
            tables = tuple(row["name"] for row in objects if row["type"] == "table")
            views = tuple(row["name"] for row in objects if row["type"] == "view")
            triggers = tuple(row["name"] for row in objects if row["type"] == "trigger")
            declared_types = {
                column[2].upper()
                for table in tables
                for column in connection.execute(f'PRAGMA table_info("{table}")')
            }

    assert pragmas == {
        "application_id": 1_280_330_819,
        "foreign_keys": 1,
        "journal_mode": "wal",
        "synchronous": 2,
        "trusted_schema": 0,
        "user_version": 1,
    }
    assert tables == (
        "action_price_bindings",
        "candidate_bundles",
        "chain_blocks",
        "decoded_actions",
        "decoded_transactions",
        "decoder_token_state",
        "discovery_chunks",
        "discovery_witnesses",
        "ownership_events",
        "phase_state",
        "position_resolutions",
        "publication_state",
        "replay_chunks",
        "replay_events",
        "replay_seed",
        "run_identity",
        "run_state",
        "schema_meta",
    )
    assert views == ()
    assert triggers == ()
    assert "REAL" not in declared_types
    assert "BLOB" not in declared_types
    assert stat.S_IMODE(paths.database.stat().st_mode) == 0o600


@pytest.mark.parametrize("drift", ("table", "version", "application_id", "index"))
def test_resume_rejects_schema_or_version_drift(tmp_path: Path, drift: str) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass
        with sqlite3.connect(paths.database) as connection:
            if drift == "table":
                connection.execute("CREATE TABLE unexpected (value TEXT) STRICT")
            elif drift == "version":
                connection.execute("PRAGMA user_version = 2")
            elif drift == "application_id":
                connection.execute("PRAGMA application_id = 1")
            else:
                connection.execute("DROP INDEX phase_state_status")
        with pytest.raises(CheckpointContractError, match="schema"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


@pytest.mark.parametrize(
    "corruption", ("discovery_partition", "replay_partition", "count", "phase")
)
def test_resume_rejects_logically_inconsistent_checkpoint(
    tmp_path: Path,
    corruption: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass
        with sqlite3.connect(paths.database) as connection:
            if corruption == "discovery_partition":
                connection.execute(
                    "UPDATE discovery_chunks SET end_block = 105 WHERE chunk_index = 0"
                )
            elif corruption == "replay_partition":
                connection.execute(
                    "UPDATE replay_chunks SET start_block = 106 WHERE chunk_index = 1"
                )
            elif corruption == "count":
                connection.execute(
                    """
                    UPDATE discovery_chunks
                    SET status = 'completed', modify_witness_count = 1,
                        completed_generation = 1
                    WHERE chunk_index = 0
                    """
                )
            else:
                connection.execute(
                    "UPDATE phase_state SET status = 'running' WHERE phase = 'candidate_discovery'"
                )
        with pytest.raises(CheckpointContractError, match="checkpoint"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


@pytest.mark.parametrize("status", ("succeeded", "failed"))
def test_terminal_checkpoint_is_not_reset_to_running_on_reopen(
    tmp_path: Path,
    status: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass
        _mark_checkpoint_succeeded(paths.database, status=status)
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            terminal = run.snapshot()

    assert terminal.status == status
    assert terminal.phase == "succeeded"
    assert terminal.attempt_number == 1
    assert terminal.generation == 1
    assert terminal.output_published is True
    assert terminal.error_code == ("checkpoint_maintenance_error" if status == "failed" else None)


def test_fresh_replaces_only_operational_namespace(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    coverage = Path(f"{paths.output}.coverage.json")
    paths.output.write_bytes(b"prior-ledger")
    coverage.write_bytes(b"prior-coverage")
    identity = _identity(paths.output)

    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            prior_run_id = run.snapshot().run_id
        paths.progress.write_text("stale progress", encoding="utf-8")
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=True) as run:
            fresh_run_id = run.snapshot().run_id

    assert fresh_run_id != prior_run_id
    assert paths.output.read_bytes() == b"prior-ledger"
    assert coverage.read_bytes() == b"prior-coverage"
    assert not paths.progress.exists()


def test_status_open_does_not_create_missing_checkpoint(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")

    with pytest.raises(CheckpointContractError, match="does not exist"):
        LPLedgerCheckpoint.open_status(paths)

    assert not paths.database.exists()
    assert not paths.run_lock.exists()
    assert not paths.progress.exists()


@pytest.mark.parametrize(
    "leaf",
    ("output", "coverage", "database", "wal", "shm", "progress", "publish_lock"),
)
def test_checkpoint_rejects_every_symlinked_namespace_leaf(
    tmp_path: Path,
    leaf: str,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    target = tmp_path / "real-file"
    target.write_bytes(b"")
    selected = {
        "output": paths.output,
        "coverage": Path(f"{paths.output}.coverage.json"),
        "database": paths.database,
        "wal": paths.wal,
        "shm": paths.shm,
        "progress": paths.progress,
        "publish_lock": paths.publish_lock,
    }[leaf]
    selected.symlink_to(target)

    with acquire_run_lock(paths):
        with pytest.raises(CheckpointContractError, match="symlink"):
            LPLedgerCheckpoint.create_or_resume(
                paths,
                _identity(paths.output),
                fresh=False,
            )


def test_run_lock_rejects_symlinked_leaf(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    target = tmp_path / "real-lock"
    target.write_bytes(b"")
    paths.run_lock.symlink_to(target)

    with pytest.raises(CheckpointContractError, match="symlink"):
        with acquire_run_lock(paths):
            raise AssertionError("unreachable")


def test_checkpoint_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    linked_paths = CheckpointPaths.from_output(linked_parent / "ledger.csv")
    with pytest.raises(CheckpointContractError, match="symlink"):
        with acquire_run_lock(linked_paths):
            raise AssertionError("unreachable")


@pytest.mark.parametrize("leaf", ("wal", "shm"))
def test_status_rejects_symlinked_sqlite_sibling(tmp_path: Path, leaf: str) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass

    sibling = getattr(paths, leaf)
    sibling.unlink(missing_ok=True)
    target = tmp_path / f"real-{leaf}"
    target.write_bytes(b"")
    sibling.symlink_to(target)

    with pytest.raises(CheckpointContractError, match="symlink"):
        LPLedgerCheckpoint.open_status(paths)


def test_resume_rechecks_database_after_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False):
            pass

        original_connect = checkpoint_module.sqlite3.connect
        real_database = tmp_path / "real-checkpoint.sqlite3"

        def swap_then_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            paths.database.replace(real_database)
            paths.database.symlink_to(real_database)
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(checkpoint_module.sqlite3, "connect", swap_then_connect)
        with pytest.raises(CheckpointContractError, match="symlink"):
            LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False)


def test_failed_sqlite_configuration_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingConnection:
        row_factory: object = None
        closed = False

        def execute(self, _statement: str) -> object:
            raise sqlite3.DatabaseError("injected pragma failure")

        def close(self) -> None:
            self.closed = True

    connection = FailingConnection()
    monkeypatch.setattr(checkpoint_module.sqlite3, "connect", lambda *_a, **_k: connection)
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    paths.database.touch(mode=0o600)

    with pytest.raises(CheckpointContractError, match="could not be opened"):
        checkpoint_module._connect_read_write(paths)

    assert connection.closed is True


def _identity(output: Path) -> RunIdentity:
    return RunIdentity(
        schema_version=1,
        exporter_version="lp-ledger-checkpoint-v1",
        verification_mode="rpc_verified",
        pool="uni-base",
        chain="base",
        chain_id=8453,
        pool_id=_HASH_A,
        pool_manager=_ADDRESS_A,
        position_manager=_ADDRESS_B,
        state_view=_ADDRESS_C,
        token0_address=_ADDRESS_D,
        token1_address=_ADDRESS_E,
        token0_decimals=6,
        token1_decimals=18,
        fee_rate="0.0005",
        invert_price=False,
        start_block=100,
        end_block=109,
        chunk_size=5,
        discovery_topics=(_HASH_A, _HASH_B),
        output_path=os.path.abspath(output),
        endpoint=EndpointSnapshot(
            start=BlockHeader(100, _HASH_A, 1_000),
            end=BlockHeader(109, _HASH_B, 2_000),
        ),
        python_version="3.12.0",
        web3_version="7.0.0",
        source_sha256=tuple((path, "a" * 64) for path in CHECKPOINT_SOURCE_PATHS),
    )


def _mutated_identity(identity: RunIdentity, mutation: str) -> RunIdentity:
    if mutation == "range":
        return replace(identity, end_block=identity.end_block + 1)
    if mutation == "endpoint":
        return replace(
            identity,
            endpoint=replace(
                identity.endpoint,
                end=replace(identity.endpoint.end, block_hash=_HASH_A),
            ),
        )
    if mutation == "source":
        return replace(identity, source_sha256=(("export_v4_lp_ledger.py", "b" * 64),))
    if mutation == "output":
        return replace(identity, output_path=f"{identity.output_path}.other")
    raise AssertionError(f"unsupported identity mutation: {mutation}")


def _mark_checkpoint_succeeded(database: Path, *, status: str) -> None:
    error_code = "checkpoint_maintenance_error" if status == "failed" else None
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE phase_state
            SET status = 'completed', completed = COALESCE(total, completed)
            """
        )
        connection.execute(
            """
            UPDATE publication_state
            SET state = 'published', expected_ledger_sha256 = ?,
                expected_sidecar_sha256 = ?, started_generation = 1,
                published_generation = 1
            WHERE singleton = 1
            """,
            ("a" * 64, "b" * 64),
        )
        connection.execute(
            """
            UPDATE run_state
            SET status = ?, phase = 'succeeded',
                finished_at_utc = updated_at_utc, output_published = 1,
                error_code = ?
            WHERE singleton = 1
            """,
            (status, error_code),
        )
