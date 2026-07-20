"""Atomic publication and compare-only verification for frozen evidence."""

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
from copy import deepcopy
from pathlib import Path
from typing import Literal, cast

from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.manifest import (
    JsonValue,
    canonical_manifest_bytes,
    load_and_validate_article_manifest,
    validate_article_manifest,
)
from research.cross_pool.reporting import RenderedArtifact

PublicationMode = Literal["publish_new", "verify_existing"]

_MANIFEST_NAME = "article_manifest.json"


class OutputPublicationError(RuntimeError):
    """Raised when evidence cannot be sealed, published, or compared safely."""


@contextmanager
def output_lock(out_dir: Path) -> Iterator[None]:
    """Serialize analysis and publication for one canonical output directory."""
    parent = out_dir.parent
    if not parent.is_dir() or out_dir.name in ("", ".", ".."):
        raise OutputPublicationError("output parent must be an existing directory")
    lock_path = parent / f".{out_dir.name}.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise OutputPublicationError("output lock could not be opened") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OutputPublicationError("another output invocation holds the lock") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def classify_output(out_dir: Path) -> PublicationMode:
    """Classify an absent target or one immutable compare-only evidence state."""
    if not os.path.lexists(out_dir):
        return "publish_new"
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise OutputPublicationError("existing output is not a valid evidence directory")
    try:
        manifest = validate_evidence_directory(out_dir)
    except (CrossPoolContractError, OSError, OutputPublicationError) as exc:
        raise OutputPublicationError("existing output is not valid evidence") from exc
    if not _is_verifiable_statistical_manifest(manifest):
        raise OutputPublicationError("existing evidence state is immutable")
    return "verify_existing"


def write_sealed_candidate(
    candidate_dir: Path,
    artifacts: Sequence[RenderedArtifact],
    manifest: Mapping[str, JsonValue],
) -> None:
    """Write a complete private candidate, with its validated manifest last."""
    if os.path.lexists(candidate_dir):
        raise OutputPublicationError("candidate directory must not already exist")
    validate_article_manifest(manifest)
    artifact_map = _artifact_map(manifest)
    materialized = tuple(artifacts)
    names = tuple(artifact.relative_name for artifact in materialized)
    if len(set(names)) != len(names) or set(names) != set(artifact_map):
        raise OutputPublicationError("candidate artifacts do not match the manifest")
    for artifact in materialized:
        if artifact.sha256 != artifact_map[artifact.relative_name]:
            raise OutputPublicationError(
                f"candidate artifact hash mismatch for {artifact.relative_name}"
            )

    candidate_dir.mkdir(mode=0o700)
    for artifact in materialized:
        _write_fsynced_file(candidate_dir / artifact.relative_name, artifact.content)
    _write_fsynced_file(
        candidate_dir / _MANIFEST_NAME,
        canonical_manifest_bytes(manifest),
    )
    _fsync_directory(candidate_dir)
    validate_evidence_directory(candidate_dir)


def publish_or_verify_candidate(
    out_dir: Path,
    candidate_dir: Path,
    *,
    mode: PublicationMode,
) -> None:
    """Publish to an absent target or prove an existing run byte-identical."""
    candidate_manifest = validate_evidence_directory(candidate_dir)
    if mode == "publish_new":
        if os.path.lexists(out_dir):
            raise OutputPublicationError("new publication target is no longer absent")
        try:
            _rename_directory_no_replace(candidate_dir, out_dir)
            _fsync_directory(out_dir.parent)
        except OSError as exc:
            raise OutputPublicationError("candidate publication failed") from exc
        return

    if mode != "verify_existing":
        raise OutputPublicationError("unsupported publication mode")
    if not _is_verifiable_statistical_manifest(candidate_manifest):
        raise OutputPublicationError(
            "compare-only verification requires generated statistical evidence"
        )
    try:
        existing_manifest = validate_evidence_directory(out_dir)
    except (CrossPoolContractError, OSError, OutputPublicationError) as exc:
        raise OutputPublicationError("existing evidence changed during verification") from exc
    if not _is_verifiable_statistical_manifest(existing_manifest):
        raise OutputPublicationError("existing evidence became immutable")
    if _reproduction_key(existing_manifest) != _reproduction_key(candidate_manifest):
        raise OutputPublicationError("candidate evidence is not provenance-comparable")

    existing_names = {path.name for path in out_dir.iterdir()}
    candidate_names = {path.name for path in candidate_dir.iterdir()}
    if existing_names != candidate_names:
        raise OutputPublicationError("deterministic rerun filename set changed")
    for name in sorted(existing_names):
        if (out_dir / name).read_bytes() != (candidate_dir / name).read_bytes():
            raise OutputPublicationError("deterministic rerun bytes changed")
    final_manifest = validate_evidence_directory(out_dir)
    if canonical_manifest_bytes(final_manifest) != canonical_manifest_bytes(
        existing_manifest
    ):
        raise OutputPublicationError("existing evidence changed during verification")
    shutil.rmtree(candidate_dir)


