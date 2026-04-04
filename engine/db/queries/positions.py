"""Position snapshot queries."""

from __future__ import annotations

import json

import aiosqlite

from engine.api.schemas import Position


async def insert_position(conn: aiosqlite.Connection, position: Position) -> None:
    await conn.execute(
        """
        INSERT INTO position_snapshots (
            venue, pair, timestamp_ms, balances_json, lp_position_json, open_orders_json,
            position_value_usd, volume_24h_usd, rates_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(venue, pair, timestamp_ms) DO UPDATE SET
            balances_json = excluded.balances_json,
            lp_position_json = excluded.lp_position_json,
            open_orders_json = excluded.open_orders_json,
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
            json.dumps(position.open_orders) if position.open_orders is not None else None,
            float(position.position_value_usd) if position.position_value_usd is not None else None,
            float(position.volume_24h_usd) if position.volume_24h_usd is not None else None,
            json.dumps({k: float(v) for k, v in position.rates.items()}) if position.rates else None,
        ),
    )
    await conn.commit()
