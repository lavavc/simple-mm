import json
import sqlite3
from pathlib import Path

import pytest

from engine.db.migrations.schema import SCHEMA_SQL
from scripts.import_pool_snapshots import import_pool_snapshots


def test_import_pool_snapshots_upserts_dex_rows(tmp_path: Path):
    db = tmp_path / "cngn.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_SQL)
    csv_path = tmp_path / "pool.csv"
    csv_path.write_text(
        "block_time,chain,pool_id,event_type,tx_hash,log_index,block_number,sqrt_price_x96,tick,active_liquidity,fee_rate,amount0,amount1,amount_usd,cngn_usd_price,token0_symbol,token1_symbol\n"
        "2026-01-01T00:00:00+00:00,base,0xpool,swap,0x1,7,10,79228162514264337593543950336,0,100,0.0015,-1000,1,1,1,cNGN,USDC\n"
    )
    first = import_pool_snapshots(db, csv_path, "uni-base")
    second = import_pool_snapshots(db, csv_path, "uni-base")
    assert first.inserted_or_updated == 1
    assert first.pool == "uni-base"
    assert first.first_timestamp_ms == 1767225600000
    assert first.last_timestamp_ms == 1767225600000
    assert second.inserted_or_updated == 1
    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "select count(*) from price_snapshots where source='uni-base_pool'"
        ).fetchone()[0]
        bid, ask, mid, metadata = conn.execute(
            "select bid, ask, mid, metadata_json from price_snapshots"
        ).fetchone()
    assert count == 1
    assert bid < mid < ask
    assert bid == pytest.approx(0.9985)
    assert mid == pytest.approx(1.0)
    assert ask == pytest.approx(1 / 0.9985)
    assert '"tx_hash": "0x1"' in metadata
    parsed_metadata = json.loads(metadata)
    assert parsed_metadata["raw_sqrt_mid"] == "1"
    assert parsed_metadata["fee_rate"] == "0.0015"
    assert parsed_metadata["quote_model"] == "dex_fee_adjusted_sqrt"
    assert parsed_metadata["price_impact_included"] is False


def test_import_pool_snapshots_sequences_rows_in_same_source_second(tmp_path: Path):
    db = tmp_path / "cngn.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_SQL)
    csv_path = tmp_path / "pool.csv"
    block_time = "2026-01-01T00:00:00+00:00"
    csv_path.write_text(
        "block_time,chain,pool_id,event_type,tx_hash,log_index,block_number,sqrt_price_x96,tick,active_liquidity,fee_rate,amount0,amount1,amount_usd,cngn_usd_price,token0_symbol,token1_symbol\n"
        f"{block_time},base,0xpool,swap,0x1,7,10,79228162514264337593543950336,0,100,0.0015,-1000,1,1,1,cNGN,USDC\n"
        f"{block_time},base,0xpool,swap,0x2,8,10,79228162514264337593543950336,0,100,0.0015,1000,-1,1,1,cNGN,USDC\n"
    )

    import_pool_snapshots(db, csv_path, "uni-base")

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "select timestamp_ms, metadata_json from price_snapshots "
            "where source='uni-base_pool' order by timestamp_ms"
        ).fetchall()
    assert len(rows) == 2
    assert rows[1][0] == rows[0][0] + 1
    metadata = [json.loads(row[1]) for row in rows]
    assert [entry["block_time"] for entry in metadata] == [block_time, block_time]
    assert [entry["tx_hash"] for entry in metadata] == ["0x1", "0x2"]


def test_import_pool_snapshots_rejects_more_than_1000_rows_in_source_second(tmp_path: Path):
    db = tmp_path / "cngn.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_SQL)
    csv_path = tmp_path / "pool.csv"
    block_time = "2026-01-01T00:00:00+00:00"
    header = (
        "block_time,chain,pool_id,event_type,tx_hash,log_index,block_number,"
        "sqrt_price_x96,tick,active_liquidity,fee_rate,amount0,amount1,"
        "amount_usd,cngn_usd_price,token0_symbol,token1_symbol\n"
    )
    rows = [
        f"{block_time},base,0xpool,swap,0x{i:x},{i},10,"
        "79228162514264337593543950336,0,100,0.0015,-1000,1,1,1,cNGN,USDC\n"
        for i in range(1001)
    ]
    csv_path.write_text(header + "".join(rows))

    with pytest.raises(ValueError, match="1000 rows"):
        import_pool_snapshots(db, csv_path, "uni-base")
