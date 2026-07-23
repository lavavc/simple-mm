from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import research.cross_pool.provenance as provenance
import research.scripts.run_cross_pool_lead_lag as cross_pool_cli
from research.backtester.lp_ledger_attribution import (
    FROZEN_REPLAY_PARSER_VERSION,
    frozen_replay_header_sha256,
    frozen_replay_parser_contract_sha256,
    frozen_replay_price_semantics_sha256,
)
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


def test_capture_replay_classifies_strict_parser_failure(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "base_replay.csv"
    malformed.write_text("wrong_header\nvalue\n")
    reasons: set[cross_pool_cli.QaReasonCode] = set()

    evidence, provenance = cross_pool_cli._capture_replay(
        malformed,
        "uni-base",
        "REPLAY_BASE_INVALID",
        reasons,
    )

    assert evidence is None
    assert provenance is None
    assert reasons == {"REPLAY_BASE_INVALID"}


def test_disjoint_valid_replays_are_classified_as_causal_alignment_failure() -> None:
    base = ReplayStreamEvidence(
        pool="uni-base",
        sha256="a" * 64,
        byte_length=1,
        row_count=2,
        header_sha256=frozen_replay_header_sha256(),
        parser_version=FROZEN_REPLAY_PARSER_VERSION,
        parser_contract_sha256=frozen_replay_parser_contract_sha256(),
        price_semantics_sha256=frozen_replay_price_semantics_sha256(),
        first_block=1,
        last_block=2,
        artifact_first_timestamp_ms=100,
        artifact_last_timestamp_ms=200,
        price_event_count=2,
        price_events_sha256="c" * 64,
        swap_count=2,
        first_timestamp_ms=100,
        last_timestamp_ms=200,
    )
    bsc = ReplayStreamEvidence(
        pool="uni-bsc",
        sha256="b" * 64,
        byte_length=1,
        row_count=2,
        header_sha256=frozen_replay_header_sha256(),
        parser_version=FROZEN_REPLAY_PARSER_VERSION,
        parser_contract_sha256=frozen_replay_parser_contract_sha256(),
        price_semantics_sha256=frozen_replay_price_semantics_sha256(),
        first_block=3,
        last_block=4,
        artifact_first_timestamp_ms=300,
        artifact_last_timestamp_ms=400,
        price_event_count=2,
        price_events_sha256="d" * 64,
        swap_count=2,
        first_timestamp_ms=300,
        last_timestamp_ms=400,
    )

    assert cross_pool_cli._validate_replay_alignment(base, bsc) == (
        "CAUSAL_ALIGNMENT_INVALID",
    )


def test_runner_classifies_replay_binding_failure_as_ledger_coverage_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_replay = ReplayStreamEvidence(
        pool="uni-base",
        sha256="a" * 64,
        byte_length=1,
        row_count=2,
        header_sha256=frozen_replay_header_sha256(),
        parser_version=FROZEN_REPLAY_PARSER_VERSION,
        parser_contract_sha256=frozen_replay_parser_contract_sha256(),
        price_semantics_sha256=frozen_replay_price_semantics_sha256(),
        first_block=1,
        last_block=2,
        artifact_first_timestamp_ms=100,
        artifact_last_timestamp_ms=200,
        price_event_count=2,
        price_events_sha256="c" * 64,
        swap_count=2,
        first_timestamp_ms=100,
        last_timestamp_ms=200,
    )
    bsc_replay = ReplayStreamEvidence(
        pool="uni-bsc",
        sha256="b" * 64,
        byte_length=1,
        row_count=2,
        header_sha256=frozen_replay_header_sha256(),
        parser_version=FROZEN_REPLAY_PARSER_VERSION,
        parser_contract_sha256=frozen_replay_parser_contract_sha256(),
        price_semantics_sha256=frozen_replay_price_semantics_sha256(),
        first_block=3,
        last_block=4,
        artifact_first_timestamp_ms=100,
        artifact_last_timestamp_ms=200,
        price_event_count=2,
        price_events_sha256="d" * 64,
        swap_count=2,
        first_timestamp_ms=100,
        last_timestamp_ms=200,
    )
    snapshots = cross_pool_cli.SnapshotPaths(
        base_features=tmp_path / "base_features.csv",
        bsc_features=tmp_path / "bsc_features.csv",
        base_replay=tmp_path / "base_replay.csv",
        bsc_replay=tmp_path / "bsc_replay.csv",
        base_ledger=tmp_path / "base_ledger.csv",
        bsc_ledger=tmp_path / "bsc_ledger.csv",
    )
    captured = cross_pool_cli.CapturedInputs(
        base_events=None,
        bsc_events=None,
        base_replay_evidence=base_replay,
        bsc_replay_evidence=bsc_replay,
        base_features_provenance=None,
        bsc_features_provenance=None,
        base_replay_provenance=None,
        bsc_replay_provenance=None,
        base_ledger_provenance=None,
        bsc_ledger_provenance=None,
        reason_codes=(),
    )
    base_coverage = object()
    bsc_coverage = object()
    coverages = iter((base_coverage, bsc_coverage))
    monkeypatch.setattr(
        cross_pool_cli,
        "replay_end_block_at_or_before",
        lambda *_args, **_kwargs: 2,
    )
    monkeypatch.setattr(
        cross_pool_cli,
        "load_verified_ledger_attribution_rows",
        lambda *_args, **_kwargs: SimpleNamespace(coverage=next(coverages)),
    )

    def validate_binding(_coverage: object, replay: ReplayStreamEvidence) -> None:
        if replay.pool == "uni-base":
            raise cross_pool_cli.CrossPoolContractError("fixture binding mismatch")

    monkeypatch.setattr(
        cross_pool_cli,
        "validate_ledger_replay_binding",
        validate_binding,
    )

    base, bsc, reasons = cross_pool_cli._verified_coverage(snapshots, captured)

    assert base is None
    assert bsc is bsc_coverage
    assert reasons == ("LEDGER_BASE_COVERAGE_INVALID",)
