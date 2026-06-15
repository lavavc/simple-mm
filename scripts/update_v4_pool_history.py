"""Incrementally update and validate both Uniswap v4 pool-history datasets."""

from __future__ import annotations

import argparse
import csv
import fcntl
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtester.v4_export import POOL_CONFIGS, _read_export_checkpoint


@dataclass(frozen=True)
class PoolUpdateJob:
    pool: str
    pool_id: str
    output_path: Path
    checkpoint_path: Path
    end_block: int | None = None


def build_export_command(job: PoolUpdateJob) -> list[str]:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "export_v4_pool_history.py"),
        "--pool",
        job.pool,
        "--output",
        str(job.output_path),
        "--resume",
        "--checkpoint-file",
        str(job.checkpoint_path),
    ]
    if job.end_block is not None:
        command.extend(["--end-block", str(job.end_block)])
    return command


def _run_subprocess(command: list[str]) -> None:
    subprocess.run(command, cwd=REPO_ROOT, check=True)


def run_export_with_retries(
    job: PoolUpdateJob,
    *,
    max_attempts: int = 8,
    initial_delay_seconds: float = 5.0,
    max_delay_seconds: float = 300.0,
    runner: Callable[[list[str]], None] = _run_subprocess,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    command = build_export_command(job)
    delay = initial_delay_seconds
    for attempt in range(1, max_attempts + 1):
        try:
            runner(command)
            return
        except subprocess.CalledProcessError:
            if attempt == max_attempts:
                raise
            print(
                f"[{job.pool}] export attempt {attempt}/{max_attempts} failed; "
                f"retrying in {delay:g}s",
                file=sys.stderr,
                flush=True,
            )
            sleep(delay)
            delay = min(delay * 2, max_delay_seconds)


@contextmanager
def acquire_update_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"pool-history update is already running: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_pool_history(job: PoolUpdateJob) -> None:
    checkpoint_block = _read_export_checkpoint(str(job.checkpoint_path), job.pool)
    if checkpoint_block is None:
        raise ValueError(f"missing checkpoint: {job.checkpoint_path}")
    if job.end_block is not None and checkpoint_block != job.end_block:
        raise ValueError(
            f"checkpoint {checkpoint_block} does not equal requested end block {job.end_block}"
        )

    previous_block: int | None = None
    seen_events: set[tuple[str, str]] = set()
    row_count = 0
    with job.output_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"pool_id", "tx_hash", "log_index", "block_number"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"history CSV is missing required fields: {job.output_path}")
        for row in reader:
            row_count += 1
            if row["pool_id"].lower() != job.pool_id.lower():
                raise ValueError(f"unexpected pool_id in {job.output_path}: {row['pool_id']}")
            block = int(row["block_number"])
            if previous_block is not None and block < previous_block:
                raise ValueError(f"block order is not monotonic in {job.output_path}")
            previous_block = block
            event_key = (row["tx_hash"].lower(), row["log_index"])
            if event_key in seen_events:
                raise ValueError(f"duplicate transaction/log event in {job.output_path}: {event_key}")
            seen_events.add(event_key)

    if row_count == 0:
        raise ValueError(f"history CSV contains no events: {job.output_path}")
    if previous_block is not None and checkpoint_block < previous_block:
        raise ValueError(
            f"checkpoint {checkpoint_block} precedes final event block {previous_block}"
        )


def update_pool_history(
    jobs: Sequence[PoolUpdateJob],
    *,
    run_export: Callable[[PoolUpdateJob], None] = run_export_with_retries,
    validate: Callable[[PoolUpdateJob], None] = validate_pool_history,
) -> None:
    for job in jobs:
        print(f"[{job.pool}] starting incremental update", flush=True)
        run_export(job)
        validate(job)
        print(f"[{job.pool}] update and validation complete", flush=True)


def _jobs(args: argparse.Namespace) -> list[PoolUpdateJob]:
    data_dir = REPO_ROOT / "data"
    requested = args.pool or list(POOL_CONFIGS)
    end_blocks = {
        "uni-base": args.base_end_block,
        "uni-bsc": args.bsc_end_block,
    }
    return [
        PoolUpdateJob(
            pool=pool,
            pool_id=POOL_CONFIGS[pool].pool_id,
            output_path=data_dir / f"{pool.replace('-', '_')}_pool_history.csv",
            checkpoint_path=data_dir / "checkpoints" / f"{pool.replace('-', '_')}.json",
            end_block=end_blocks[pool],
        )
        for pool in requested
    ]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", action="append", choices=sorted(POOL_CONFIGS))
    parser.add_argument("--base-end-block", type=int)
    parser.add_argument("--bsc-end-block", type=int)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--initial-delay-seconds", type=float, default=5.0)
    parser.add_argument("--max-delay-seconds", type=float, default=300.0)
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=REPO_ROOT / "data" / "checkpoints" / "pool-history-update.lock",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    jobs = _jobs(args)

    def run(job: PoolUpdateJob) -> None:
        run_export_with_retries(
            job,
            max_attempts=args.max_attempts,
            initial_delay_seconds=args.initial_delay_seconds,
            max_delay_seconds=args.max_delay_seconds,
        )

    with acquire_update_lock(args.lock_file):
        update_pool_history(jobs, run_export=run)


if __name__ == "__main__":
    main()