def augment_evidence_directory(
    out_dir: Path,
    artifacts: Sequence[RenderedArtifact],
    manifest: Mapping[str, JsonValue],
) -> None:
    """Atomically replace sealed statistics with their economic augmentation.

    The caller must hold ``output_lock(out_dir)`` for the validation, analysis,
    and augmentation transaction.
    """
    existing_manifest = validate_evidence_directory(out_dir)
    if not _is_verifiable_statistical_manifest(existing_manifest):
        raise OutputPublicationError(
            "economic augmentation requires generated statistical evidence"
        )
    validate_article_manifest(manifest)
    if not _is_economic_augmented_manifest(manifest):
        raise OutputPublicationError(
            "economic augmentation requires an unreviewed terminal economic state"
        )

    existing_artifacts = _artifact_map(existing_manifest)
    augmented_artifacts = _artifact_map(manifest)
    if any(
        augmented_artifacts.get(name) != digest
        for name, digest in existing_artifacts.items()
    ):
        raise OutputPublicationError(
            "economic augmentation cannot replace statistical artifacts"
        )
    if _statistical_manifest_projection(
        existing_manifest,
        existing_artifacts,
    ) != _statistical_manifest_projection(manifest, existing_artifacts):
        raise OutputPublicationError(
            "economic augmentation cannot change sealed statistical manifest fields"
        )
    additions = tuple(artifacts)
    addition_names = tuple(artifact.relative_name for artifact in additions)
    expected_additions = set(augmented_artifacts).difference(existing_artifacts)
    if len(set(addition_names)) != len(addition_names) or set(addition_names) != expected_additions:
        raise OutputPublicationError(
            "economic augmentation artifacts do not match the merged manifest"
        )
    addition_map = {artifact.relative_name: artifact for artifact in additions}
    for name, artifact in addition_map.items():
        if artifact.sha256 != augmented_artifacts[name]:
            raise OutputPublicationError(
                f"economic augmentation artifact hash mismatch for {name}"
            )

    stage_dir = Path(
        tempfile.mkdtemp(prefix=f".{out_dir.name}.economic-", dir=out_dir.parent)
    )
    exchanged = False
    try:
        for name in sorted(augmented_artifacts):
            content = (
                addition_map[name].content
                if name in addition_map
                else (out_dir / name).read_bytes()
            )
            _write_fsynced_file(stage_dir / name, content)
        _write_fsynced_file(
            stage_dir / _MANIFEST_NAME,
            canonical_manifest_bytes(manifest),
        )
        _fsync_directory(stage_dir)
        validate_evidence_directory(stage_dir)

        current_manifest = validate_evidence_directory(out_dir)
        if canonical_manifest_bytes(current_manifest) != canonical_manifest_bytes(
            existing_manifest
        ):
            raise OutputPublicationError(
                "statistical evidence changed during economic augmentation"
            )
        _rename_directory_exchange(stage_dir, out_dir)
        exchanged = True
        _fsync_directory(out_dir.parent)
        try:
            validate_evidence_directory(out_dir)
        except (CrossPoolContractError, OSError, OutputPublicationError) as exc:
            try:
                _rename_directory_exchange(stage_dir, out_dir)
                exchanged = False
                _fsync_directory(out_dir.parent)
            except (OSError, OutputPublicationError) as rollback_exc:
                raise OutputPublicationError(
                    "economic augmentation validation and rollback both failed"
                ) from rollback_exc
            raise OutputPublicationError(
                "economic augmentation failed final validation and was rolled back"
            ) from exc
        shutil.rmtree(stage_dir)
        exchanged = False
        _fsync_directory(out_dir.parent)
    except Exception:
        if not exchanged and os.path.lexists(stage_dir):
            shutil.rmtree(stage_dir)
        raise


