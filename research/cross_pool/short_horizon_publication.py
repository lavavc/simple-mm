"""Atomic publication and review for independent short-horizon evidence."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.short_horizon_artifacts import (
    SHORT_HORIZON_ARTIFACT_NAMES,
    ShortHorizonArtifact,
)
from research.cross_pool.short_horizon_manifest import (
    JsonValue,
    ShortHorizonManifestEvidence,
    canonical_short_horizon_manifest_bytes,
    load_and_validate_short_horizon_manifest,
    reviewed_short_horizon_manifest,
    short_horizon_manifest_evidence,
    validate_short_horizon_manifest,
)
from research.cross_pool.short_horizon_reporting import (
    ShortHorizonEvidence,
    parse_short_horizon_events_csv,
    validate_rendered_short_horizon_artifacts,
)

PublicationMode = Literal["publish_new", "verify_existing"]
_MANIFEST_NAME = "short_horizon_manifest.json"


class ShortHorizonPublicationError(RuntimeError):
    """Raised when short-horizon evidence cannot be published safely."""


@contextmanager
def output_lock(out_dir: Path) -> Iterator[None]:
    """Serialize analysis and publication for one canonical output directory."""
    parent = out_dir.parent
    if not parent.is_dir() or out_dir.name in ("", ".", ".."):
        raise ShortHorizonPublicationError("output parent must be an existing directory")
    lock_path = parent / f".{out_dir.name}.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise ShortHorizonPublicationError("output lock could not be opened") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ShortHorizonPublicationError("another output invocation holds the lock") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def classify_short_horizon_output(out_dir: Path) -> PublicationMode:
    if not os.path.lexists(out_dir):
        return "publish_new"
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise ShortHorizonPublicationError("existing output is not a valid evidence directory")
    try:
        manifest = validate_short_horizon_evidence_directory(out_dir)
    except (CrossPoolContractError, OSError, ShortHorizonPublicationError) as exc:
        raise ShortHorizonPublicationError(
            "existing output is not valid short-horizon evidence"
        ) from exc
    if not _is_verifiable_manifest(manifest):
        raise ShortHorizonPublicationError("existing short-horizon evidence is immutable")
    return "verify_existing"


def write_short_horizon_candidate(
    candidate_dir: Path,
    artifacts: Sequence[ShortHorizonArtifact],
    manifest: Mapping[str, JsonValue],
) -> None:
    if os.path.lexists(candidate_dir):
        raise ShortHorizonPublicationError("candidate directory must not already exist")
    validate_short_horizon_manifest(manifest)
    artifact_map = _artifact_map(manifest)
    materialized = tuple(artifacts)
    names = tuple(artifact.relative_name for artifact in materialized)
    if len(set(names)) != len(names) or set(names) != set(artifact_map):
        raise ShortHorizonPublicationError("candidate artifacts do not match the manifest")
    for artifact in materialized:
        if artifact.sha256 != artifact_map[artifact.relative_name]:
            raise ShortHorizonPublicationError(
                f"candidate artifact hash mismatch for {artifact.relative_name}"
            )
    if artifact_map:
        _validate_artifact_semantics(materialized, manifest)
    candidate_created = False
    try:
        candidate_dir.mkdir(mode=0o700)
        candidate_created = True
        for artifact in materialized:
            _write_fsynced_file(
                candidate_dir / artifact.relative_name,
                artifact.content,
            )
        _write_fsynced_file(
            candidate_dir / _MANIFEST_NAME,
            canonical_short_horizon_manifest_bytes(manifest),
        )
        _fsync_directory(candidate_dir)
        validate_short_horizon_evidence_directory(candidate_dir)
    except Exception as exc:
        if candidate_created:
            try:
                shutil.rmtree(candidate_dir)
                _fsync_directory(candidate_dir.parent)
            except Exception as cleanup_exc:
                raise ShortHorizonPublicationError(
                    "short-horizon candidate sealing and cleanup both failed"
                ) from cleanup_exc
        if isinstance(exc, ShortHorizonPublicationError):
            raise
        raise ShortHorizonPublicationError("short-horizon candidate could not be sealed") from exc


def publish_or_verify_short_horizon_candidate(
    out_dir: Path,
    candidate_dir: Path,
    *,
    mode: PublicationMode,
) -> None:
    candidate_manifest = validate_short_horizon_evidence_directory(candidate_dir)
    if mode == "publish_new":
        if os.path.lexists(out_dir):
            raise ShortHorizonPublicationError("new publication target is no longer absent")
        try:
            _rename_directory_no_replace(candidate_dir, out_dir)
            _fsync_directory(out_dir.parent)
            validate_short_horizon_evidence_directory(out_dir)
        except Exception as exc:
            raise ShortHorizonPublicationError("candidate publication failed") from exc
        return
    if mode != "verify_existing":
        raise ShortHorizonPublicationError("unsupported publication mode")
    if not _is_verifiable_manifest(candidate_manifest):
        raise ShortHorizonPublicationError(
            "compare-only verification requires generated QA-pass evidence"
        )
    try:
        existing_manifest = validate_short_horizon_evidence_directory(out_dir)
    except (CrossPoolContractError, OSError, ShortHorizonPublicationError) as exc:
        raise ShortHorizonPublicationError("existing evidence changed during verification") from exc
    if not _is_verifiable_manifest(existing_manifest):
        raise ShortHorizonPublicationError("existing short-horizon evidence is immutable")
    existing_names = {path.name for path in out_dir.iterdir()}
    candidate_names = {path.name for path in candidate_dir.iterdir()}
    if existing_names != candidate_names:
        raise ShortHorizonPublicationError("deterministic rerun filename set changed")
    for name in sorted(existing_names):
        if (out_dir / name).read_bytes() != (candidate_dir / name).read_bytes():
            raise ShortHorizonPublicationError("deterministic rerun bytes changed")
    final_manifest = validate_short_horizon_evidence_directory(out_dir)
    if canonical_short_horizon_manifest_bytes(final_manifest) != (
        canonical_short_horizon_manifest_bytes(existing_manifest)
    ):
        raise ShortHorizonPublicationError("existing evidence changed during verification")
    shutil.rmtree(candidate_dir)


def validate_short_horizon_evidence_directory(
    directory: Path,
) -> dict[str, JsonValue]:
    if directory.is_symlink() or not directory.is_dir():
        raise ShortHorizonPublicationError("evidence path must be a regular directory")
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise ShortHorizonPublicationError("evidence directory could not be read") from exc
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ShortHorizonPublicationError("evidence directory contains a non-file entry")
    manifest = load_and_validate_short_horizon_manifest(directory / _MANIFEST_NAME)
    artifact_map = _artifact_map(manifest)
    expected_names = set(artifact_map) | {_MANIFEST_NAME}
    if {path.name for path in entries} != expected_names:
        raise ShortHorizonPublicationError("evidence directory has an unexpected file set")
    for name, expected_digest in artifact_map.items():
        try:
            actual_digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        except OSError as exc:
            raise ShortHorizonPublicationError("evidence artifact could not be read") from exc
        if actual_digest != expected_digest:
            raise ShortHorizonPublicationError(f"evidence artifact hash mismatch for {name}")
    if artifact_map:
        artifacts = tuple(
            ShortHorizonArtifact(
                relative_name=name,
                content=(directory / name).read_bytes(),
            )
            for name in SHORT_HORIZON_ARTIFACT_NAMES
        )
        _validate_artifact_semantics(artifacts, manifest)
    return manifest


def review_short_horizon_evidence(
    out_dir: Path,
    *,
    reviewed_by: str,
    reviewed_at_utc: str,
) -> dict[str, JsonValue]:
    with output_lock(out_dir):
        try:
            original = validate_short_horizon_evidence_directory(out_dir)
            reviewed = reviewed_short_horizon_manifest(
                original,
                reviewed_by=reviewed_by,
                reviewed_at_utc=reviewed_at_utc,
            )
        except (CrossPoolContractError, OSError, ShortHorizonPublicationError) as exc:
            raise ShortHorizonPublicationError(
                "review requires QA-pass generated short-horizon evidence"
            ) from exc
        stage_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{out_dir.name}.review-",
                dir=out_dir.parent,
            )
        )
        manifest_path = out_dir / _MANIFEST_NAME
        try:
            original_path = stage_dir / "original.json"
            reviewed_path = stage_dir / "reviewed.json"
            _write_fsynced_file(
                original_path,
                canonical_short_horizon_manifest_bytes(original),
            )
            _write_fsynced_file(
                reviewed_path,
                canonical_short_horizon_manifest_bytes(reviewed),
            )
            _fsync_directory(stage_dir)
            try:
                os.replace(reviewed_path, manifest_path)
            except OSError as exc:
                raise ShortHorizonPublicationError("review publication failed") from exc
            try:
                _fsync_directory(out_dir)
                final = validate_short_horizon_evidence_directory(out_dir)
                if final != reviewed:
                    raise ShortHorizonPublicationError(
                        "reviewed manifest changed during publication"
                    )
            except Exception as validation_exc:
                try:
                    os.replace(original_path, manifest_path)
                    _fsync_directory(out_dir)
                    validate_short_horizon_evidence_directory(out_dir)
                except Exception as rollback_exc:
                    raise ShortHorizonPublicationError(
                        "review validation and rollback both failed"
                    ) from rollback_exc
                raise ShortHorizonPublicationError(
                    "review validation failed and was rolled back"
                ) from validation_exc
            return final
        finally:
            _remove_review_stage(stage_dir)


def _artifact_map(manifest: Mapping[str, JsonValue]) -> dict[str, str]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str) for name, digest in raw.items()
    ):
        raise ShortHorizonPublicationError("manifest artifact map is invalid")
    return cast(dict[str, str], raw)


def _is_verifiable_manifest(manifest: Mapping[str, JsonValue]) -> bool:
    qa = manifest.get("qa")
    review = manifest.get("review")
    return (
        manifest.get("artifact_status") == "generated_unreviewed"
        and isinstance(qa, dict)
        and qa.get("status") == "pass"
        and isinstance(review, dict)
        and review.get("status") == "pending"
    )


def _validate_artifact_semantics(
    artifacts: Sequence[ShortHorizonArtifact],
    manifest: Mapping[str, JsonValue],
) -> None:
    try:
        projection = short_horizon_manifest_evidence(manifest)
        _validate_artifact_semantics_cached(tuple(artifacts), projection)
    except (CrossPoolContractError, KeyError) as exc:
        raise ShortHorizonPublicationError(
            "short-horizon artifacts are not semantically bound to the manifest"
        ) from exc


@lru_cache(maxsize=16)
def _validate_artifact_semantics_cached(
    artifacts: tuple[ShortHorizonArtifact, ...],
    projection: ShortHorizonManifestEvidence,
) -> None:
    artifact_map = {item.relative_name: item for item in artifacts}
    studies = parse_short_horizon_events_csv(artifact_map["short_horizon_events.csv"].content)
    evidence = ShortHorizonEvidence(
        studies=studies,
        inferences=projection.inferences,
        parent_anchor_reconciled=projection.parent_anchor_reconciled,
    )
    if evidence.qa_counts != projection.qa_counts:
        raise CrossPoolContractError("artifact QA counts do not match the manifest")
    validate_rendered_short_horizon_artifacts(artifacts, evidence)


def _write_fsynced_file(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("candidate write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ShortHorizonPublicationError("candidate file could not be written") from exc


def _rename_directory_no_replace(source: Path, target: Path) -> None:
    """Atomically rename one directory only when the target is still absent."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as exc:  # pragma: no cover - supported macOS surface.
            raise ShortHorizonPublicationError(
                "atomic no-replace publication is unavailable"
            ) from exc
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(source_bytes, target_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:  # pragma: no cover - platform dependent.
            raise ShortHorizonPublicationError(
                "atomic no-replace publication is unavailable"
            ) from exc
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, target_bytes, 0x00000001)
    else:  # pragma: no cover - publication requires POSIX fcntl.
        raise ShortHorizonPublicationError("atomic no-replace publication is unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise ShortHorizonPublicationError("new publication target is no longer absent")
    raise OSError(error_number, os.strerror(error_number), target)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ShortHorizonPublicationError("evidence directory could not be synchronized") from exc


def _remove_review_stage(stage_dir: Path) -> None:
    try:
        shutil.rmtree(stage_dir)
    except OSError as exc:
        raise ShortHorizonPublicationError("private review stage could not be removed") from exc
