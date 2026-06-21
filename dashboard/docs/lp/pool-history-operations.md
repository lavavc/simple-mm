---
title: Pool History Operations
order: 5
---

# Pool History Data Operations

The backtester datasets are updated by `scripts/update_v4_pool_history.py`.
The updater runs Base and BSC sequentially, resumes after the last durable
checkpoint, retries transient RPC failures, and validates each CSV before
moving to the next pool.

The active research data model is broader than the raw pool CSVs. See
`autoresearch/data-methodology-refactor.md` for the planned DEX pool snapshot
bridge, paper-faithful LP lifecycle ledger, event-time price reconstruction,
and derived episode features.

## Manual update

Update both pools to each chain's latest block:

```bash
python scripts/update_v4_pool_history.py
```

For a reproducible historical cutoff, pass explicit chain end blocks:

```bash
python scripts/update_v4_pool_history.py \
  --base-end-block 47130126 \
  --bsc-end-block 103315324
```

Checkpoints are stored in `data/checkpoints/`. The updater lock in the same
directory prevents manual and scheduled runs from overlapping. Do not delete a
checkpoint unless the corresponding CSV is also being rebuilt deliberately.

## Daily LaunchAgent

Install the plist with an explicit local time. This example uses 03:15:

```bash
python scripts/install_pool_history_launchd.py --hour 3 --minute 15
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.cngn.pool-history-update.plist" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.cngn.pool-history-update.plist"
```

The LaunchAgent uses the repository as its working directory, so the existing
`.env` supplies RPC configuration. Output is written to:

- `logs/pool-history-update.stdout.log`
- `logs/pool-history-update.stderr.log`

Run it immediately without changing the schedule:

```bash
launchctl kickstart -k "gui/$(id -u)/com.cngn.pool-history-update"
```

Unload it with:

```bash
launchctl bootout "gui/$(id -u)/com.cngn.pool-history-update"
```

If a day is missed, the next run catches up from the checkpoint rather than
limiting itself to a calendar-day slice.

## Research Methodology Guardrails

Pool CSV updates must remain append-only and auditable. Derived research data
should be built from the CSVs or a sidecar ledger rather than replacing raw
rows in place.

Before serious LP or Fair Value runs, verify:

- each pool has expected token order and cNGN/USD inversion
- `sqrt_price_x96` is the canonical marginal pool price source
- raw CSV `cngn_usd_price` is classified as `sqrt_mid`, `swap_amount_ratio`, or `unexplained`
- `unexplained` stored-price rows are zero before derived imports or backtests
- swap rows have canonical signed cNGN flow fields
- `price_snapshots` contains `uni-base_pool` and `uni-bsc_pool` rows for DEX premium features
- liquidity-operation `sqrt_price_x96`, `tick`, and `cngn_usd_price` are reconstructed by event order, not block-end state
- causal cone feature tables report as-of source ages and missingness
- calendar stress slices are available alongside swap-count walk-forward windows

### Non-Destructive Research Runbook

Do not run these commands against a CSV while an updater is appending to it.
Use completed snapshots or make fresh snapshots first:

```bash
mkdir -p data/snapshots data/quality data/derived

cp data/uni_base_pool_history.csv \
  data/snapshots/uni_base_pool_history_pre_replay_refactor.csv
cp data/uni_bsc_pool_history.csv \
  data/snapshots/uni_bsc_pool_history_pre_replay_refactor.csv
```

Run pool quality reports on the snapshots:

```bash
python3 scripts/report_pool_history_quality.py \
  --csv data/snapshots/uni_base_pool_history_pre_replay_refactor.csv \
  --pool uni-base \
  --out data/quality/uni_base_pool_history_pre_replay_refactor.md

python3 scripts/report_pool_history_quality.py \
  --csv data/snapshots/uni_bsc_pool_history_pre_replay_refactor.csv \
  --pool uni-bsc \
  --out data/quality/uni_bsc_pool_history_pre_replay_refactor.md
```

Build replay-corrected pool histories from the snapshots:

```bash
python3 scripts/replay_pool_history_prices.py \
  --input data/snapshots/uni_base_pool_history_pre_replay_refactor.csv \
  --output data/derived/uni_base_pool_history_replay.csv \
  --pool uni-base

python3 scripts/replay_pool_history_prices.py \
  --input data/snapshots/uni_bsc_pool_history_pre_replay_refactor.csv \
  --output data/derived/uni_bsc_pool_history_replay.csv \
  --pool uni-bsc
```

Import DEX pool context into `price_snapshots` and build causal pool feature
tables:

