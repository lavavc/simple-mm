"""Arbitrage attempt and leg queries."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import aiosqlite

from engine.types import ArbitrageOpportunity, ArbitrageTrade, DexArbOpportunity, coerce_decimal
from engine.arb.routing.route_registry import ROUTES


def _cex_direction_for_venues(buy_venue: str, sell_venue: str) -> str:
    for route in ROUTES:
        if route.pipeline != "cex_dex":
            continue
        if route.buy_leg.venue == buy_venue and route.sell_leg.venue == sell_venue:
            return route.direction
    raise ValueError(f"Unknown CEX route: {buy_venue} -> {sell_venue}")


def _cex_opp_from_row(row: aiosqlite.Row) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        id=row["id"],
        timestamp=row["detected_at_ms"],
        buy_venue=row["buy_venue"],
        sell_venue=row["sell_venue"],
        buy_price=Decimal(str(row["signal_price_buy"])),
        sell_price=Decimal(str(row["signal_price_sell"])),
        gross_spread_bps=row["gross_spread_bps"],
        net_spread_bps=row["net_spread_bps"],
        recommended_size_usd=Decimal(str(row["optimal_size_usd"])),
        expected_profit_usd=Decimal(str(row["expected_profit_usd"])),
        status=row["status"],
        actual_profit_usd=coerce_decimal(row["actual_profit_usd"]),
        reason=row["reason"],
        buy_amount_cngn=coerce_decimal(row["buy_amount_cngn"]),
        buy_tx_hash=row["buy_tx_hash"],
    )


def _dex_opp_from_row(row: aiosqlite.Row) -> DexArbOpportunity:
    return DexArbOpportunity(
        id=row["id"],
        timestamp=row["detected_at_ms"],
        direction=row["direction"],
        optimal_size_usd=Decimal(str(row["optimal_size_usd"])),
        expected_profit_usd=Decimal(str(row["expected_profit_usd"])),
        cngn_transferred=Decimal(str(row["cngn_transferred"])),
        expected_usd_out=Decimal(str(row["expected_usd_out"])),
        status=row["status"],
        net_spread_bps=row["net_spread_bps"],
        actual_profit_usd=coerce_decimal(row["actual_profit_usd"]),
        reason=row["reason"],
        uni_bsc_price=coerce_decimal(row["uni_bsc_price"]),
        uni_base_price=coerce_decimal(row["uni_base_price"]),
        buy_tx_hash=row["buy_tx_hash"],
        sell_tx_hash=row["sell_tx_hash"],
        slippage_tolerance_bps=row["slippage_tolerance_bps"],
        uni_bsc_fee_bps=row["uni_bsc_fee_bps"],
        uni_base_fee_bps=row["uni_base_fee_bps"],
        gas_usd=coerce_decimal(row["gas_usd"]),
        buy_amount_cngn=coerce_decimal(row["buy_amount_cngn"]),
        executed_size_usd=coerce_decimal(row["executed_size_usd"]),
    )


async def upsert_cex_attempt(
    conn: aiosqlite.Connection,
    opp: ArbitrageOpportunity,
    engine_key: str | None = None,
) -> None:
    now_ms = int(time.time() * 1000)
    await conn.execute(
        """
        INSERT INTO arb_attempts (
            id, pipeline, direction, buy_venue, sell_venue, detected_at_ms, updated_at_ms,
            status, reason, signal_price_buy, signal_price_sell, gross_spread_bps,
            net_spread_bps, expected_profit_usd, actual_profit_usd, optimal_size_usd,
            buy_amount_cngn, buy_tx_hash, engine_key
        ) VALUES (?, 'cex_dex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            updated_at_ms = excluded.updated_at_ms,
            status = excluded.status,
            reason = excluded.reason,
            signal_price_buy = excluded.signal_price_buy,
            signal_price_sell = excluded.signal_price_sell,
            gross_spread_bps = excluded.gross_spread_bps,
            net_spread_bps = excluded.net_spread_bps,
            expected_profit_usd = excluded.expected_profit_usd,
            actual_profit_usd = COALESCE(excluded.actual_profit_usd, arb_attempts.actual_profit_usd),
            optimal_size_usd = excluded.optimal_size_usd,
            buy_amount_cngn = COALESCE(excluded.buy_amount_cngn, arb_attempts.buy_amount_cngn),
            buy_tx_hash = COALESCE(excluded.buy_tx_hash, arb_attempts.buy_tx_hash),
            engine_key = COALESCE(excluded.engine_key, arb_attempts.engine_key)
        """,
        (
            opp.id,
            _cex_direction_for_venues(opp.buy_venue, opp.sell_venue),
            opp.buy_venue,
            opp.sell_venue,
            opp.timestamp,
            now_ms,
            opp.status,
            opp.reason,
            float(opp.buy_price),
            float(opp.sell_price),
            opp.gross_spread_bps,
            opp.net_spread_bps,
            float(opp.expected_profit_usd),
            float(opp.actual_profit_usd) if opp.actual_profit_usd is not None else None,
            float(opp.recommended_size_usd),
            float(opp.buy_amount_cngn) if opp.buy_amount_cngn is not None else None,
            opp.buy_tx_hash,
            engine_key,
        ),
    )
    await conn.commit()


async def update_cex_attempt(
    conn: aiosqlite.Connection,
    opp_id: str,
    *,
    status: str,
    actual_profit_usd: float | None = None,
    reason: str | None = None,
    buy_amount_cngn: float | None = None,
    buy_tx_hash: str | None = None,
) -> None:
    updates = ["status = ?", "updated_at_ms = ?"]
    params: list[Any] = [status, int(time.time() * 1000)]
    if reason is not None:
        updates.append("reason = ?")
        params.append(reason)
    if actual_profit_usd is not None:
        updates.append("actual_profit_usd = ?")
        params.append(actual_profit_usd)
    if buy_amount_cngn is not None:
        updates.append("buy_amount_cngn = ?")
        params.append(buy_amount_cngn)
    if buy_tx_hash is not None:
        updates.append("buy_tx_hash = ?")
        params.append(buy_tx_hash)
    params.append(opp_id)
    await conn.execute(
        f"UPDATE arb_attempts SET {', '.join(updates)} WHERE id = ? AND pipeline = 'cex_dex'",
        params,
    )
    await conn.commit()


async def get_cex_attempts(
    conn: aiosqlite.Connection,
    status: str | None = None,
    from_ts: int | None = None,
    to_ts: int | None = None,
    limit: int = 50,
) -> list[ArbitrageOpportunity]:
    query = "SELECT * FROM arb_attempts WHERE pipeline = 'cex_dex'"
    params: list[Any] = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if from_ts is not None:
        query += " AND detected_at_ms >= ?"
        params.append(from_ts)
    if to_ts is not None:
        query += " AND detected_at_ms <= ?"
        params.append(to_ts)
    query += " ORDER BY detected_at_ms DESC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(query, params)
    return [_cex_opp_from_row(row) for row in await cursor.fetchall()]


async def get_cex_attempt(conn: aiosqlite.Connection, opp_id: str) -> ArbitrageOpportunity | None:
    cursor = await conn.execute(
        "SELECT * FROM arb_attempts WHERE id = ? AND pipeline = 'cex_dex'",
        (opp_id,),
    )
    row = await cursor.fetchone()
    return _cex_opp_from_row(row) if row else None


async def upsert_dex_attempt(
    conn: aiosqlite.Connection,
    opp: DexArbOpportunity,
    engine_key: str | None = None,
) -> None:
    now_ms = int(time.time() * 1000)
    await conn.execute(
        """
        INSERT INTO arb_attempts (
            id, pipeline, direction, buy_venue, sell_venue, detected_at_ms, updated_at_ms,
            status, reason, net_spread_bps, expected_profit_usd, actual_profit_usd,
            optimal_size_usd, executed_size_usd, buy_amount_cngn, cngn_transferred,
            expected_usd_out, uni_bsc_price, uni_base_price, slippage_tolerance_bps,
            uni_bsc_fee_bps, uni_base_fee_bps, gas_usd, buy_tx_hash, sell_tx_hash, engine_key
        ) VALUES (?, 'dex_dex', ?, '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            updated_at_ms = excluded.updated_at_ms,
            status = excluded.status,
            reason = excluded.reason,
            net_spread_bps = excluded.net_spread_bps,
            expected_profit_usd = excluded.expected_profit_usd,
            actual_profit_usd = COALESCE(excluded.actual_profit_usd, arb_attempts.actual_profit_usd),
            optimal_size_usd = excluded.optimal_size_usd,
            executed_size_usd = COALESCE(excluded.executed_size_usd, arb_attempts.executed_size_usd),
            buy_amount_cngn = COALESCE(excluded.buy_amount_cngn, arb_attempts.buy_amount_cngn),
            cngn_transferred = excluded.cngn_transferred,
            expected_usd_out = excluded.expected_usd_out,
            uni_bsc_price = excluded.uni_bsc_price,
            uni_base_price = excluded.uni_base_price,
            slippage_tolerance_bps = excluded.slippage_tolerance_bps,
            uni_bsc_fee_bps = excluded.uni_bsc_fee_bps,
            uni_base_fee_bps = excluded.uni_base_fee_bps,
            gas_usd = excluded.gas_usd,
            buy_tx_hash = COALESCE(excluded.buy_tx_hash, arb_attempts.buy_tx_hash),
            sell_tx_hash = COALESCE(excluded.sell_tx_hash, arb_attempts.sell_tx_hash),
            engine_key = COALESCE(excluded.engine_key, arb_attempts.engine_key)
        """,
        (
            opp.id,
            opp.direction,
            opp.timestamp,
            now_ms,
            opp.status,
            opp.reason,
            opp.net_spread_bps,
            float(opp.expected_profit_usd),
            float(opp.actual_profit_usd) if opp.actual_profit_usd is not None else None,
            float(opp.optimal_size_usd),
            float(opp.executed_size_usd) if opp.executed_size_usd is not None else None,
            float(opp.buy_amount_cngn) if opp.buy_amount_cngn is not None else None,
            float(opp.cngn_transferred),
            float(opp.expected_usd_out),
            float(opp.uni_bsc_price) if opp.uni_bsc_price is not None else None,
            float(opp.uni_base_price) if opp.uni_base_price is not None else None,
            opp.slippage_tolerance_bps,
            opp.uni_bsc_fee_bps,
            opp.uni_base_fee_bps,
            float(opp.gas_usd) if opp.gas_usd is not None else None,
            opp.buy_tx_hash,
            opp.sell_tx_hash,
            engine_key,
        ),
    )
    await conn.commit()


async def update_dex_attempt(
    conn: aiosqlite.Connection,
    opp_id: str,
    *,
    status: str,
    actual_profit_usd: float | None = None,
    reason: str | None = None,
    buy_tx_hash: str | None = None,
    sell_tx_hash: str | None = None,
    buy_amount_cngn: Decimal | None = None,
    executed_size_usd: float | None = None,
) -> None:
    updates = ["status = ?", "updated_at_ms = ?"]
    params: list[Any] = [status, int(time.time() * 1000)]
    if actual_profit_usd is not None:
        updates.append("actual_profit_usd = ?")
        params.append(actual_profit_usd)
    if reason is not None:
        updates.append("reason = ?")
        params.append(reason)
    if buy_tx_hash is not None:
        updates.append("buy_tx_hash = ?")
        params.append(buy_tx_hash)
    if sell_tx_hash is not None:
        updates.append("sell_tx_hash = ?")
        params.append(sell_tx_hash)
    if buy_amount_cngn is not None:
        updates.append("buy_amount_cngn = ?")
        params.append(float(buy_amount_cngn))
    if executed_size_usd is not None:
        updates.append("executed_size_usd = ?")
        params.append(executed_size_usd)
    params.append(opp_id)
    await conn.execute(
        f"UPDATE arb_attempts SET {', '.join(updates)} WHERE id = ? AND pipeline = 'dex_dex'",
        params,
    )
    await conn.commit()


async def expire_old_dex_attempts(conn: aiosqlite.Connection, cutoff_ts: int) -> None:
    await conn.execute(
        """
        UPDATE arb_attempts
        SET status = 'expired', reason = 'Timeout', updated_at_ms = ?
        WHERE pipeline = 'dex_dex'
          AND status IN ('detected', 'executing')
          AND detected_at_ms < ?
        """,
        (int(time.time() * 1000), cutoff_ts),
    )
    await conn.commit()


async def get_dex_attempts(
    conn: aiosqlite.Connection,
    status: str | None = None,
    from_ts: int | None = None,
    to_ts: int | None = None,
    limit: int = 50,
) -> list[DexArbOpportunity]:
    query = "SELECT * FROM arb_attempts WHERE pipeline = 'dex_dex'"
    params: list[Any] = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if from_ts is not None:
        query += " AND detected_at_ms >= ?"
        params.append(from_ts)
    if to_ts is not None:
        query += " AND detected_at_ms <= ?"
        params.append(to_ts)
    query += " ORDER BY detected_at_ms DESC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(query, params)
    return [_dex_opp_from_row(row) for row in await cursor.fetchall()]


async def get_dex_attempt(conn: aiosqlite.Connection, opp_id: str) -> DexArbOpportunity | None:
    cursor = await conn.execute(
        "SELECT * FROM arb_attempts WHERE id = ? AND pipeline = 'dex_dex'",
        (opp_id,),
    )
    row = await cursor.fetchone()
    return _dex_opp_from_row(row) if row else None


async def get_active_dex_attempt(conn: aiosqlite.Connection, direction: str) -> str | None:
    cursor = await conn.execute(
        """
        SELECT id FROM arb_attempts
        WHERE pipeline = 'dex_dex'
          AND direction = ?
          AND status IN ('detected', 'executing')
        ORDER BY detected_at_ms DESC
        LIMIT 1
        """,
        (direction,),
    )
    row = await cursor.fetchone()
    return row["id"] if row else None


async def get_arbitrage_stats(conn: aiosqlite.Connection, from_ts: int) -> dict[str, Decimal | int]:
    cursor = await conn.execute(
        """
        SELECT
            COUNT(*) AS total_detected,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS total_executed,
            SUM(CASE WHEN status = 'completed' THEN actual_profit_usd ELSE 0 END) AS total_profit,
            SUM(COALESCE(executed_size_usd, optimal_size_usd, 0)) AS total_volume
        FROM arb_attempts
        WHERE detected_at_ms >= ?
        """,
        (from_ts,),
    )
    row = await cursor.fetchone()
    if row is None:
        return {
            "opportunities_detected": 0,
            "opportunities_executed": 0,
            "total_profit_usd": Decimal("0"),
            "total_volume_usd": Decimal("0"),
        }
    return {
        "opportunities_detected": row["total_detected"] or 0,
        "opportunities_executed": row["total_executed"] or 0,
        "total_profit_usd": Decimal(str(row["total_profit"] or 0)),
        "total_volume_usd": Decimal(str(row["total_volume"] or 0)),
    }


async def upsert_leg(
    conn: aiosqlite.Connection,
    *,
    attempt_id: str,
    leg_role: str,
    venue: str,
    amount: Decimal,
    status: str,
    timestamp_ms: int,
    asset_symbol: str | None = None,
    price: Decimal | None = None,
    tx_hash: str | None = None,
    error: str | None = None,
) -> int | None:
    cursor = await conn.execute(
        """
        INSERT INTO arb_legs (
            attempt_id, leg_role, venue, asset_symbol, amount, price, tx_hash, status, error, timestamp_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(attempt_id, leg_role) DO UPDATE SET
            venue = excluded.venue,
            asset_symbol = excluded.asset_symbol,
            amount = excluded.amount,
            price = excluded.price,
            tx_hash = excluded.tx_hash,
            status = excluded.status,
            error = excluded.error,
            timestamp_ms = excluded.timestamp_ms
        """,
        (
            attempt_id,
            leg_role,
            venue,
            asset_symbol,
            float(amount),
            float(price) if price is not None else None,
            tx_hash,
            status,
            error,
            timestamp_ms,
        ),
    )
    await conn.commit()
    return cursor.lastrowid


async def get_legs(
    conn: aiosqlite.Connection,
    opportunity_id: str | None = None,
    limit: int = 50,
) -> list[ArbitrageTrade]:
    query = "SELECT * FROM arb_legs WHERE 1=1"
    params: list[Any] = []
    if opportunity_id:
        query += " AND attempt_id = ?"
        params.append(opportunity_id)
    query += " ORDER BY timestamp_ms DESC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    return [
        ArbitrageTrade(
            id=row["id"],
            opportunity_id=row["attempt_id"],
            venue=row["venue"],
            side=row["leg_role"],
            amount=Decimal(str(row["amount"])),
            price=coerce_decimal(row["price"]),
            tx_hash=row["tx_hash"],
            status=row["status"],
            timestamp=row["timestamp_ms"],
            error=row["error"],
        )
        for row in rows
    ]
