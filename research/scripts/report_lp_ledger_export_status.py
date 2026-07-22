"""Report local LP-ledger export progress without calling RPC."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.lp_ledger_checkpoint import (  # noqa: E402
    CheckpointPaths,
    ExportStatus,
    read_export_status,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "outputs",
        nargs="+",
        type=Path,
        help="One or more canonical LP-ledger CSV output paths.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit canonical machine-readable JSON.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    statuses = tuple(
        read_export_status(CheckpointPaths.from_output(output)) for output in args.outputs
    )
    if args.json:
        print(
            json.dumps(
                {"exports": [status.as_dict() for status in statuses]},
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        print(_render_table(statuses))
    return 0


def _render_table(statuses: tuple[ExportStatus, ...]) -> str:
    headers = (
        "OUTPUT",
        "POOL",
        "RANGE",
        "STATE",
        "PHASE",
        "PROGRESS",
        "ACTION_TX",
        "ACTIONS",
        "TOKENS",
        "TRANSFER_LOGS",
        "RELEVANT_LOGS",
        "RELEVANT_TX",
        "OWNERSHIP",
        "BOUND",
        "ROWS",
        "AGE",
        "RATE",
        "ETA",
        "PUBLISHED",
        "RESUMABLE",
    )
    rows = tuple(
        (
            Path(status.output).name,
            status.pool or "-",
            _range(status),
            status.state,
            status.phase or "-",
            _progress(status),
            _optional_int(status.action_candidate_transaction_count),
            _optional_int(status.action_count),
            _optional_int(status.frozen_token_count),
            _optional_int(status.full_transfer_log_count),
            _optional_int(status.relevant_transfer_witness_count),
            _optional_int(status.relevant_transfer_transaction_count),
            _optional_int(status.ownership_event_count),
            _optional_int(status.bound_action_count),
            _optional_int(status.ledger_row_count),
            _duration(status.updated_age_seconds),
            "-" if status.rate_per_second is None else f"{status.rate_per_second}/s",
            _duration(status.eta_seconds),
            "yes" if status.output_published else "no",
            "yes" if status.resumable else "no",
        )
        for status in statuses
    )
    widths = tuple(
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    )
    rendered = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)),
        "  ".join("-" * width for width in widths),
    ]
    rendered.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows
    )
    return "\n".join(rendered)


def _range(status: ExportStatus) -> str:
    if status.start_block is None or status.end_block is None:
        return "-"
    return f"{status.start_block}-{status.end_block}"


def _progress(status: ExportStatus) -> str:
    if status.completed is None:
        return "-"
    total = "?" if status.total is None else str(status.total)
    unit = "" if status.unit is None else f" {status.unit}"
    return f"{status.completed}/{total}{unit}"


def _optional_int(value: int | None) -> str:
    return "-" if value is None else str(value)


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    hours, remainder = divmod(seconds, 3_600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
