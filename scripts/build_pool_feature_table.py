"""Build causal swap-level pool feature tables from exported V4 history."""

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
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtester.cone_features import TimestampedValue, causal_percentile
from backtester.cone_features import previous_or_equal_with_age
from backtester.pool_features import derive_swap_flow
from backtester.pool_price_semantics import (
    classify_pool_price_row,
    fee_adjusted_bid_ask,
)


FAIR_PRICE_SOURCE = "quidax"
OUTPUT_FIELDS = [
    "timestamp_ms",
    "pool",
    "block_number",
    "tx_hash",
    "log_index",
    "raw_sqrt_mid",
    "fee_adjusted_bid",
    "fee_adjusted_ask",
    "stored_cngn_usd_price",
    "stored_price_model",
    "realized_volatility",
    "realized_volatility_cone_pct",
    "dex_premium_bps",
    "dex_premium_cone_pct",
    "active_liquidity_cone_pct",
    "active_share_cone_pct",
    "swap_flow_imbalance",
    "swap_flow_imbalance_cone_pct",
    "fee_apr",
    "fee_apr_cone_pct",
    "volume_cone_pct",
    "source_age_ms",
]
SUPPORTED_POOLS = ("uni-base", "uni-bsc")
SECONDS_PER_YEAR = Decimal("31536000")


@dataclass(frozen=True)
class FeatureBuildSummary:
    pool: str
    rows_written: int
    first_timestamp_ms: int | None
    last_timestamp_ms: int | None


