"""Export Uniswap v4 LP lifecycle ledger rows."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtester.v4_event_replay import ReplayedEvent
from backtester.v4_export import POOL_CONFIGS
from backtester.v4_lp_ledger import (
    DecodedLiquidityAction,
    LPLedgerRow,
    OwnershipEvent,
    build_lp_ledger_rows,
)


def export_fixture_lp_ledger(
    decoded_actions_path: Path,
    ownership_events_path: Path,
    price_events_path: Path,
    output_path: Path,
) -> int:
    rows = build_lp_ledger_rows(
        decoded_actions=[
            _decoded_action_from_json(item)
            for item in _read_json_list(decoded_actions_path)
        ],
        ownership_events=[
            OwnershipEvent(**item)
            for item in _read_json_list(ownership_events_path)
        ],
        price_events=[
            ReplayedEvent(**item)
            for item in _read_json_list(price_events_path)
        ],
    )
    _write_ledger_rows(output_path, rows)
    return len(rows)


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"expected a list of objects: {path}")
    return payload


def _decoded_action_from_json(payload: dict[str, Any]) -> DecodedLiquidityAction:
    decimal_fields = {"amount0", "amount1", "collect_amount0", "collect_amount1"}
    normalized = {
        key: Decimal(str(value)) if key in decimal_fields else value
        for key, value in payload.items()
    }
    return DecodedLiquidityAction(**normalized)


def _write_ledger_rows(output_path: Path, rows: list[LPLedgerRow]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(LPLedgerRow)]
    with output_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row))


def _csv_row(row: LPLedgerRow) -> dict[str, object]:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in asdict(row).items()
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True, choices=sorted(POOL_CONFIGS))
    parser.add_argument("--start-block", required=True, type=int)
    parser.add_argument("--end-block", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--decoded-actions", type=Path)
    parser.add_argument("--ownership-events", type=Path)
    parser.add_argument("--price-events", type=Path)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    fixture_paths = (args.decoded_actions, args.ownership_events, args.price_events)
    if all(path is not None for path in fixture_paths):
        count = export_fixture_lp_ledger(
            args.decoded_actions,
            args.ownership_events,
            args.price_events,
            args.out,
        )
        print(f"wrote {count} LP ledger rows to {args.out}")
        return
    raise SystemExit(
        "RPC LP ledger export is not implemented yet; provide "
        "--decoded-actions, --ownership-events, and --price-events fixture JSON inputs."
    )


if __name__ == "__main__":
    main()
