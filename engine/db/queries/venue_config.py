"""Venue config queries."""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite


async def get_venue_config(conn: aiosqlite.Connection, venue: str) -> dict[str, Any] | None:
    cursor = await conn.execute(
        "SELECT venue, params_json FROM venue_config WHERE venue = ?",
        (venue,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "venue": row["venue"],
        "enabled": True,
        "params": json.loads(row["params_json"]),
    }


async def update_venue_config(conn: aiosqlite.Connection, venue: str, params: dict[str, Any]) -> None:
    await conn.execute(
        """
        INSERT INTO venue_config (venue, params_json, updated_at_ms)
        VALUES (?, ?, ?)
        ON CONFLICT(venue) DO UPDATE SET
            params_json = excluded.params_json,
            updated_at_ms = excluded.updated_at_ms
        """,
        (venue, json.dumps(params), int(time.time() * 1000)),
    )
    await conn.commit()
