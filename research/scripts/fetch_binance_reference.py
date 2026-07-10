"""Fetch Binance spot klines as analyzer-compatible reference prices."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


BINANCE_DATA_API_URL = "https://data-api.binance.vision/api/v3/klines"


@dataclass(frozen=True)
class BinanceKlineReferencePoint:
    timestamp_ms: int
    reference_price: str
    open: str
    high: str
    low: str
    close: str
    volume: str


@dataclass(frozen=True)
class BinanceSourceMetadata:
    symbol: str
    interval: str
    requested_start_ms: int
    requested_end_ms: int
    row_count: int
    first_timestamp_ms: int | None
    last_timestamp_ms: int | None
    source_url: str


JsonGetter = Callable[[str], object]


def fetch_binance_klines(
    *,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
    base_url: str = BINANCE_DATA_API_URL,
    get_json: JsonGetter | None = None,
) -> list[BinanceKlineReferencePoint]:
    if start_ms >= end_ms:
        raise ValueError("start_ms must be less than end_ms")
    if limit <= 0 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")

    getter = get_json or _get_json
    cursor = start_ms
    rows: list[BinanceKlineReferencePoint] = []
    while cursor < end_ms:
        url = _build_url(
            base_url,
            symbol=symbol,
            interval=interval,
            start_ms=cursor,
            end_ms=end_ms,
            limit=limit,
        )
        payload = getter(url)
        if not isinstance(payload, list):
            raise ValueError("Binance klines response must be a list")
        page = [_parse_kline(row) for row in payload]
        if not page:
            break
        rows.extend(page)
        next_cursor = page[-1].timestamp_ms + 1
        if next_cursor <= cursor:
            raise ValueError("Binance kline pagination did not advance")
        cursor = next_cursor
        if len(page) < limit:
            break
    return [row for row in rows if start_ms <= row.timestamp_ms < end_ms]


def write_reference_csv(
    path: Path,
    rows: Sequence[BinanceKlineReferencePoint],
    *,
    symbol: str,
    interval: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "timestamp_ms",
        "reference_price",
        "symbol",
        "interval",
        "open",
        "high",
        "low",
        "close",
        "volume",
    )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "timestamp_ms": row.timestamp_ms,
                    "reference_price": row.reference_price,
                    "symbol": symbol,
                    "interval": interval,
                    "open": row.open,
                    "high": row.high,
                    "low": row.low,
                    "close": row.close,
                    "volume": row.volume,
                }
            )


def render_metadata_json(metadata: BinanceSourceMetadata) -> str:
    return json.dumps(asdict(metadata), indent=2, sort_keys=True) + "\n"


def _build_url(
    base_url: str,
    *,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int,
) -> str:
    params = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
    )
    return f"{base_url}?{params}"


def _parse_kline(row: object) -> BinanceKlineReferencePoint:
    if not isinstance(row, list) or len(row) < 6:
        raise ValueError("invalid Binance kline row")
    return BinanceKlineReferencePoint(
        timestamp_ms=int(row[0]),
        reference_price=str(row[4]),
        open=str(row[1]),
        high=str(row[2]),
        low=str(row[3]),
        close=str(row[4]),
        volume=str(row[5]),
    )


def _get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="USDTNGN")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--start-ms", required=True, type=int)
    parser.add_argument("--end-ms", required=True, type=int)
    parser.add_argument("--out", required=True)
    parser.add_argument("--metadata-out", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--base-url", default=BINANCE_DATA_API_URL)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    rows = fetch_binance_klines(
        symbol=args.symbol,
        interval=args.interval,
        start_ms=args.start_ms,
        end_ms=args.end_ms,
        limit=args.limit,
        base_url=args.base_url,
    )
    out_path = Path(args.out)
    metadata_path = Path(args.metadata_out)
    write_reference_csv(out_path, rows, symbol=args.symbol, interval=args.interval)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        render_metadata_json(
            BinanceSourceMetadata(
                symbol=args.symbol,
                interval=args.interval,
                requested_start_ms=args.start_ms,
                requested_end_ms=args.end_ms,
                row_count=len(rows),
                first_timestamp_ms=rows[0].timestamp_ms if rows else None,
                last_timestamp_ms=rows[-1].timestamp_ms if rows else None,
                source_url=args.base_url,
            )
        )
    )
    print(f"wrote {len(rows)} Binance {args.symbol} {args.interval} rows to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
