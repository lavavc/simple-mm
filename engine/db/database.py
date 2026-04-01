"""Async SQLite database management."""

import aiosqlite
import json
import time
from pathlib import Path
from typing import Optional, Any
from decimal import Decimal

from engine.api.schemas import (
    Alert,
    ArbitrageHistoryEvent,
    ArbitrageHistoryItem,
    ArbitrageHistoryWalletSnapshot,
    ArbitrageOpportunity,
    ArbitrageTrade,
    DexArbOpportunity,
    Position,
    PriceQuote,
)


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Connect to database and initialize schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._init_schema()

    async def close(self):
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _init_schema(self):
        """Initialize database schema."""
        await self._conn.executescript(
            """
            -- System state
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            -- Price history
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                source TEXT NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                mid REAL NOT NULL,
                metadata TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_price_time ON price_snapshots(timestamp);
            CREATE INDEX IF NOT EXISTS idx_price_source_time ON price_snapshots(source, timestamp);

            -- Venue positions
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue TEXT NOT NULL,
                pair TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                cngn_balance REAL NOT NULL,
                usdt_balance REAL NOT NULL,
                usdc_balance REAL DEFAULT 0,
                lp_token_id TEXT,
                lp_liquidity TEXT,
                range_min REAL,
                range_max REAL,
                in_range INTEGER,
                open_buy_orders INTEGER,
                open_sell_orders INTEGER,
                position_value_usd REAL,
                volume_24h_usd REAL
            );
            CREATE INDEX IF NOT EXISTS idx_position_venue_time ON positions(venue, timestamp);

            -- Action log
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                venue TEXT NOT NULL,
                action_type TEXT NOT NULL,
                direction TEXT,
                amount_in REAL,
                token_in TEXT,
                amount_out REAL,
                token_out TEXT,
                price REAL,
                tx_hash TEXT,
                status TEXT NOT NULL,
                error TEXT,
                triggered_by TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_actions_time ON actions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_actions_venue ON actions(venue);

            -- Venue configuration
            CREATE TABLE IF NOT EXISTS venue_config (
                venue TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                params TEXT NOT NULL
            );

            -- Alerts
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                acknowledged INTEGER DEFAULT 0
            );

            -- Arbitrage opportunities
            CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
                id TEXT PRIMARY KEY,
                timestamp INTEGER NOT NULL,
                buy_venue TEXT NOT NULL,
                sell_venue TEXT NOT NULL,
                buy_price REAL NOT NULL,
                sell_price REAL NOT NULL,
                gross_spread_bps INTEGER NOT NULL,
                net_spread_bps INTEGER NOT NULL,
                recommended_size_usd REAL NOT NULL,
                expected_profit_usd REAL NOT NULL,
                status TEXT NOT NULL,
                actual_profit_usd REAL,
                reason TEXT,
                buy_amount_cngn REAL,
                buy_tx_hash TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_arb_opp_time ON arbitrage_opportunities(timestamp);
            CREATE INDEX IF NOT EXISTS idx_arb_opp_status ON arbitrage_opportunities(status);

            -- Arbitrage trades (individual legs)
            CREATE TABLE IF NOT EXISTS arbitrage_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                side TEXT NOT NULL,
                amount REAL NOT NULL,
                price REAL,
                tx_hash TEXT,
                status TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                error TEXT,
                FOREIGN KEY (opportunity_id) REFERENCES arbitrage_opportunities(id)
            );
            CREATE INDEX IF NOT EXISTS idx_arb_trades_opp ON arbitrage_trades(opportunity_id);
            CREATE INDEX IF NOT EXISTS idx_arb_trades_time ON arbitrage_trades(timestamp);

            -- DEX Arbitrage opportunities
            CREATE TABLE IF NOT EXISTS dex_arbitrage_opportunities (
                id TEXT PRIMARY KEY,
                timestamp INTEGER NOT NULL,
                direction TEXT NOT NULL,
                optimal_size_usd REAL NOT NULL,
                expected_profit_usd REAL NOT NULL,
                cngn_transferred REAL NOT NULL,
                expected_usd_out REAL NOT NULL,
                status TEXT NOT NULL,
                net_spread_bps INTEGER NOT NULL,
            actual_profit_usd REAL,
                reason TEXT,
                uni_bsc_price REAL,
                uni_base_price REAL,
                buy_tx_hash TEXT,
                sell_tx_hash TEXT,
                slippage_tolerance_bps INTEGER,
                uni_bsc_fee_bps INTEGER,
                uni_base_fee_bps INTEGER,
                gas_usd REAL,
                buy_amount_cngn REAL,
                executed_size_usd REAL
            );
            CREATE INDEX IF NOT EXISTS idx_dex_arb_opp_time ON dex_arbitrage_opportunities(timestamp);
            CREATE INDEX IF NOT EXISTS idx_dex_arb_opp_status ON dex_arbitrage_opportunities(status);

            -- Arbitrage lifecycle history
            CREATE TABLE IF NOT EXISTS arbitrage_history_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id TEXT NOT NULL,
                pipeline TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                direction TEXT NOT NULL,
                buy_venue TEXT NOT NULL,
                sell_venue TEXT NOT NULL,
                status TEXT NOT NULL,
                optimal_size_usd REAL,
                routed_size_usd REAL,
                executed_size_usd REAL,
                expected_profit_usd REAL,
                actual_profit_usd REAL,
                net_profit_usd REAL,
                net_spread_bps INTEGER,
                reason TEXT,
                buy_wallet_stable_symbol TEXT,
                buy_wallet_stable_balance REAL,
                buy_wallet_cngn_balance REAL,
                sell_wallet_stable_symbol TEXT,
                sell_wallet_stable_balance REAL,
                sell_wallet_cngn_balance REAL,
                buy_tx_hash TEXT,
                sell_tx_hash TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_arb_history_opp ON arbitrage_history_events(opportunity_id);
            CREATE INDEX IF NOT EXISTS idx_arb_history_time ON arbitrage_history_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_arb_history_pipeline ON arbitrage_history_events(pipeline);
            """
        )
        await self._conn.commit()
        await self._migrate_schema()

    async def _migrate_schema(self):
        """Run incremental schema migrations."""
        cursor = await self._conn.execute("PRAGMA table_info(arbitrage_opportunities)")
        arb_cols = {row[1] for row in await cursor.fetchall()}
        if "buy_amount_cngn" not in arb_cols:
            await self._conn.execute(
                "ALTER TABLE arbitrage_opportunities ADD COLUMN buy_amount_cngn REAL"
            )
        if "buy_tx_hash" not in arb_cols:
            await self._conn.execute(
                "ALTER TABLE arbitrage_opportunities ADD COLUMN buy_tx_hash TEXT"
            )

        cursor = await self._conn.execute("PRAGMA table_info(dex_arbitrage_opportunities)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "pancake_price" in cols:
            await self._conn.execute(
                "ALTER TABLE dex_arbitrage_opportunities RENAME COLUMN pancake_price TO uni_bsc_price"
            )
        if "aerodrome_price" in cols:
            await self._conn.execute(
                "ALTER TABLE dex_arbitrage_opportunities RENAME COLUMN aerodrome_price TO uni_base_price"
            )
        if "pancake_fee_bps" in cols:
            await self._conn.execute(
                "ALTER TABLE dex_arbitrage_opportunities RENAME COLUMN pancake_fee_bps TO uni_bsc_fee_bps"
            )
        if "aerodrome_fee_bps" in cols:
            await self._conn.execute(
                "ALTER TABLE dex_arbitrage_opportunities RENAME COLUMN aerodrome_fee_bps TO uni_base_fee_bps"
            )
        if "estimated_gas_usd" in cols:
            await self._conn.execute(
                "ALTER TABLE dex_arbitrage_opportunities RENAME COLUMN estimated_gas_usd TO gas_usd"
            )
        if "buy_amount_cngn" not in cols:
            await self._conn.execute(
                "ALTER TABLE dex_arbitrage_opportunities ADD COLUMN buy_amount_cngn REAL"
            )
        if "executed_size_usd" not in cols:
            await self._conn.execute(
                "ALTER TABLE dex_arbitrage_opportunities ADD COLUMN executed_size_usd REAL"
            )

        cursor = await self._conn.execute("PRAGMA table_info(arbitrage_history_events)")
        history_cols = {row[1] for row in await cursor.fetchall()}
        history_additions = {
            "optimal_size_usd": "ALTER TABLE arbitrage_history_events ADD COLUMN optimal_size_usd REAL",
            "routed_size_usd": "ALTER TABLE arbitrage_history_events ADD COLUMN routed_size_usd REAL",
            "executed_size_usd": "ALTER TABLE arbitrage_history_events ADD COLUMN executed_size_usd REAL",
            "expected_profit_usd": "ALTER TABLE arbitrage_history_events ADD COLUMN expected_profit_usd REAL",
            "actual_profit_usd": "ALTER TABLE arbitrage_history_events ADD COLUMN actual_profit_usd REAL",
            "net_profit_usd": "ALTER TABLE arbitrage_history_events ADD COLUMN net_profit_usd REAL",
            "net_spread_bps": "ALTER TABLE arbitrage_history_events ADD COLUMN net_spread_bps INTEGER",
            "reason": "ALTER TABLE arbitrage_history_events ADD COLUMN reason TEXT",
            "buy_wallet_stable_symbol": "ALTER TABLE arbitrage_history_events ADD COLUMN buy_wallet_stable_symbol TEXT",
            "buy_wallet_stable_balance": "ALTER TABLE arbitrage_history_events ADD COLUMN buy_wallet_stable_balance REAL",
            "buy_wallet_cngn_balance": "ALTER TABLE arbitrage_history_events ADD COLUMN buy_wallet_cngn_balance REAL",
            "sell_wallet_stable_symbol": "ALTER TABLE arbitrage_history_events ADD COLUMN sell_wallet_stable_symbol TEXT",
            "sell_wallet_stable_balance": "ALTER TABLE arbitrage_history_events ADD COLUMN sell_wallet_stable_balance REAL",
            "sell_wallet_cngn_balance": "ALTER TABLE arbitrage_history_events ADD COLUMN sell_wallet_cngn_balance REAL",
            "buy_tx_hash": "ALTER TABLE arbitrage_history_events ADD COLUMN buy_tx_hash TEXT",
            "sell_tx_hash": "ALTER TABLE arbitrage_history_events ADD COLUMN sell_tx_hash TEXT",
        }
        for col, ddl in history_additions.items():
            if col not in history_cols:
                await self._conn.execute(ddl)
        cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_arb_history_opp_event'"
        )
        if not await cursor.fetchone():
            await self._conn.execute(
                """
                DELETE FROM arbitrage_history_events
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM arbitrage_history_events
                    GROUP BY opportunity_id, event_type
                )
                """
            )
            await self._conn.execute(
                """
                CREATE UNIQUE INDEX idx_arb_history_opp_event
                ON arbitrage_history_events(opportunity_id, event_type)
                """
            )

        cursor = await self._conn.execute("PRAGMA table_info(positions)")
        pos_cols = {row[1] for row in await cursor.fetchall()}
        if "pool_tvl_usd" in pos_cols:
            await self._conn.execute(
                "ALTER TABLE positions RENAME COLUMN pool_tvl_usd TO position_value_usd"
            )
        await self._conn.commit()

    # === System State ===

    async def get_system_state(self, key: str) -> Optional[str]:
        """Get system state value."""
        cursor = await self._conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_system_state(self, key: str, value: str):
        """Set system state value."""
        await self._conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
            """,
            (key, value, int(time.time() * 1000), value, int(time.time() * 1000)),
        )
        await self._conn.commit()

    # === Price Snapshots ===

    async def insert_price_snapshot(self, quote: PriceQuote, metadata: Optional[dict] = None):
        """Insert a price snapshot."""
        await self._conn.execute(
            """
            INSERT INTO price_snapshots (timestamp, source, bid, ask, mid, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                quote.timestamp,
                quote.source,
                float(quote.bid),
                float(quote.ask),
                float(quote.mid),
                json.dumps(metadata) if metadata else None,
            ),
        )
        await self._conn.commit()

    async def get_recent_prices(self, limit: int = 100) -> list[Decimal]:
        """Get recent mid prices for SD calculation."""
        cursor = await self._conn.execute(
            "SELECT mid FROM price_snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [Decimal(str(row["mid"])) for row in reversed(rows)]

    async def get_price_history(
        self,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get price history."""
        query = "SELECT * FROM price_snapshots WHERE 1=1"
        params: list[Any] = []

        if from_ts:
            query += " AND timestamp >= ?"
            params.append(from_ts)
        if to_ts:
            query += " AND timestamp <= ?"
            params.append(to_ts)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_price_snapshots_in_window(
        self,
        from_ts: int,
        to_ts: int,
        source: Optional[str] = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Get price snapshots within a time window for TWAP computation.

        Returns rows sorted by timestamp ascending (oldest first) so that
        time-weighting can be applied sequentially.

        Args:
            from_ts: Start timestamp in milliseconds.
            to_ts: End timestamp in milliseconds.
            source: Optional source/venue name filter (partial match).
            limit: Maximum rows to return.

        Returns:
            List of dicts with keys: timestamp, source, bid, ask, mid.
        """
        query = "SELECT timestamp, source, bid, ask, mid FROM price_snapshots WHERE timestamp >= ? AND timestamp <= ?"
        params: list[Any] = [from_ts, to_ts]

        if source:
            query += " AND source LIKE ?"
            params.append(f"%{source}%")

        query += " ORDER BY timestamp ASC LIMIT ?"
        params.append(limit)

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # === Positions ===

    async def insert_position(self, position: Position):
        """Insert a position snapshot."""
        lp = position.lp_position
        orders = position.open_orders

        await self._conn.execute(
            """
            INSERT INTO positions (
                venue, pair, timestamp, cngn_balance, usdt_balance, usdc_balance,
                lp_token_id, lp_liquidity, range_min, range_max, in_range,
                open_buy_orders, open_sell_orders, position_value_usd, volume_24h_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position.venue,
                position.pair,
                position.timestamp,
                float(position.balances.get("cngn", 0)),
                float(position.balances.get("usdt", 0)),
                float(position.balances.get("usdc", 0)),
                lp.token_id if lp else None,
                lp.liquidity if lp else None,
                float(lp.range_min) if lp else None,
                float(lp.range_max) if lp else None,
                1 if lp and lp.in_range else 0 if lp else None,
                orders.get("buy_count") if orders else None,
                orders.get("sell_count") if orders else None,
                float(position.position_value_usd) if position.position_value_usd is not None else None,
                float(position.volume_24h_usd) if position.volume_24h_usd is not None else None,
            ),
        )
        await self._conn.commit()

    async def get_pool_metrics_history(self, venues: list[str], from_ts: int) -> list[dict]:
        """Return pool TVL and volume snapshots for given venues since from_ts."""
        placeholders = ",".join("?" * len(venues))
        cursor = await self._conn.execute(
            f"SELECT timestamp, venue, position_value_usd, volume_24h_usd FROM positions "
            f"WHERE venue IN ({placeholders}) AND timestamp >= ? AND position_value_usd IS NOT NULL "
            f"ORDER BY timestamp ASC",
            (*venues, from_ts),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # === Actions ===

    async def insert_action(
        self,
        venue: str,
        action_type: str,
        triggered_by: str,
        status: str,
        direction: Optional[str] = None,
        amount_in: Optional[float] = None,
        token_in: Optional[str] = None,
        amount_out: Optional[float] = None,
        token_out: Optional[str] = None,
        price: Optional[float] = None,
        tx_hash: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """Insert an action log entry."""
        await self._conn.execute(
            """
            INSERT INTO actions (
                timestamp, venue, action_type, direction, amount_in, token_in,
                amount_out, token_out, price, tx_hash, status, error, triggered_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                venue,
                action_type,
                direction,
                amount_in,
                token_in,
                amount_out,
                token_out,
                price,
                tx_hash,
                status,
                error,
                triggered_by,
            ),
        )
        await self._conn.commit()

    async def get_actions(
        self,
        venue: Optional[str] = None,
        action_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Get recent actions."""
        query = "SELECT * FROM actions WHERE 1=1"
        params: list[Any] = []

        if venue:
            query += " AND venue = ?"
            params.append(venue)
        if action_type:
            query += " AND action_type = ?"
            params.append(action_type)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # === Venue Config ===

    async def get_venue_config(self, venue: str) -> Optional[dict]:
        """Get venue configuration."""
        cursor = await self._conn.execute(
            "SELECT * FROM venue_config WHERE venue = ?", (venue,)
        )
        row = await cursor.fetchone()
        if row:
            return {
                "venue": row["venue"],
                "enabled": bool(row["enabled"]),
                "params": json.loads(row["params"]),
            }
        return None

    async def update_venue_config(self, venue: str, params: dict):
        """Update venue configuration."""
        await self._conn.execute(
            """
            INSERT INTO venue_config (venue, enabled, params)
            VALUES (?, 1, ?)
            ON CONFLICT(venue) DO UPDATE SET params = ?
            """,
            (venue, json.dumps(params), json.dumps(params)),
        )
        await self._conn.commit()

    # === Alerts ===

    async def insert_alert(
        self, severity: str, category: str, message: str, dedup: bool = False
    ) -> int:
        """Insert an alert and return its ID. If dedup=True, skip if an identical unacknowledged alert exists."""
        if dedup:
            cursor = await self._conn.execute(
                "SELECT id FROM alerts WHERE category=? AND message=? AND acknowledged=0 LIMIT 1",
                (category, message),
            )
            if await cursor.fetchone():
                return 0
        cursor = await self._conn.execute(
            """
            INSERT INTO alerts (timestamp, severity, category, message)
            VALUES (?, ?, ?, ?)
            """,
            (int(time.time() * 1000), severity, category, message),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_alerts(self, limit: int = 20) -> list[Alert]:
        """Get recent alerts."""
        cursor = await self._conn.execute(
            "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [
            Alert(
                id=row["id"],
                timestamp=row["timestamp"],
                severity=row["severity"],
                category=row["category"],
                message=row["message"],
                acknowledged=bool(row["acknowledged"]),
            )
            for row in rows
        ]

    async def acknowledge_alert(self, alert_id: int):
        """Mark an alert as acknowledged."""
        await self._conn.execute(
            "UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,)
        )
        await self._conn.commit()

    # === Arbitrage Opportunities ===

    async def insert_arbitrage_opportunity(self, opp: ArbitrageOpportunity):
        """Insert a detected arbitrage opportunity."""
        await self._conn.execute(
            """
            INSERT INTO arbitrage_opportunities (
                id, timestamp, buy_venue, sell_venue, buy_price, sell_price,
                gross_spread_bps, net_spread_bps, recommended_size_usd,
                expected_profit_usd, status, actual_profit_usd, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opp.id,
                opp.timestamp,
                opp.buy_venue,
                opp.sell_venue,
                float(opp.buy_price),
                float(opp.sell_price),
                opp.gross_spread_bps,
                opp.net_spread_bps,
                float(opp.recommended_size_usd),
                float(opp.expected_profit_usd),
                opp.status,
                float(opp.actual_profit_usd) if opp.actual_profit_usd else None,
                opp.reason,
            ),
        )
        await self._conn.commit()

    async def update_arbitrage_opportunity(
        self,
        opp_id: str,
        status: str,
        actual_profit_usd: Optional[float] = None,
        reason: Optional[str] = None,
        buy_amount_cngn: Optional[float] = None,
        buy_tx_hash: Optional[str] = None,
    ):
        """Update an arbitrage opportunity status."""
        sets = ["status = ?", "reason = ?"]
        vals: list[Any] = [status, reason]
        if actual_profit_usd is not None:
            sets.append("actual_profit_usd = ?")
            vals.append(actual_profit_usd)
        if buy_amount_cngn is not None:
            sets.append("buy_amount_cngn = ?")
            vals.append(buy_amount_cngn)
        if buy_tx_hash is not None:
            sets.append("buy_tx_hash = ?")
            vals.append(buy_tx_hash)
        vals.append(opp_id)
        await self._conn.execute(
            f"UPDATE arbitrage_opportunities SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        await self._conn.commit()

    async def get_arbitrage_opportunities(
        self,
        status: Optional[str] = None,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        limit: int = 50,
    ) -> list[ArbitrageOpportunity]:
        """Get arbitrage opportunities with optional filters."""
        query = "SELECT * FROM arbitrage_opportunities WHERE 1=1"
        params: list[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status)
        if from_ts:
            query += " AND timestamp >= ?"
            params.append(from_ts)
        if to_ts:
            query += " AND timestamp <= ?"
            params.append(to_ts)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()

        return [
            ArbitrageOpportunity(
                id=row["id"],
                timestamp=row["timestamp"],
                buy_venue=row["buy_venue"],
                sell_venue=row["sell_venue"],
                buy_price=Decimal(str(row["buy_price"])),
                sell_price=Decimal(str(row["sell_price"])),
                gross_spread_bps=row["gross_spread_bps"],
                net_spread_bps=row["net_spread_bps"],
                recommended_size_usd=Decimal(str(row["recommended_size_usd"])),
                expected_profit_usd=Decimal(str(row["expected_profit_usd"])),
                status=row["status"],
                actual_profit_usd=Decimal(str(row["actual_profit_usd"]))
                if row["actual_profit_usd"]
                else None,
                reason=row["reason"],
                buy_amount_cngn=Decimal(str(row["buy_amount_cngn"]))
                if row["buy_amount_cngn"] is not None
                else None,
                buy_tx_hash=row["buy_tx_hash"],
            )
            for row in rows
        ]

    async def get_arbitrage_opportunity(self, opp_id: str) -> Optional[ArbitrageOpportunity]:
        """Fetch a single CEX-DEX arbitrage opportunity by id."""
        cursor = await self._conn.execute(
            "SELECT * FROM arbitrage_opportunities WHERE id = ?", (opp_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ArbitrageOpportunity(
            id=row["id"],
            timestamp=row["timestamp"],
            buy_venue=row["buy_venue"],
            sell_venue=row["sell_venue"],
            buy_price=Decimal(str(row["buy_price"])),
            sell_price=Decimal(str(row["sell_price"])),
            gross_spread_bps=row["gross_spread_bps"],
            net_spread_bps=row["net_spread_bps"],
            recommended_size_usd=Decimal(str(row["recommended_size_usd"])),
            expected_profit_usd=Decimal(str(row["expected_profit_usd"])),
            status=row["status"],
            actual_profit_usd=Decimal(str(row["actual_profit_usd"])) if row["actual_profit_usd"] else None,
            reason=row["reason"],
            buy_amount_cngn=Decimal(str(row["buy_amount_cngn"])) if row["buy_amount_cngn"] is not None else None,
            buy_tx_hash=row["buy_tx_hash"],
        )

    async def insert_dex_arbitrage_opportunity(self, opp: DexArbOpportunity):
        """Insert a detected DEX arbitrage opportunity."""
        await self._conn.execute(
            """
            INSERT INTO dex_arbitrage_opportunities (
                id, timestamp, direction, optimal_size_usd, expected_profit_usd,
                cngn_transferred, expected_usd_out, status, net_spread_bps,
                actual_profit_usd, reason, uni_bsc_price, uni_base_price,
                buy_tx_hash, sell_tx_hash, slippage_tolerance_bps, uni_bsc_fee_bps,
                uni_base_fee_bps, gas_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opp.id,
                opp.timestamp,
                opp.direction,
                float(opp.optimal_size_usd),
                float(opp.expected_profit_usd),
                float(opp.cngn_transferred),
                float(opp.expected_usd_out),
                opp.status,
                opp.net_spread_bps,
                float(opp.actual_profit_usd) if opp.actual_profit_usd is not None else None,
                opp.reason,
                float(opp.uni_bsc_price) if opp.uni_bsc_price is not None else None,
                float(opp.uni_base_price) if opp.uni_base_price is not None else None,
                opp.buy_tx_hash,
                opp.sell_tx_hash,
                opp.slippage_tolerance_bps,
                opp.uni_bsc_fee_bps,
                opp.uni_base_fee_bps,
                float(opp.gas_usd) if opp.gas_usd is not None else None,
            ),
        )
        await self._conn.commit()

    async def update_dex_arbitrage_opportunity(
        self,
        opp_id: str,
        status: str,
        actual_profit_usd: Optional[float] = None,
        reason: Optional[str] = None,
    ):
        """Update a DEX arbitrage opportunity."""
        if actual_profit_usd is not None:
            await self._conn.execute(
                """
                UPDATE dex_arbitrage_opportunities
                SET status = ?, actual_profit_usd = ?, reason = ?
                WHERE id = ?
                """,
                (status, actual_profit_usd, reason, opp_id),
            )
        else:
            await self._conn.execute(
                """
                UPDATE dex_arbitrage_opportunities
                SET status = ?, reason = ?
                WHERE id = ?
                """,
                (status, reason, opp_id),
            )
        await self._conn.commit()

    async def update_dex_arbitrage_execution_state(
        self,
        opp_id: str,
        *,
        status: str,
        buy_tx_hash: Optional[str] = None,
        sell_tx_hash: Optional[str] = None,
        reason: Optional[str] = None,
        buy_amount_cngn: Optional[Decimal] = None,
        executed_size_usd: Optional[float] = None,
        actual_profit_usd: Optional[float] = None,
    ):
        """Update DEX arbitrage execution status and tx hashes."""
        updates = ["status = ?"]
        params: list[Any] = [status]

        if buy_tx_hash is not None:
            updates.append("buy_tx_hash = ?")
            params.append(buy_tx_hash)
        if sell_tx_hash is not None:
            updates.append("sell_tx_hash = ?")
            params.append(sell_tx_hash)
        if reason is not None:
            updates.append("reason = ?")
            params.append(reason)
        if buy_amount_cngn is not None:
            updates.append("buy_amount_cngn = ?")
            params.append(float(buy_amount_cngn))
        if executed_size_usd is not None:
            updates.append("executed_size_usd = ?")
            params.append(executed_size_usd)
        if actual_profit_usd is not None:
            updates.append("actual_profit_usd = ?")
            params.append(actual_profit_usd)

        params.append(opp_id)
        await self._conn.execute(
            f"UPDATE dex_arbitrage_opportunities SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await self._conn.commit()

    async def expire_old_dex_arbitrage_opportunities(self, cutoff_ts: int):
        """Mark opportunities older than cutoff and still detected/executing as expired."""
        await self._conn.execute(
            """
            UPDATE dex_arbitrage_opportunities
            SET status = 'expired', reason = 'Timeout'
            WHERE status IN ('detected', 'executing') AND timestamp < ?
            """,
            (cutoff_ts,),
        )
        await self._conn.commit()

    async def get_dex_arbitrage_opportunities(
        self,
        status: Optional[str] = None,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        limit: int = 50,
    ) -> list[DexArbOpportunity]:
        """Get DEX arbitrage opportunities with optional filters."""
        query = "SELECT * FROM dex_arbitrage_opportunities WHERE 1=1"
        params: list[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status)
        if from_ts:
            query += " AND timestamp >= ?"
            params.append(from_ts)
        if to_ts:
            query += " AND timestamp <= ?"
            params.append(to_ts)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()

        return [
            DexArbOpportunity(
                id=row["id"],
                timestamp=row["timestamp"],
                direction=row["direction"],
                optimal_size_usd=Decimal(str(row["optimal_size_usd"])),
                expected_profit_usd=Decimal(str(row["expected_profit_usd"])),
                cngn_transferred=Decimal(str(row["cngn_transferred"])),
                expected_usd_out=Decimal(str(row["expected_usd_out"])),
                status=row["status"],
                net_spread_bps=row["net_spread_bps"],
                actual_profit_usd=Decimal(str(row["actual_profit_usd"])) if row["actual_profit_usd"] else None,
                reason=row["reason"],
                uni_bsc_price=Decimal(str(row["uni_bsc_price"])) if row["uni_bsc_price"] is not None else None,
                uni_base_price=Decimal(str(row["uni_base_price"])) if row["uni_base_price"] is not None else None,
                buy_tx_hash=row["buy_tx_hash"],
                sell_tx_hash=row["sell_tx_hash"],
                slippage_tolerance_bps=dict(row).get("slippage_tolerance_bps"),
                uni_bsc_fee_bps=dict(row).get("uni_bsc_fee_bps"),
                uni_base_fee_bps=dict(row).get("uni_base_fee_bps"),
                gas_usd=Decimal(str(dict(row).get("gas_usd"))) if dict(row).get("gas_usd") is not None else None,
                buy_amount_cngn=Decimal(str(dict(row).get("buy_amount_cngn"))) if dict(row).get("buy_amount_cngn") is not None else None,
                executed_size_usd=Decimal(str(dict(row).get("executed_size_usd"))) if dict(row).get("executed_size_usd") is not None else None,
            )
            for row in rows
        ]

    async def get_dex_arbitrage_opportunity(self, opp_id: str) -> Optional[DexArbOpportunity]:
        """Get a single DEX arbitrage opportunity by id."""
        cursor = await self._conn.execute(
            "SELECT * FROM dex_arbitrage_opportunities WHERE id = ?",
            (opp_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        return DexArbOpportunity(
            id=row["id"],
            timestamp=row["timestamp"],
            direction=row["direction"],
            optimal_size_usd=Decimal(str(row["optimal_size_usd"])),
            expected_profit_usd=Decimal(str(row["expected_profit_usd"])),
            cngn_transferred=Decimal(str(row["cngn_transferred"])),
            expected_usd_out=Decimal(str(row["expected_usd_out"])),
            status=row["status"],
            net_spread_bps=row["net_spread_bps"],
            actual_profit_usd=Decimal(str(row["actual_profit_usd"])) if row["actual_profit_usd"] else None,
            reason=row["reason"],
            uni_bsc_price=Decimal(str(row["uni_bsc_price"])) if row["uni_bsc_price"] is not None else None,
            uni_base_price=Decimal(str(row["uni_base_price"])) if row["uni_base_price"] is not None else None,
            buy_tx_hash=row["buy_tx_hash"],
            sell_tx_hash=row["sell_tx_hash"],
            slippage_tolerance_bps=dict(row).get("slippage_tolerance_bps"),
            uni_bsc_fee_bps=dict(row).get("uni_bsc_fee_bps"),
            uni_base_fee_bps=dict(row).get("uni_base_fee_bps"),
            gas_usd=Decimal(str(dict(row).get("gas_usd"))) if dict(row).get("gas_usd") is not None else None,
            buy_amount_cngn=Decimal(str(dict(row).get("buy_amount_cngn"))) if dict(row).get("buy_amount_cngn") is not None else None,
        )

    async def upsert_arbitrage_history_event(self, event: ArbitrageHistoryEvent):
        """Insert or refresh a lifecycle event for an arbitrage attempt."""
        await self._conn.execute(
            """
            INSERT INTO arbitrage_history_events (
                opportunity_id, pipeline, event_type, timestamp, direction,
                buy_venue, sell_venue, status, optimal_size_usd, routed_size_usd,
                executed_size_usd, expected_profit_usd, actual_profit_usd, net_profit_usd,
                net_spread_bps, reason, buy_wallet_stable_symbol,
                buy_wallet_stable_balance, buy_wallet_cngn_balance, sell_wallet_stable_symbol,
                sell_wallet_stable_balance, sell_wallet_cngn_balance, buy_tx_hash, sell_tx_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(opportunity_id, event_type) DO UPDATE SET
                timestamp = excluded.timestamp,
                direction = excluded.direction,
                buy_venue = excluded.buy_venue,
                sell_venue = excluded.sell_venue,
                status = excluded.status,
                optimal_size_usd = excluded.optimal_size_usd,
                routed_size_usd = excluded.routed_size_usd,
                executed_size_usd = excluded.executed_size_usd,
                expected_profit_usd = excluded.expected_profit_usd,
                actual_profit_usd = excluded.actual_profit_usd,
                net_profit_usd = excluded.net_profit_usd,
                net_spread_bps = excluded.net_spread_bps,
                reason = excluded.reason,
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
                event.pipeline,
                event.event_type,
                event.timestamp,
                event.direction,
                event.buy_venue,
                event.sell_venue,
                event.status,
                float(event.optimal_size_usd) if event.optimal_size_usd is not None else None,
                float(event.routed_size_usd) if event.routed_size_usd is not None else None,
                float(event.executed_size_usd) if event.executed_size_usd is not None else None,
                float(event.expected_profit_usd) if event.expected_profit_usd is not None else None,
                float(event.actual_profit_usd) if event.actual_profit_usd is not None else None,
                float(event.net_profit_usd) if event.net_profit_usd is not None else None,
                event.net_spread_bps,
                event.reason,
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
        await self._conn.commit()

    def _history_event_from_row(self, row: aiosqlite.Row) -> ArbitrageHistoryEvent:
        buy_wallet = None
        if (
            row["buy_wallet_stable_symbol"] is not None
            or row["buy_wallet_stable_balance"] is not None
            or row["buy_wallet_cngn_balance"] is not None
        ):
            buy_wallet = ArbitrageHistoryWalletSnapshot(
                stable_symbol=row["buy_wallet_stable_symbol"],
                stable_balance=Decimal(str(row["buy_wallet_stable_balance"])) if row["buy_wallet_stable_balance"] is not None else None,
                cngn_balance=Decimal(str(row["buy_wallet_cngn_balance"])) if row["buy_wallet_cngn_balance"] is not None else None,
            )

        sell_wallet = None
        if (
            row["sell_wallet_stable_symbol"] is not None
            or row["sell_wallet_stable_balance"] is not None
            or row["sell_wallet_cngn_balance"] is not None
        ):
            sell_wallet = ArbitrageHistoryWalletSnapshot(
                stable_symbol=row["sell_wallet_stable_symbol"],
                stable_balance=Decimal(str(row["sell_wallet_stable_balance"])) if row["sell_wallet_stable_balance"] is not None else None,
                cngn_balance=Decimal(str(row["sell_wallet_cngn_balance"])) if row["sell_wallet_cngn_balance"] is not None else None,
            )

        return ArbitrageHistoryEvent(
            id=row["id"],
            opportunity_id=row["opportunity_id"],
            pipeline=row["pipeline"],
            event_type=row["event_type"],
            timestamp=row["timestamp"],
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
            buy_wallet=buy_wallet,
            sell_wallet=sell_wallet,
            buy_tx_hash=row["buy_tx_hash"],
            sell_tx_hash=row["sell_tx_hash"],
        )

    async def get_arbitrage_history(
        self,
        pipeline: Optional[str] = None,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        limit: int = 50,
    ) -> list[ArbitrageHistoryItem]:
        """Return grouped lifecycle history for routed arbitrage attempts."""
        filters = ["event_type IN ('routed', 'executed', 'failed')"]
        params: list[Any] = []
        if pipeline:
            filters.append("pipeline = ?")
            params.append(pipeline)
        if from_ts:
            filters.append("timestamp >= ?")
            params.append(from_ts)
        if to_ts:
            filters.append("timestamp <= ?")
            params.append(to_ts)
        where_sql = " AND ".join(filters)

        latest_query = (
            "SELECT opportunity_id, MAX(timestamp) AS latest_ts "
            "FROM arbitrage_history_events "
            f"WHERE {where_sql} "
            "GROUP BY opportunity_id "
            "ORDER BY latest_ts DESC "
            "LIMIT ?"
        )
        cursor = await self._conn.execute(latest_query, [*params, limit])
        latest_rows = await cursor.fetchall()
        if not latest_rows:
            return []

        opp_ids = [row["opportunity_id"] for row in latest_rows]
        ordering = {row["opportunity_id"]: idx for idx, row in enumerate(latest_rows)}
        placeholders = ", ".join("?" for _ in opp_ids)
        event_filters = [
            f"opportunity_id IN ({placeholders})",
            "event_type IN ('routed', 'executed', 'failed')",
        ]
        event_params: list[Any] = [*opp_ids]
        if pipeline:
            event_filters.append("pipeline = ?")
            event_params.append(pipeline)
        # Keep the original routed snapshot even if it predates from_ts, but do not
        # leak later events beyond the requested upper bound.
        if to_ts:
            event_filters.append("timestamp <= ?")
            event_params.append(to_ts)
        cursor = await self._conn.execute(
            f"""
            SELECT *
            FROM arbitrage_history_events
            WHERE {' AND '.join(event_filters)}
            ORDER BY timestamp ASC, id ASC
            """,
            event_params,
        )
        rows = await cursor.fetchall()

        grouped: dict[str, list[ArbitrageHistoryEvent]] = {}
        for row in rows:
            event = self._history_event_from_row(row)
            grouped.setdefault(event.opportunity_id, []).append(event)

        items: list[ArbitrageHistoryItem] = []
        for opp_id in sorted(grouped, key=lambda key: ordering[key]):
            events = grouped[opp_id]
            routed = next((event for event in events if event.event_type == "routed"), events[0])
            latest = events[-1]
            final = next((event for event in reversed(events) if event.event_type in {"executed", "failed"}), None)
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
                    executed_size_usd=final.executed_size_usd if final else None,
                    expected_profit_usd=routed.expected_profit_usd,
                    actual_profit_usd=final.actual_profit_usd if final else None,
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

    async def get_arbitrage_stats(
        self,
        from_ts: int,
    ) -> dict:
        """Get aggregated arbitrage statistics since timestamp."""
        cursor = await self._conn.execute(
            """
            SELECT
                COUNT(*) as total_detected,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as total_executed,
                SUM(CASE WHEN status = 'completed' THEN actual_profit_usd ELSE 0 END) as total_profit,
                SUM(size_usd) as total_volume
            FROM (
                SELECT status, actual_profit_usd, recommended_size_usd as size_usd
                FROM arbitrage_opportunities WHERE timestamp >= ?
                UNION ALL
                SELECT status, actual_profit_usd, optimal_size_usd as size_usd
                FROM dex_arbitrage_opportunities WHERE timestamp >= ?
            )
            """,
            (from_ts, from_ts),
        )
        row = await cursor.fetchone()

        return {
            "opportunities_detected": row["total_detected"] or 0,
            "opportunities_executed": row["total_executed"] or 0,
            "total_profit_usd": Decimal(str(row["total_profit"] or 0)),
            "total_volume_usd": Decimal(str(row["total_volume"] or 0)),
        }

    # === Arbitrage Trades ===

    async def insert_arbitrage_trade(self, trade: ArbitrageTrade) -> int:
        """Insert an arbitrage trade leg."""
        cursor = await self._conn.execute(
            """
            INSERT INTO arbitrage_trades (
                opportunity_id, venue, side, amount, price, tx_hash,
                status, timestamp, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.opportunity_id,
                trade.venue,
                trade.side,
                float(trade.amount),
                float(trade.price) if trade.price else None,
                trade.tx_hash,
                trade.status,
                trade.timestamp,
                trade.error,
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def update_arbitrage_trade(
        self,
        trade_id: int,
        status: str,
        price: Optional[float] = None,
        tx_hash: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """Update an arbitrage trade status."""
        updates = ["status = ?"]
        params: list[Any] = [status]

        if price is not None:
            updates.append("price = ?")
            params.append(price)
        if tx_hash is not None:
            updates.append("tx_hash = ?")
            params.append(tx_hash)
        if error is not None:
            updates.append("error = ?")
            params.append(error)

        params.append(trade_id)

        await self._conn.execute(
            f"UPDATE arbitrage_trades SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await self._conn.commit()

    async def get_arbitrage_trades(
        self,
        opportunity_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[ArbitrageTrade]:
        """Get arbitrage trades for an opportunity."""
        query = "SELECT * FROM arbitrage_trades WHERE 1=1"
        params: list[Any] = []

        if opportunity_id:
            query += " AND opportunity_id = ?"
            params.append(opportunity_id)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()

        return [
            ArbitrageTrade(
                id=row["id"],
                opportunity_id=row["opportunity_id"],
                venue=row["venue"],
                side=row["side"],
                amount=Decimal(str(row["amount"])),
                price=Decimal(str(row["price"])) if row["price"] else None,
                tx_hash=row["tx_hash"],
                status=row["status"],
                timestamp=row["timestamp"],
                error=row["error"],
            )
            for row in rows
        ]


# Global database instance
_db: Optional[Database] = None


async def get_db() -> Database:
    """Get the global database instance."""
    global _db
    if _db is None:
        from engine.config import settings

        _db = Database(settings.db_path)
        await _db.connect()
    return _db
