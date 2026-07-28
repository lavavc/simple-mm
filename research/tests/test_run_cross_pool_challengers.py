from __future__ import annotations

import csv
import hashlib
import io
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from research.cross_pool import challenger_publication
from research.cross_pool.challenger_artifacts import (
    CHALLENGER_ARTIFACT_NAMES,
    ChallengerArtifact,
)
from research.cross_pool.challenger_manifest import (
    REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENTS,
    REQUIRED_REVIEW_ADJUDICATIONS,
    REQUIRED_REVIEW_SUMMARY,
    canonical_challenger_manifest_bytes,
)
from research.cross_pool.challenger_publication import (
    ChallengerPublicationError,
    review_challenger_evidence,
    validate_challenger_evidence_directory,
    validate_v1_1_supersession_baseline,
    write_challenger_candidate,
)
from research.cross_pool.contracts import CrossPoolContractError
from research.scripts.run_cross_pool_challengers import (
    ChallengerCliError,
    RunArguments,
    _capture_source_identity_at,
    run,
)

PARENT_DIR = Path("research/results/cross_pool_lead_lag")
V1_1_DIR = Path("research/results/cross_pool_challengers_v1_1")


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode("utf-8").strip()


def test_source_identity_hashes_actual_diff_and_rejects_untracked_closure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "research@example.invalid")
    _git(repository, "config", "user.name", "Research Test")
    source = repository / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "source.py")
    _git(repository, "commit", "-m", "fixture")
    fixture_commit = _git(repository, "rev-parse", "HEAD")

    clean_commit, clean_diff = _capture_source_identity_at(
        repository_root=repository,
        source_paths=("source.py",),
    )
    assert clean_commit == fixture_commit
    assert clean_diff == hashlib.sha256(b"").hexdigest()

    source.write_text("VALUE = 2\n", encoding="utf-8")
    first = _capture_source_identity_at(
        repository_root=repository,
        source_paths=("source.py",),
    )
    second = _capture_source_identity_at(
        repository_root=repository,
        source_paths=("source.py",),
    )
    assert first == second
    assert first[1] != clean_diff

    (repository / "untracked.py").write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(ChallengerCliError, match="must be tracked"):
        _capture_source_identity_at(
            repository_root=repository,
            source_paths=("source.py", "untracked.py"),
        )


def test_v1_1_supersession_baseline_rejects_altered_artifact(
    tmp_path: Path,
) -> None:
    altered = tmp_path / "altered-v1-1"
    shutil.copytree(V1_1_DIR, altered)
    metrics = altered / "challenger_metrics.csv"
    metrics.write_bytes(metrics.read_bytes() + b"tamper\n")

    with pytest.raises(ChallengerPublicationError, match="artifact hash mismatch"):
        validate_v1_1_supersession_baseline(altered)

    changed_manifest = tmp_path / "changed-v1-1-manifest"
    shutil.copytree(V1_1_DIR, changed_manifest)
    manifest_path = changed_manifest / "challenger_manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    with pytest.raises(ChallengerPublicationError, match="manifest hash changed"):
        validate_v1_1_supersession_baseline(changed_manifest)


def test_review_evidence_validates_both_numerical_nulls_and_summary(
    tmp_path: Path,
) -> None:
    assert (
        challenger_publication._validate_required_review_evidence(V1_1_DIR)
        == REQUIRED_REVIEW_SUMMARY
    )

    for index, adjudication in enumerate(REQUIRED_REVIEW_ADJUDICATIONS):
        altered = tmp_path / f"altered-null-{index}"
        shutil.copytree(V1_1_DIR, altered)
        contrasts = altered / "challenger_contrasts.csv"
        raw = contrasts.read_bytes()
        observed = adjudication["observed"]
        assert isinstance(observed, dict)
        point = repr(observed["point_bps"]).encode("ascii")
        assert point in raw
        contrasts.write_bytes(raw.replace(point, b"-9e-12", 1))
        with pytest.raises(ChallengerPublicationError, match="observation"):
            challenger_publication._validate_required_review_evidence(altered)


