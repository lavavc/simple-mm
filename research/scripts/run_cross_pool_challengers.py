"""Run and atomically publish the post-hoc predictive challenger study."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from engine.web3_utils import redact_rpc_credentials
from research.cross_pool.challenger_inference import infer_challengers
from research.cross_pool.challenger_manifest import (
    CHALLENGER_SOURCE_PATHS,
    build_blocked_challenger_manifest,
    build_generated_challenger_manifest,
)
from research.cross_pool.challenger_parent import load_challenger_parent
from research.cross_pool.challenger_publication import (
    ChallengerPublicationError,
    PublicationMode,
    challenger_output_lock,
    classify_challenger_output,
    publish_or_verify_challenger_candidate,
    validate_v1_supersession_baseline,
    write_challenger_candidate,
)
from research.cross_pool.challenger_reporting import render_challenger_artifacts
from research.cross_pool.challengers import run_challengers
from research.cross_pool.contracts import CrossPoolContractError


class ChallengerCliError(RuntimeError):
    """Raised for challenger argument errors before publication."""


class _FailClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ChallengerCliError(message)


@dataclass(frozen=True)
class RunArguments:
    parent_dir: Path
    supersedes_dir: Path
    out_dir: Path


def build_parser() -> argparse.ArgumentParser:
    parser = _FailClosedArgumentParser(
        description="Run the post-hoc Base/BSC predictive challenger study",
    )
    parser.add_argument("--parent-dir", type=Path, required=True)
    parser.add_argument("--supersedes-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        namespace = build_parser().parse_args(raw_arguments)
        return run(
            RunArguments(
                parent_dir=cast(Path, namespace.parent_dir),
                supersedes_dir=cast(Path, namespace.supersedes_dir),
                out_dir=cast(Path, namespace.out_dir),
            )
        )
    except Exception as exc:
        message = redact_rpc_credentials(
            str(exc),
            sensitive_values=tuple(value for value in raw_arguments if value),
        )
        print(f"challenger run failed: {message}", file=sys.stderr)
        return 1


def run(arguments: RunArguments) -> int:
    with challenger_output_lock(arguments.out_dir):
        mode = classify_challenger_output(arguments.out_dir)
        return _run_locked(arguments, mode)


def _run_locked(arguments: RunArguments, mode: PublicationMode) -> int:
    code_commit, source_diff_sha256 = _capture_source_identity()
    stage_root = Path(
        tempfile.mkdtemp(
            prefix=f".{arguments.out_dir.name}.stage.",
            dir=arguments.out_dir.parent,
        )
    )
    candidate_dir = stage_root / "candidate"
    try:
        try:
            validate_v1_supersession_baseline(arguments.supersedes_dir)
            parent = load_challenger_parent(arguments.parent_dir)
            study = run_challengers(parent)
            inference = infer_challengers(study)
            artifacts = render_challenger_artifacts(study, inference)
            manifest = build_generated_challenger_manifest(
                parent=parent,
                study=study,
                inference=inference,
                artifacts=artifacts,
                code_commit=code_commit,
                source_diff_sha256=source_diff_sha256,
            )
            status = 0
        except (ChallengerPublicationError, CrossPoolContractError, OSError):
            if mode != "publish_new":
                raise
            artifacts = ()
            manifest = build_blocked_challenger_manifest(
                reason="PARENT_OR_ANALYSIS_CONTRACT_INVALID",
                code_commit=code_commit,
                source_diff_sha256=source_diff_sha256,
            )
            status = 1
        write_challenger_candidate(candidate_dir, artifacts, manifest)
        publish_or_verify_challenger_candidate(
            arguments.out_dir,
            candidate_dir,
            mode=mode,
        )
        return status
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root)


def _capture_source_identity() -> tuple[str, str]:
    return _capture_source_identity_at(
        repository_root=_REPOSITORY_ROOT,
        source_paths=CHALLENGER_SOURCE_PATHS,
    )


def _capture_source_identity_at(
    *,
    repository_root: Path,
    source_paths: tuple[str, ...],
) -> tuple[str, str]:
    if not source_paths or len(set(source_paths)) != len(source_paths):
        raise ChallengerCliError("challenger source closure is invalid")
    try:
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ChallengerCliError("challenger Git identity could not be captured") from exc
    commit = completed.stdout.decode("ascii").strip()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ChallengerCliError("challenger Git commit identity is invalid")
    try:
        subprocess.run(
            ("git", "ls-files", "--error-unmatch", "--", *source_paths),
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
        diff = subprocess.run(
            ("git", "diff", "--binary", "HEAD", "--", *source_paths),
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ChallengerCliError(
            "challenger source closure must be tracked before execution"
        ) from exc
    return commit, hashlib.sha256(diff.stdout).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
