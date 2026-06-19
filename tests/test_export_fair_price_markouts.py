import json
from decimal import Decimal

import pytest

from engine.db.connection import SQLiteConnectionManager
from engine.db.migrations import bootstrap_schema
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
