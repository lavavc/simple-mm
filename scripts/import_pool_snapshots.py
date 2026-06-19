"""Import exported V4 pool history rows into price_snapshots."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtester.pool_features import derive_swap_flow
from backtester.pool_price_semantics import (
    classify_pool_price_row,
    fee_adjusted_bid_ask,
)
from engine.types import PriceQuote


SOURCE_NAMES = {
    "uni-base": "uni-base_pool",
    "uni-bsc": "uni-bsc_pool",
}

UPSERT_SQL = """
INSERT INTO price_snapshots (source, timestamp_ms, bid, ask, mid, metadata_json)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(source, timestamp_ms) DO UPDATE SET
    bid = excluded.bid,
    ask = excluded.ask,
    mid = excluded.mid,
    metadata_json = excluded.metadata_json
"""


@dataclass(frozen=True)
class ImportSummary:
    pool: str
    inserted_or_updated: int
    first_timestamp_ms: int | None
    last_timestamp_ms: int | None


def iter_pool_price_quotes(csv_path: Path, pool: str) -> Iterator[tuple[PriceQuote, dict[str, object]]]:
    source = _source_name(pool)
    source_second_counts: dict[int, int] = {}
    with csv_path.open(newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            block_time_ms = _block_time_ms(row["block_time"])
            source_second = block_time_ms // 1000
            sequence = source_second_counts.get(source_second, 0)
            if sequence >= 1000:
                raise ValueError("more than 1000 rows share the same source second")
            source_second_counts[source_second] = sequence + 1
            quote, metadata = _quote_from_row(row, source, block_time_ms + sequence)
            metadata["source_second_sequence"] = sequence
            yield quote, metadata


def import_pool_snapshots(db_path: Path, csv_path: Path, pool: str) -> ImportSummary:
    inserted_or_updated = 0
    first_timestamp_ms: int | None = None
    last_timestamp_ms: int | None = None

    with sqlite3.connect(db_path) as conn:
        for quote, metadata in iter_pool_price_quotes(csv_path, pool):
            conn.execute(
                UPSERT_SQL,
                (
                    quote.source,
                    quote.timestamp,
                    float(quote.bid),
                    float(quote.ask),
                    float(quote.mid),
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            inserted_or_updated += 1
            if first_timestamp_ms is None or quote.timestamp < first_timestamp_ms:
                first_timestamp_ms = quote.timestamp
            if last_timestamp_ms is None or quote.timestamp > last_timestamp_ms:
                last_timestamp_ms = quote.timestamp
        conn.commit()

    return ImportSummary(
        pool=pool,
        inserted_or_updated=inserted_or_updated,
        first_timestamp_ms=first_timestamp_ms,
        last_timestamp_ms=last_timestamp_ms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--pool", required=True, choices=sorted(SOURCE_NAMES))
    args = parser.parse_args()

    summary = import_pool_snapshots(args.db, args.csv, args.pool)
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0


def _quote_from_row(
    row: dict[str, str],
    source: str,
    timestamp_ms: int,
) -> tuple[PriceQuote, dict[str, object]]:
    sqrt_price_x96 = int(row["sqrt_price_x96"])
    if sqrt_price_x96 <= 0:
        raise ValueError("sqrt_price_x96 must be positive")

    fee_rate = Decimal(str(row["fee_rate"]))
    price_semantics = classify_pool_price_row(row)
    if price_semantics.stored_price_model == "unexplained":
        raise ValueError(
            "stored cngn_usd_price matches neither sqrt-derived price nor swap amount ratio"
        )

    mid = price_semantics.raw_sqrt_mid
    bid, ask = fee_adjusted_bid_ask(mid, fee_rate)

    quote = PriceQuote(
        source=source,
        timestamp=timestamp_ms,
        bid=bid,
        ask=ask,
        mid=mid,
    )
    swap_flow = derive_swap_flow(row)
    metadata: dict[str, object] = {
        "block_time": row["block_time"],
        "block_number": int(row["block_number"]),
        "chain": row["chain"],
        "event_type": row["event_type"],
        "fee_rate": str(fee_rate),
        "fee_adjusted_ask": str(ask),
        "fee_adjusted_bid": str(bid),
        "gas_included": False,
        "hooks_included": False,
        "log_index": int(row["log_index"]),
        "pool_id": row["pool_id"],
        "price_impact_included": False,
        "quote_model": "dex_fee_adjusted_sqrt",
        "raw_sqrt_mid": str(mid),
        "routing_included": False,
        "signed_cngn_amount": str(swap_flow.signed_cngn_amount),
        "signed_usd_notional": str(swap_flow.signed_usd_notional),
        "sqrt_price_x96": str(sqrt_price_x96),
        "stored_cngn_usd_price": str(price_semantics.stored_cngn_usd_price),
        "stored_price_model": price_semantics.stored_price_model,
        "amount_ratio_price": (
            str(price_semantics.amount_ratio_price)
            if price_semantics.amount_ratio_price is not None
            else None
        ),
        "tick": int(row["tick"]),
        "tick_crossing_included": False,
        "tx_hash": row["tx_hash"],
        "cngn_flow_direction": swap_flow.cngn_flow_direction,
    }
    return quote, metadata


def _block_time_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("block_time must include a timezone")
    return int(parsed.timestamp() * 1000)


def _source_name(pool: str) -> str:
    try:
        return SOURCE_NAMES[pool]
    except KeyError as exc:
        raise ValueError(f"Unsupported pool {pool!r}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
