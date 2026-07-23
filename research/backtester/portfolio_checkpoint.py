"""Atomic, identity-bound checkpoints for weighted-portfolio evaluation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping


class CheckpointIdentityError(ValueError):
    """Raised when existing checkpoint bytes belong to another run."""


class CheckpointStateError(ValueError):
    """Raised when checkpoint state is malformed or non-contiguous."""


def _canonical_json_bytes(payload: object) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CheckpointStateError("checkpoint payload is not canonical JSON") from exc
    return (encoded + "\n").encode()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class PortfolioCheckpointStore:
    """Persist one complete primary-window payload at a time."""

    def __init__(
        self,
        root: Path,
        identity: Mapping[str, object],
        *,
        total_windows: int,
        phase: str = "primary",
    ) -> None:
        if total_windows <= 0:
            raise ValueError("total_windows must be positive")
        pool = identity.get("pool")
        if not isinstance(pool, str) or not pool:
            raise ValueError("checkpoint identity requires a pool")
        if phase not in {"primary", "best_sleeve_removal"}:
            raise ValueError("unsupported checkpoint phase")
        identity_phase = identity.get("phase")
        if identity_phase is not None and identity_phase != phase:
            raise CheckpointIdentityError("checkpoint phase conflicts with run identity")
        self.root = root
        self.identity = {**identity, "phase": phase}
        self.total_windows = total_windows
        self.pool = pool
        self.phase = phase
        self._identity_bytes = _canonical_json_bytes(self.identity)
        self.identity_sha256 = hashlib.sha256(self._identity_bytes).hexdigest()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(self) -> None:
        with self._locked():
            identity_path = self.root / "identity.json"
            payload = _canonical_json_bytes(
                {
                    "identity": self.identity,
                    "identity_sha256": self.identity_sha256,
                }
            )
            if identity_path.exists():
                if identity_path.read_bytes() != payload:
                    raise CheckpointIdentityError(
                        "checkpoint identity does not match the requested run"
                    )
            else:
                _atomic_write(identity_path, payload)
            completed = self._load_windows_unlocked()
            self._write_progress_unlocked(len(completed))

    def append_window(self, index: int, payload: Mapping[str, object]) -> None:
        with self._locked():
            self._require_initialized_unlocked()
            completed = self._load_windows_unlocked()
            if index != len(completed):
                raise CheckpointStateError(
                    f"window {index} is not the next contiguous checkpoint"
                )
            if index >= self.total_windows:
                raise CheckpointStateError("checkpoint exceeds declared window count")
            owned_payload = dict(payload)
            if owned_payload.get("window_index") != index:
                raise CheckpointStateError("checkpoint payload window index is incoherent")
            self._validate_carried_cash(owned_payload)
            wrapped = {
                "identity_sha256": self.identity_sha256,
                "window": owned_payload,
            }
            _atomic_write(self._window_path(index), _canonical_json_bytes(wrapped))
            self._write_progress_unlocked(index + 1)

    def next_window_index(self) -> int:
        return len(self.load_windows())

    def load_windows(self) -> tuple[dict[str, object], ...]:
        with self._locked():
            self._require_initialized_unlocked()
            return self._load_windows_unlocked()

    def _load_windows_unlocked(self) -> tuple[dict[str, object], ...]:
        paths = sorted(self.root.glob("window-*.json"))
        indexes: list[int] = []
        for path in paths:
            try:
                indexes.append(int(path.stem.removeprefix("window-")))
            except ValueError as exc:
                raise CheckpointStateError(
                    f"malformed checkpoint filename {path.name!r}"
                ) from exc
        if indexes != list(range(len(indexes))):
            raise CheckpointStateError("checkpoint window files are not contiguous")
        if len(indexes) > self.total_windows:
            raise CheckpointStateError("checkpoint has more windows than declared")

        payloads: list[dict[str, object]] = []
        for index, path in zip(indexes, paths, strict=True):
            try:
                raw = path.read_bytes()
                wrapped = json.loads(raw)
            except (json.JSONDecodeError, OSError) as exc:
                raise CheckpointStateError(
                    f"checkpoint window {index} is unreadable"
                ) from exc
            if not isinstance(wrapped, dict):
                raise CheckpointStateError(f"checkpoint window {index} is not an object")
            if raw != _canonical_json_bytes(wrapped):
                raise CheckpointStateError(
                    f"checkpoint window {index} bytes are not canonical"
                )
            if wrapped.get("identity_sha256") != self.identity_sha256:
                raise CheckpointIdentityError(
                    f"checkpoint window {index} has a different run identity"
                )
            payload = wrapped.get("window")
            if not isinstance(payload, dict) or payload.get("window_index") != index:
                raise CheckpointStateError(
                    f"checkpoint window {index} payload is incoherent"
                )
            self._validate_carried_cash(payload)
            payloads.append(payload)
        return tuple(payloads)

    def _validate_carried_cash(self, payload: Mapping[str, object]) -> None:
        carried = payload.get("carried_closing_cash_usd")
        if carried is None:
            return
        if not isinstance(carried, dict):
            raise CheckpointStateError("carried closing cash must be an object")
        for method, value in carried.items():
            if (
                not isinstance(method, str)
                or not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise CheckpointStateError(
                    "carried closing cash values must be finite non-negative numbers"
                )

    def _require_initialized_unlocked(self) -> None:
        identity_path = self.root / "identity.json"
        if not identity_path.exists():
            raise CheckpointStateError("checkpoint store is not initialized")
        expected = _canonical_json_bytes(
            {
                "identity": self.identity,
                "identity_sha256": self.identity_sha256,
            }
        )
        if identity_path.read_bytes() != expected:
            raise CheckpointIdentityError(
                "checkpoint identity does not match the requested run"
            )

    def _write_progress_unlocked(self, completed_windows: int) -> None:
        _atomic_write(
            self.root / "progress.json",
            _canonical_json_bytes(
                {
                    "completed_windows": completed_windows,
                    "next_window_index": completed_windows,
                    "phase": self.phase,
                    "pool": self.pool,
                    "total_windows": self.total_windows,
                }
            ),
        )

    def _window_path(self, index: int) -> Path:
        return self.root / f"window-{index:06d}.json"
