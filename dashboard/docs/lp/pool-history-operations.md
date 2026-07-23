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
`research/autoresearch/data-methodology-refactor.md` for the planned DEX pool snapshot
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

Checkpoints are stored in `research/data/checkpoints/`. The updater lock in the same
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

Before serious DEX-only LP runs, verify:

- each pool has expected token order and cNGN/USD inversion
- `sqrt_price_x96` is the canonical marginal pool price source
- raw CSV `cngn_usd_price` is classified as `sqrt_mid`, `swap_amount_ratio`, or `unexplained`
- `unexplained` stored-price rows are zero before derived imports or backtests
- swap rows have canonical signed cNGN flow fields
- liquidity-operation `sqrt_price_x96`, `tick`, and `cngn_usd_price` are reconstructed by event order, not block-end state
- causal cone feature tables report as-of source ages and missingness
- calendar stress slices are available alongside swap-count walk-forward windows

For deferred Fair Value or DEX-premium runs, additionally verify that
historical Quidax coverage exists and that `price_snapshots` contains
`uni-base_pool` and `uni-bsc_pool` context rows.

### Non-Destructive Research Runbook

Do not run these commands against a CSV while an updater is appending to it.
Use completed snapshots or make fresh snapshots first:

```bash
mkdir -p research/data/snapshots research/data/quality research/data/derived

cp research/data/uni_base_pool_history.csv \
  research/data/snapshots/uni_base_pool_history_pre_replay_refactor.csv
cp research/data/uni_bsc_pool_history.csv \
  research/data/snapshots/uni_bsc_pool_history_pre_replay_refactor.csv
```

Run pool quality reports on the snapshots:

```bash
python3 research/scripts/report_pool_history_quality.py \
  --csv research/data/snapshots/uni_base_pool_history_pre_replay_refactor.csv \
  --pool uni-base \
  --out research/data/quality/uni_base_pool_history_pre_replay_refactor.md

python3 research/scripts/report_pool_history_quality.py \
  --csv research/data/snapshots/uni_bsc_pool_history_pre_replay_refactor.csv \
  --pool uni-bsc \
  --out research/data/quality/uni_bsc_pool_history_pre_replay_refactor.md
```

Build replay-corrected pool histories from the snapshots:

```bash
python3 research/scripts/replay_pool_history_prices.py \
  --input research/data/snapshots/uni_base_pool_history_pre_replay_refactor.csv \
  --output research/data/derived/uni_base_pool_history_replay.csv \
  --pool uni-base

python3 research/scripts/replay_pool_history_prices.py \
  --input research/data/snapshots/uni_bsc_pool_history_pre_replay_refactor.csv \
  --output research/data/derived/uni_bsc_pool_history_replay.csv \
  --pool uni-bsc
```

Build causal pool feature tables directly from replayed pool history:

```bash
python3 research/scripts/build_pool_feature_table.py \
  --pool uni-base \
  --csv research/data/derived/uni_base_pool_history_replay.csv \
  --db data/cngn.db \
  --out research/data/derived/uni_base_pool_features.csv \
  --cone-lookback-seconds 3600,86400,604800

python3 research/scripts/build_pool_feature_table.py \
  --pool uni-bsc \
  --csv research/data/derived/uni_bsc_pool_history_replay.csv \
  --db data/cngn.db \
  --out research/data/derived/uni_bsc_pool_features.csv \
  --cone-lookback-seconds 3600,86400,604800
```

The `price_snapshots` import and Fair Value markout steps are deferred for
active LP research until historical Quidax data exists. The pool feature tables
remain useful for DEX-only LP tests; Quidax-dependent DEX-premium columns should
be treated as unavailable.

Build calendar stress slices from the same causal feature tables. These reports
are diagnostics for regime tests; they are not a replacement for swap-count
walk-forward selection.

