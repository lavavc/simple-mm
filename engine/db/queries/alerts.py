"""Alert queries."""

from __future__ import annotations

import time

import aiosqlite

from engine.api.schemas import Alert


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
            """
            INSERT INTO alerts (
                category, severity, message, dedupe_key, status,
                first_seen_at_ms, last_seen_at_ms, occurrence_count
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, 1)
            ON CONFLICT(dedupe_key) WHERE dedupe_key IS NOT NULL AND status = 'open'
            DO UPDATE SET
                severity = excluded.severity,
                message = excluded.message,
                last_seen_at_ms = excluded.last_seen_at_ms,
                occurrence_count = alerts.occurrence_count + 1
            RETURNING id
            """,
            (category, severity, message, key, now_ms, now_ms),
        )
        row = await cursor.fetchone()
        await conn.commit()
        return int(row["id"])

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
    return int(cursor.lastrowid)


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
