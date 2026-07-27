from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import pytest

from research.cross_pool.short_horizon_manifest import (
    canonical_short_horizon_manifest_bytes,
)
from research.cross_pool.short_horizon_publication import (
    ShortHorizonPublicationError,
    classify_short_horizon_output,
    output_lock,
    publish_or_verify_short_horizon_candidate,
    review_short_horizon_evidence,
    validate_short_horizon_evidence_directory,
    write_short_horizon_candidate,
)
from research.tests.short_horizon_manifest_fixtures import (
    sealed_artifacts as rendered_artifacts,
)
from research.tests.short_horizon_manifest_fixtures import (
    sealed_manifest as generated_manifest,
)


def test_publish_new_then_compare_byte_identical_candidate(tmp_path: Path) -> None:
    out_dir = tmp_path / "evidence"
    first_candidate = tmp_path / "candidate-one"
    second_candidate = tmp_path / "candidate-two"
    manifest = generated_manifest()
    artifacts = rendered_artifacts()

    with output_lock(out_dir):
        assert classify_short_horizon_output(out_dir) == "publish_new"
        write_short_horizon_candidate(first_candidate, artifacts, manifest)
        publish_or_verify_short_horizon_candidate(
            out_dir,
            first_candidate,
            mode="publish_new",
        )
    assert validate_short_horizon_evidence_directory(out_dir) == manifest

    with output_lock(out_dir):
        assert classify_short_horizon_output(out_dir) == "verify_existing"
        write_short_horizon_candidate(second_candidate, artifacts, manifest)
        publish_or_verify_short_horizon_candidate(
            out_dir,
            second_candidate,
            mode="verify_existing",
        )
    assert not second_candidate.exists()


def test_candidate_mismatch_and_symlink_output_fail_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    manifest = generated_manifest()
    write_short_horizon_candidate(candidate, rendered_artifacts(), manifest)
    publish_or_verify_short_horizon_candidate(
        out_dir,
        candidate,
        mode="publish_new",
    )

    mismatched = tmp_path / "candidate-mismatch"
    changed = deepcopy(manifest)
    changed_provenance = dict(changed["provenance"])
    changed_runtime = dict(changed_provenance["runtime"])
    changed_runtime["numpy"] = "9.9.9"
    changed_provenance["runtime"] = changed_runtime
    changed["provenance"] = changed_provenance
    write_short_horizon_candidate(mismatched, rendered_artifacts(), changed)
    with pytest.raises(ShortHorizonPublicationError, match="rerun bytes changed"):
        publish_or_verify_short_horizon_candidate(
            out_dir,
            mismatched,
            mode="verify_existing",
        )

    semantically_invalid = tmp_path / "candidate-invalid"
    invalid_artifacts = list(rendered_artifacts())
    invalid_artifacts[0] = type(invalid_artifacts[0])(
        relative_name=invalid_artifacts[0].relative_name,
        content=b"changed\n",
    )
    invalid_manifest = deepcopy(manifest)
    invalid_manifest["artifacts"] = dict(manifest["artifacts"])
    invalid_manifest["artifacts"][invalid_artifacts[0].relative_name] = invalid_artifacts[0].sha256
    with pytest.raises(ShortHorizonPublicationError, match="semantically bound"):
        write_short_horizon_candidate(
            semantically_invalid,
            tuple(invalid_artifacts),
            invalid_manifest,
        )

    symlink = tmp_path / "evidence-link"
    symlink.symlink_to(out_dir, target_is_directory=True)
    with pytest.raises(ShortHorizonPublicationError, match="valid evidence directory"):
        classify_short_horizon_output(symlink)


def test_output_lock_rejects_a_second_holder(tmp_path: Path) -> None:
    out_dir = tmp_path / "evidence"

    with output_lock(out_dir):
        with pytest.raises(ShortHorizonPublicationError, match="holds the lock"):
            with output_lock(out_dir):
                pass


def test_output_lock_rejects_a_symlink_lock_file(tmp_path: Path) -> None:
    out_dir = tmp_path / "evidence"
    target = tmp_path / "lock-target"
    target.write_text("do not follow\n", encoding="utf-8")
    (tmp_path / ".evidence.lock").symlink_to(target)

    with pytest.raises(ShortHorizonPublicationError, match="could not be opened"):
        with output_lock(out_dir):
            pass

    assert target.read_text(encoding="utf-8") == "do not follow\n"