```bash
python3 research/scripts/report_pool_feature_stress_slices.py \
  --features research/data/derived/uni_base_pool_features.csv \
  --out research/data/quality/uni_base_pool_feature_stress_slices.md \
  --fields realized_volatility_cone_pct_1h,active_liquidity_cone_pct_1h,active_liquidity_running_max_share_cone_pct_1h,swap_flow_imbalance_cone_pct_1h,fee_intensity_proxy_cone_pct_1h,volume_cone_pct_1h

python3 research/scripts/report_pool_feature_stress_slices.py \
  --features research/data/derived/uni_bsc_pool_features.csv \
  --out research/data/quality/uni_bsc_pool_feature_stress_slices.md \
  --fields realized_volatility_cone_pct_1h,active_liquidity_cone_pct_1h,active_liquidity_running_max_share_cone_pct_1h,swap_flow_imbalance_cone_pct_1h,fee_intensity_proxy_cone_pct_1h,volume_cone_pct_1h
```

Export Fair Value markouts with pool features and then run regime-stability
diagnostics. This step is parked until historical Quidax coverage exists:

```bash
python3 research/scripts/export_fair_price_markouts.py \
  --db data/cngn.db \
  --out research/data/derived/fair_price_markouts_with_pool_features.csv \
  --pool-feature-csv uni-base=research/data/derived/uni_base_pool_features.csv \
  --pool-feature-csv uni-bsc=research/data/derived/uni_bsc_pool_features.csv \
  --quality-out research/data/quality/fair_price_markouts_with_pool_features.json

python3 research/scripts/report_backtest_regime_stability.py \
  --windows research/data/derived/backtest_walk_forward_windows.csv \
  --features research/data/derived/uni_base_pool_features.csv \
  --out research/data/quality/backtest_regime_stability.md
```

### Resumable verified LP ledger exports

The no-candidate LP lifecycle command is the verified, checkpointed action-first
export. It resumes automatically from the output-derived SQLite checkpoint and
must cover the exact frozen replay range from pool inception. Use `--fresh` only
to replace incompatible or intentionally abandoned operational state; it is
rejected for fixture and explicit-candidate-list modes. Schema-v1 operational
files have no compatibility path and therefore require an explicit `--fresh`.

```bash
python3 research/scripts/export_v4_lp_ledger.py \
  --pool uni-base \
  --start-block 42926879 \
  --end-block 47514853 \
  --out research/data/derived/uni_base_lp_ledger.csv

python3 research/scripts/export_v4_lp_ledger.py \
  --pool uni-bsc \
  --start-block 84655203 \
  --end-block 105135905 \
  --out research/data/derived/uni_bsc_lp_ledger.csv
```

For each output `ledger.csv`, the operational namespace is:

- `ledger.csv.checkpoint.sqlite3`, plus live `-wal` and `-shm` siblings;
- `ledger.csv.progress.json`;
- `ledger.csv.run.lock`; and
- `ledger.csv.publish.lock`.

These files are resumability and observability state, not research evidence.
Only the validated `ledger.csv` plus `ledger.csv.coverage.json` pair is canonical
evidence. Do not copy, cite, or feed a partial checkpoint into the backtest.

Read status without making any RPC request:

```bash
python3 research/scripts/report_lp_ledger_export_status.py \
  research/data/derived/uni_base_lp_ledger.csv \
  research/data/derived/uni_bsc_lp_ledger.csv

python3 research/scripts/report_lp_ledger_export_status.py --json \
  research/data/derived/uni_base_lp_ledger.csv \
  research/data/derived/uni_bsc_lp_ledger.csv
```

The schema-v2 phases are `preflight`, action discovery/fetch/decode, token-set
freeze, full transfer scan, relevant-transfer fetch/decode, replay-input bind,
price replay, build, publish, and succeeded. Status includes the durable action,
token, unfiltered transfer, relevant transfer, ownership, bound-action, and row
counters. The JSON form also preserves the last durable unit, generation,
stable error code, rate, and ETA. A compatible rerun resumes the current phase;
completed chunks and bundles are not fetched again.

SIGINT and SIGTERM record the current attempt as interrupted when cleanup can
run. SIGKILL or a host restart leaves the last durable checkpoint intact; a free
run lock makes status report the stale running attempt as interrupted. A busy
terminal WAL checkpoint leaves the published pair valid and is retried on the
next invocation. A non-busy WAL maintenance error preserves the publication
fact under `checkpoint_maintenance_error`; the same command revalidates the
exact pair and endpoint, retries maintenance, and performs no completed RPC
scan or bundle fetch again.

