import json
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from engine.db.connection import SQLiteConnectionManager
from engine.db.migrations import bootstrap_schema
from engine.db.migrations.schema import SCHEMA_SQL
from scripts.export_fair_price_markouts import (
    PriceSnapshot,
    build_markout_rows,
    load_price_snapshots,
)


def _quidax_snapshot(
    timestamp_ms: int,
    *,
    bid: str = "0.000620",
    ask: str = "0.000630",
    mid: str = "0.000625",
    bid_levels: list[list[str]],
    ask_levels: list[list[str]],
) -> PriceSnapshot:
    return PriceSnapshot(
        source="quidax",
        timestamp_ms=timestamp_ms,
        bid=Decimal(bid),
        ask=Decimal(ask),
        mid=Decimal(mid),
        metadata={
            "capture_type": "ticker_depth",
            "depth": {
                "bid_levels": [
                    {"price": price, "amount": amount} for price, amount in bid_levels
                ],
                "ask_levels": [
                    {"price": price, "amount": amount} for price, amount in ask_levels
                ],
            },
        },
    )


def _source_snapshot(
    source: str,
    timestamp_ms: int,
    mid: str,
) -> PriceSnapshot:
    return PriceSnapshot(
        source=source,
        timestamp_ms=timestamp_ms,
        bid=Decimal(mid),
        ask=Decimal(mid),
        mid=Decimal(mid),
        metadata=None,
    )


