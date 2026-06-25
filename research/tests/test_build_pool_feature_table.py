import csv
import json
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from engine.db.migrations.schema import SCHEMA_SQL

EXPECTED_FIELDS = [
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
    "active_liquidity_running_max_share",
    "active_liquidity_running_max_share_cone_pct",
    "active_liquidity_running_max_denominator",
    "swap_flow_imbalance",
    "swap_flow_imbalance_cone_pct",
    "fee_intensity_proxy",
    "fee_intensity_proxy_cone_pct",
    "fee_intensity_proxy_model",
    "volume_cone_pct",
    "source_age_ms",
]


def test_build_pool_feature_table_writes_causal_swap_features(tmp_path: Path):
    db_path = tmp_path / "cngn.db"
    _write_price_snapshot_db(db_path)
    csv_path = tmp_path / "pool.csv"
    out_path = tmp_path / "derived" / "features.csv"
    _write_pool_csv(csv_path)

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[2]
                / "research/scripts/build_pool_feature_table.py",
            ),
            "--pool",
            "uni-base",
            "--csv",
            str(csv_path),
            "--db",
            str(db_path),
            "--out",
            str(out_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["rows_written"] == 2

    with out_path.open(newline="") as output_file:
        reader = csv.DictReader(output_file)
        assert reader.fieldnames == EXPECTED_FIELDS
        rows = list(reader)

    assert [row["tx_hash"] for row in rows] == ["0x1", "0x2"]
    assert rows[0]["raw_sqrt_mid"] == "1"
    assert rows[0]["stored_cngn_usd_price"] == "0.9985"
    assert rows[0]["stored_price_model"] == "swap_amount_ratio"
    assert Decimal(rows[0]["fee_adjusted_bid"]) == Decimal("0.9985")
    assert Decimal(rows[0]["fee_adjusted_ask"]) == pytest.approx(
        Decimal("1") / Decimal("0.9985")
    )
    assert rows[0]["dex_premium_bps"] == "0"
    assert rows[0]["dex_premium_cone_pct"] == ""
    assert rows[0]["source_age_ms"] == "0"

    assert rows[1]["raw_sqrt_mid"] == "4"
    assert rows[1]["stored_cngn_usd_price"] == "4"
    assert rows[1]["stored_price_model"] == "sqrt_mid"
    assert rows[1]["realized_volatility"] != ""
    assert rows[1]["dex_premium_bps"] == "10000"
    assert rows[1]["dex_premium_cone_pct"] == "1.0000000000"
    assert rows[1]["active_liquidity_cone_pct"] == "0.0000000000"
    assert rows[1]["active_liquidity_running_max_share"] == "0.5"
    assert rows[1]["active_liquidity_running_max_share_cone_pct"] == "0.0000000000"
    assert rows[1]["active_liquidity_running_max_denominator"] == "100"
    assert rows[1]["swap_flow_imbalance"] == "-1"
    assert rows[1]["swap_flow_imbalance_cone_pct"] == "0.0000000000"
    assert rows[1]["fee_intensity_proxy"] != ""
    assert rows[1]["fee_intensity_proxy_cone_pct"] == ""
    assert rows[1]["fee_intensity_proxy_model"] == (
        "fee_rate_volume_over_active_liquidity_annualized_proxy"
    )
    assert rows[1]["volume_cone_pct"] == "0.0000000000"
    assert rows[1]["source_age_ms"] == "500"


def test_build_pool_feature_table_blanks_asof_fields_when_no_fair_source(
    tmp_path: Path,
):
    db_path = tmp_path / "cngn.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
    csv_path = tmp_path / "pool.csv"
    out_path = tmp_path / "derived" / "features.csv"
    _write_pool_csv(csv_path)

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[2]
                / "research/scripts/build_pool_feature_table.py",
            ),
            "--pool",
            "uni-base",
            "--csv",
            str(csv_path),
            "--db",
            str(db_path),
            "--out",
            str(out_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    with out_path.open(newline="") as output_file:
        rows = list(csv.DictReader(output_file))

    assert rows[0]["dex_premium_bps"] == ""
    assert rows[0]["source_age_ms"] == ""


def test_build_pool_feature_table_adds_causal_lookback_cones(tmp_path: Path) -> None:
    db_path = tmp_path / "cngn.db"
    _write_price_snapshot_db(db_path)
    csv_path = tmp_path / "pool.csv"
    out_path = tmp_path / "derived" / "features.csv"
    _write_windowed_pool_csv(csv_path)

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[2]
                / "research/scripts/build_pool_feature_table.py",
            ),
            "--pool",
            "uni-base",
            "--csv",
            str(csv_path),
            "--db",
            str(db_path),
            "--out",
            str(out_path),
            "--cone-lookback-seconds",
            "3600",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    with out_path.open(newline="") as output_file:
        reader = csv.DictReader(output_file)
        rows = list(reader)

    assert "active_liquidity_cone_pct_1h" in reader.fieldnames
    assert "volume_cone_pct_1h" in reader.fieldnames
    assert rows[2]["active_liquidity_cone_pct"] == "0.5000000000"
    assert rows[2]["active_liquidity_cone_pct_1h"] == "1.0000000000"
    assert rows[2]["volume_cone_pct"] == "0.5000000000"
    assert rows[2]["volume_cone_pct_1h"] == "1.0000000000"


def _write_price_snapshot_db(db_path: Path) -> None:
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
                ("quidax", 1767225601000, 1, 1, 1, None),
                ("quidax", 1767225601500, 2, 2, 2, None),
            ],
        )


def _write_pool_csv(csv_path: Path) -> None:
    csv_path.write_text(
        "block_time,chain,pool_id,event_type,tx_hash,log_index,block_number,"
        "sqrt_price_x96,tick,active_liquidity,fee_rate,amount0,amount1,"
        "amount_usd,cngn_usd_price,token0_symbol,token1_symbol\n"
        "2026-01-01T00:00:01+00:00,base,0xpool,swap,0x1,7,10,"
        "79228162514264337593543950336,0,100,0.0015,-1000,998.5,"
        "998.5,0.9985,cNGN,USDC\n"
        "2026-01-01T00:00:01.500000+00:00,base,0xpool,mint,0xmint,8,11,"
        "79228162514264337593543950336,0,100,0.0015,0,0,0,1,cNGN,USDC\n"
        "2026-01-01T00:00:02+00:00,base,0xpool,swap,0x2,9,12,"
        "158456325028528675187087900672,0,50,0.0015,100,-200,"
        "200,4,cNGN,USDC\n"
    )


def _write_windowed_pool_csv(csv_path: Path) -> None:
    csv_path.write_text(
        "block_time,chain,pool_id,event_type,tx_hash,log_index,block_number,"
        "sqrt_price_x96,tick,active_liquidity,fee_rate,amount0,amount1,"
        "amount_usd,cngn_usd_price,token0_symbol,token1_symbol\n"
        "2026-01-01T00:00:01+00:00,base,0xpool,swap,0x1,7,10,"
        "79228162514264337593543950336,0,100,0.0015,-1000,998.5,"
        "998.5,0.9985,cNGN,USDC\n"
        "2026-01-01T00:00:02+00:00,base,0xpool,swap,0x2,9,12,"
        "158456325028528675187087900672,0,50,0.0015,100,-200,"
        "200,4,cNGN,USDC\n"
        "2026-01-01T01:00:01.500000+00:00,base,0xpool,swap,0x3,10,13,"
        "158456325028528675187087900672,0,75,0.0015,-100,100,"
        "250,4,cNGN,USDC\n"
    )
