"""Review and stamp a QA-pass challenger evidence package."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from engine.web3_utils import redact_rpc_credentials
from research.cross_pool.challenger_publication import review_challenger_evidence


class ChallengerReviewCliError(RuntimeError):
    """Raised for review argument errors before mutation."""


class _FailClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ChallengerReviewCliError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _FailClosedArgumentParser(
        description="Review and stamp QA-pass challenger evidence",
    )
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--supersedes-dir", type=Path, required=True)
    parser.add_argument("--reviewed-by", required=True)
    parser.add_argument("--reviewed-at-utc", required=True)
    parser.add_argument(
        "--acknowledge-numerical-null",
        action="append",
        required=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        namespace = build_parser().parse_args(raw_arguments)
        review_challenger_evidence(
            cast(Path, namespace.evidence_dir),
            supersedes_dir=cast(Path, namespace.supersedes_dir),
            reviewed_by=cast(str, namespace.reviewed_by),
            reviewed_at_utc=cast(str, namespace.reviewed_at_utc),
            numerical_null_acknowledgements=tuple(
                cast(list[str], namespace.acknowledge_numerical_null)
            ),
        )
        return 0
    except Exception as exc:
        message = redact_rpc_credentials(
            str(exc),
            sensitive_values=tuple(value for value in raw_arguments if value),
        )
        print(f"challenger review failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
