"""Action log queries."""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite


async def insert_action(
    conn: aiosqlite.Connection,
    *,
    venue: str,
    action_type: str,
    triggered_by: str,
    status: str,
    direction: str | None = None,
    amount_in: float | None = None,
    token_in: str | None = None,
    amount_out: float | None = None,
    token_out: str | None = None,
    price: float | None = None,
    tx_hash: str | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> int | None:
    now_ms = int(time.time() * 1000)
    metadata_json = json.dumps(metadata) if metadata is not None else None
    if idempotency_key:
        cursor = await conn.execute(
            "SELECT id FROM actions WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        row = await cursor.fetchone()
        if row is not None:
            await conn.execute(
                """
                UPDATE actions
                SET timestamp_ms = ?, status = ?, error = ?, tx_hash = COALESCE(?, tx_hash), price = COALESCE(?, price), metadata_json = COALESCE(?, metadata_json)
                WHERE id = ?
                """,
                (now_ms, status, error, tx_hash, price, metadata_json, row["id"]),
            )
            await conn.commit()
            return int(row["id"])

    cursor = await conn.execute(
        """
        INSERT INTO actions (
            timestamp_ms, venue, action_type, triggered_by, status, direction,
            amount_in, token_in, amount_out, token_out, price, tx_hash, error, metadata_json, idempotency_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_ms,
            venue,
            action_type,
            triggered_by,
            status,
            direction,
            amount_in,
            token_in,
            amount_out,
            token_out,
            price,
            tx_hash,
            error,
            metadata_json,
            idempotency_key,
        ),
    )
    await conn.commit()
    return cursor.lastrowid


async def get_actions(
    conn: aiosqlite.Connection,
    venue: str | None = None,
    action_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM actions WHERE 1=1"
    params: list[Any] = []
    if venue:
        query += " AND venue = ?"
        params.append(venue)
    if action_type:
        query += " AND action_type = ?"
        params.append(action_type)
    query += " ORDER BY timestamp_ms DESC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    return [
        {
            "id": row["id"],
            "timestamp": row["timestamp_ms"],
            "venue": row["venue"],
            "action_type": row["action_type"],
            "direction": row["direction"],
            "amount_in": row["amount_in"],
            "token_in": row["token_in"],
            "amount_out": row["amount_out"],
            "token_out": row["token_out"],
            "price": row["price"],
            "tx_hash": row["tx_hash"],
            "status": row["status"],
            "error": row["error"],
            "triggered_by": row["triggered_by"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
        }
        for row in rows
    ]