```bash
python3 scripts/import_pool_snapshots.py \
  --db data/cngn.db \
  --csv data/derived/uni_base_pool_history_replay.csv \
  --pool uni-base

python3 scripts/import_pool_snapshots.py \
  --db data/cngn.db \
  --csv data/derived/uni_bsc_pool_history_replay.csv \
  --pool uni-bsc

python3 scripts/build_pool_feature_table.py \
  --pool uni-base \
  --csv data/derived/uni_base_pool_history_replay.csv \
  --db data/cngn.db \
  --out data/derived/uni_base_pool_features.csv

python3 scripts/build_pool_feature_table.py \
  --pool uni-bsc \
  --csv data/derived/uni_bsc_pool_history_replay.csv \
  --db data/cngn.db \
  --out data/derived/uni_bsc_pool_features.csv
```

Export Fair Value markouts with pool features and then run regime-stability
diagnostics:

```bash
python3 scripts/export_fair_price_markouts.py \
  --db data/cngn.db \
  --out data/derived/fair_price_markouts_with_pool_features.csv \
  --pool-feature-csv uni-base=data/derived/uni_base_pool_features.csv \
  --pool-feature-csv uni-bsc=data/derived/uni_bsc_pool_features.csv \
  --quality-out data/quality/fair_price_markouts_with_pool_features.json

python3 scripts/report_backtest_regime_stability.py \
  --windows data/derived/backtest_walk_forward_windows.csv \
  --features data/derived/uni_base_pool_features.csv \
  --out data/quality/backtest_regime_stability.md
```

The LP lifecycle ledger exporter is currently fixture-backed until the full
PositionManager RPC decoder is added. The executable fixture path is:

```bash
python3 scripts/export_v4_lp_ledger.py \
  --pool uni-base \
  --start-block 42926879 \
  --end-block 47130126 \
  --decoded-actions data/derived/uni_base_decoded_actions.json \
  --ownership-events data/derived/uni_base_ownership_events.json \
  --price-events data/derived/uni_base_price_events.json \
  --out data/derived/uni_base_lp_ledger.csv

python3 scripts/export_v4_lp_ledger.py \
  --pool uni-bsc \
  --start-block 84655203 \
  --end-block 103315324 \
  --decoded-actions data/derived/uni_bsc_decoded_actions.json \
  --ownership-events data/derived/uni_bsc_ownership_events.json \
  --price-events data/derived/uni_bsc_price_events.json \
  --out data/derived/uni_bsc_lp_ledger.csv
```

After LP ledgers exist, export gas sidecars:

```bash
python3 scripts/export_tx_receipts.py \
  --chain base \
  --tx-csv data/derived/uni_base_lp_ledger.csv \
  --out data/derived/uni_base_tx_receipts.csv

python3 scripts/export_tx_receipts.py \
  --chain bsc \
  --tx-csv data/derived/uni_bsc_lp_ledger.csv \
  --out data/derived/uni_bsc_tx_receipts.csv
```

The V4 exporter applies event-time price replay after decoding a chunk and
before writing CSV rows. Swap and initialize rows keep their event-native pool
price; mint, burn, and collect rows inherit the most recent prior event-time
price or a prior-block seed. This prevents same-block block-end lookahead from
entering liquidity-operation price fields while preserving the existing CSV
schema.

To rebuild corrected research histories from already completed raw CSVs without
new RPC calls:

```bash
PYTHONPATH=. python3 scripts/replay_pool_history_prices.py \
  --input data/uni_base_pool_history.csv \
  --output data/derived/uni_base_pool_history_replay.csv \
  --pool uni-base

PYTHONPATH=. python3 scripts/replay_pool_history_prices.py \
  --input data/uni_bsc_pool_history.csv \
  --output data/derived/uni_bsc_pool_history_replay.csv \
  --pool uni-bsc
```

Then validate:

```bash
PYTHONPATH=. python3 scripts/report_pool_history_quality.py \
  --csv data/derived/uni_base_pool_history_replay.csv \
  --pool uni-base \
  --out data/quality/uni_base_pool_history_replay.md

PYTHONPATH=. python3 scripts/report_pool_history_quality.py \
  --csv data/derived/uni_bsc_pool_history_replay.csv \
  --pool uni-bsc \
  --out data/quality/uni_bsc_pool_history_replay.md
```

Do not use a full from-genesis RPC export as the default replay rebuild path
until the PositionManager scan is refactored. The current exporter is suitable
for incremental catch-up, but a full rebuild has to scan the global
PositionManager log stream per chunk and then fetch candidate transactions.
