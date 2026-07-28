"""Atomic publication and explicit review for challenger evidence."""

from __future__ import annotations

import csv
import ctypes
import errno
import fcntl
import hashlib
import io
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

from research.cross_pool.challenger_artifacts import (
    ChallengerArtifact,
)
from research.cross_pool.challenger_manifest import (
    REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENT,
    REQUIRED_REVIEW_ADJUDICATIONS,
    V1_ARTIFACT_SHA256,
    V1_MANIFEST_SHA256,
    JsonValue,
    canonical_challenger_manifest_bytes,
    load_challenger_manifest,
    reviewed_challenger_manifest,
    validate_challenger_manifest,
)
from research.cross_pool.challenger_reporting import (
    validate_challenger_artifact_bundle,
    validate_challenger_artifact_projection,
)
from research.cross_pool.contracts import CrossPoolContractError

PublicationMode = Literal["publish_new", "verify_existing"]
_MANIFEST_NAME = "challenger_manifest.json"


class ChallengerPublicationError(RuntimeError):
    """Raised when challenger evidence cannot be published safely."""


@contextmanager
def challenger_output_lock(out_dir: Path) -> Iterator[None]:
    parent = out_dir.parent
    if not parent.is_dir() or out_dir.name in ("", ".", ".."):
        raise ChallengerPublicationError("output parent must be an existing directory")
    lock_path = parent / f".{out_dir.name}.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ChallengerPublicationError("challenger output lock could not be opened") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ChallengerPublicationError(
                "another challenger invocation holds the lock"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def classify_challenger_output(out_dir: Path) -> PublicationMode:
    if not os.path.lexists(out_dir):
        return "publish_new"
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise ChallengerPublicationError("existing challenger output is not a directory")
    manifest = validate_challenger_evidence_directory(out_dir)
    if not _is_verifiable(manifest):
        raise ChallengerPublicationError("existing challenger evidence is immutable")
    return "verify_existing"


def write_challenger_candidate(
    candidate_dir: Path,
    artifacts: Sequence[ChallengerArtifact],
    manifest: dict[str, JsonValue],
) -> None:
    if os.path.lexists(candidate_dir):
        raise ChallengerPublicationError("challenger candidate already exists")
    validate_challenger_manifest(manifest)
    materialized = tuple(artifacts)
    artifact_map = _manifest_artifact_map(manifest)
    names = tuple(artifact.relative_name for artifact in materialized)
    if len(set(names)) != len(names) or set(names) != set(artifact_map):
        raise ChallengerPublicationError("candidate artifacts do not match the manifest")
    for artifact in materialized:
        if artifact.sha256 != artifact_map[artifact.relative_name]:
            raise ChallengerPublicationError(
                f"candidate artifact hash mismatch for {artifact.relative_name}"
            )
    created = False
    try:
        candidate_dir.mkdir(mode=0o700)
        created = True
        for artifact in materialized:
            _write_fsynced_file(candidate_dir / artifact.relative_name, artifact.content)
        _write_fsynced_file(
            candidate_dir / _MANIFEST_NAME,
            canonical_challenger_manifest_bytes(manifest),
        )
        _fsync_directory(candidate_dir)
        validate_challenger_evidence_directory(candidate_dir)
    except (ChallengerPublicationError, CrossPoolContractError, OSError) as exc:
        if created:
            shutil.rmtree(candidate_dir, ignore_errors=True)
        if isinstance(exc, ChallengerPublicationError):
            raise
        raise ChallengerPublicationError("challenger candidate could not be sealed") from exc


