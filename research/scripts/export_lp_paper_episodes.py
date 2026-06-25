"""Export paper-style LP episodes from V4 LP ledger CSV rows."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.lp_paper_episodes import PaperLPEpisode, reconstruct_paper_episodes
from research.backtester.v4_lp_ledger import LPLedgerRow


_DECIMAL_FIELDS = {
    "amount0",
    "amount1",
    "amount0_actual",
    "amount1_actual",
    "collect_amount0",
    "collect_amount1",
    "cngn_usd_price_at_event",
}
_INT_FIELDS = {
    "block_number",
    "log_index",
    "event_order",
    "token_id",
    "liquidity_delta",
    "liquidity_after",
    "sqrt_price_x96_at_event",
    "tick_at_event",
    "timestamp_ms",
}
_OPTIONAL_INT_FIELDS = {"tick_lower", "tick_upper"}
_OPTIONAL_STR_FIELDS = {"lp_owner"}


def export_paper_episodes(ledger_path: Path, output_path: Path) -> int:
    rows = _read_ledger_rows(ledger_path)
    episodes = reconstruct_paper_episodes(rows)
    _write_episode_rows(output_path, episodes)
    return len(episodes)


def _read_ledger_rows(path: Path) -> list[LPLedgerRow]:
    expected_fields = [field.name for field in fields(LPLedgerRow)]
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty LP ledger CSV: {path}")
        missing = sorted(set(expected_fields).difference(reader.fieldnames))
        if missing:
            raise ValueError(f"LP ledger CSV missing fields {missing}: {path}")
        return [
            LPLedgerRow(**_normalize_ledger_payload(row))
            for row in reader
        ]


def _normalize_ledger_payload(row: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field in fields(LPLedgerRow):
        value = row[field.name]
        if field.name in _DECIMAL_FIELDS:
            normalized[field.name] = Decimal(value)
        elif field.name in _INT_FIELDS:
            normalized[field.name] = int(value)
        elif field.name in _OPTIONAL_INT_FIELDS:
            normalized[field.name] = int(value) if value else None
        elif field.name in _OPTIONAL_STR_FIELDS:
            normalized[field.name] = value if value else None
        else:
            normalized[field.name] = value
    return normalized


def _write_episode_rows(output_path: Path, episodes: list[PaperLPEpisode]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(PaperLPEpisode)]
    with output_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for episode in episodes:
            writer.writerow(_csv_row(episode))


def _csv_row(episode: PaperLPEpisode) -> dict[str, object]:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in asdict(episode).items()
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    count = export_paper_episodes(args.ledger, args.out)
    print(f"wrote {count} paper LP episodes to {args.out}")


if __name__ == "__main__":
    main()
