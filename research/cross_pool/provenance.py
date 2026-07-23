"""Deterministic input, source-tree, and runtime provenance capture."""

from __future__ import annotations

import csv
import errno
import hashlib
import io
import json
import os
import platform
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, cast

import matplotlib
import numpy as np
from matplotlib import font_manager, ft2font

from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.figures import FIGURE_RCPARAMS
from research.cross_pool.manifest import InputFileProvenance, RuntimeEnvironment

TimestampField = Literal["timestamp_ms", "block_time"]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FROZEN_SOURCE_PATHS = ("engine", "research", "pyproject.toml")


class ProvenanceCaptureError(RuntimeError):
    """Raised when runtime or Git provenance cannot be captured."""


@dataclass(frozen=True)
class ImmutableInputSnapshot:
    """One descriptor-captured private input and its byte identity."""

    path: Path
    sha256: str
    byte_count: int


def _remove_owned_snapshot(
    destination: Path,
    identity: tuple[int, int],
) -> None:
    try:
        observed = destination.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ProvenanceCaptureError(
            "partial input snapshot could not be inspected"
        ) from exc
    if (observed.st_dev, observed.st_ino) != identity:
        raise ProvenanceCaptureError(
            "partial input snapshot path no longer identifies the created file"
        )
    try:
        destination.unlink()
    except OSError as exc:
        raise ProvenanceCaptureError(
            "partial input snapshot could not be removed"
        ) from exc


def snapshot_regular_file(
    source: Path,
    destination: Path,
) -> ImmutableInputSnapshot | None:
    """Copy one regular non-symlink input into an exclusive private file."""
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return None
        raise ProvenanceCaptureError("snapshot source could not be read") from exc

    destination_descriptor: int | None = None
    destination_created = False
    destination_identity: tuple[int, int] | None = None
    completed = False
    digest = hashlib.sha256()
    byte_count = 0
    try:
        source_before = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_before.st_mode):
            return None
        try:
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            destination_created = True
            destination_stat = os.fstat(destination_descriptor)
            destination_identity = (
                destination_stat.st_dev,
                destination_stat.st_ino,
            )
        except OSError as exc:
            raise ProvenanceCaptureError(
                "private input snapshot could not be written"
            ) from exc

        while True:
            try:
                chunk = os.read(source_descriptor, 1 << 20)
            except OSError as exc:
                raise ProvenanceCaptureError(
                    "snapshot source could not be read"
                ) from exc
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
            view = memoryview(chunk)
            while view:
                try:
                    written = os.write(destination_descriptor, view)
                except OSError as exc:
                    raise ProvenanceCaptureError(
                        "private input snapshot could not be written"
                    ) from exc
                if written <= 0:
                    raise ProvenanceCaptureError(
                        "private input snapshot write made no progress"
                    )
                view = view[written:]
        source_after = os.fstat(source_descriptor)
        identity_before = (
            source_before.st_dev,
            source_before.st_ino,
            source_before.st_size,
            source_before.st_mtime_ns,
            source_before.st_ctime_ns,
        )
        identity_after = (
            source_after.st_dev,
            source_after.st_ino,
            source_after.st_size,
            source_after.st_mtime_ns,
            source_after.st_ctime_ns,
        )
        if identity_before != identity_after or byte_count != source_before.st_size:
            raise ProvenanceCaptureError("snapshot source changed while copying")
        os.fsync(destination_descriptor)
        completed = True
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)
        if destination_created and not completed:
            assert destination_identity is not None
            _remove_owned_snapshot(destination, destination_identity)

    try:
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        assert destination_identity is not None
        _remove_owned_snapshot(destination, destination_identity)
        raise ProvenanceCaptureError(
            "private input snapshot directory could not be synced"
        ) from exc
    return ImmutableInputSnapshot(
        path=destination,
        sha256=digest.hexdigest(),
        byte_count=byte_count,
    )