def publish_or_verify_challenger_candidate(
    out_dir: Path,
    candidate_dir: Path,
    *,
    mode: PublicationMode,
) -> None:
    candidate_manifest = validate_challenger_evidence_directory(candidate_dir)
    if mode == "publish_new":
        if os.path.lexists(out_dir):
            raise ChallengerPublicationError("challenger output appeared during publication")
        _rename_directory_no_replace(candidate_dir, out_dir)
        _fsync_directory(out_dir.parent)
        validate_challenger_evidence_directory(out_dir)
        return
    if mode != "verify_existing" or not _is_verifiable(candidate_manifest):
        raise ChallengerPublicationError("unsupported challenger verification state")
    existing_manifest = validate_challenger_evidence_directory(out_dir)
    if not _is_verifiable(existing_manifest):
        raise ChallengerPublicationError("existing challenger evidence became immutable")
    existing_names = {path.name for path in out_dir.iterdir()}
    candidate_names = {path.name for path in candidate_dir.iterdir()}
    if existing_names != candidate_names:
        raise ChallengerPublicationError("challenger deterministic filename set changed")
    for name in sorted(existing_names):
        if (out_dir / name).read_bytes() != (candidate_dir / name).read_bytes():
            raise ChallengerPublicationError(
                f"challenger deterministic bytes changed for {name}"
            )
    shutil.rmtree(candidate_dir)


