import json
from pathlib import Path

from research.scripts.fetch_binance_reference import (
    BinanceKlineReferencePoint,
    BinanceSourceMetadata,
    fetch_binance_klines,
    render_metadata_json,
    write_reference_csv,
)


def test_fetch_binance_klines_paginates_and_uses_close_as_reference() -> None:
    calls: list[str] = []
    pages = [
        [
            [1000, "10", "12", "9", "11", "100", 1999, "1100", 5, "10", "110", "0"],
            [2000, "11", "13", "10", "12", "100", 2999, "1200", 5, "10", "120", "0"],
        ],
        [
            [3000, "12", "14", "11", "13", "100", 3999, "1300", 5, "10", "130", "0"],
        ],
    ]

    def fake_get_json(url: str) -> object:
        calls.append(url)
        return pages.pop(0)

    rows = fetch_binance_klines(
        symbol="USDTNGN",
        interval="1m",
        start_ms=1000,
        end_ms=4000,
        limit=2,
        get_json=fake_get_json,
    )

    assert rows == [
        BinanceKlineReferencePoint(1000, "11", "10", "12", "9", "11", "100"),
        BinanceKlineReferencePoint(2000, "12", "11", "13", "10", "12", "100"),
        BinanceKlineReferencePoint(3000, "13", "12", "14", "11", "13", "100"),
    ]
    assert "startTime=1000" in calls[0]
    assert "startTime=2001" in calls[1]


def test_write_reference_csv_uses_analyzer_compatible_columns(tmp_path: Path) -> None:
    out = tmp_path / "reference.csv"

    write_reference_csv(
        out,
        [
            BinanceKlineReferencePoint(1000, "11", "10", "12", "9", "11", "100"),
        ],
        symbol="USDTNGN",
        interval="1m",
    )

    assert out.read_text().splitlines() == [
        "timestamp_ms,reference_price,symbol,interval,open,high,low,close,volume",
        "1000,11,USDTNGN,1m,10,12,9,11,100",
    ]


def test_render_metadata_json_records_zero_row_overlap() -> None:
    metadata = BinanceSourceMetadata(
        symbol="USDTNGN",
        interval="1m",
        requested_start_ms=1775648033112,
        requested_end_ms=1783067243088,
        row_count=0,
        first_timestamp_ms=None,
        last_timestamp_ms=None,
        source_url="https://data-api.binance.vision/api/v3/klines",
    )

    payload = json.loads(render_metadata_json(metadata))

    assert payload["symbol"] == "USDTNGN"
    assert payload["row_count"] == 0
    assert payload["first_timestamp_ms"] is None
    assert payload["source_url"] == "https://data-api.binance.vision/api/v3/klines"
