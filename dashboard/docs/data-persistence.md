---
title: Data Persistence
order: 6
---

Historical data persists across crashes. SQLite is a file-based database, so all data survives restarts and crashes. When starting the engine:

1. The existing .db file is opened (not recreated)
2. `engine/db/migrations/schema.py` bootstraps the current schema with `CREATE TABLE IF NOT EXISTS`
3. Domain stores are constructed on top of that connection by `engine/db/repository.py`
4. All previous price snapshots, positions, alerts, and arbitrage data are still there as long as the schema is compatible with the running code

## Current DB shape

The DB layer is intentionally split into a thin container plus focused stores.

- `DatabaseRepository` owns connection lifecycle and schema bootstrap
- `SystemStateStore` persists system switches like trading pause/resume
- `PriceStore` persists `price_snapshots`
- `PositionStore` persists `position_snapshots`
- `ActionStore` persists operator and engine actions
- `AlertStore` persists deduplicated alerts
- `VenueConfigStore` persists live per-venue params
- `ArbitrageStore` persists arbitrage attempts and legs
- `HistoryStore` persists lifecycle events for arbitrage attempts
- `PoolMetricsStore` serves historical pool metrics queries

This split matters operationally too: most subsystems now depend on narrow store protocols from `engine/db/backend.py` rather than on a catch-all repository surface.

For LP specifically, the `actions` table is the canonical audit surface. Ratio-prep swaps, mints, removals, manual withdraws, and shutdown unwinds are stored there with structured metadata so the LP package does not need a second bespoke audit trail.

## Downtime

[Historical Data] ----gap---- [New Data]
   (preserved)     (missing)   (starts on restart)

- Before the gap: All price data from previous runs is intact and queryable
- During the gap: No data points exist (engine wasn't running to capture them)
- After restart: New snapshots are inserted every 30s (per price_update_interval)

## Arbitrage persistence model

Arbitrage data is intentionally normalized into three related tables:

- `arb_attempts` stores one row per opportunity/attempt with route metadata, signal fields, execution state, and summary profitability fields
- `arb_legs` stores individual buy/sell legs for an attempt
- `arb_history_events` stores lifecycle events such as `routed`, `executed`, and `failed`

History queries join these layers so the dashboard can render both the latest attempt state and the event timeline without duplicating route metadata in every event row.

## No Automatic Pruning

Currently there's no TTL or retention policy. Data accumulates indefinitely. This means:
- The dashboard can query weeks/months back if data exists
- The DB file will grow over time
- We may eventually need to add a cleanup job for old data

## API Access

The API and dashboard read from the store/query layer, not from raw SQL inside route handlers. For example, price history queries use `get_price_snapshots_in_window(from_ts, to_ts)`, so the dashboard can request:
- Last 1 hour
- Last 24 hours
- Any arbitrary time window

If you request 24 hours but only have 30 minutes of data, you'll get only those 30 minutes of points.

## TODO

For full historical continuity:
1. Consider a backup strategy for ./data/cngn.db
2. Add a pruning job if you don't need data older than N days
3. Monitor DB file size - SQLite handles hundreds of MB fine, but GB+ can slow queries