def validate_challenger_evidence_directory(
    directory: Path,
) -> dict[str, JsonValue]:
    if directory.is_symlink() or not directory.is_dir():
        raise ChallengerPublicationError("challenger evidence path must be a regular directory")
    entries = tuple(directory.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ChallengerPublicationError("challenger evidence contains a non-file entry")
    manifest = load_challenger_manifest(directory / _MANIFEST_NAME)
    artifact_map = _manifest_artifact_map(manifest)
    expected_names = set(artifact_map) | {_MANIFEST_NAME}
    if {path.name for path in entries} != expected_names:
        raise ChallengerPublicationError("challenger evidence file set is not exact")
    artifact_bytes: dict[str, bytes] = {}
    for name, expected_digest in artifact_map.items():
        raw = (directory / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise ChallengerPublicationError(f"challenger artifact hash mismatch for {name}")
        _validate_artifact_projection(name, raw)
        artifact_bytes[name] = raw
    if artifact_bytes:
        try:
            failure_count = validate_challenger_artifact_bundle(artifact_bytes)
        except CrossPoolContractError as exc:
            raise ChallengerPublicationError(
                "challenger artifact bundle is quantitatively inconsistent"
            ) from exc
        registry = manifest.get("registry")
        if not isinstance(registry, dict) or registry.get("model_fit_failures") != failure_count:
            raise ChallengerPublicationError(
                "challenger manifest failure count does not match the artifacts"
            )
    return manifest


def validate_v1_supersession_baseline(directory: Path) -> None:
    try:
        raw_manifest = (directory / _MANIFEST_NAME).read_bytes()
    except OSError as exc:
        raise ChallengerPublicationError(
            "immutable challenger v1 manifest could not be read"
        ) from exc
    if hashlib.sha256(raw_manifest).hexdigest() != V1_MANIFEST_SHA256:
        raise ChallengerPublicationError("immutable challenger v1 manifest hash changed")
    manifest = validate_challenger_evidence_directory(directory)
    if (
        manifest.get("schema_version") != "1.0.0"
        or manifest.get("artifact_status") != "generated_unreviewed"
        or manifest.get("artifacts") != V1_ARTIFACT_SHA256
    ):
        raise ChallengerPublicationError("immutable challenger v1 baseline is invalid")


def review_challenger_evidence(
    out_dir: Path,
    *,
    supersedes_dir: Path,
    reviewed_by: str,
    reviewed_at_utc: str,
    numerical_null_acknowledgement: str,
) -> None:
    with challenger_output_lock(out_dir):
        validate_v1_supersession_baseline(supersedes_dir)
        original = validate_challenger_evidence_directory(out_dir)
        if numerical_null_acknowledgement != REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENT:
            raise ChallengerPublicationError(
                "required numerical-null acknowledgement is missing"
            )
        _validate_required_review_adjudication(out_dir)
        reviewed = reviewed_challenger_manifest(
            original,
            reviewed_by=reviewed_by,
            reviewed_at_utc=reviewed_at_utc,
            adjudications=REQUIRED_REVIEW_ADJUDICATIONS,
        )
        original_bytes = canonical_challenger_manifest_bytes(original)
        reviewed_bytes = canonical_challenger_manifest_bytes(reviewed)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".challenger-review-",
            dir=out_dir,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(reviewed_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, out_dir / _MANIFEST_NAME)
            _fsync_directory(out_dir)
            validate_challenger_evidence_directory(out_dir)
        except Exception as exc:
            temporary_path.unlink(missing_ok=True)
            rollback = out_dir / ".challenger-review-rollback"
            try:
                _write_fsynced_file(rollback, original_bytes)
                os.replace(rollback, out_dir / _MANIFEST_NAME)
                _fsync_directory(out_dir)
                validate_challenger_evidence_directory(out_dir)
            except Exception as rollback_exc:
                raise ChallengerPublicationError(
                    "challenger review and rollback both failed"
                ) from rollback_exc
            raise ChallengerPublicationError(
                "challenger review failed and was rolled back"
            ) from exc


def _validate_required_review_adjudication(out_dir: Path) -> None:
    raw = (out_dir / "challenger_contrasts.csv").read_bytes()
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    except UnicodeDecodeError as exc:  # pragma: no cover - bundle validation precedes review.
        raise ChallengerPublicationError("challenger contrast review row is invalid") from exc
    expected = REQUIRED_REVIEW_ADJUDICATIONS[0]
    cell = cast(dict[str, JsonValue], expected["cell"])
    matches = [
        row
        for row in reader
        if row["family"] == cell["family"]
        and row["direction"] == cell["direction"]
        and int(row["horizon_ms"]) == cell["horizon_ms"]
        and row["support"] == cell["support"]
        and row["endpoint"] == cell["endpoint"]
        and row["contrast"] == cell["contrast"]
        and row["comparator_variant"] == cell["comparator_variant"]
        and row["candidate_variant"] == cell["candidate_variant"]
    ]
    if len(matches) != 1:
        raise ChallengerPublicationError("required numerical-null cell is missing")
    row = matches[0]
    observed = cast(dict[str, JsonValue], expected["observed"])
    actual = {
        "point_bps": float(row["point"]),
        "simultaneous_lower_bps": float(row["simultaneous_lower"]),
        "simultaneous_upper_bps": float(row["simultaneous_upper"]),
        "adjusted_p_value": float(row["adjusted_p_value"]),
    }
    if row["status"] != "adjudicable" or actual != observed:
        raise ChallengerPublicationError(
            "required numerical-null observation does not match the review record"
        )


def _is_verifiable(manifest: dict[str, JsonValue]) -> bool:
    qa = manifest.get("qa")
    review = manifest.get("review")
    return (
        manifest.get("artifact_status") == "generated_unreviewed"
        and isinstance(qa, dict)
        and qa.get("status") == "pass"
        and isinstance(review, dict)
        and review.get("status") == "pending"
    )


def _manifest_artifact_map(manifest: dict[str, JsonValue]) -> dict[str, str]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str)
        for name, digest in raw.items()
    ):
        raise ChallengerPublicationError("challenger manifest artifact map is invalid")
    return cast(dict[str, str], raw)


def _validate_artifact_projection(name: str, raw: bytes) -> None:
    try:
        validate_challenger_artifact_projection(name, raw)
    except CrossPoolContractError as exc:
        raise ChallengerPublicationError(
            f"challenger artifact projection is invalid for {name}"
        ) from exc


def _write_fsynced_file(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("challenger write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ChallengerPublicationError("challenger file could not be written") from exc


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_no_replace(source: Path, target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(source_bytes, target_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, target_bytes, 1)
    else:  # pragma: no cover - supported research platforms are macOS and Linux.
        raise ChallengerPublicationError("atomic no-replace rename is unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise ChallengerPublicationError("challenger publication target already exists")
        raise ChallengerPublicationError(
            f"challenger atomic publication failed with errno {error_number}"
        )
