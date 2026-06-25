import csv
from decimal import Decimal

import httpx

from research.scripts.build_native_token_price_sidecar import (
    CoinGeckoPricePoint,
    TimeRange,
    build_native_price_rows,
    coingecko_prices_to_points,
    fetch_coingecko_points,
    ledger_time_ranges,
)


def _write_ledger(path, rows):
    fieldnames = ["chain", "timestamp_ms"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_ledger_time_ranges_apply_padding_by_chain(tmp_path):
    base = tmp_path / "base.csv"
    bsc = tmp_path / "bsc.csv"
    _write_ledger(
        base,
        [
            {"chain": "base", "timestamp_ms": "100000"},
            {"chain": "base", "timestamp_ms": "200000"},
        ],
    )
    _write_ledger(
        bsc,
        [
            {"chain": "bsc", "timestamp_ms": "150000"},
            {"chain": "bsc", "timestamp_ms": "250000"},
        ],
    )

    ranges = ledger_time_ranges({"base": base, "bsc": bsc}, padding_ms=60_000)

    assert ranges["base"].start_ms == 40_000
    assert ranges["base"].end_ms == 260_000
    assert ranges["bsc"].start_ms == 90_000
    assert ranges["bsc"].end_ms == 310_000


def test_coingecko_prices_to_points_parse_and_sort_prices():
    points = coingecko_prices_to_points(
        {
            "prices": [
                [2_000, 2040.125],
                [1_000, 2030.25],
                [1_000, 2030.25],
            ],
        }
    )

    assert points == [
        CoinGeckoPricePoint(timestamp_ms=1_000, price_usd=Decimal("2030.25")),
        CoinGeckoPricePoint(timestamp_ms=2_000, price_usd=Decimal("2040.125")),
    ]


def test_build_native_price_rows_map_coin_ids_to_chains():
    rows = build_native_price_rows(
        {
            "base": [
                CoinGeckoPricePoint(timestamp_ms=1_000, price_usd=Decimal("2000")),
                CoinGeckoPricePoint(timestamp_ms=2_000, price_usd=Decimal("2010")),
            ],
            "bsc": [
                CoinGeckoPricePoint(timestamp_ms=1_500, price_usd=Decimal("650")),
            ],
        }
    )

    assert rows == [
        {
            "chain": "base",
            "timestamp_ms": "1000",
            "native_token_usd": "2000",
            "source": "coingecko:ethereum",
        },
        {
            "chain": "bsc",
            "timestamp_ms": "1500",
            "native_token_usd": "650",
            "source": "coingecko:binancecoin",
        },
        {
            "chain": "base",
            "timestamp_ms": "2000",
            "native_token_usd": "2010",
            "source": "coingecko:ethereum",
        },
    ]


def test_fetch_coingecko_points_retries_rate_limits():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"prices": [[1_000, 2000.0]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    points = fetch_coingecko_points(
        client,
        coin_id="ethereum",
        time_range=TimeRange(start_ms=0, end_ms=2_000),
        chunk_days=1,
        max_retries=2,
        retry_sleep_seconds=0,
        request_sleep_seconds=0,
    )

    assert attempts == 2
    assert points == [CoinGeckoPricePoint(timestamp_ms=1_000, price_usd=Decimal("2000.0"))]