def sha256_file(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProvenanceCaptureError("provenance file could not be read") from exc
    return hashlib.sha256(raw).hexdigest()


def csv_file_provenance(
    path: Path,
    *,
    timestamp_field: TimestampField,
) -> InputFileProvenance:
    """Hash a CSV snapshot and bind its ordered block/time interval."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CrossPoolContractError("input CSV could not be read") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError("input CSV must use UTF-8") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if (
        reader.fieldnames is None
        or timestamp_field not in reader.fieldnames
        or "block_number" not in reader.fieldnames
    ):
        raise CrossPoolContractError(
            f"input CSV requires block_number and {timestamp_field} for provenance"
        )
    timestamps: list[int] = []
    blocks: list[int] = []
    for row_number, row in enumerate(reader, start=2):
        raw_timestamp = row.get(timestamp_field)
        if raw_timestamp is None or not raw_timestamp.strip():
            raise CrossPoolContractError(
                f"input CSV row {row_number} has no {timestamp_field}"
            )
        timestamps.append(
            _parse_timestamp(raw_timestamp.strip(), timestamp_field, row_number)
        )
        raw_block = row.get("block_number")
        if raw_block is None or not raw_block.strip():
            raise CrossPoolContractError(
                f"input CSV row {row_number} has no block_number"
            )
        try:
            block_number = int(raw_block.strip())
        except ValueError as exc:
            raise CrossPoolContractError(
                f"input CSV row {row_number} block_number must be an integer"
            ) from exc
        if block_number <= 0 or str(block_number) != raw_block.strip():
            raise CrossPoolContractError(
                f"input CSV row {row_number} block_number must be canonical and positive"
            )
        blocks.append(block_number)
    if not timestamps:
        raise CrossPoolContractError("input CSV provenance requires at least one row")
    if any(
        current < previous
        for previous, current in zip(timestamps, timestamps[1:], strict=False)
    ):
        raise CrossPoolContractError(
            "input CSV provenance timestamps must be nondecreasing"
        )
    if any(
        current < previous
        for previous, current in zip(blocks, blocks[1:], strict=False)
    ):
        raise CrossPoolContractError(
            "input CSV provenance blocks must be nondecreasing"
        )
    return InputFileProvenance(
        sha256=hashlib.sha256(raw).hexdigest(),
        rows=len(timestamps),
        first_block=blocks[0],
        last_block=blocks[-1],
        first_timestamp_ms=timestamps[0],
        last_timestamp_ms=timestamps[-1],
    )


def collect_runtime_environment() -> RuntimeEnvironment:
    """Capture the runtime fields that scope deterministic artifact bytes."""
    show_config = cast(Callable[..., object], np.show_config)
    numpy_build = show_config(mode="dicts")
    if not isinstance(numpy_build, dict):
        raise ProvenanceCaptureError(
            "NumPy build configuration is unavailable as a mapping"
        )
    numpy_build_bytes = json.dumps(
        numpy_build,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    rcparams_bytes = json.dumps(
        FIGURE_RCPARAMS,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    font_name = "DejaVu Sans"
    font_path = Path(
        font_manager.findfont(
            font_manager.FontProperties(family=[font_name]),
            fallback_to_default=False,
        )
    )
    backend = str(matplotlib.get_backend())
    if backend.lower() != "agg":
        raise ProvenanceCaptureError("frozen figures require the Agg backend")
    return RuntimeEnvironment(
        python_version=platform.python_version(),
        numpy_version=np.__version__,
        numpy_build_sha256=hashlib.sha256(numpy_build_bytes).hexdigest(),
        machine_architecture=platform.machine(),
        byte_order=sys.byteorder,
        bit_generator="PCG64",
        draw_dtype="int64",
        matplotlib_version=matplotlib.__version__,
        matplotlib_backend="Agg",
        freetype_version=ft2font.__freetype_version__,
        font_name=font_name,
        font_file_sha256=sha256_file(font_path),
        figure_rcparams_sha256=hashlib.sha256(rcparams_bytes).hexdigest(),
    )


def capture_git_state(repository_root: Path) -> tuple[str, str]:
    """Bind execution to a commit after proving source is tracked and clean."""
    commit = _git_output(repository_root, "rev-parse", "HEAD").decode("ascii").strip()
    diff = _git_output(
        repository_root,
        "diff",
        "--binary",
        "HEAD",
        "--",
        *_FROZEN_SOURCE_PATHS,
    )
    untracked = _git_output(
        repository_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        *_FROZEN_SOURCE_PATHS,
    )
    if diff or untracked:
        raise ProvenanceCaptureError(
            "frozen executable source must be fully tracked and clean"
        )
    return commit, hashlib.sha256(diff).hexdigest()


def _git_output(repository_root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProvenanceCaptureError("Git provenance could not be captured") from exc
    return completed.stdout


def _parse_timestamp(
    value: str,
    timestamp_field: TimestampField,
    row_number: int,
) -> int:
    if timestamp_field == "timestamp_ms":
        try:
            timestamp_ms = int(value)
        except ValueError as exc:
            raise CrossPoolContractError(
                f"input CSV row {row_number} timestamp_ms must be an integer"
            ) from exc
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise CrossPoolContractError(
                f"input CSV row {row_number} block_time must be ISO-8601"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise CrossPoolContractError(
                f"input CSV row {row_number} block_time must be UTC-aware"
            )
        normalized = parsed.astimezone(timezone.utc)
        if normalized.microsecond % 1_000 != 0:
            raise CrossPoolContractError(
                f"input CSV row {row_number} block_time must have millisecond precision"
            )
        elapsed = normalized - _EPOCH
        timestamp_ms = (
            elapsed.days * 86_400_000
            + elapsed.seconds * 1_000
            + elapsed.microseconds // 1_000
        )
    if timestamp_ms <= 0:
        raise CrossPoolContractError(
            f"input CSV row {row_number} timestamp must follow the Unix epoch"
        )
    return timestamp_ms
