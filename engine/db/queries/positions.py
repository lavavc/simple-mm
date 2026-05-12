"""Position snapshot queries."""

from __future__ import annotations

import json

import aiosqlite

from engine.types import Position


async def insert_position(conn: aiosqlite.Connection, position: Position) -> None:
    await conn.execute(
        """
        INSERT INTO position_snapshots (
            venue, pair, timestamp_ms, balances_json, lp_position_json,
            position_value_usd, volume_24h_usd, rates_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(venue, pair, timestamp_ms) DO UPDATE SET
            balances_json = excluded.balances_json,
            lp_position_json = excluded.lp_position_json,
            position_value_usd = excluded.position_value_usd,
            volume_24h_usd = excluded.volume_24h_usd,
            rates_json = excluded.rates_json
        """,
        (
            position.venue,
            position.pair,
            position.timestamp,
            json.dumps({k: float(v) for k, v in position.balances.items()}),
            json.dumps(position.lp_position.model_dump(mode="json")) if position.lp_position else None,
            float(position.position_value_usd) if position.position_value_usd is not None else None,
            float(position.volume_24h_usd) if position.volume_24h_usd is not None else None,
            json.dumps({k: float(v) for k, v in position.rates.items()}) if position.rates else None,
        ),
    )
    await conn.commit()


async def get_position_snapshots(
    conn: aiosqlite.Connection,
    venue: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
    limit: int = 5000,
) -> list[dict]:
    query = """
        SELECT venue, pair, timestamp_ms, balances_json, lp_position_json,
               position_value_usd, volume_24h_usd, rates_json
        FROM position_snapshots
        WHERE venue = ?
    """
    params: list[object] = [venue]
    if from_ts is not None:
        query += " AND timestamp_ms >= ?"
        params.append(from_ts)
    if to_ts is not None:
        query += " AND timestamp_ms <= ?"
        params.append(to_ts)
    query += " ORDER BY timestamp_ms ASC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    return [
        {
            "venue": row["venue"],
            "pair": row["pair"],
            "timestamp": row["timestamp_ms"],
            "balances": json.loads(row["balances_json"]) if row["balances_json"] else {},
            "lp_position": json.loads(row["lp_position_json"]) if row["lp_position_json"] else None,
            "position_value_usd": row["position_value_usd"],
            "volume_24h_usd": row["volume_24h_usd"],
            "rates": json.loads(row["rates_json"]) if row["rates_json"] else None,
        }
        for row in rows
    ]
