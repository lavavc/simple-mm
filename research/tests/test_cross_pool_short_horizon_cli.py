from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from research.cross_pool.contracts import CrossPoolContractError


def test_runner_parser_requires_every_bound_input() -> None:
    from research.scripts.run_cross_pool_short_horizon import build_parser

    namespace = build_parser().parse_args(
        [
            "--base-features",
            "base.csv",
            "--bsc-features",
            "bsc.csv",
            "--parent-manifest",
            "article_manifest.json",
            "--girum-note",
            "girum.md",
            "--out-dir",
            "evidence",
        ]
    )
    assert namespace.base_features == Path("base.csv")
    assert namespace.bsc_features == Path("bsc.csv")
    assert namespace.parent_manifest == Path("article_manifest.json")
    assert namespace.girum_note == Path("girum.md")
    assert namespace.out_dir == Path("evidence")


def test_runner_redacts_parser_and_runtime_failures(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research.scripts.run_cross_pool_short_horizon as runner

    parser_secret = "parser-secret-value"
    assert runner.main(["--unknown", parser_secret]) == 1
    captured = capsys.readouterr()
    assert parser_secret not in captured.err

    runtime_secret = "runtime-secret-value"

    def fail_run(_arguments: object) -> int:
        raise RuntimeError(f"https://eth-mainnet.g.alchemy.com/v2/{runtime_secret}")

    monkeypatch.setattr(runner, "run", fail_run)
    argv = [
        "--base-features",
        runtime_secret,
        "--bsc-features",
        "bsc.csv",
        "--parent-manifest",
        "article_manifest.json",
        "--girum-note",
        "girum.md",
        "--out-dir",
        "evidence",
    ]
    assert runner.main(argv) == 1
    captured = capsys.readouterr()
    assert runtime_secret not in captured.err
    assert "[REDACTED]" in captured.err


def test_review_command_requires_explicit_identity_and_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research.scripts.review_cross_pool_short_horizon as reviewer

    called: dict[str, object] = {}

    def record_review(
        out_dir: Path,
        *,
        reviewed_by: str,
        reviewed_at_utc: str,
    ) -> dict[str, object]:
        called.update(
            out_dir=out_dir,
            reviewed_by=reviewed_by,
            reviewed_at_utc=reviewed_at_utc,
        )
        return {"artifact_status": "reviewed"}

    monkeypatch.setattr(reviewer, "review_short_horizon_evidence", record_review)
    assert (
        reviewer.main(
            [
                "--evidence-dir",
                "evidence",
                "--reviewed-by",
                "sol_ultra",
                "--reviewed-at-utc",
                "2026-07-27T12:00:00Z",
            ]
        )
        == 0
    )
    assert called == {
        "out_dir": Path("evidence"),
        "reviewed_by": "sol_ultra",
        "reviewed_at_utc": "2026-07-27T12:00:00Z",
    }


def test_snapshot_is_private_exact_and_rejects_nonregular_inputs(tmp_path: Path) -> None:
    import research.scripts.run_cross_pool_short_horizon as runner

    source = tmp_path / "source.csv"
    source.write_bytes(b"frozen bytes\n")
    destination = tmp_path / "snapshot.csv"
    snapshot = runner._snapshot_regular_file(source, destination)

    assert snapshot is not None
    assert destination.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert snapshot.identity.byte_count == len(source.read_bytes())

    symlink = tmp_path / "source-link.csv"
    symlink.symlink_to(source)
    with pytest.raises(CrossPoolContractError, match="could not be opened"):
        runner._snapshot_regular_file(symlink, tmp_path / "symlink-snapshot.csv")

    fifo = tmp_path / "source-fifo"
    os.mkfifo(fifo)
    assert runner._snapshot_regular_file(fifo, tmp_path / "fifo-snapshot.csv") is None

    occupied = tmp_path / "occupied-snapshot.csv"
    occupied.write_bytes(b"owned by an earlier operation\n")
    with pytest.raises(CrossPoolContractError, match="could not be written"):
        runner._snapshot_regular_file(source, occupied)
    assert occupied.read_bytes() == b"owned by an earlier operation\n"


def test_private_stage_cleanup_failure_is_not_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research.scripts.run_cross_pool_short_horizon as runner

    stage = tmp_path / "stage"
    stage.mkdir()

    def fail_cleanup(_path: Path) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(runner.shutil, "rmtree", fail_cleanup)
    with pytest.raises(CrossPoolContractError, match="could not be removed"):
        runner._remove_private_stage(stage)