The action-first contract first scans and reconciles every target-pool
PoolManager `ModifyLiquidity` action. Only then does it freeze the relevant
position token IDs. It subsequently scans every PositionManager ERC-721
`Transfer` log as coverage evidence, while fetching transaction and receipt
bundles only for transfers involving a frozen token ID. The sidecar keeps the
unfiltered transfer-scan count/digest separate from the relevant witness and
eligible-bundle sets, so filtering does not masquerade as complete discovery.

The exporter reuses, rather than rescans, the frozen pool-history replay inputs:

- Base: SHA-256
  `41c3d5b945abdffde32087590c21115914404f496069519c572e393b667f99b9`,
  639921 bytes, 1596 rows, blocks 42926879 through 47514853.
- BSC: SHA-256
  `bf99f9a17ea2ff0048da7c7eec4fa0c8fa3b9e7e5586e6200919b66edaccd1e2`,
  1330201 bytes, 3119 rows, blocks 84655203 through 105135905.

Preflight binds those exact bytes, their parser and price-semantics contracts,
and the start/end block hashes and timestamps. Endpoint headers are recaptured
on every attempt and immediately before publication. A changed endpoint,
replay file, source digest, runtime, range, pool configuration, or output path
is incompatible and fails closed. A target-pool action that the configured
PositionManager decoder cannot represent also fails the run.

Before either final rename, the checkpoint durably records the expected CSV and
sidecar SHA-256 values. Each final rename and rollback is followed by a parent
directory `fsync`. If a process stops between renames, readers reject the mixed
pair; the next compatible invocation rebuilds the expected bytes and installs
only the checkpoint-exact missing or stale counterpart. Both exact files are
strictly revalidated before publication becomes terminal. Files that do not
match an authorized expected half, or a separately valid prior pair, remain
untouched for manual investigation. Retained backup paths named by a rollback
error are recovery evidence and must not be deleted until the prior pair is
restored.

For a bounded live smoke, keep the full frozen range so replay identity remains
valid, interrupt immediately after the first durable action-discovery chunk,
inspect status, and rerun the identical command without `--fresh`. Do not shorten
the requested end block: a one-chunk range cannot bind the frozen replay bytes.

`--candidate-tx-csv` and fixture exports remain explicitly unverified and
non-resumable. They use the same output run lock and write terminal operational
progress on success, failure, or handled interruption, but never create a
schema-v2 SQLite checkpoint and cannot support a complete market-structure
diagnostic. Their terminal error records contain only stable redacted codes.
Regenerate the canonical verified pair whenever the frozen analysis cutoff
deliberately changes.

Use the fixture path for deterministic decoder tests or hand-built fixtures:

```bash
python3 research/scripts/export_v4_lp_ledger.py \
  --pool uni-base \
  --start-block 42926879 \
  --end-block 47130126 \
  --decoded-actions research/data/derived/uni_base_decoded_actions.json \
  --ownership-events research/data/derived/uni_base_ownership_events.json \
  --price-events research/data/derived/uni_base_price_events.json \
  --out research/data/derived/uni_base_lp_ledger.csv

python3 research/scripts/export_v4_lp_ledger.py \
  --pool uni-bsc \
  --start-block 84655203 \
  --end-block 103315324 \
  --decoded-actions research/data/derived/uni_bsc_decoded_actions.json \
  --ownership-events research/data/derived/uni_bsc_ownership_events.json \
  --price-events research/data/derived/uni_bsc_price_events.json \
  --out research/data/derived/uni_bsc_lp_ledger.csv
```

After LP ledgers exist, run attribution QA before exact-PnL reconstruction:

```bash
python3 research/scripts/report_lp_ledger_attribution.py \
  --ledger uni-base=research/data/derived/uni_base_lp_ledger.csv \
  --ledger uni-bsc=research/data/derived/uni_bsc_lp_ledger.csv \
  --out research/data/quality/lp_ledger_attribution.md
```

Attribution QA reads the canonical ledger identity, ordering, owner, attribution,
and event-time price columns through one strict boundary. It fails closed on a
pool-orientation mismatch, a first row after the frozen pool-inception block,
duplicate or noncanonical action order, negative actual token amounts, an
unsupported positive-liquidity attribution status, or no positive tracked
opening liquidity. Each positive-liquidity action counts as a gross addition,
including repeated additions to an existing position.
Valid nonzero 20-byte owners are normalized only in memory; malformed, empty,
and zero addresses remain unknown.

