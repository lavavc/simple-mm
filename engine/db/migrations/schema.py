"""Fresh schema bootstrap for the trading engine DB."""

from __future__ import annotations

import aiosqlite


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS venue_config (
    venue TEXT PRIMARY KEY,
    params_json TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    dedupe_key TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    first_seen_at_ms INTEGER NOT NULL,
    last_seen_at_ms INTEGER NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_alerts_last_seen ON alerts(last_seen_at_ms DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open_dedupe
ON alerts(dedupe_key)
WHERE dedupe_key IS NOT NULL AND status = 'open';

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    venue TEXT NOT NULL,
    action_type TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    status TEXT NOT NULL,
    direction TEXT,
    amount_in REAL,
    token_in TEXT,
    amount_out REAL,
    token_out TEXT,
    price REAL,
    tx_hash TEXT,
    error TEXT,
    metadata_json TEXT,
    idempotency_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_time ON actions(timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS idx_actions_venue ON actions(venue, timestamp_ms DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_idempotency
ON actions(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    bid REAL NOT NULL,
    ask REAL NOT NULL,
    mid REAL NOT NULL,
    metadata_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_price_source_timestamp
ON price_snapshots(source, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_price_timestamp ON price_snapshots(timestamp_ms);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    pair TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    balances_json TEXT NOT NULL,
    lp_position_json TEXT,
    open_orders_json TEXT,
    position_value_usd REAL,
    volume_24h_usd REAL,
    rates_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_unique
ON position_snapshots(venue, pair, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_position_venue_time
ON position_snapshots(venue, timestamp_ms);

CREATE TABLE IF NOT EXISTS arb_attempts (
    id TEXT PRIMARY KEY,
    pipeline TEXT NOT NULL,
    direction TEXT NOT NULL,
    buy_venue TEXT NOT NULL,
    sell_venue TEXT NOT NULL,
    detected_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    signal_price_buy REAL,
    signal_price_sell REAL,
    gross_spread_bps INTEGER,
    net_spread_bps INTEGER,
    expected_profit_usd REAL,
    actual_profit_usd REAL,
    optimal_size_usd REAL,
    routed_size_usd REAL,
    executed_size_usd REAL,
    buy_amount_cngn REAL,
    cngn_transferred REAL,
    expected_usd_out REAL,
    uni_bsc_price REAL,
    uni_base_price REAL,
    slippage_tolerance_bps INTEGER,
    uni_bsc_fee_bps INTEGER,
    uni_base_fee_bps INTEGER,
    gas_usd REAL,
    buy_tx_hash TEXT,
    sell_tx_hash TEXT,
    engine_key TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_arb_engine_key
ON arb_attempts(engine_key)
WHERE engine_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_arb_status ON arb_attempts(status);
CREATE INDEX IF NOT EXISTS idx_arb_pipeline_time ON arb_attempts(pipeline, detected_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_arb_pipeline_status_time
ON arb_attempts(pipeline, status, detected_at_ms DESC);

CREATE TABLE IF NOT EXISTS arb_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    leg_role TEXT NOT NULL,
    venue TEXT NOT NULL,
    asset_symbol TEXT,
    amount REAL,
    price REAL,
    tx_hash TEXT,
    status TEXT NOT NULL,
    error TEXT,
    timestamp_ms INTEGER NOT NULL,
    FOREIGN KEY (attempt_id) REFERENCES arb_attempts(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_arb_leg_unique
ON arb_legs(attempt_id, leg_role);

CREATE TABLE IF NOT EXISTS arb_history_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    optimal_size_usd REAL,
    routed_size_usd REAL,
    executed_size_usd REAL,
    expected_profit_usd REAL,
    actual_profit_usd REAL,
    net_profit_usd REAL,
    net_spread_bps INTEGER,
    buy_wallet_stable_symbol TEXT,
    buy_wallet_stable_balance REAL,
    buy_wallet_cngn_balance REAL,
    sell_wallet_stable_symbol TEXT,
    sell_wallet_stable_balance REAL,
    sell_wallet_cngn_balance REAL,
    buy_tx_hash TEXT,
    sell_tx_hash TEXT,
    FOREIGN KEY (attempt_id) REFERENCES arb_attempts(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_arb_history_unique
ON arb_history_events(attempt_id, event_type);
CREATE INDEX IF NOT EXISTS idx_arb_history_time ON arb_history_events(timestamp_ms DESC);
"""


async def bootstrap_schema(conn: aiosqlite.Connection) -> None:
    """Create the fresh schema if it does not already exist."""
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
