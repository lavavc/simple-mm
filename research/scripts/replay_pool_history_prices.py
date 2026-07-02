"""Rewrite V4 pool CSV price fields with event-time replay semantics."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.clmm_math import cngn_price_from_sqrt_price_x96
from research.backtester.v4_event_replay import ReplayEvent, attach_event_time_state
from research.backtester.v4_export import POOL_CONFIGS, ExportPoolConfig


@dataclass(frozen=True)
class ReplayPoolHistorySummary:
    pool: str
    input_path: Path
    output_path: Path
    row_count: int
    changed_price_rows: int


def replay_pool_history_prices(
    input_path: Path,
    output_path: Path,
    pool: str,
) -> ReplayPoolHistorySummary:
    config = POOL_CONFIGS[pool]
    with input_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"pool history CSV has no header: {input_path}")
        fieldnames = reader.fieldnames
        rows = list(reader)

    replay_events = [
        ReplayEvent(
            block_number=int(row["block_number"]),
            log_index=int(row["log_index"]),
            event_order=event_order,
            event_type=row["event_type"],
            sqrt_price_x96=int(row["sqrt_price_x96"]) if row["event_type"] in {"initialize", "swap"} else None,
            tick=int(row["tick"]) if row["event_type"] in {"initialize", "swap"} else None,
        )
        for event_order, row in enumerate(rows)
    ]
    replayed_events = attach_event_time_state(replay_events, None)

    changed_price_rows = 0
    output_rows: list[dict[str, str]] = []
    for row, replayed_event in zip(rows, replayed_events):
        output_row = dict(row)
        if row["event_type"] in {"mint", "burn", "collect"}:
            output_row["sqrt_price_x96"] = str(replayed_event.event_time_sqrt_price_x96)
            output_row["tick"] = str(replayed_event.event_time_tick)
        output_row["cngn_usd_price"] = str(
            _cngn_price_from_sqrt_price(int(output_row["sqrt_price_x96"]), config)
        )
        if _price_fields_changed(row, output_row):
            changed_price_rows += 1
        output_rows.append(output_row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    return ReplayPoolHistorySummary(
        pool=pool,
        input_path=input_path,
        output_path=output_path,
        row_count=len(output_rows),
        changed_price_rows=changed_price_rows,
    )


def _cngn_price_from_sqrt_price(sqrt_price_x96: int, config: ExportPoolConfig) -> float:
    return cngn_price_from_sqrt_price_x96(
        sqrt_price_x96,
        config.token0_decimals,
        config.token1_decimals,
        config.invert_price,
    )

def _price_fields_changed(before: Mapping[str, str], after: Mapping[str, str]) -> bool:
    return (
        before["sqrt_price_x96"] != after["sqrt_price_x96"]
        or before["tick"] != after["tick"]
        or before["cngn_usd_price"] != after["cngn_usd_price"]
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pool", required=True, choices=sorted(POOL_CONFIGS))
    args = parser.parse_args()

    summary = replay_pool_history_prices(args.input, args.output, args.pool)
    print(
        f"{summary.pool}: rewrote {summary.row_count} rows to {summary.output_path}; "
        f"changed_price_rows={summary.changed_price_rows}"
    )


if __name__ == "__main__":
    _main()
