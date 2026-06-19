"""Export CEX-led fair-price markout rows from persisted price snapshots."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from bisect import bisect_left
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, TextIO

import aiosqlite

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_HORIZONS_SECONDS = (10, 30, 60, 120, 300, 600)
DEFAULT_TARGET_USD = Decimal("100")
DEX_SOURCE_ALIASES = {
    "uni-base": ("uni-base_pool", "uni_base_pool"),
    "uni-bsc": ("uni-bsc_pool", "uni_bsc_pool"),
}


@dataclass(frozen=True)
class PriceSnapshot:
    source: str
    timestamp_ms: int
    bid: Decimal
    ask: Decimal
    mid: Decimal
    metadata: dict[str, Any] | None


@dataclass(frozen=True)
class BookLevel:
    price_cngn_per_usdt: Decimal
    amount_usdt: Decimal


@dataclass(frozen=True)
class QuidaxExecutablePrices:
    buy_cngn_usd: Decimal | None
    sell_cngn_usd: Decimal | None

    @property
    def executable_mid(self) -> Decimal | None:
        if self.buy_cngn_usd is None or self.sell_cngn_usd is None:
            return None
        with localcontext() as context:
            context.prec = 50
            return (self.buy_cngn_usd + self.sell_cngn_usd) / Decimal("2")


async def load_price_snapshots(
    db_path: str,
    *,
    from_ts: int | None = None,
    to_ts: int | None = None,
    limit: int | None = None,
) -> list[PriceSnapshot]:
    query = (
        "SELECT source, timestamp_ms, bid, ask, mid, metadata_json "
        "FROM price_snapshots WHERE 1=1"
    )
    params: list[Any] = []
    if from_ts is not None:
        query += " AND timestamp_ms >= ?"
        params.append(from_ts)
    if to_ts is not None:
        query += " AND timestamp_ms <= ?"
        params.append(to_ts)
    query += " ORDER BY timestamp_ms ASC, source ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()

    snapshots: list[PriceSnapshot] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        snapshots.append(
            PriceSnapshot(
                source=str(row["source"]),
                timestamp_ms=int(row["timestamp_ms"]),
                bid=Decimal(str(row["bid"])),
                ask=Decimal(str(row["ask"])),
                mid=Decimal(str(row["mid"])),
                metadata=metadata,
            )
        )
    return snapshots


def build_markout_rows(
    snapshots: list[PriceSnapshot],
    *,
    horizons_seconds: list[int],
    target_usd: Decimal,
    max_label_lag_ms: int | None = None,
) -> list[dict[str, str]]:
    quidax_rows = [row for row in snapshots if row.source == "quidax"]
    bybit_rows = [row for row in snapshots if row.source == "bybit_p2p"]
    dex_rows = {
        venue: [
            row
            for row in snapshots
            if row.source in aliases
        ]
        for venue, aliases in DEX_SOURCE_ALIASES.items()
    }
    quidax_timestamps = [row.timestamp_ms for row in quidax_rows]
    bybit_timestamps = [row.timestamp_ms for row in bybit_rows]
    dex_timestamps = {
        venue: [row.timestamp_ms for row in rows]
        for venue, rows in dex_rows.items()
    }
    output: list[dict[str, str]] = []

    for row in quidax_rows:
        current_top = _quidax_top_of_book_prices(row)
        current_exec = _quidax_executable_prices(row, target_usd=target_usd)
        book_features = _quidax_book_features(row)
        bybit_row = _previous_or_equal(bybit_rows, bybit_timestamps, row.timestamp_ms)
        record = {
            "timestamp_ms": str(row.timestamp_ms),
            "source": row.source,
            "target_usd": _format_decimal(target_usd),
            "quidax_ticker_bid": _format_decimal(row.bid),
            "quidax_ticker_ask": _format_decimal(row.ask),
            "quidax_ticker_mid": _format_decimal(row.mid),
            "quidax_top_bid": _format_optional_decimal(current_top.sell_cngn_usd),
            "quidax_top_ask": _format_optional_decimal(current_top.buy_cngn_usd),
            "quidax_top_mid": _format_optional_decimal(current_top.executable_mid),
            "quidax_buy_cngn_usd": _format_optional_decimal(current_exec.buy_cngn_usd),
            "quidax_sell_cngn_usd": _format_optional_decimal(current_exec.sell_cngn_usd),
            "quidax_executable_mid": _format_optional_decimal(current_exec.executable_mid),
            **book_features,
            **_dex_reference_fields(
                row,
                dex_rows=dex_rows,
                dex_timestamps=dex_timestamps,
                reference_mid=current_exec.executable_mid,
            ),
            "bybit_mid": _format_optional_decimal(bybit_row.mid if bybit_row else None),
            "bybit_age_ms": str(row.timestamp_ms - bybit_row.timestamp_ms)
            if bybit_row
            else "",
        }

        has_future_markout = False
        for horizon in horizons_seconds:
            target_timestamp_ms = row.timestamp_ms + horizon * 1000
            future = _first_at_or_after(
                quidax_rows,
                quidax_timestamps,
                target_timestamp_ms,
            )
            lag_ms = future.timestamp_ms - target_timestamp_ms if future is not None else None
            if (
                lag_ms is not None
                and max_label_lag_ms is not None
                and lag_ms > max_label_lag_ms
            ):
                future = None
                lag_ms = None
            if future is not None:
                has_future_markout = True
            label = (
                _quidax_executable_prices(future, target_usd=target_usd)
                if future is not None
                else QuidaxExecutablePrices(None, None)
            )
            prefix = f"label_{horizon}s"
            record[f"{prefix}_timestamp_ms"] = str(future.timestamp_ms) if future else ""
            record[f"{prefix}_lag_ms"] = str(lag_ms) if lag_ms is not None else ""
            record[f"{prefix}_buy_cngn_usd"] = _format_optional_decimal(label.buy_cngn_usd)
            record[f"{prefix}_sell_cngn_usd"] = _format_optional_decimal(label.sell_cngn_usd)
            record[f"{prefix}_executable_mid"] = _format_optional_decimal(label.executable_mid)

        if not has_future_markout:
            continue

        output.append(record)

    return output


def _fieldnames(horizons_seconds: list[int]) -> list[str]:
    base = [
        "timestamp_ms",
        "source",
        "target_usd",
        "quidax_ticker_bid",
        "quidax_ticker_ask",
        "quidax_ticker_mid",
        "quidax_top_bid",
        "quidax_top_ask",
        "quidax_top_mid",
        "quidax_buy_cngn_usd",
        "quidax_sell_cngn_usd",
        "quidax_executable_mid",
        "quidax_imbalance_top1_usdt",
        "quidax_imbalance_topn_usdt",
        "quidax_imbalance_topn_cngn",
        "quidax_cngn_usd_pressure_topn",
        "quidax_owa_mid_top1",
        "quidax_owa_mid_topn",
        "quidax_microprice_top1",
        "uni_base_mid",
        "uni_base_age_ms",
        "uni_base_premium_bps",
        "uni_bsc_mid",
        "uni_bsc_age_ms",
        "uni_bsc_premium_bps",
        "bybit_mid",
        "bybit_age_ms",
    ]
    for horizon in horizons_seconds:
        prefix = f"label_{horizon}s"
        base.extend(
            [
                f"{prefix}_timestamp_ms",
                f"{prefix}_lag_ms",
                f"{prefix}_buy_cngn_usd",
                f"{prefix}_sell_cngn_usd",
                f"{prefix}_executable_mid",
            ]
        )
    return base


def _quidax_top_of_book_prices(row: PriceSnapshot) -> QuidaxExecutablePrices:
    bids, asks = _quidax_book(row)
    buy = _divide(Decimal("1"), bids[0].price_cngn_per_usdt) if bids else None
    sell = _divide(Decimal("1"), asks[0].price_cngn_per_usdt) if asks else None
    return QuidaxExecutablePrices(buy_cngn_usd=buy, sell_cngn_usd=sell)


def _quidax_executable_prices(
    row: PriceSnapshot,
    *,
    target_usd: Decimal,
) -> QuidaxExecutablePrices:
    top = _quidax_top_of_book_prices(row)
    if top.executable_mid is None:
        return QuidaxExecutablePrices(None, None)

    buy = _walk_bids_buy_cngn(row, target_usd=target_usd)
    target_cngn = _divide(target_usd, top.executable_mid)
    sell = _walk_asks_sell_cngn(row, target_cngn=target_cngn)
    return QuidaxExecutablePrices(buy_cngn_usd=buy, sell_cngn_usd=sell)


def _quidax_book(row: PriceSnapshot) -> tuple[list[BookLevel], list[BookLevel]]:
    metadata = row.metadata or {}
    depth = metadata.get("depth", {})
    if not isinstance(depth, dict):
        return [], []
    bids = _parse_book_levels(depth.get("bid_levels", []), reverse=True)
    asks = _parse_book_levels(depth.get("ask_levels", []), reverse=False)
    return bids, asks


def _parse_book_levels(raw: Any, *, reverse: bool) -> list[BookLevel]:
    if not isinstance(raw, list):
        return []
    levels: list[BookLevel] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        price = Decimal(str(item.get("price", "0")))
        amount = Decimal(str(item.get("amount", "0")))
        if price <= 0 or amount <= 0:
            continue
        levels.append(BookLevel(price_cngn_per_usdt=price, amount_usdt=amount))
    return sorted(levels, key=lambda level: level.price_cngn_per_usdt, reverse=reverse)


def _walk_bids_buy_cngn(row: PriceSnapshot, *, target_usd: Decimal) -> Decimal | None:
    with localcontext() as context:
        context.prec = 50
        bids, _ = _quidax_book(row)
        remaining_usdt = target_usd
        total_cngn = Decimal("0")
        for bid in bids:
            if remaining_usdt <= 0:
                break
            fill_usdt = min(remaining_usdt, bid.amount_usdt)
            total_cngn += fill_usdt * bid.price_cngn_per_usdt
            remaining_usdt -= fill_usdt
        if remaining_usdt > 0 or total_cngn <= 0:
            return None
        return target_usd / total_cngn


def _walk_asks_sell_cngn(row: PriceSnapshot, *, target_cngn: Decimal) -> Decimal | None:
    with localcontext() as context:
        context.prec = 50
        _, asks = _quidax_book(row)
        remaining_cngn = target_cngn
        total_usdt = Decimal("0")
        for ask in asks:
            if remaining_cngn <= 0:
                break
            level_cngn = ask.amount_usdt * ask.price_cngn_per_usdt
            fill_cngn = min(remaining_cngn, level_cngn)
            total_usdt += fill_cngn / ask.price_cngn_per_usdt
            remaining_cngn -= fill_cngn
        if remaining_cngn > 0 or target_cngn <= 0:
            return None
        return total_usdt / target_cngn


def _quidax_book_features(row: PriceSnapshot) -> dict[str, str]:
    bids, asks = _quidax_book(row)
    top_bids = bids[:1]
    top_asks = asks[:1]
    topn_bid_usdt = _book_usdt(top_bids)
    topn_ask_usdt = _book_usdt(top_asks)
    all_bid_usdt = _book_usdt(bids)
    all_ask_usdt = _book_usdt(asks)
    all_bid_cngn = _book_cngn(bids)
    all_ask_cngn = _book_cngn(asks)
    top = _quidax_top_of_book_prices(row)

    imbalance_topn = _imbalance(all_bid_usdt, all_ask_usdt)
    return {
        "quidax_imbalance_top1_usdt": _format_optional_decimal(
            _imbalance(topn_bid_usdt, topn_ask_usdt)
        ),
        "quidax_imbalance_topn_usdt": _format_optional_decimal(imbalance_topn),
        "quidax_imbalance_topn_cngn": _format_optional_decimal(
            _imbalance(all_bid_cngn, all_ask_cngn)
        ),
        # Quidax native pair is USDT/cNGN. Native bid-heavy pressure raises
        # cNGN-per-USDT and lowers USD-per-cNGN, so cNGN/USD pressure flips sign.
        "quidax_cngn_usd_pressure_topn": _format_optional_decimal(
            -imbalance_topn if imbalance_topn is not None else None
        ),
        "quidax_owa_mid_top1": _format_optional_decimal(
            _order_weighted_mid(
                bid_price=top.sell_cngn_usd,
                ask_price=top.buy_cngn_usd,
                bid_qty=_book_cngn(top_asks),
                ask_qty=_book_cngn(top_bids),
                sqrt_weighted=True,
            )
        ),
        "quidax_owa_mid_topn": _format_optional_decimal(
            _order_weighted_mid(
                bid_price=top.sell_cngn_usd,
                ask_price=top.buy_cngn_usd,
                bid_qty=_book_cngn(asks),
                ask_qty=_book_cngn(bids),
                sqrt_weighted=True,
            )
        ),
        "quidax_microprice_top1": _format_optional_decimal(
            _order_weighted_mid(
                bid_price=top.sell_cngn_usd,
                ask_price=top.buy_cngn_usd,
                bid_qty=_book_cngn(top_asks),
                ask_qty=_book_cngn(top_bids),
                sqrt_weighted=False,
            )
        ),
    }


def _dex_reference_fields(
    row: PriceSnapshot,
    *,
    dex_rows: dict[str, list[PriceSnapshot]],
    dex_timestamps: dict[str, list[int]],
    reference_mid: Decimal | None,
) -> dict[str, str]:
    fields: dict[str, str] = {}
    for venue in ("uni-base", "uni-bsc"):
        key = venue.replace("-", "_")
        dex_row = _previous_or_equal(
            dex_rows.get(venue, []),
            dex_timestamps.get(venue, []),
            row.timestamp_ms,
        )
        mid = dex_row.mid if dex_row else None
        fields[f"{key}_mid"] = _format_optional_decimal(mid)
        fields[f"{key}_age_ms"] = str(row.timestamp_ms - dex_row.timestamp_ms) if dex_row else ""
        fields[f"{key}_premium_bps"] = _format_optional_decimal(
            _premium_bps(mid, reference_mid)
        )
    return fields


def _book_usdt(levels: list[BookLevel]) -> Decimal:
    return sum((level.amount_usdt for level in levels), Decimal("0"))


def _book_cngn(levels: list[BookLevel]) -> Decimal:
    return sum(
        (level.amount_usdt * level.price_cngn_per_usdt for level in levels),
        Decimal("0"),
    )


def _imbalance(left: Decimal, right: Decimal) -> Decimal | None:
    total = left + right
    if total <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return (left - right) / total


def _order_weighted_mid(
    *,
    bid_price: Decimal | None,
    ask_price: Decimal | None,
    bid_qty: Decimal,
    ask_qty: Decimal,
    sqrt_weighted: bool,
) -> Decimal | None:
    if bid_price is None or ask_price is None or bid_qty <= 0 or ask_qty <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        bid_weight = bid_qty.sqrt() if sqrt_weighted else bid_qty
        ask_weight = ask_qty.sqrt() if sqrt_weighted else ask_qty
        total = bid_weight + ask_weight
        if total <= 0:
            return None
        return (ask_weight / total) * bid_price + (bid_weight / total) * ask_price


def _premium_bps(price: Decimal | None, reference: Decimal | None) -> Decimal | None:
    if price is None or reference is None or reference <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return (price - reference) / reference * Decimal("10000")


def _first_at_or_after(
    rows: list[PriceSnapshot],
    timestamps: list[int],
    target_timestamp_ms: int,
) -> PriceSnapshot | None:
    idx = bisect_left(timestamps, target_timestamp_ms)
    if idx >= len(rows):
        return None
    return rows[idx]


def _previous_or_equal(
    rows: list[PriceSnapshot],
    timestamps: list[int],
    target_timestamp_ms: int,
) -> PriceSnapshot | None:
    idx = bisect_left(timestamps, target_timestamp_ms)
    if idx < len(timestamps) and timestamps[idx] == target_timestamp_ms:
        return rows[idx]
    if idx == 0:
        return None
    return rows[idx - 1]


def _format_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    return _format_decimal(value)


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return numerator / denominator


def _format_decimal(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 28
        rounded = +value
    return format(rounded.normalize(), "f")


def _parse_horizons(raw: str) -> list[int]:
    horizons = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not horizons:
        raise ValueError("At least one horizon is required")
    if any(horizon <= 0 for horizon in horizons):
        raise ValueError("Horizons must be positive seconds")
    return horizons


def _write_csv(rows: list[dict[str, str]], fieldnames: list[str], out: TextIO) -> None:
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Quidax CEX executable fair-price markout rows.",
    )
    parser.add_argument("--db", required=True, help="Path to engine SQLite DB")
    parser.add_argument("--out", required=True, help="Output CSV path, or '-' for stdout")
    parser.add_argument(
        "--horizons",
        default=",".join(str(item) for item in DEFAULT_HORIZONS_SECONDS),
        help="Comma-separated markout horizons in seconds",
    )
    parser.add_argument("--target-usd", default=str(DEFAULT_TARGET_USD))
    parser.add_argument("--from-ts", type=int, default=None)
    parser.add_argument("--to-ts", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--max-label-lag-seconds",
        type=float,
        default=None,
        help="Drop labels whose first available future row is later than this tolerance.",
    )
    args = parser.parse_args()

    horizons = _parse_horizons(args.horizons)
    target_usd = Decimal(str(args.target_usd))
    if target_usd <= 0:
        raise ValueError("--target-usd must be positive")

    snapshots = await load_price_snapshots(
        args.db,
        from_ts=args.from_ts,
        to_ts=args.to_ts,
        limit=args.limit,
    )
    rows = build_markout_rows(
        snapshots,
        horizons_seconds=horizons,
        target_usd=target_usd,
        max_label_lag_ms=(
            int(args.max_label_lag_seconds * 1000)
            if args.max_label_lag_seconds is not None
            else None
        ),
    )
    fieldnames = _fieldnames(horizons)

    if args.out == "-":
        _write_csv(rows, fieldnames, sys.stdout)
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as file:
        _write_csv(rows, fieldnames, file)


if __name__ == "__main__":
    asyncio.run(_main())