Cross-pool concentration reporting additionally requires both verified coverage
sidecars to extend through the inclusive common replay cutoff. The last LP row
is not a coverage watermark: a quiet tail may contain no ledger action even when
the RPC scan is complete. Missing, stale, candidate-list, fixture, or short-range
sidecars fail closed.

Then rebuild paper LP episodes from exact-attributed ledgers:

```bash
python3 research/scripts/export_lp_paper_episodes.py \
  --ledger research/data/derived/uni_base_lp_ledger.csv \
  --out research/data/derived/uni_base_paper_episodes.csv

python3 research/scripts/export_lp_paper_episodes.py \
  --ledger research/data/derived/uni_bsc_lp_ledger.csv \
  --out research/data/derived/uni_bsc_paper_episodes.csv
```

Run close-side attribution QA after rebuilding episodes:

```bash
python3 research/scripts/report_lp_paper_episode_attribution.py \
  --ledger uni-base=research/data/derived/uni_base_lp_ledger.csv \
  --ledger uni-bsc=research/data/derived/uni_bsc_lp_ledger.csv \
  --out research/data/quality/lp_paper_episode_attribution.md
```

Paper episode reconstruction opens lots only from exact-attributed mint/increase
rows. Burn or burn_collect rows close those lots FIFO by pool, owner, and tick
range. Collect proceeds are included in realized closing capital when they appear
on the close row, on a zero-delta collect row in the same transaction as the
close, or on an interim zero-delta collect row while the exact lot is still
open. Collect rows without a matching exact open lot are ignored for exact-PnL
studies rather than being inferred onto an unknown basis. Episode exports include
`close_attribution_status` and `close_attribution_source` so reports can separate
direct close-row proceeds (`exact_collect`), same-transaction collect rows
(`same_tx_collect`), interim collect rows (`interim_collect`), mixed-source
closes, zero-proceed closes, and unmatched collect capital.

Close-side receipt attribution resolves the Uniswap V4 periphery
`MSG_SENDER` recipient sentinel (`0x0000000000000000000000000000000000000001`)
to the effective sender and `ADDRESS_THIS`
(`0x0000000000000000000000000000000000000002`) to the PositionManager before
scanning pool-token `Transfer` logs. Only transfers from the PoolManager to that
resolved recipient count. Reversed pair order is accepted, while overlapping
take actions, duplicate matching transfers, and withdrawals without a supported
`TAKE_PAIR` fail closed rather than silently changing or zeroing proceeds.

Treat the `paper_compatible` sample in the close-attribution QA report as the
default realized-PnL sample for paper-style studies. It includes direct,
same-transaction, interim, and mixed collect-attributed closes, and excludes
`zero_collect_close` episodes whose close proceeds remain unresolved. The
`strict` sample includes only direct and same-transaction collect attribution.
The `lower_bound` sample includes zero-collect closes as zero proceeds and is a
stress/sensitivity view, not the default optimization target. The
`zero_collect_excluded` row reports unresolved capital removed from the default
sample.

Paper episode exports use the paper Figure 3 15-type position taxonomy.
Gas-aware episode feature exports also attach LP-level realized terminal PnL,
episode count, and the Appendix A cumulative realized-PnL path win-score over
the observed ledger window for that pool/owner.

After LP ledgers exist, export gas sidecars:

```bash
python3 research/scripts/export_tx_receipts.py \
  --chain base \
  --tx-csv research/data/derived/uni_base_lp_ledger.csv \
  --out research/data/derived/uni_base_tx_receipts.csv

python3 research/scripts/export_tx_receipts.py \
  --chain bsc \
  --tx-csv research/data/derived/uni_bsc_lp_ledger.csv \
  --out research/data/derived/uni_bsc_tx_receipts.csv
```

Then join paper episodes to receipt sidecars for gas-aware episode features:

Build or refresh the local native gas-token USD sidecar first when net-of-gas
USD accounting is required. This is the only internet-dependent step in the
episode-feature flow; once `native_token_usd_prices.csv` exists, the episode
exports below can run offline.

