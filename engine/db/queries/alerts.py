"""Alert queries."""

from __future__ import annotations

import time

import aiosqlite

from engine.types import Alert


def _require_lastrowid(lastrowid: int | None) -> int:
    if lastrowid is None:
        raise RuntimeError("sqlite did not return a row id for alert insert")
    return lastrowid


async def insert_alert(
    conn: aiosqlite.Connection,
    *,
    severity: str,
    category: str,
    message: str,
    dedup: bool = False,
    dedupe_key: str | None = None,
) -> int:
    now_ms = int(time.time() * 1000)
    key = dedupe_key or (f"{category}:{message}" if dedup else None)
    if key:
        cursor = await conn.execute(
            "SELECT id FROM alerts WHERE dedupe_key = ? AND status = 'open' LIMIT 1",
            (key,),
        )
        row = await cursor.fetchone()
        if row is not None:
            await conn.execute(
                """
                UPDATE alerts
                SET severity = ?, message = ?, last_seen_at_ms = ?, occurrence_count = occurrence_count + 1
                WHERE id = ?
                """,
                (severity, message, now_ms, row["id"]),
            )
            await conn.commit()
            return int(row["id"])

        cursor = await conn.execute(
            """
            INSERT INTO alerts (
                category, severity, message, dedupe_key, status,
                first_seen_at_ms, last_seen_at_ms, occurrence_count
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, 1)
            """,
            (category, severity, message, key, now_ms, now_ms),
        )
        await conn.commit()
        return _require_lastrowid(cursor.lastrowid)

    cursor = await conn.execute(
        """
        INSERT INTO alerts (
            category, severity, message, dedupe_key, status,
            first_seen_at_ms, last_seen_at_ms, occurrence_count
        ) VALUES (?, ?, ?, NULL, 'open', ?, ?, 1)
        """,
        (category, severity, message, now_ms, now_ms),
    )
    await conn.commit()
    return _require_lastrowid(cursor.lastrowid)


async def get_alerts(conn: aiosqlite.Connection, limit: int = 20) -> list[Alert]:
    cursor = await conn.execute(
        "SELECT * FROM alerts ORDER BY last_seen_at_ms DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        Alert(
            id=row["id"],
            timestamp=row["last_seen_at_ms"],
            severity=row["severity"],
            category=row["category"],
            message=row["message"],
            acknowledged=row["status"] != "open",
        )
        for row in rows
    ]


async def acknowledge_alert(conn: aiosqlite.Connection, alert_id: int) -> None:
    await conn.execute(
        "UPDATE alerts SET status = 'acknowledged', last_seen_at_ms = ? WHERE id = ?",
        (int(time.time() * 1000), alert_id),
    )
    await conn.commit()
