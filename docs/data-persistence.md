  # Data Persistence

 Historical data persists across crashes. SQLite is a file-based database, so all data survives restarts and crashes. When starting the engine:

  1. The existing .db file is opened (not recreated)
  2. Schema migrations use CREATE TABLE IF NOT EXISTS - won't destroy existing tables
  3. All previous price snapshots, positions, alerts, and arbitrage data are still there

  ## What You'll See After Downtime

  [Historical Data] ----gap---- [New Data]
     (preserved)     (missing)   (starts on restart)

  - Before the gap: All price data from previous runs is intact and queryable
  - During the gap: No data points exist (engine wasn't running to capture them)
  - After restart: New snapshots are inserted every 30s (per price_update_interval)

  ## No Automatic Pruning

  Currently there's no TTL or retention policy. Data accumulates indefinitely. This means:
  - The dashboard can query weeks/months back if data exists
  - The DB file will grow over time
  - You may eventually want to add a cleanup job for old data

  ## API Access

  The API supports time-range queries via get_price_snapshots_in_window(from_ts, to_ts), so the dashboard can request:
  - Last 1 hour
  - Last 24 hours
  - Any arbitrary time window

  If you request 24 hours but only have 30 minutes of data, you'll get only those 30 minutes of points.

  ## Recommendation

  If you want full historical continuity for the live server:
  1. Consider a backup strategy for ./data/cngn.db
  2. Add a pruning job if you don't need data older than N days
  3. Monitor DB file size - SQLite handles hundreds of MB fine, but GB+ can slow queries