def test_review_is_atomic_manifest_only_and_makes_output_immutable(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    manifest = generated_manifest()
    artifacts = rendered_artifacts()
    write_short_horizon_candidate(candidate, artifacts, manifest)
    publish_or_verify_short_horizon_candidate(
        out_dir,
        candidate,
        mode="publish_new",
    )
    before = {
        name: ((out_dir / name).read_bytes(), (out_dir / name).stat().st_mtime_ns)
        for name in manifest["artifacts"]
    }

    reviewed = review_short_horizon_evidence(
        out_dir,
        reviewed_by="sol_ultra",
        reviewed_at_utc="2026-07-27T12:00:00Z",
    )

    assert reviewed["artifact_status"] == "reviewed"
    assert all(
        ((out_dir / name).read_bytes(), (out_dir / name).stat().st_mtime_ns) == identity
        for name, identity in before.items()
    )
    assert not any(path.name.startswith(f".{out_dir.name}.review-") for path in tmp_path.iterdir())
    with pytest.raises(ShortHorizonPublicationError, match="immutable"):
        classify_short_horizon_output(out_dir)
    with pytest.raises(ShortHorizonPublicationError, match="QA-pass generated"):
        review_short_horizon_evidence(
            out_dir,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T12:00:00Z",
        )


def test_review_staging_failure_preserves_original_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = __import__(
        "research.cross_pool.short_horizon_publication",
        fromlist=["short_horizon_publication"],
    )
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    manifest = generated_manifest()
    write_short_horizon_candidate(candidate, rendered_artifacts(), manifest)
    publish_or_verify_short_horizon_candidate(
        out_dir,
        candidate,
        mode="publish_new",
    )
    original = (out_dir / "short_horizon_manifest.json").read_bytes()
    real_replace = os.replace

    def fail_first_replace(source: str | bytes | Path, target: str | bytes | Path) -> None:
        if Path(target).name == "short_horizon_manifest.json":
            raise OSError("injected replace failure")
        real_replace(source, target)

    monkeypatch.setattr(publication.os, "replace", fail_first_replace)
    with pytest.raises(ShortHorizonPublicationError, match="review publication failed"):
        review_short_horizon_evidence(
            out_dir,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T12:00:00Z",
        )

    assert (out_dir / "short_horizon_manifest.json").read_bytes() == original
    assert validate_short_horizon_evidence_directory(out_dir) == manifest


def test_review_directory_sync_failure_rolls_back_original_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = __import__(
        "research.cross_pool.short_horizon_publication",
        fromlist=["short_horizon_publication"],
    )
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    manifest = generated_manifest()
    write_short_horizon_candidate(candidate, rendered_artifacts(), manifest)
    publish_or_verify_short_horizon_candidate(
        out_dir,
        candidate,
        mode="publish_new",
    )
    original = (out_dir / "short_horizon_manifest.json").read_bytes()
    real_fsync = publication._fsync_directory
    failed = False

    def fail_review_sync(directory: Path) -> None:
        nonlocal failed
        if directory == out_dir and not failed:
            failed = True
            raise OSError("injected directory sync failure")
        real_fsync(directory)

    monkeypatch.setattr(publication, "_fsync_directory", fail_review_sync)
    with pytest.raises(ShortHorizonPublicationError, match="rolled back"):
        review_short_horizon_evidence(
            out_dir,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T12:00:00Z",
        )

    assert (out_dir / "short_horizon_manifest.json").read_bytes() == original
    assert validate_short_horizon_evidence_directory(out_dir) == manifest


def test_review_stage_cleanup_failure_is_visible_after_valid_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = __import__(
        "research.cross_pool.short_horizon_publication",
        fromlist=["short_horizon_publication"],
    )
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    manifest = generated_manifest()
    write_short_horizon_candidate(candidate, rendered_artifacts(), manifest)
    publish_or_verify_short_horizon_candidate(
        out_dir,
        candidate,
        mode="publish_new",
    )

    def fail_cleanup(_path: Path) -> None:
        raise OSError("injected review-stage cleanup failure")

    monkeypatch.setattr(publication.shutil, "rmtree", fail_cleanup)
    with pytest.raises(ShortHorizonPublicationError, match="review stage could not be removed"):
        review_short_horizon_evidence(
            out_dir,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T12:00:00Z",
        )

    reviewed = validate_short_horizon_evidence_directory(out_dir)
    assert reviewed["artifact_status"] == "reviewed"


def test_candidate_writer_rejects_hash_mismatch_and_existing_path(tmp_path: Path) -> None:
    manifest = generated_manifest()
    artifacts = rendered_artifacts()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    with pytest.raises(ShortHorizonPublicationError, match="must not already exist"):
        write_short_horizon_candidate(candidate, artifacts, manifest)

    candidate.rmdir()
    changed = deepcopy(manifest)
    changed_artifacts = dict(changed["artifacts"])
    changed_artifacts[artifacts[0].relative_name] = "0" * 64
    changed["artifacts"] = changed_artifacts
    with pytest.raises(Exception, match="artifact"):
        write_short_horizon_candidate(candidate, artifacts, changed)


def test_candidate_write_failure_removes_partial_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = __import__(
        "research.cross_pool.short_horizon_publication",
        fromlist=["short_horizon_publication"],
    )
    candidate = tmp_path / "candidate"
    real_write = publication._write_fsynced_file
    write_count = 0

    def fail_second_write(path: Path, content: bytes) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected candidate write failure")
        real_write(path, content)

    monkeypatch.setattr(publication, "_write_fsynced_file", fail_second_write)
    with pytest.raises(ShortHorizonPublicationError, match="could not be sealed"):
        write_short_horizon_candidate(
            candidate,
            rendered_artifacts(),
            generated_manifest(),
        )

    assert not candidate.exists()


def test_manifest_file_is_written_with_exact_canonical_bytes(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    manifest = generated_manifest()
    write_short_horizon_candidate(candidate, rendered_artifacts(), manifest)

    assert (candidate / "short_horizon_manifest.json").read_bytes() == (
        canonical_short_horizon_manifest_bytes(manifest)
    )
