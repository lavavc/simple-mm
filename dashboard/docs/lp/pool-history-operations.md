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

The LP lifecycle ledger exporter retains deterministic fixture and explicit
candidate-list paths. The verified no-candidate command shape below is reserved
for the checkpointed action-first runner; until its remaining transfer, replay,
build, and publication phases are wired, it fails closed and writes no output:

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

Completed verified exports will write an adjacent `*.csv.coverage.json` sidecar
that binds the exact ledger bytes to the requested block range and chain ID.
The action-first contract scans and reconciles target-pool PoolManager
`ModifyLiquidity` actions before freezing relevant token IDs, then attests the
complete PositionManager ERC-721 `Transfer` scan while fetching transaction
bundles only for those IDs. `--candidate-tx-csv` and fixture exports remain
unverified and cannot support a complete market-structure diagnostic. A
target-pool liquidity transaction that the configured PositionManager decoder
cannot represent fails the export. The sidecar records
both endpoint block hashes and timestamps, the observed ledger row range, and
the canonical producer-attested candidate set and digest. Endpoint headers are
captured before candidate and replay reads and must match exactly after those
reads. Each fetched transaction and receipt must also agree on hash, block, and
transaction index before decoding. The CSV and sidecar are staged and validated
together; a failed replacement restores the prior valid pair or leaves no new
pair when the exporter catches the publication error. A nonblocking advisory
lock at `*.csv.publish.lock` serializes cooperating exporters targeting the same
CSV through staging, validation, replacement, rollback, and cleanup; a
concurrent exporter fails before changing either final file. A process
interruption between the two renames can leave a mixed pair; readers reject it
and the exporter refuses to overwrite it. Restore the retained backup named in
any rollback error, or deliberately remove both final files, before retrying.
Regenerate both whenever the analysis cutoff moves.

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