def test_run_publish_compare_and_review_are_atomic_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "challengers"
    arguments = RunArguments(
        parent_dir=PARENT_DIR,
        supersedes_dir=V1_1_DIR,
        out_dir=out_dir,
    )

    assert run(arguments) == 0
    generated = validate_challenger_evidence_directory(out_dir)
    original = {path.name: path.read_bytes() for path in out_dir.iterdir()}
    baseline = {path.name: path.read_bytes() for path in V1_1_DIR.iterdir()}
    assert generated["schema_version"] == "1.2.0"
    assert generated["artifact_status"] == "generated_unreviewed"
    assert {
        name: original[name] for name in CHALLENGER_ARTIFACT_NAMES
    } == {name: baseline[name] for name in CHALLENGER_ARTIFACT_NAMES}

    assert run(arguments) == 0
    assert {path.name: path.read_bytes() for path in out_dir.iterdir()} == original

    for invalid_acknowledgements in (
        (
            REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENTS[0],
            REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENTS[0],
        ),
        (*REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENTS, "unexpected-cell"),
    ):
        with pytest.raises(ChallengerPublicationError, match="acknowledgement"):
            review_challenger_evidence(
                out_dir,
                supersedes_dir=V1_1_DIR,
                reviewed_by="sol_ultra",
                reviewed_at_utc="2026-07-27T23:59:00Z",
                numerical_null_acknowledgements=invalid_acknowledgements,
            )
        assert {path.name: path.read_bytes() for path in out_dir.iterdir()} == original

    with pytest.raises(ChallengerPublicationError, match="acknowledgement"):
        review_challenger_evidence(
            out_dir,
            supersedes_dir=V1_1_DIR,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T23:59:00Z",
            numerical_null_acknowledgements=("wrong-cell",),
        )
    assert {path.name: path.read_bytes() for path in out_dir.iterdir()} == original

    with pytest.raises(ChallengerPublicationError, match="acknowledgement"):
        review_challenger_evidence(
            out_dir,
            supersedes_dir=V1_1_DIR,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T23:59:00Z",
            numerical_null_acknowledgements=(
                REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENTS[0],
            ),
    )
    assert {path.name: path.read_bytes() for path in out_dir.iterdir()} == original

    real_fsync_directory = challenger_publication._fsync_directory
    failures_remaining = 1

    def fail_first_review_fsync(directory: Path) -> None:
        nonlocal failures_remaining
        if directory == out_dir and failures_remaining:
            failures_remaining -= 1
            raise OSError("injected review fsync failure")
        real_fsync_directory(directory)

    monkeypatch.setattr(
        challenger_publication,
        "_fsync_directory",
        fail_first_review_fsync,
    )
    with pytest.raises(ChallengerPublicationError, match="was rolled back"):
        review_challenger_evidence(
            out_dir,
            supersedes_dir=V1_1_DIR,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T23:59:00Z",
            numerical_null_acknowledgements=REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENTS,
        )
    assert {path.name: path.read_bytes() for path in out_dir.iterdir()} == original
    monkeypatch.setattr(
        challenger_publication,
        "_fsync_directory",
        real_fsync_directory,
    )

    review_challenger_evidence(
        out_dir,
        supersedes_dir=V1_1_DIR,
        reviewed_by="sol_ultra",
        reviewed_at_utc="2026-07-27T23:59:00Z",
        numerical_null_acknowledgements=tuple(
            reversed(REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENTS)
        ),
    )
    reviewed = validate_challenger_evidence_directory(out_dir)
    assert reviewed["artifact_status"] == "reviewed"
    assert reviewed["review"]["adjudications"] == list(REQUIRED_REVIEW_ADJUDICATIONS)
    assert reviewed["review"]["source_price_summary"] == REQUIRED_REVIEW_SUMMARY
    assert set(path.name for path in out_dir.iterdir()) == set(original)
    assert {
        name: (out_dir / name).read_bytes() for name in CHALLENGER_ARTIFACT_NAMES
    } == {name: original[name] for name in CHALLENGER_ARTIFACT_NAMES}

    tampered_dir = tmp_path / "self-hashed-malformed"
    shutil.copytree(out_dir, tampered_dir)
    metrics_path = tampered_dir / "challenger_metrics.csv"
    metrics_path.write_bytes(
        metrics_path.read_bytes().replace(b"family,variant", b"famxly,variant", 1)
    )
    tampered_manifest = deepcopy(reviewed)
    tampered_manifest["artifacts"]["challenger_metrics.csv"] = hashlib.sha256(
        metrics_path.read_bytes()
    ).hexdigest()
    (tampered_dir / "challenger_manifest.json").write_bytes(
        canonical_challenger_manifest_bytes(tampered_manifest)
    )
    with pytest.raises(CrossPoolContractError, match="statistical artifacts"):
        validate_challenger_evidence_directory(tampered_dir)

    numeric_dir = tmp_path / "self-hashed-wrong-number"
    shutil.copytree(out_dir, numeric_dir)
    predictions_path = numeric_dir / "challenger_predictions.csv"
    reader = csv.DictReader(io.StringIO(predictions_path.read_text(encoding="utf-8")))
    prediction_rows = list(reader)
    assert reader.fieldnames is not None
    prediction_rows[0]["prediction_bps"] = repr(
        float(prediction_rows[0]["prediction_bps"]) + 1.0
    )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(prediction_rows)
    predictions_path.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    numeric_manifest = deepcopy(reviewed)
    numeric_manifest["artifacts"]["challenger_predictions.csv"] = hashlib.sha256(
        predictions_path.read_bytes()
    ).hexdigest()
    (numeric_dir / "challenger_manifest.json").write_bytes(
        canonical_challenger_manifest_bytes(numeric_manifest)
    )
    with pytest.raises(CrossPoolContractError, match="statistical artifacts"):
        validate_challenger_evidence_directory(numeric_dir)

    rejected_candidate = tmp_path / "rejected-candidate"
    numeric_artifacts = tuple(
        ChallengerArtifact(name, (numeric_dir / name).read_bytes())
        for name in CHALLENGER_ARTIFACT_NAMES
    )
    with pytest.raises(CrossPoolContractError, match="statistical artifacts"):
        write_challenger_candidate(
            rejected_candidate,
            numeric_artifacts,
            numeric_manifest,
        )
    assert not rejected_candidate.exists()

    fsync_candidate = tmp_path / "fsync-failure-candidate"
    valid_artifacts = tuple(
        ChallengerArtifact(name, (out_dir / name).read_bytes())
        for name in CHALLENGER_ARTIFACT_NAMES
    )

    def fail_directory_fsync(_directory: Path) -> None:
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(
        challenger_publication,
        "_fsync_directory",
        fail_directory_fsync,
    )
    with pytest.raises(ChallengerPublicationError, match="could not be sealed"):
        write_challenger_candidate(fsync_candidate, valid_artifacts, reviewed)
    assert not fsync_candidate.exists()


