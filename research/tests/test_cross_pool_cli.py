from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import research.cross_pool.provenance as provenance
import research.scripts.run_cross_pool_lead_lag as cross_pool_cli
from research.cross_pool.manifest import load_and_validate_article_manifest
from research.cross_pool.market_structure import ReplayStreamEvidence
from research.scripts.run_cross_pool_lead_lag import main


def test_direct_script_entry_point_starts_from_repository_root() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "research/scripts/run_cross_pool_lead_lag.py",
            "--help",
        ],
        cwd=repository_root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--base-features" in result.stdout
    assert "--out-dir" in result.stdout


def test_missing_inputs_publish_only_one_qa_blocked_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cross_pool_cli,
        "capture_git_state",
        lambda _root: ("a" * 40, hashlib.sha256(b"").hexdigest()),
    )
    out_dir = tmp_path / "evidence"
    missing = tmp_path / "missing.csv"
    argv = (
        "--base-features",
        str(missing),
        "--bsc-features",
        str(missing),
        "--base-replay",
        str(missing),
        "--bsc-replay",
        str(missing),
        "--base-ledger",
        str(missing),
        "--bsc-ledger",
        str(missing),
        "--out-dir",
        str(out_dir),
    )

    assert main(argv) == 2
    assert {path.name for path in out_dir.iterdir()} == {"article_manifest.json"}
    manifest = load_and_validate_article_manifest(out_dir / "article_manifest.json")
    assert manifest["artifact_status"] == "qa_blocked"
    assert manifest["qa"]["reasons"] == [
        "FEATURE_BASE_INVALID",
        "FEATURE_BSC_INVALID",
        "LEDGER_BASE_COVERAGE_INVALID",
        "LEDGER_BSC_COVERAGE_INVALID",
        "REPLAY_BASE_INVALID",
        "REPLAY_BSC_INVALID",
    ]

    before = (out_dir / "article_manifest.json").read_bytes()
    assert main(argv) == 1
    assert (out_dir / "article_manifest.json").read_bytes() == before


def test_snapshot_rejects_symlinks_but_escalates_operational_read_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(b"header\nvalue\n")
    symlink = tmp_path / "symlink.csv"
    symlink.symlink_to(source)
    assert provenance.snapshot_regular_file(
        symlink,
        tmp_path / "symlink-copy.csv",
    ) is None

    destination = tmp_path / "copy.csv"
    original_read_bytes = Path.read_bytes
    original_open = provenance.os.open

    def failed_read_bytes(path: Path) -> bytes:
        if path == source:
            raise OSError(errno.EIO, "fixture I/O failure")
        return original_read_bytes(path)

    def failed_open(path: object, flags: int, *args: object) -> int:
        if Path(path) == source:
            raise OSError(errno.EIO, "fixture I/O failure")
        return original_open(path, flags, *args)

    monkeypatch.setattr(Path, "read_bytes", failed_read_bytes)
    monkeypatch.setattr(provenance.os, "open", failed_open)
    with pytest.raises(
        provenance.ProvenanceCaptureError,
        match="snapshot source could not be read",
    ):
        provenance.snapshot_regular_file(source, destination)
    assert not destination.exists()


def test_snapshot_never_removes_a_destination_it_did_not_create(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(b"source\n")
    destination = tmp_path / "existing.csv"
    destination.write_bytes(b"preserve\n")

    with pytest.raises(
        provenance.ProvenanceCaptureError,
        match="could not be written",
    ):
        provenance.snapshot_regular_file(source, destination)

    assert destination.read_bytes() == b"preserve\n"


def test_disjoint_valid_replays_are_classified_as_causal_alignment_failure() -> None:
    base = ReplayStreamEvidence(
        pool="uni-base",
        swap_count=2,
        first_timestamp_ms=100,
        last_timestamp_ms=200,
    )
    bsc = ReplayStreamEvidence(
        pool="uni-bsc",
        swap_count=2,
        first_timestamp_ms=300,
        last_timestamp_ms=400,
    )

    assert cross_pool_cli._validate_replay_alignment(base, bsc) == (
        "CAUSAL_ALIGNMENT_INVALID",
    )
