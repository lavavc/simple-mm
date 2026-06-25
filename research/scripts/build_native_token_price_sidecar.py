"""Build local native gas-token USD price sidecars for LP episode accounting."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

NATIVE_COIN_BY_CHAIN = {
    "base": "ethereum",
    "bsc": "binancecoin",
}
COINGECKO_RANGE_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"
SIDECAR_FIELDS = ["chain", "timestamp_ms", "native_token_usd", "source"]
MILLISECONDS_PER_HOUR = 3_600_000
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class TimeRange:
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class CoinGeckoPricePoint:
    timestamp_ms: int
    price_usd: Decimal


def ledger_time_ranges(
    ledger_paths_by_chain: Mapping[str, Path],
    *,
    padding_ms: int,
) -> dict[str, TimeRange]:
    if padding_ms < 0:
        raise ValueError("padding_ms must be non-negative")

    ranges: dict[str, TimeRange] = {}
    for chain, path in ledger_paths_by_chain.items():
        if chain not in NATIVE_COIN_BY_CHAIN:
            raise ValueError(f"unsupported chain={chain}")
        timestamps: list[int] = []
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"empty ledger CSV: {path}")
            missing = sorted({"chain", "timestamp_ms"}.difference(reader.fieldnames))
            if missing:
                raise ValueError(f"ledger CSV missing fields {missing}: {path}")
            for row in reader:
                row_chain = row["chain"].strip()
                if row_chain != chain:
                    raise ValueError(
                        f"ledger chain mismatch expected={chain} actual={row_chain} path={path}"
                    )
                timestamps.append(int(row["timestamp_ms"]))
        if not timestamps:
            raise ValueError(f"ledger CSV has no rows: {path}")
        ranges[chain] = TimeRange(
            start_ms=min(timestamps) - padding_ms,
            end_ms=max(timestamps) + padding_ms,
        )
    return ranges


def coingecko_prices_to_points(payload: Mapping[str, object]) -> list[CoinGeckoPricePoint]:
    raw_prices = payload.get("prices")
    if not isinstance(raw_prices, list):
        raise ValueError("CoinGecko response missing prices")

    prices_by_timestamp: dict[int, Decimal] = {}
    for raw_price in raw_prices:
        if not isinstance(raw_price, list) or len(raw_price) != 2:
            raise ValueError("CoinGecko price entry must be [timestamp_ms, price]")
        timestamp_ms = _coingecko_timestamp(raw_price[0])
        price_usd = _coingecko_price(raw_price[1])
        previous = prices_by_timestamp.get(timestamp_ms)
        if previous is not None and previous != price_usd:
            raise ValueError(f"conflicting CoinGecko price timestamp_ms={timestamp_ms}")
        prices_by_timestamp[timestamp_ms] = price_usd

    return [
        CoinGeckoPricePoint(timestamp_ms=timestamp_ms, price_usd=price_usd)
        for timestamp_ms, price_usd in sorted(prices_by_timestamp.items())
    ]


def build_native_price_rows(
    points_by_chain: Mapping[str, Sequence[CoinGeckoPricePoint]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for chain, points in points_by_chain.items():
        coin_id = _coin_id_for_chain(chain)
        for point in points:
            rows.append(
                {
                    "chain": chain,
                    "timestamp_ms": str(point.timestamp_ms),
                    "native_token_usd": str(point.price_usd),
                    "source": f"coingecko:{coin_id}",
                }
            )
    return sorted(rows, key=lambda row: (int(row["timestamp_ms"]), row["chain"]))


def fetch_coingecko_points(
    client: httpx.Client,
    *,
    coin_id: str,
    time_range: TimeRange,
    chunk_days: int,
    max_retries: int = 5,
    retry_sleep_seconds: float = 10.0,
    request_sleep_seconds: float = 1.0,
) -> list[CoinGeckoPricePoint]:
    if chunk_days <= 0:
        raise ValueError("chunk_days must be positive")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    if retry_sleep_seconds < 0:
        raise ValueError("retry_sleep_seconds must be non-negative")
    if request_sleep_seconds < 0:
        raise ValueError("request_sleep_seconds must be non-negative")
    chunk_seconds = chunk_days * SECONDS_PER_DAY
    start_seconds = time_range.start_ms // 1000
    end_seconds = (time_range.end_ms + 999) // 1000
    if start_seconds >= end_seconds:
        raise ValueError(
            f"empty time range start_ms={time_range.start_ms} end_ms={time_range.end_ms}"
        )

    points_by_timestamp: dict[int, Decimal] = {}
    cursor = start_seconds
    while cursor < end_seconds:
        window_end = min(cursor + chunk_seconds, end_seconds)
        payload = _fetch_coingecko_payload(
            client,
            coin_id=coin_id,
            start_seconds=cursor,
            end_seconds=window_end,
            max_retries=max_retries,
            retry_sleep_seconds=retry_sleep_seconds,
        )
        for point in coingecko_prices_to_points(payload):
            previous = points_by_timestamp.get(point.timestamp_ms)
            if previous is not None and previous != point.price_usd:
                raise ValueError(f"conflicting CoinGecko price timestamp_ms={point.timestamp_ms}")
            points_by_timestamp[point.timestamp_ms] = point.price_usd
        cursor = window_end
        if cursor < end_seconds and request_sleep_seconds > 0:
            time.sleep(request_sleep_seconds)

    return [
        CoinGeckoPricePoint(timestamp_ms=timestamp_ms, price_usd=price_usd)
        for timestamp_ms, price_usd in sorted(points_by_timestamp.items())
    ]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        action="append",
        required=True,
        help="Ledger CSV as chain=path. Supported chains: base, bsc.",
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--padding-hours", required=True, type=int)
    parser.add_argument("--chunk-days", default=80, type=int)
    parser.add_argument("--max-retries", default=5, type=int)
    parser.add_argument("--retry-sleep-seconds", default=10.0, type=float)
    parser.add_argument("--request-sleep-seconds", default=1.0, type=float)
    parser.add_argument("--timeout-seconds", default=30, type=float)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    ledger_paths = _parse_ledger_args(args.ledger)
    ranges = ledger_time_ranges(
        ledger_paths,
        padding_ms=args.padding_hours * MILLISECONDS_PER_HOUR,
    )
    points_by_chain: dict[str, list[CoinGeckoPricePoint]] = {}
    with httpx.Client(timeout=args.timeout_seconds) as client:
        for chain, time_range in sorted(ranges.items()):
            points_by_chain[chain] = fetch_coingecko_points(
                client,
                coin_id=_coin_id_for_chain(chain),
                time_range=time_range,
                chunk_days=args.chunk_days,
                max_retries=args.max_retries,
                retry_sleep_seconds=args.retry_sleep_seconds,
                request_sleep_seconds=args.request_sleep_seconds,
            )
    rows = build_native_price_rows(points_by_chain)
    _write_sidecar(args.out, rows)
    print(f"wrote {len(rows)} native token price rows to {args.out}")


def _parse_ledger_args(values: Sequence[str]) -> dict[str, Path]:
    ledger_paths: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"ledger must be chain=path: {value}")
        chain, raw_path = value.split("=", 1)
        chain = chain.strip()
        if chain in ledger_paths:
            raise ValueError(f"duplicate ledger chain={chain}")
        ledger_paths[chain] = Path(raw_path)
    return ledger_paths


def _fetch_coingecko_payload(
    client: httpx.Client,
    *,
    coin_id: str,
    start_seconds: int,
    end_seconds: int,
    max_retries: int,
    retry_sleep_seconds: float,
) -> Mapping[str, object]:
    response: httpx.Response | None = None
    for attempt in range(max_retries + 1):
        response = client.get(
            COINGECKO_RANGE_URL.format(coin_id=coin_id),
            params={
                "vs_currency": "usd",
                "from": str(start_seconds),
                "to": str(end_seconds),
            },
        )
        if response.status_code != 429 or attempt == max_retries:
            break
        sleep_seconds = _retry_after_seconds(response.headers) or retry_sleep_seconds
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    if response is None:
        raise RuntimeError("CoinGecko request was not attempted")
    response.raise_for_status()
    payload = json.loads(response.text, parse_float=Decimal)
    if not isinstance(payload, dict):
        raise ValueError("CoinGecko response must be an object")
    return payload


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    raw_value = headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        retry_after = float(raw_value)
    except ValueError:
        return None
    if retry_after < 0:
        return None
    return retry_after


def _write_sidecar(path: Path, rows: Sequence[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SIDECAR_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _coin_id_for_chain(chain: str) -> str:
    try:
        return NATIVE_COIN_BY_CHAIN[chain]
    except KeyError as exc:
        raise ValueError(f"unsupported chain={chain}") from exc


def _coingecko_timestamp(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("CoinGecko timestamp must be numeric")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    raise ValueError("CoinGecko timestamp must be numeric")


def _coingecko_price(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("CoinGecko price must be numeric")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise ValueError("CoinGecko price must be numeric")


if __name__ == "__main__":
    main()
