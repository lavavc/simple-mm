"""System state queries."""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite


async def get_system_state(conn: aiosqlite.Connection, key: str) -> str | None:
    cursor = await conn.execute(
        "SELECT value_json FROM system_state WHERE key = ?",
        (key,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value_json"])
    except Exception:
        return row["value_json"]


async def set_system_state(conn: aiosqlite.Connection, key: str, value: Any) -> None:
    payload = value if isinstance(value, str) else json.dumps(value)
    now_ms = int(time.time() * 1000)
    await conn.execute(
        """
        INSERT INTO system_state (key, value_json, updated_at_ms)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at_ms = excluded.updated_at_ms
        """,
        (key, payload, now_ms),
    )
    await conn.commit()