```bash
python3 research/scripts/build_native_token_price_sidecar.py \
  --ledger base=research/data/derived/uni_base_lp_ledger.csv \
  --ledger bsc=research/data/derived/uni_bsc_lp_ledger.csv \
  --padding-hours 24 \
  --out research/data/derived/native_token_usd_prices.csv
```

```bash
python3 research/scripts/export_lp_episode_features.py \
  --ledger research/data/derived/uni_base_lp_ledger.csv \
  --receipts research/data/derived/uni_base_tx_receipts.csv \
  --out research/data/derived/uni_base_lp_episode_features.csv

python3 research/scripts/export_lp_episode_features.py \
  --ledger research/data/derived/uni_bsc_lp_ledger.csv \
  --receipts research/data/derived/uni_bsc_tx_receipts.csv \
  --out research/data/derived/uni_bsc_lp_episode_features.csv
```

`export_lp_episode_features.py` always reports native-token gas fees in wei and
native units. Pass `--native-token-usd <price>` only for a controlled run with
one explicit price. For historical net-of-gas USD accounting, pass a local
previous-or-equal price sidecar instead:

```bash
python3 research/scripts/export_lp_episode_features.py \
  --ledger research/data/derived/uni_base_lp_ledger.csv \
  --receipts research/data/derived/uni_base_tx_receipts.csv \
  --native-price-csv research/data/derived/native_token_usd_prices.csv \
  --native-price-max-age-ms 3600000 \
  --out research/data/derived/uni_base_lp_episode_features.csv

python3 research/scripts/export_lp_episode_features.py \
  --ledger research/data/derived/uni_bsc_lp_ledger.csv \
  --receipts research/data/derived/uni_bsc_tx_receipts.csv \
  --native-price-csv research/data/derived/native_token_usd_prices.csv \
  --native-price-max-age-ms 3600000 \
  --out research/data/derived/uni_bsc_lp_episode_features.csv
```

The sidecar must contain `chain,timestamp_ms,native_token_usd,source`. The
exporter prices each open/close gas transaction at the latest prior native
price for that chain, records `native_price_max_age_ms`, and fails if the
source is missing or stale. Without either price source, gas USD and net-PnL
USD fields stay blank and `net_pnl_status` records `native_price_missing`.
Episodes with interim collect attribution include allocated interim-collect
transaction gas. If multiple exact open lots are active for the same owner/range
at the collect timestamp, the exporter splits collect gas by remaining active
liquidity share and records
`gas_attribution_status=open_close_with_interim_collect_allocated`.

The V4 exporter applies event-time price replay after decoding a chunk and
before writing CSV rows. Swap and initialize rows keep their event-native pool
price; mint, burn, and collect rows inherit the most recent prior event-time
price or a prior-block seed. This prevents same-block block-end lookahead from
entering liquidity-operation price fields while preserving the existing CSV
schema.

To rebuild corrected research histories from already completed raw CSVs without
new RPC calls:

```bash
PYTHONPATH=. python3 research/scripts/replay_pool_history_prices.py \
  --input research/data/uni_base_pool_history.csv \
  --output research/data/derived/uni_base_pool_history_replay.csv \
  --pool uni-base

PYTHONPATH=. python3 research/scripts/replay_pool_history_prices.py \
  --input research/data/uni_bsc_pool_history.csv \
  --output research/data/derived/uni_bsc_pool_history_replay.csv \
  --pool uni-bsc
```

Then validate:

```bash
PYTHONPATH=. python3 research/scripts/report_pool_history_quality.py \
  --csv research/data/derived/uni_base_pool_history_replay.csv \
  --pool uni-base \
  --out research/data/quality/uni_base_pool_history_replay.md

PYTHONPATH=. python3 research/scripts/report_pool_history_quality.py \
  --csv research/data/derived/uni_bsc_pool_history_replay.csv \
  --pool uni-bsc \
  --out research/data/quality/uni_bsc_pool_history_replay.md
```

The ledger exporter scans target-pool liquidity logs and PositionManager
transfer logs in bounded chunks, then fetches each candidate transaction once.
The full inception-to-cutoff rebuild is intentionally separate from the faster
incremental pool-history replay path.