def build_pool_feature_table(
    db_path: Path,
    csv_path: Path,
    pool: str,
    out_path: Path,
) -> FeatureBuildSummary:
    if pool not in SUPPORTED_POOLS:
        raise ValueError(f"Unsupported pool {pool!r}")
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    fair_values = _load_fair_values(db_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    histories: dict[str, list[Decimal]] = {
        "realized_volatility": [],
        "dex_premium_bps": [],
        "active_liquidity": [],
        "active_share": [],
        "swap_flow_imbalance": [],
        "fee_apr": [],
        "volume": [],
    }
    last_swap_price: Decimal | None = None
    last_swap_timestamp_ms: int | None = None
    max_active_liquidity: Decimal | None = None
    source_second_counts: dict[int, int] = {}
    rows_written = 0
    first_timestamp_ms: int | None = None
    last_timestamp_ms: int | None = None

    with csv_path.open(newline="") as input_file, out_path.open(
        "w",
        newline="",
    ) as output_file:
        reader = csv.DictReader(input_file)
        writer = csv.DictWriter(output_file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()

        for row in reader:
            timestamp_ms = _sequenced_timestamp_ms(row, source_second_counts)
            if row["event_type"] != "swap":
                continue

            feature_row = _build_feature_row(
                row=row,
                pool=pool,
                timestamp_ms=timestamp_ms,
                fair_values=fair_values,
                histories=histories,
                last_swap_price=last_swap_price,
                last_swap_timestamp_ms=last_swap_timestamp_ms,
                max_active_liquidity=max_active_liquidity,
            )
            writer.writerow(feature_row.output)

            last_swap_price = feature_row.raw_sqrt_mid
            last_swap_timestamp_ms = timestamp_ms
            if (
                max_active_liquidity is None
                or feature_row.active_liquidity > max_active_liquidity
            ):
                max_active_liquidity = feature_row.active_liquidity

            for history_name, value in feature_row.history_values.items():
                if value is not None:
                    histories[history_name].append(value)

            rows_written += 1
            if first_timestamp_ms is None:
                first_timestamp_ms = timestamp_ms
            last_timestamp_ms = timestamp_ms

    return FeatureBuildSummary(
        pool=pool,
        rows_written=rows_written,
        first_timestamp_ms=first_timestamp_ms,
        last_timestamp_ms=last_timestamp_ms,
    )


@dataclass(frozen=True)
class BuiltFeatureRow:
    output: dict[str, str]
    raw_sqrt_mid: Decimal
    active_liquidity: Decimal
    history_values: dict[str, Decimal | None]


def _build_feature_row(
    *,
    row: dict[str, str],
    pool: str,
    timestamp_ms: int,
    fair_values: list[TimestampedValue],
    histories: dict[str, list[Decimal]],
    last_swap_price: Decimal | None,
    last_swap_timestamp_ms: int | None,
    max_active_liquidity: Decimal | None,
) -> BuiltFeatureRow:
    price_semantics = classify_pool_price_row(row)
    raw_sqrt_mid = price_semantics.raw_sqrt_mid
    fee_rate = Decimal(str(row["fee_rate"]))
    fee_adjusted_bid, fee_adjusted_ask = fee_adjusted_bid_ask(raw_sqrt_mid, fee_rate)
    fair_value = previous_or_equal_with_age(fair_values, timestamp_ms, None)
    fair_mid = cast(Decimal, fair_value.value) if fair_value is not None else None
    dex_premium_bps = _dex_premium_bps(raw_sqrt_mid, fair_mid)

    realized_volatility = (
        _absolute_log_return(last_swap_price, raw_sqrt_mid)
        if last_swap_price is not None
        else None
    )
    active_liquidity = Decimal(str(row["active_liquidity"]))
    active_share = _active_share(active_liquidity, max_active_liquidity)
    swap_flow = derive_swap_flow(row)
    swap_flow_imbalance = _swap_flow_imbalance(swap_flow.signed_usd_notional)
    volume = Decimal(str(row["amount_usd"])).copy_abs()
    fee_apr = _fee_apr(
        fee_rate=fee_rate,
        volume=volume,
        active_liquidity=active_liquidity,
        last_swap_timestamp_ms=last_swap_timestamp_ms,
        timestamp_ms=timestamp_ms,
    )

    history_values = {
        "realized_volatility": realized_volatility,
        "dex_premium_bps": dex_premium_bps,
        "active_liquidity": active_liquidity,
        "active_share": active_share,
        "swap_flow_imbalance": swap_flow_imbalance,
        "fee_apr": fee_apr,
        "volume": volume,
    }
    cone_percentiles = {
        name: causal_percentile(histories[name], value) if value is not None else None
        for name, value in history_values.items()
    }

    return BuiltFeatureRow(
        output={
            "timestamp_ms": str(timestamp_ms),
            "pool": pool,
            "block_number": row["block_number"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "raw_sqrt_mid": _format_decimal(raw_sqrt_mid),
            "fee_adjusted_bid": _format_decimal(fee_adjusted_bid),
            "fee_adjusted_ask": _format_decimal(fee_adjusted_ask),
            "stored_cngn_usd_price": _format_decimal(
                price_semantics.stored_cngn_usd_price,
            ),
            "stored_price_model": price_semantics.stored_price_model,
            "realized_volatility": _format_optional_decimal(realized_volatility),
            "realized_volatility_cone_pct": _format_optional_decimal(
                cone_percentiles["realized_volatility"],
            ),
            "dex_premium_bps": _format_optional_decimal(dex_premium_bps),
            "dex_premium_cone_pct": _format_optional_decimal(
                cone_percentiles["dex_premium_bps"],
            ),
            "active_liquidity_cone_pct": _format_optional_decimal(
                cone_percentiles["active_liquidity"],
            ),
            "active_share_cone_pct": _format_optional_decimal(
                cone_percentiles["active_share"],
            ),
            "swap_flow_imbalance": _format_decimal(swap_flow_imbalance),
            "swap_flow_imbalance_cone_pct": _format_optional_decimal(
                cone_percentiles["swap_flow_imbalance"],
            ),
            "fee_apr": _format_optional_decimal(fee_apr),
            "fee_apr_cone_pct": _format_optional_decimal(
                cone_percentiles["fee_apr"],
            ),
            "volume_cone_pct": _format_optional_decimal(cone_percentiles["volume"]),
            "source_age_ms": str(fair_value.age_ms) if fair_value is not None else "",
        },
        raw_sqrt_mid=raw_sqrt_mid,
        active_liquidity=active_liquidity,
        history_values=history_values,
    )


def _load_fair_values(db_path: Path) -> list[TimestampedValue]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT timestamp_ms, mid
            FROM price_snapshots
            WHERE source = ?
            ORDER BY timestamp_ms ASC
            """,
            (FAIR_PRICE_SOURCE,),
        ).fetchall()

    fair_values: list[TimestampedValue] = []
    for timestamp_ms, mid in rows:
        fair_mid = Decimal(str(mid))
        if fair_mid <= 0:
            raise ValueError("fair price snapshot mid must be positive")
        fair_values.append(
            TimestampedValue(timestamp_ms=int(timestamp_ms), value=fair_mid),
        )
    return fair_values


def _sequenced_timestamp_ms(
    row: dict[str, str],
    source_second_counts: dict[int, int],
) -> int:
    block_time_ms = _block_time_ms(row["block_time"])
    source_second = block_time_ms // 1000
    sequence = source_second_counts.get(source_second, 0)
    if sequence >= 1000:
        raise ValueError("more than 1000 rows share the same source second")
    source_second_counts[source_second] = sequence + 1
    return block_time_ms + sequence


def _block_time_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("block_time must include a timezone")
    return int(parsed.timestamp() * 1000)


def _absolute_log_return(previous: Decimal, current: Decimal) -> Decimal:
    if previous <= 0 or current <= 0:
        raise ValueError("swap prices must be positive")
    with localcontext() as context:
        context.prec = 60
        return (current / previous).ln().copy_abs()


def _dex_premium_bps(pool_mid: Decimal, fair_mid: Decimal | None) -> Decimal | None:
    if fair_mid is None:
        return None
    if fair_mid <= 0:
        raise ValueError("fair mid must be positive")
    with localcontext() as context:
        context.prec = 60
        return ((pool_mid / fair_mid) - Decimal("1")) * Decimal("10000")


def _active_share(
    active_liquidity: Decimal,
    max_active_liquidity: Decimal | None,
) -> Decimal | None:
    if active_liquidity < 0:
        raise ValueError("active_liquidity must not be negative")
    denominator = active_liquidity
    if max_active_liquidity is not None and max_active_liquidity > denominator:
        denominator = max_active_liquidity
    if denominator <= 0:
        return None
    with localcontext() as context:
        context.prec = 60
        return active_liquidity / denominator


def _swap_flow_imbalance(signed_usd_notional: Decimal) -> Decimal:
    volume = signed_usd_notional.copy_abs()
    if volume == 0:
        return Decimal("0")
    return signed_usd_notional / volume


def _fee_apr(
    *,
    fee_rate: Decimal,
    volume: Decimal,
    active_liquidity: Decimal,
    last_swap_timestamp_ms: int | None,
    timestamp_ms: int,
) -> Decimal | None:
    if active_liquidity < 0:
        raise ValueError("active_liquidity must not be negative")
    if active_liquidity == 0 or last_swap_timestamp_ms is None:
        return None

    elapsed_ms = timestamp_ms - last_swap_timestamp_ms
    if elapsed_ms <= 0:
        return None

    with localcontext() as context:
        context.prec = 60
        elapsed_seconds = Decimal(elapsed_ms) / Decimal("1000")
        return (fee_rate * volume / active_liquidity) * (
            SECONDS_PER_YEAR / elapsed_seconds
        )


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _format_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    return _format_decimal(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True, choices=SUPPORTED_POOLS)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    summary = build_pool_feature_table(
        db_path=args.db,
        csv_path=args.csv,
        pool=args.pool,
        out_path=args.out,
    )
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
