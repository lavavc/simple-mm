"""Arbitrage history event queries."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import aiosqlite

from engine.types import (
    ArbitrageHistoryEvent,
    ArbitrageHistoryItem,
    ArbitrageHistoryWalletSnapshot,
)


def _wallet(stable_symbol: Any, stable_balance: Any, cngn_balance: Any) -> ArbitrageHistoryWalletSnapshot | None:
    if stable_symbol is None and stable_balance is None and cngn_balance is None:
        return None
    return ArbitrageHistoryWalletSnapshot(
        stable_symbol=stable_symbol,
        stable_balance=Decimal(str(stable_balance)) if stable_balance is not None else None,
        cngn_balance=Decimal(str(cngn_balance)) if cngn_balance is not None else None,
    )


def _event_from_joined_row(row: aiosqlite.Row) -> ArbitrageHistoryEvent:
    return ArbitrageHistoryEvent(
        id=row["id"],
        opportunity_id=row["attempt_id"],
        pipeline=row["pipeline"],
        event_type=row["event_type"],
        timestamp=row["timestamp_ms"],
        direction=row["direction"],
        buy_venue=row["buy_venue"],
        sell_venue=row["sell_venue"],
        status=row["status"],
        optimal_size_usd=Decimal(str(row["optimal_size_usd"])) if row["optimal_size_usd"] is not None else None,
        routed_size_usd=Decimal(str(row["routed_size_usd"])) if row["routed_size_usd"] is not None else None,
        executed_size_usd=Decimal(str(row["executed_size_usd"])) if row["executed_size_usd"] is not None else None,
        expected_profit_usd=Decimal(str(row["expected_profit_usd"])) if row["expected_profit_usd"] is not None else None,
        actual_profit_usd=Decimal(str(row["actual_profit_usd"])) if row["actual_profit_usd"] is not None else None,
        net_profit_usd=Decimal(str(row["net_profit_usd"])) if row["net_profit_usd"] is not None else None,
        net_spread_bps=row["net_spread_bps"],
        reason=row["reason"],
        buy_wallet=_wallet(
            row["buy_wallet_stable_symbol"],
            row["buy_wallet_stable_balance"],
            row["buy_wallet_cngn_balance"],
        ),
        sell_wallet=_wallet(
            row["sell_wallet_stable_symbol"],
            row["sell_wallet_stable_balance"],
            row["sell_wallet_cngn_balance"],
        ),
        buy_tx_hash=row["buy_tx_hash"],
        sell_tx_hash=row["sell_tx_hash"],
    )


async def upsert_arbitrage_history_event(conn: aiosqlite.Connection, event: ArbitrageHistoryEvent) -> None:
    await conn.execute(
        """
        INSERT INTO arb_attempts (
            id, pipeline, direction, buy_venue, sell_venue, detected_at_ms, updated_at_ms, status, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            pipeline = excluded.pipeline,
            direction = excluded.direction,
            buy_venue = excluded.buy_venue,
            sell_venue = excluded.sell_venue,
            updated_at_ms = excluded.updated_at_ms
        """,
        (
            event.opportunity_id,
            event.pipeline,
            event.direction,
            event.buy_venue,
            event.sell_venue,
            event.timestamp,
            event.timestamp,
            event.status,
            event.reason,
        ),
    )
    await conn.execute(
        """
        INSERT INTO arb_history_events (
            attempt_id, event_type, timestamp_ms, status, reason, optimal_size_usd,
            routed_size_usd, executed_size_usd, expected_profit_usd, actual_profit_usd,
            net_profit_usd, net_spread_bps, buy_wallet_stable_symbol,
            buy_wallet_stable_balance, buy_wallet_cngn_balance, sell_wallet_stable_symbol,
            sell_wallet_stable_balance, sell_wallet_cngn_balance, buy_tx_hash, sell_tx_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(attempt_id, event_type) DO UPDATE SET
            timestamp_ms = excluded.timestamp_ms,
            status = excluded.status,
            reason = excluded.reason,
            optimal_size_usd = excluded.optimal_size_usd,
            routed_size_usd = excluded.routed_size_usd,
            executed_size_usd = excluded.executed_size_usd,
            expected_profit_usd = excluded.expected_profit_usd,
            actual_profit_usd = excluded.actual_profit_usd,
            net_profit_usd = excluded.net_profit_usd,
            net_spread_bps = excluded.net_spread_bps,
            buy_wallet_stable_symbol = excluded.buy_wallet_stable_symbol,
            buy_wallet_stable_balance = excluded.buy_wallet_stable_balance,
            buy_wallet_cngn_balance = excluded.buy_wallet_cngn_balance,
            sell_wallet_stable_symbol = excluded.sell_wallet_stable_symbol,
            sell_wallet_stable_balance = excluded.sell_wallet_stable_balance,
            sell_wallet_cngn_balance = excluded.sell_wallet_cngn_balance,
            buy_tx_hash = excluded.buy_tx_hash,
            sell_tx_hash = excluded.sell_tx_hash
        """,
        (
            event.opportunity_id,
            event.event_type,
            event.timestamp,
            event.status,
            event.reason,
            float(event.optimal_size_usd) if event.optimal_size_usd is not None else None,
            float(event.routed_size_usd) if event.routed_size_usd is not None else None,
            float(event.executed_size_usd) if event.executed_size_usd is not None else None,
            float(event.expected_profit_usd) if event.expected_profit_usd is not None else None,
            float(event.actual_profit_usd) if event.actual_profit_usd is not None else None,
            float(event.net_profit_usd) if event.net_profit_usd is not None else None,
            event.net_spread_bps,
            event.buy_wallet.stable_symbol if event.buy_wallet else None,
            float(event.buy_wallet.stable_balance) if event.buy_wallet and event.buy_wallet.stable_balance is not None else None,
            float(event.buy_wallet.cngn_balance) if event.buy_wallet and event.buy_wallet.cngn_balance is not None else None,
            event.sell_wallet.stable_symbol if event.sell_wallet else None,
            float(event.sell_wallet.stable_balance) if event.sell_wallet and event.sell_wallet.stable_balance is not None else None,
            float(event.sell_wallet.cngn_balance) if event.sell_wallet and event.sell_wallet.cngn_balance is not None else None,
            event.buy_tx_hash,
            event.sell_tx_hash,
        ),
    )
    await conn.commit()


async def get_arbitrage_history(
    conn: aiosqlite.Connection,
    pipeline: str | None = None,
    from_ts: int | None = None,
    to_ts: int | None = None,
    limit: int = 50,
) -> list[ArbitrageHistoryItem]:
    id_filters = ["e.event_type IN ('routed', 'executed', 'failed')"]
    having_clauses: list[str] = []
    id_params: list[Any] = []
    if pipeline is not None:
        id_filters.append("a.pipeline = ?")
        id_params.append(pipeline)
    if to_ts is not None:
        id_filters.append("e.timestamp_ms <= ?")
        id_params.append(to_ts)
    if from_ts is not None:
        having_clauses.append("MAX(e.timestamp_ms) >= ?")
        id_params.append(from_ts)
    id_params.append(limit)
    id_cursor = await conn.execute(
        f"""
        WITH matched_attempts AS (
            SELECT
                e.attempt_id,
                MAX(e.timestamp_ms) AS latest_ts
            FROM arb_history_events e
            JOIN arb_attempts a ON a.id = e.attempt_id
            WHERE {' AND '.join(id_filters)}
            GROUP BY e.attempt_id
            {'HAVING ' + ' AND '.join(having_clauses) if having_clauses else ''}
            ORDER BY latest_ts DESC
            LIMIT ?
        )
        SELECT attempt_id
        FROM matched_attempts
        ORDER BY latest_ts DESC
        """,
        id_params,
    )
    attempt_ids = [row["attempt_id"] for row in await id_cursor.fetchall()]
    if not attempt_ids:
        return []

    placeholders = ", ".join("?" for _ in attempt_ids)
    detail_params: list[Any] = list(attempt_ids)
    detail_filters = [
        f"e.attempt_id IN ({placeholders})",
        "e.event_type IN ('routed', 'executed', 'failed')",
    ]
    if to_ts is not None:
        detail_filters.append("e.timestamp_ms <= ?")
        detail_params.append(to_ts)
    cursor = await conn.execute(
        f"""
        SELECT e.*, a.pipeline, a.direction, a.buy_venue, a.sell_venue
        FROM arb_history_events e
        JOIN arb_attempts a ON a.id = e.attempt_id
        WHERE {' AND '.join(detail_filters)}
        ORDER BY e.timestamp_ms ASC, e.id ASC
        """,
        detail_params,
    )
    rows = await cursor.fetchall()
    grouped: dict[str, list[ArbitrageHistoryEvent]] = {}
    for row in rows:
        event = _event_from_joined_row(row)
        grouped.setdefault(event.opportunity_id, []).append(event)

    items: list[ArbitrageHistoryItem] = []
    for opp_id in attempt_ids:
        if opp_id not in grouped:
            continue
        events = sorted(grouped[opp_id], key=lambda item: ((item.timestamp or 0), item.id or 0))
        routed = next((event for event in events if event.event_type == "routed"), events[0])
        latest = events[-1]
        final = next((event for event in reversed(events) if event.event_type in {"executed", "failed"}), latest)
        items.append(
            ArbitrageHistoryItem(
                opportunity_id=opp_id,
                pipeline=latest.pipeline,
                direction=latest.direction,
                buy_venue=latest.buy_venue,
                sell_venue=latest.sell_venue,
                latest_status=latest.status,
                latest_event_type=latest.event_type,
                routed_at=routed.timestamp,
                updated_at=latest.timestamp,
                optimal_size_usd=routed.optimal_size_usd,
                routed_size_usd=routed.routed_size_usd,
                executed_size_usd=final.executed_size_usd,
                expected_profit_usd=routed.expected_profit_usd,
                actual_profit_usd=final.actual_profit_usd,
                net_profit_usd=routed.net_profit_usd,
                net_spread_bps=routed.net_spread_bps,
                reason=latest.reason,
                buy_wallet=routed.buy_wallet,
                sell_wallet=routed.sell_wallet,
                buy_tx_hash=latest.buy_tx_hash,
                sell_tx_hash=latest.sell_tx_hash,
            )
        )
    return items
