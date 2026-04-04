"""Price snapshot queries."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import aiosqlite

from engine.api.schemas import PriceQuote


async def insert_price_snapshot(
    conn: aiosqlite.Connection,
    quote: PriceQuote,
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO price_snapshots (source, timestamp_ms, bid, ask, mid, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, timestamp_ms) DO UPDATE SET
            bid = excluded.bid,
            ask = excluded.ask,
            mid = excluded.mid,
            metadata_json = excluded.metadata_json
        """,
        (
            quote.source,
            quote.timestamp,
            float(quote.bid),
            float(quote.ask),
            float(quote.mid),
            json.dumps(metadata) if metadata is not None else None,
        ),
    )
    await conn.commit()


async def get_recent_prices(conn: aiosqlite.Connection, limit: int = 100) -> list[Decimal]:
    cursor = await conn.execute(
        "SELECT mid FROM price_snapshots ORDER BY timestamp_ms DESC LIMIT ?",
        (limit,),
    )
    rows = list(await cursor.fetchall())
    return [Decimal(str(row["mid"])) for row in reversed(rows)]


async def get_price_history(
    conn: aiosqlite.Connection,
    from_ts: int | None = None,
    to_ts: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = "SELECT source, timestamp_ms, bid, ask, mid, metadata_json FROM price_snapshots WHERE 1=1"
    params: list[Any] = []
    if from_ts is not None:
        query += " AND timestamp_ms >= ?"
        params.append(from_ts)
    if to_ts is not None:
        query += " AND timestamp_ms <= ?"
        params.append(to_ts)
    query += " ORDER BY timestamp_ms DESC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    history: list[dict[str, Any]] = []
    for row in rows:
        history.append(
            {
                "source": row["source"],
                "timestamp": row["timestamp_ms"],
                "bid": row["bid"],
                "ask": row["ask"],
                "mid": row["mid"],
                "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
            }
        )
    return history


async def get_price_snapshots_in_window(
    conn: aiosqlite.Connection,
    from_ts: int,
    to_ts: int,
    source: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    query = (
        "SELECT timestamp_ms, source, bid, ask, mid "
        "FROM price_snapshots WHERE timestamp_ms >= ? AND timestamp_ms <= ?"
    )
    params: list[Any] = [from_ts, to_ts]
    if source:
        query += " AND source LIKE ?"
        params.append(f"%{source}%")
    query += " ORDER BY timestamp_ms ASC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    return [
        {
            "timestamp": row["timestamp_ms"],
            "source": row["source"],
            "bid": row["bid"],
            "ask": row["ask"],
            "mid": row["mid"],
        }
        for row in rows
    ]
