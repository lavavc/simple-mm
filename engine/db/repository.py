"""Top-level DB repository facade."""

from __future__ import annotations

from engine.api.schemas import ArbitrageHistoryEvent, ArbitrageOpportunity, ArbitrageTrade, DexArbOpportunity, Position, PriceQuote

from .connection import SQLiteConnectionManager
from .migrations import bootstrap_schema
from .queries import actions, alerts, arbitrage, history, pool_metrics, positions, prices, system_state, venue_config


class DatabaseRepository:
    """Application-facing facade over the SQLite trading DB."""

    def __init__(self, connection_manager: SQLiteConnectionManager):
        self.connection_manager = connection_manager

    @property
    def _conn(self):
        return self.connection_manager.connection

    async def connect(self) -> "DatabaseRepository":
        conn = await self.connection_manager.connect()
        await bootstrap_schema(conn)
        return self

    async def close(self) -> None:
        await self.connection_manager.close()

    async def get_system_state(self, key: str):
        return await system_state.get_system_state(self._conn, key)

    async def set_system_state(self, key: str, value):
        await system_state.set_system_state(self._conn, key, value)

    async def insert_price_snapshot(self, quote: PriceQuote, metadata: dict | None = None):
        await prices.insert_price_snapshot(self._conn, quote, metadata)

    async def get_recent_prices(self, limit: int = 100):
        return await prices.get_recent_prices(self._conn, limit)

    async def get_price_history(self, from_ts: int | None = None, to_ts: int | None = None, limit: int = 100):
        return await prices.get_price_history(self._conn, from_ts, to_ts, limit)

    async def get_price_snapshots_in_window(
        self,
        from_ts: int,
        to_ts: int,
        source: str | None = None,
        limit: int = 5000,
    ):
        return await prices.get_price_snapshots_in_window(self._conn, from_ts, to_ts, source, limit)

    async def insert_position(self, position: Position):
        await positions.insert_position(self._conn, position)

    async def get_pool_metrics_history(self, venues: list[str], from_ts: int):
        return await pool_metrics.get_pool_metrics_history(self._conn, venues, from_ts)

    async def insert_action(self, **kwargs):
        return await actions.insert_action(self._conn, **kwargs)

    async def get_actions(self, venue: str | None = None, action_type: str | None = None, limit: int = 50):
        return await actions.get_actions(self._conn, venue, action_type, limit)

    async def get_venue_config(self, venue: str):
        return await venue_config.get_venue_config(self._conn, venue)

    async def update_venue_config(self, venue: str, params: dict):
        await venue_config.update_venue_config(self._conn, venue, params)

    async def insert_alert(self, **kwargs):
        return await alerts.insert_alert(self._conn, **kwargs)

    async def get_alerts(self, limit: int = 20):
        return await alerts.get_alerts(self._conn, limit)

    async def acknowledge_alert(self, alert_id: int):
        await alerts.acknowledge_alert(self._conn, alert_id)

    async def insert_arbitrage_opportunity(self, opp: ArbitrageOpportunity):
        await arbitrage.upsert_cex_attempt(self._conn, opp)

    async def update_arbitrage_opportunity(self, opp_id: str, **kwargs):
        await arbitrage.update_cex_attempt(self._conn, opp_id, **kwargs)

    async def get_arbitrage_opportunities(self, status: str | None = None, from_ts: int | None = None, to_ts: int | None = None, limit: int = 50):
        return await arbitrage.get_cex_attempts(self._conn, status, from_ts, to_ts, limit)

    async def get_arbitrage_opportunity(self, opp_id: str):
        return await arbitrage.get_cex_attempt(self._conn, opp_id)

    async def insert_dex_arbitrage_opportunity(self, opp: DexArbOpportunity):
        await arbitrage.upsert_dex_attempt(self._conn, opp)

    async def update_dex_arbitrage_opportunity(self, opp_id: str, status: str, actual_profit_usd=None, reason=None):
        await arbitrage.update_dex_attempt(
            self._conn,
            opp_id,
            status=status,
            actual_profit_usd=actual_profit_usd,
            reason=reason,
        )

    async def update_dex_arbitrage_execution_state(self, opp_id: str, **kwargs):
        await arbitrage.update_dex_attempt(self._conn, opp_id, **kwargs)

    async def expire_old_dex_arbitrage_opportunities(self, cutoff_ts: int):
        await arbitrage.expire_old_dex_attempts(self._conn, cutoff_ts)

    async def get_dex_arbitrage_opportunities(self, status: str | None = None, from_ts: int | None = None, to_ts: int | None = None, limit: int = 50):
        return await arbitrage.get_dex_attempts(self._conn, status, from_ts, to_ts, limit)

    async def get_dex_arbitrage_opportunity(self, opp_id: str):
        return await arbitrage.get_dex_attempt(self._conn, opp_id)

    async def get_active_dex_opportunity(self, direction: str):
        return await arbitrage.get_active_dex_attempt(self._conn, direction)

    async def upsert_arbitrage_history_event(self, event: ArbitrageHistoryEvent):
        await history.upsert_arbitrage_history_event(self._conn, event)

    async def get_arbitrage_history(self, pipeline: str | None = None, from_ts: int | None = None, to_ts: int | None = None, limit: int = 50):
        return await history.get_arbitrage_history(self._conn, pipeline, from_ts, to_ts, limit)

    async def get_arbitrage_stats(self, from_ts: int):
        return await arbitrage.get_arbitrage_stats(self._conn, from_ts)

    async def insert_arbitrage_trade(self, trade: ArbitrageTrade):
        return await arbitrage.upsert_leg(
            self._conn,
            attempt_id=trade.opportunity_id,
            leg_role=trade.side,
            venue=trade.venue,
            amount=trade.amount,
            status=trade.status,
            timestamp_ms=trade.timestamp,
            price=trade.price,
            tx_hash=trade.tx_hash,
            error=trade.error,
        )

    async def update_arbitrage_trade(
        self,
        trade_id: int,
        status: str,
        price: float | None = None,
        tx_hash: str | None = None,
        error: str | None = None,
    ):
        cursor = await self._conn.execute(
            "SELECT attempt_id, leg_role, venue, amount, asset_symbol, timestamp_ms FROM arb_legs WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        await arbitrage.upsert_leg(
            self._conn,
            attempt_id=row["attempt_id"],
            leg_role=row["leg_role"],
            venue=row["venue"],
            amount=row["amount"],
            status=status,
            timestamp_ms=row["timestamp_ms"],
            asset_symbol=row["asset_symbol"],
            price=price,
            tx_hash=tx_hash,
            error=error,
        )

    async def get_arbitrage_trades(self, opportunity_id: str | None = None, limit: int = 50):
        return await arbitrage.get_legs(self._conn, opportunity_id, limit)


async def open_repository(db_path: str) -> DatabaseRepository:
    """Open and initialize a repository for the given DB path."""
    repo = DatabaseRepository(SQLiteConnectionManager(db_path))
    await repo.connect()
    return repo