def test_v1_1_review_attempt_is_non_mutating() -> None:
    original = {path.name: path.read_bytes() for path in V1_1_DIR.iterdir()}

    with pytest.raises(CrossPoolContractError, match="schema 1.2"):
        review_challenger_evidence(
            V1_1_DIR,
            supersedes_dir=V1_1_DIR,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T23:59:00Z",
            numerical_null_acknowledgements=REQUIRED_NUMERICAL_NULL_ACKNOWLEDGEMENTS,
        )

    assert {path.name: path.read_bytes() for path in V1_1_DIR.iterdir()} == original


def test_invalid_parent_publishes_only_blocked_manifest(tmp_path: Path) -> None:
    out_dir = tmp_path / "blocked"

    assert (
        run(
            RunArguments(
                parent_dir=tmp_path / "missing",
                supersedes_dir=V1_1_DIR,
                out_dir=out_dir,
            )
        )
        == 1
    )

    manifest = validate_challenger_evidence_directory(out_dir)
    assert manifest["artifact_status"] == "qa_blocked"
    assert manifest["qa"]["status"] == "blocked"
    assert manifest["qa"]["reasons"] == ["PARENT_OR_ANALYSIS_CONTRACT_INVALID"]
    assert set(path.name for path in out_dir.iterdir()) == {"challenger_manifest.json"}


def test_invalid_v1_1_baseline_publishes_only_blocked_manifest(tmp_path: Path) -> None:
    altered = tmp_path / "altered-v1-1"
    shutil.copytree(V1_1_DIR, altered)
    metrics = altered / "challenger_metrics.csv"
    metrics.write_bytes(metrics.read_bytes() + b"tamper\n")
    out_dir = tmp_path / "blocked-baseline"

    assert (
        run(
            RunArguments(
                parent_dir=PARENT_DIR,
                supersedes_dir=altered,
                out_dir=out_dir,
            )
        )
        == 1
    )
    manifest = validate_challenger_evidence_directory(out_dir)
    assert manifest["artifact_status"] == "qa_blocked"
    assert manifest["qa"]["reasons"] == ["SUPERSESSION_BASELINE_INVALID"]
    assert set(path.name for path in out_dir.iterdir()) == {"challenger_manifest.json"}


def test_unexpected_programming_error_is_not_sealed_as_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    out_dir = tmp_path / "unexpected"

    def raise_programmer_error(_parent) -> None:
        raise RuntimeError("programmer defect")

    monkeypatch.setattr(
        "research.scripts.run_cross_pool_challengers.run_challengers",
        raise_programmer_error,
    )

    with pytest.raises(RuntimeError, match="programmer defect"):
        run(
            RunArguments(
                parent_dir=PARENT_DIR,
                supersedes_dir=V1_1_DIR,
                out_dir=out_dir,
            )
        )
    assert not out_dir.exists()
