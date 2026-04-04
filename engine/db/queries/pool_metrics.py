"""Pool metrics queries."""

from __future__ import annotations

from typing import Any

import aiosqlite


async def get_pool_metrics_history(
    conn: aiosqlite.Connection,
    venues: list[str],
    from_ts: int,
) -> list[dict[str, Any]]:
    if not venues:
        return []
    placeholders = ",".join("?" for _ in venues)
    cursor = await conn.execute(
        f"""
        SELECT timestamp_ms, venue, position_value_usd, volume_24h_usd
        FROM position_snapshots
        WHERE venue IN ({placeholders})
          AND timestamp_ms >= ?
          AND position_value_usd IS NOT NULL
        ORDER BY timestamp_ms ASC
        """,
        (*venues, from_ts),
    )
    rows = await cursor.fetchall()
    return [
        {
            "timestamp": row["timestamp_ms"],
            "venue": row["venue"],
            "position_value_usd": row["position_value_usd"],
            "volume_24h_usd": row["volume_24h_usd"],
        }
        for row in rows
    ]