def test_build_markout_rows_uses_future_quidax_executable_depth() -> None:
    rows = build_markout_rows(
        [
            _quidax_snapshot(
                1_000,
                bid_levels=[["1600", "100"]],
                ask_levels=[["1610", "100"]],
            ),
            _quidax_snapshot(
                11_000,
                bid="0.000625",
                ask="0.000635",
                mid="0.000630",
                bid_levels=[["1620", "100"]],
                ask_levels=[["1630", "100"]],
            ),
        ],
        horizons_seconds=[10],
        target_usd=Decimal("10"),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["timestamp_ms"] == "1000"
    assert row["source"] == "quidax"
    assert row["target_usd"] == "10"
    assert row["quidax_ticker_mid"] == "0.000625"
    assert row["quidax_top_mid"] == "0.0006230590062111801242236024845"
    assert row["quidax_buy_cngn_usd"] == "0.000625"
    assert row["quidax_sell_cngn_usd"] == "0.0006211180124223602484472049689"
    assert row["label_10s_timestamp_ms"] == "11000"
    assert row["label_10s_lag_ms"] == "0"
    assert row["label_10s_buy_cngn_usd"] == "0.0006172839506172839506172839506"
    assert row["label_10s_sell_cngn_usd"] == "0.0006134969325153374233128834356"
    assert row["label_10s_executable_mid"] == "0.0006153904415663106869650836931"


def test_build_markout_rows_exports_book_imbalance_owa_and_dex_divergence() -> None:
    rows = build_markout_rows(
        [
            _source_snapshot("uni-base_pool", 500, "0.000620"),
            _source_snapshot("uni-bsc_pool", 800, "0.000640"),
            _quidax_snapshot(
                1_000,
                bid_levels=[["1600", "10"], ["1590", "30"]],
                ask_levels=[["1610", "20"], ["1620", "40"]],
            ),
            _quidax_snapshot(
                11_000,
                bid_levels=[["1605", "100"]],
                ask_levels=[["1615", "100"]],
            ),
        ],
        horizons_seconds=[10],
        target_usd=Decimal("10"),
    )

    row = rows[0]

    assert row["quidax_imbalance_top1_usdt"] == "-0.3333333333333333333333333333"
    assert row["quidax_imbalance_topn_usdt"] == "-0.2"
    assert row["quidax_cngn_usd_pressure_topn"] == "0.2"
    assert row["quidax_owa_mid_top1"] == "0.0006233949616750590033310394744"
    assert row["quidax_owa_mid_topn"] == "0.0006232623175097076680019177853"
    assert row["quidax_microprice_top1"] == "0.0006237113734182108708538439731"
    assert row["uni_base_mid"] == "0.00062"
    assert row["uni_base_age_ms"] == "500"
    assert row["uni_base_premium_bps"] == "-49.09657320872274143302180685"
    assert row["uni_bsc_mid"] == "0.00064"
    assert row["uni_bsc_age_ms"] == "200"
    assert row["uni_bsc_premium_bps"] == "271.9003115264797507788161994"


def test_build_markout_rows_joins_pool_features_by_previous_or_equal_timestamp() -> None:
    rows = build_markout_rows(
        [
            PriceSnapshot(
                source="quidax",
                timestamp_ms=10_000,
                bid=Decimal("1"),
                ask=Decimal("1"),
                mid=Decimal("1"),
                metadata={"capture_type": "ticker_depth"},
            ),
            PriceSnapshot(
                source="quidax",
                timestamp_ms=20_000,
                bid=Decimal("1.1"),
                ask=Decimal("1.1"),
                mid=Decimal("1.1"),
                metadata={"capture_type": "ticker_depth"},
            ),
        ],
        horizons_seconds=[10],
        target_usd=Decimal("100"),
        pool_feature_rows={
            "uni-base": [
                {
                    "timestamp_ms": "9000",
                    "dex_premium_cone_pct": "0.90",
                    "swap_flow_imbalance_cone_pct": "0.80",
                    "active_liquidity_cone_pct": "0.10",
                }
            ],
            "uni-bsc": [
                {
                    "timestamp_ms": "7000",
                    "dex_premium_cone_pct": "0.70",
                    "swap_flow_imbalance_cone_pct": "0.60",
                    "active_liquidity_cone_pct": "0.50",
                },
                {
                    "timestamp_ms": "10001",
                    "dex_premium_cone_pct": "0.99",
                    "swap_flow_imbalance_cone_pct": "0.99",
                    "active_liquidity_cone_pct": "0.99",
                },
            ],
        },
        feature_max_age_ms=2_000,
    )

    assert rows[0]["uni_base_feature_age_ms"] == "1000"
    assert rows[0]["uni_base_dex_premium_cone_pct"] == "0.90"
    assert rows[0]["uni_base_swap_flow_imbalance_cone_pct"] == "0.80"
    assert rows[0]["uni_base_active_liquidity_cone_pct"] == "0.10"
    assert rows[0]["uni_bsc_feature_age_ms"] == ""
    assert rows[0]["uni_bsc_dex_premium_cone_pct"] == ""


def test_build_markout_rows_skips_labels_when_future_depth_cannot_fill_size() -> None:
    rows = build_markout_rows(
        [
            _quidax_snapshot(
                1_000,
                bid_levels=[["1600", "100"]],
                ask_levels=[["1610", "100"]],
            ),
            _quidax_snapshot(
                11_000,
                bid_levels=[["1620", "1"]],
                ask_levels=[["1630", "1"]],
            ),
        ],
        horizons_seconds=[10],
        target_usd=Decimal("10"),
    )

    assert rows[0]["label_10s_buy_cngn_usd"] == ""
    assert rows[0]["label_10s_sell_cngn_usd"] == ""
    assert rows[0]["label_10s_executable_mid"] == ""


def test_build_markout_rows_rejects_stale_future_labels() -> None:
    rows = build_markout_rows(
        [
            _quidax_snapshot(
                1_000,
                bid_levels=[["1600", "100"]],
                ask_levels=[["1610", "100"]],
            ),
            _quidax_snapshot(
                41_000,
                bid_levels=[["1620", "100"]],
                ask_levels=[["1630", "100"]],
            ),
        ],
        horizons_seconds=[10],
        target_usd=Decimal("10"),
        max_label_lag_ms=15_000,
    )

    assert rows == []


@pytest.mark.asyncio
async def test_load_price_snapshots_reads_metadata_from_sqlite(tmp_path) -> None:
    db_path = tmp_path / "cngn.db"
    manager = SQLiteConnectionManager(str(db_path))
    conn = await manager.connect()
    try:
        await bootstrap_schema(conn)
        metadata = {"capture_type": "ticker_depth", "depth": {"bid_levels": []}}
        await conn.execute(
            """
            INSERT INTO price_snapshots (source, timestamp_ms, bid, ask, mid, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "quidax",
                1_000,
                0.00062,
                0.00063,
                0.000625,
                json.dumps(metadata),
            ),
        )
        await conn.commit()
    finally:
        await manager.close()

    snapshots = await load_price_snapshots(str(db_path))

    assert snapshots == [
        PriceSnapshot(
            source="quidax",
            timestamp_ms=1_000,
            bid=Decimal("0.00062"),
            ask=Decimal("0.00063"),
            mid=Decimal("0.000625"),
            metadata=metadata,
        )
    ]


def test_export_fair_price_markouts_writes_pool_feature_quality_json(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "cngn.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            """
            INSERT INTO price_snapshots (
                source, timestamp_ms, bid, ask, mid, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("quidax", 10_000, 1, 1, 1, json.dumps({"capture_type": "ticker_depth"})),
                (
                    "quidax",
                    20_000,
                    1.1,
                    1.1,
                    1.1,
                    json.dumps({"capture_type": "ticker_depth"}),
                ),
                (
                    "quidax",
                    30_000,
                    1.2,
                    1.2,
                    1.2,
                    json.dumps({"capture_type": "ticker_depth"}),
                ),
            ],
        )
    feature_csv = tmp_path / "uni_base_features.csv"
    feature_csv.write_text(
        "timestamp_ms,pool,dex_premium_cone_pct,swap_flow_imbalance_cone_pct,"
        "active_liquidity_cone_pct\n"
        "9000,uni-base,0.90,0.80,0.10\n"
        "19000,uni-base,,0.40,0.20\n"
    )
    out_csv = tmp_path / "markouts.csv"
    quality_out = tmp_path / "quality.json"

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "scripts/export_fair_price_markouts.py"
            ),
            "--db",
            str(db_path),
            "--out",
            str(out_csv),
            "--horizons",
            "10",
            "--pool-feature-csv",
            f"uni-base={feature_csv}",
            "--quality-out",
            str(quality_out),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    quality = json.loads(quality_out.read_text())
    assert quality["feature_max_age_seconds"] == 900
    assert quality["missing_feature_counts"] == {"uni-base": 1}
    assert quality["median_feature_age_ms"] == {"uni-base": 1000}
    assert quality["max_feature_age_ms"] == {"uni-base": 1000}
    assert quality["pool_features"]["uni-base"]["observed_rows"] == 1
    assert quality["pool_features"]["uni-base"]["missing_rows"] == 1
    assert quality["pool_features"]["uni-base"]["max_age_ms"] == 1000


def test_export_fair_price_markouts_rejects_nonfinite_feature_max_age(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "cngn.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
    out_csv = tmp_path / "markouts.csv"

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "scripts/export_fair_price_markouts.py"
            ),
            "--db",
            str(db_path),
            "--out",
            str(out_csv),
            "--feature-max-age-seconds",
            "NaN",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "--feature-max-age-seconds must be finite" in result.stderr