def validate_evidence_directory(directory: Path) -> dict[str, JsonValue]:
    """Validate canonical bytes, exact file coverage, and every artifact hash."""
    if directory.is_symlink() or not directory.is_dir():
        raise OutputPublicationError("evidence path must be a regular directory")
    entries = tuple(directory.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise OutputPublicationError("evidence directory contains a non-file entry")
    manifest_path = directory / _MANIFEST_NAME
    manifest = load_and_validate_article_manifest(manifest_path)
    artifact_map = _artifact_map(manifest)
    expected_names = set(artifact_map) | {_MANIFEST_NAME}
    if {path.name for path in entries} != expected_names:
        raise OutputPublicationError("evidence directory has an unexpected file set")
    for name, expected_digest in artifact_map.items():
        actual_digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise OutputPublicationError(f"evidence artifact hash mismatch for {name}")
    return manifest


def _artifact_map(manifest: Mapping[str, JsonValue]) -> dict[str, str]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str)
        for name, digest in raw.items()
    ):
        raise OutputPublicationError("manifest artifact map is invalid")
    return cast(dict[str, str], raw)


def _statistical_manifest_projection(
    manifest: Mapping[str, JsonValue],
    statistical_artifacts: Mapping[str, str],
) -> bytes:
    projected = deepcopy(dict(manifest))
    projected.pop("economics", None)
    publication = cast(dict[str, JsonValue], projected["publication"])
    publication.pop("economic_class", None)
    figures = cast(dict[str, JsonValue], projected["figures"])
    figures.pop("lp_performance", None)
    artifacts = cast(dict[str, JsonValue], projected["artifacts"])
    projected["artifacts"] = {
        name: artifacts.get(name) for name in statistical_artifacts
    }
    return canonical_manifest_bytes(projected)


def _is_verifiable_statistical_manifest(manifest: Mapping[str, JsonValue]) -> bool:
    qa = manifest.get("qa")
    economics = manifest.get("economics")
    review = manifest.get("review")
    return (
        manifest.get("artifact_status") == "generated_unreviewed"
        and isinstance(qa, dict)
        and qa.get("status") == "pass"
        and isinstance(economics, dict)
        and economics.get("status") == "pending"
        and isinstance(review, dict)
        and review.get("status") == "pending"
    )


def _is_economic_augmented_manifest(manifest: Mapping[str, JsonValue]) -> bool:
    qa = manifest.get("qa")
    economics = manifest.get("economics")
    review = manifest.get("review")
    return (
        manifest.get("artifact_status") == "generated_unreviewed"
        and isinstance(qa, dict)
        and qa.get("status") == "pass"
        and isinstance(economics, dict)
        and economics.get("status") in {"complete", "not_adjudicable_qa"}
        and isinstance(review, dict)
        and review.get("status") == "pending"
    )


def _reproduction_key(manifest: Mapping[str, JsonValue]) -> bytes:
    return canonical_manifest_bytes(
        {
            "schema_version": manifest["schema_version"],
            "provenance": manifest["provenance"],
        }
    )


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
        raise OutputPublicationError("candidate file could not be written") from exc


def _rename_directory_no_replace(source: Path, target: Path) -> None:
    """Atomically rename one directory only when the target is still absent."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as exc:  # pragma: no cover - supported macOS surface.
            raise OutputPublicationError(
                "atomic no-replace publication is unavailable"
            ) from exc
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(source_bytes, target_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:  # pragma: no cover - platform dependent.
            raise OutputPublicationError(
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
    else:  # pragma: no cover - publication already requires POSIX fcntl.
        raise OutputPublicationError(
            "atomic no-replace publication is unavailable"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise OutputPublicationError(
            "new publication target is no longer absent"
        )
    raise OSError(error_number, os.strerror(error_number), target)


def _rename_directory_exchange(source: Path, target: Path) -> None:
    """Atomically exchange two existing directories on supported POSIX hosts."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as exc:  # pragma: no cover - supported macOS surface.
            raise OutputPublicationError(
                "atomic directory exchange is unavailable"
            ) from exc
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(source_bytes, target_bytes, 0x00000002)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:  # pragma: no cover - platform dependent.
            raise OutputPublicationError(
                "atomic directory exchange is unavailable"
            ) from exc
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, target_bytes, 0x00000002)
    else:  # pragma: no cover - publication already requires POSIX fcntl.
        raise OutputPublicationError("atomic directory exchange is unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number), target)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise OutputPublicationError("evidence directory could not be synchronized") from exc
