import csv
import subprocess

import pytest

from scripts.update_v4_pool_history import (
    PoolUpdateJob,
    acquire_update_lock,
    build_export_command,
    run_export_with_retries,
    update_pool_history,
    validate_pool_history,
)


def _write_history(path, pool_id, rows):
    fieldnames = ["pool_id", "tx_hash", "log_index", "block_number"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({"pool_id": pool_id, **row})


def test_build_export_command_always_resumes_with_checkpoint(tmp_path):
    job = PoolUpdateJob(
        pool="uni-base",
        pool_id="0xabc",
        output_path=tmp_path / "history.csv",
        checkpoint_path=tmp_path / "checkpoint.json",
        end_block=456,
    )

    command = build_export_command(job)

    assert "--resume" in command
    assert command[command.index("--checkpoint-file") + 1] == str(job.checkpoint_path)
    assert command[command.index("--end-block") + 1] == "456"


def test_retry_export_uses_bounded_exponential_backoff(tmp_path):
    job = PoolUpdateJob("uni-base", "0xabc", tmp_path / "history.csv", tmp_path / "checkpoint.json")
    attempts = []
    delays = []

    def runner(command):
        attempts.append(command)
        if len(attempts) < 3:
            raise subprocess.CalledProcessError(1, command)

    run_export_with_retries(
        job,
        max_attempts=3,
        initial_delay_seconds=2,
        max_delay_seconds=3,
        runner=runner,
        sleep=delays.append,
    )

    assert len(attempts) == 3
    assert delays == [2, 3]


def test_retry_export_raises_after_final_attempt(tmp_path):
    job = PoolUpdateJob("uni-base", "0xabc", tmp_path / "history.csv", tmp_path / "checkpoint.json")

    def runner(command):
        raise subprocess.CalledProcessError(1, command)

    with pytest.raises(subprocess.CalledProcessError):
        run_export_with_retries(job, max_attempts=2, runner=runner, sleep=lambda _delay: None)


def test_update_pool_history_runs_jobs_sequentially(tmp_path):
    jobs = [
        PoolUpdateJob("uni-base", "0xbase", tmp_path / "base.csv", tmp_path / "base.json"),
        PoolUpdateJob("uni-bsc", "0xbsc", tmp_path / "bsc.csv", tmp_path / "bsc.json"),
    ]
    calls = []

    update_pool_history(
        jobs,
        run_export=lambda job: calls.append(("run", job.pool)),
        validate=lambda job: calls.append(("validate", job.pool)),
    )

    assert calls == [
        ("run", "uni-base"),
        ("validate", "uni-base"),
        ("run", "uni-bsc"),
        ("validate", "uni-bsc"),
    ]


def test_update_lock_rejects_overlapping_run(tmp_path):
    lock_path = tmp_path / "update.lock"

    with acquire_update_lock(lock_path):
        with pytest.raises(RuntimeError, match="already running"):
            with acquire_update_lock(lock_path):
                pass


def test_validate_pool_history_checks_order_duplicates_pool_and_checkpoint(tmp_path):
    history = tmp_path / "history.csv"
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"pool":"uni-base","last_scanned_block":150}\n')
    _write_history(
        history,
        "0xabc",
        [
            {"tx_hash": "0x1", "log_index": 1, "block_number": 100},
            {"tx_hash": "0x2", "log_index": 2, "block_number": 125},
        ],
    )
    job = PoolUpdateJob("uni-base", "0xabc", history, checkpoint, end_block=150)

    validate_pool_history(job)

    _write_history(
        history,
        "0xabc",
        [
            {"tx_hash": "0x1", "log_index": 1, "block_number": 125},
            {"tx_hash": "0x1", "log_index": 1, "block_number": 100},
        ],
    )
    with pytest.raises(ValueError, match="block order"):
        validate_pool_history(job)

    _write_history(
        history,
        "0xabc",
        [
            {"tx_hash": "0x1", "log_index": 1, "block_number": 100},
            {"tx_hash": "0x1", "log_index": 1, "block_number": 125},
        ],
    )
    with pytest.raises(ValueError, match="duplicate"):
        validate_pool_history(job)

    _write_history(history, "0xwrong", [{"tx_hash": "0x1", "log_index": 1, "block_number": 100}])
    with pytest.raises(ValueError, match="pool_id"):
        validate_pool_history(job)

    _write_history(history, "0xabc", [{"tx_hash": "0x1", "log_index": 1, "block_number": 100}])
    checkpoint.write_text('{"pool":"uni-base","last_scanned_block":149}\n')
    with pytest.raises(ValueError, match="checkpoint"):
        validate_pool_history(job)
