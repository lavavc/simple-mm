"""Import exported V4 pool history rows into price_snapshots."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtester.clmm_math import sqrt_price_x96_to_native_price
from backtester.pool_features import derive_swap_flow
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
    fee_multiplier = Decimal("1") - fee_rate
    if fee_multiplier <= 0 or fee_rate < 0:
        raise ValueError("fee_rate must be at least 0 and less than 1")

    mid = _raw_sqrt_mid(row, sqrt_price_x96)
    with localcontext() as context:
        context.prec = 60
        bid = mid * fee_multiplier
        ask = mid / fee_multiplier

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
        "tick": int(row["tick"]),
        "tick_crossing_included": False,
        "tx_hash": row["tx_hash"],
        "cngn_flow_direction": swap_flow.cngn_flow_direction,
    }
    return quote, metadata


def _raw_sqrt_mid(row: dict[str, str], sqrt_price_x96: int) -> Decimal:
    chain = row["chain"].strip()
    token0_symbol = row["token0_symbol"].strip()
    token1_symbol = row["token1_symbol"].strip()
    token0_decimals = _token_decimals(token0_symbol, chain)
    token1_decimals = _token_decimals(token1_symbol, chain)
    invert_price = token1_symbol.upper() == "CNGN"
    native = sqrt_price_x96_to_native_price(sqrt_price_x96, token0_decimals, token1_decimals)
    if native <= 0:
        raise ValueError("sqrt-derived mid must be positive")
    if invert_price:
        with localcontext() as context:
            context.prec = 60
            return Decimal("1") / native
    return native


def _token_decimals(symbol: str, chain: str) -> int:
    normalized_symbol = symbol.strip().upper()
    normalized_chain = chain.strip().lower()
    if normalized_symbol in {"CNGN", "USDC"}:
        return 6
    if normalized_symbol == "USDT":
        return 18 if normalized_chain in {"bsc", "bnb"} else 6
    raise ValueError(f"Cannot infer decimals for token symbol {symbol!r} on chain {chain!r}")


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
