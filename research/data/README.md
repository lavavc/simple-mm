# Research Data

Research datasets, derived feature tables, and quality inputs belong under this directory.

The runtime SQLite database remains at `data/cngn.db` because the engine and dashboard use that
path as local application state. Exported or derived research artifacts should be written here or
under `research/results/` instead of adding new top-level data folders.

This directory is ignored by default. Check in only small fixtures under `research/data/fixtures/`,
small summarized reports under `research/data/reports/`, and README files that document larger
local artifacts.

## Lead/Lag Study Datasets (local, regenerable)

All are gitignored; regenerate from source as follows.

| File | Contents | Regenerate |
|---|---|---|
| `pool_swaps.json` | Every V4 Swap event in the Base and BSC cNGN pools since 2026-04-08, with block timestamps and post-swap NGN/USD price | `python research/scripts/fetch_pool_swaps.py` (needs RPC access; ~20 min) |
| `tx_senders_cache.json` (at repo `data/`) | Sender address for every swap tx above | `python research/scripts/build_tx_senders_cache.py` |
| `prices.csv.gz` | Engine quote history, all venues (`source,timestamp_ms,mid`) | On the production host: `sqlite3 data/cngn.db "SELECT source||','||timestamp_ms||','||mid FROM price_snapshots;" \| gzip > prices.csv.gz` |
| `arbs_full.json.gz` | Completed arb attempts with tx hashes and P&L | On the production host: `sqlite3 -json data/cngn.db "SELECT id, direction, status, reason, detected_at_ms, buy_tx_hash, sell_tx_hash, executed_size_usd, actual_profit_usd, net_spread_bps FROM arb_attempts WHERE status='completed';" \| gzip > arbs_full.json.gz` |
