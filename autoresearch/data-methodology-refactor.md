# DEX LP and Fair Value Data Methodology Refactor

## Goal

Build a data methodology that can support both:

- DEX LP strategy hypothesis tests using venue-local Uniswap V4 pool history.
- Fair Value markout tests using CEX executable labels with DEX pool context as explanatory features.

The refactor must preserve the architecture boundary: LP research may consume market-layer fair-price outputs, but LP decisions and LP research features must not depend on arb route state or execution internals.

The research framing should separate parameter stress from strategy performance. Grid winners are not the primary object. The primary object is whether a market parameter is stressed, economically meaningful, tradable at our size, stable enough to estimate, and robust after costs.

## Methodology Standard

Every derived dataset must satisfy these rules:

- Use only information available at or before the decision timestamp.
- Keep `uni-base` and `uni-bsc` raw data separate.
- Store raw on-chain facts before derived features.
- Recompute derived features causally from raw facts.
- Treat DEX pool price as a feature or control, never as the Fair Value label.
- Preserve enough identifiers to audit every research row back to chain data.
- Report as-of join age for every CEX, DEX, and pool-data feature.
- Treat opportunity screens separately from full costed backtests.

## Required Data Products

### 1. Raw Pool Event History

The existing pool CSVs remain the append-only source for pool-level events:

- swaps
- initialize events
- liquidity modifications
- position-manager collect payouts where decoded

The raw CSV should continue to capture pool identity, block/log ordering, sqrt price, tick, active liquidity, fee rate, signed token amounts, token symbols, range bounds, and liquidity deltas.

Required refinements:

- Add canonical signed cNGN flow fields for swaps:
  - `cngn_flow_direction`
  - `signed_cngn_amount`
  - `signed_usd_notional`
- Treat `sqrt_price_x96` as canonical for marginal pool price. Do not use raw CSV `cngn_usd_price` as a canonical pool-state field.
- Classify the stored CSV `cngn_usd_price` as diagnostic metadata:
  - `sqrt_mid` when it matches the sqrt-derived marginal price.
  - `swap_amount_ratio` when it matches realized stable/cNGN swap amounts.
  - `unexplained` when it matches neither; this is a hard data-quality alert.
- Add validation that token order and inversion are correct for each pool.
- Add an event-density and coverage report after every update.

Rolling imbalance windows should be produced in a derived feature artifact, not stored as raw CSV columns.

### 2. Pool Snapshot Bridge for Fair Value Research

Fair Value markouts expect previous-or-equal DEX context rows in `price_snapshots` with these sources:

- `uni-base_pool`
- `uni-bsc_pool`

Current Quidax and Bybit collectors do not create those rows. The pool-history pipeline therefore needs an idempotent bridge that imports swap-derived pool prices into `price_snapshots`.

Bridge requirements:

- Use swap `sqrt_price_x96` to compute cNGN/USD midpoint.
- Store `mid` as the raw sqrt-derived marginal price.
- Store `bid = mid * (1 - fee_rate)` and `ask = mid / (1 - fee_rate)` as fee-adjusted, infinitesimal executable prices.
- Store source as `uni-base_pool` or `uni-bsc_pool`.
- Preserve true block timestamp, block number, transaction hash, and log index in `metadata_json`.
- Include active liquidity, fee rate, signed swap amounts, and canonical cNGN flow fields in metadata.
- Include `quote_model: dex_fee_adjusted_sqrt`, `raw_sqrt_mid`, `fee_adjusted_bid`, `fee_adjusted_ask`, `stored_cngn_usd_price`, `stored_price_model`, and `price_impact_included: false` in metadata.
- Reject rows whose stored `cngn_usd_price` is classified as `unexplained`; preserve `swap_amount_ratio` rows as legacy diagnostics while importing sqrt-derived `mid`.
- Avoid duplicate `source,timestamp_ms` collisions by using a deterministic subsecond offset while preserving the true timestamp in metadata.
- Never write DEX rows as Fair Value labels. They are context features only.

### 3. Paper-Faithful LP Lifecycle Ledger

The Urusov et al. CLMM methodology reconstructs realized LP outcomes from transaction-level mint, burn, and collect data. Our current backtester is paper-inspired, but it is not a faithful population-level reconstruction because it lacks a clean owner/token-id ledger.

Add a separate LP lifecycle ledger rather than overloading the pool CSV.

Minimum ledger fields:

- `chain`
- `pool_id`
- `block_number`
- `block_time`
- `tx_hash`
- `log_index`
- `event_order`
- `event_type`
- `position_manager`
- `token_id`
- `lp_owner`
- `owner_source`
- `tick_lower`
- `tick_upper`
- `liquidity_delta`
- `liquidity_after`
- `amount0`
- `amount1`
- `amount0_raw`
- `amount1_raw`
- `collect_amount0`
- `collect_amount1`
- `sqrt_price_x96_at_event`
- `tick_at_event`
- `cngn_usd_price_at_event`

The ledger should track PositionManager `Transfer` events for token ownership and token ids. For increases, decreases, burns, and collect payouts, the row must carry the token id and the best known owner at the event timestamp.

### 4. Event-Time Price Reconstruction

The exporter must not use end-of-block pool state as the price for all liquidity events in the block. That creates lookahead when a liquidity action and a swap share a block.

Required reconstruction:

- Sort initialize, swap, pool-manager modify-liquidity logs, and decoded position-manager actions by `(block_number, log_index, event_order)`.
- Seed each block with the latest known prior pool price, or the prior block state if needed.
- Update carried pool price on initialize and swap events.
- Attach the carried price to liquidity and collect rows.
- Record whether the attached price came from same-block prior event, prior event from an earlier block, or a chain state fallback.

Liquidity modifications do not change price, so the correct event-time price is the carried pool price at that log position, not the block-end slot0.

### 5. Derived LP Episodes and Features

The raw ledger feeds deterministic derived tables or files:

- `lp_position_episodes`
- `lp_episode_features`
- `lp_strategy_summaries`

Episode reconstruction should follow the paper:

- Group by venue, LP owner, and exact range.
- Match burn plus collect payout rows into composite removal events.
- Order events chronologically.
- Treat mints as positive liquidity and burn+ rows as negative liquidity.
- Drop burn+ liquidity that exceeds cumulative observed mint liquidity, because it likely belongs to a pre-sample position.
- Close positions FIFO by liquidity balance.
- Split partial removal payouts proportionally when a burn+ closes one position and begins closing the next.
- Compute opening capital, closing capital, realized PnL, capital-weighted close price, and close timestamp.

The derived feature layer must include:

- full 15-type paper taxonomy
- start/end price buckets relative to range
- midpoint placement at entry
- delta traversal
- lower and upper boundary relative metrics
- duration
- gross PnL
- gas-adjusted PnL when receipt data is available
- fee, inventory, gas, and ratio-swap attribution where available
- exact paper win-score over realized cumulative PnL path
- mark-to-market win-score as a separate backtester metric

### 6. Receipt and Gas Sidecar

The paper does not model relocation costs separately, but our strategy decisions need both gross and net views.

Add a receipt sidecar keyed by `chain,tx_hash` with:

- `gas_used`
- `effective_gas_price_wei`
- `native_fee_wei`
- `tx_from`
- `tx_to`
- `block_number`

Gas should be joined into derived LP episodes, not required for the raw pool CSV to remain usable.

### 7. Causal Cone and Stress Feature Tables

Parameter cones should become first-class derived features. They measure whether the current state is unusual relative to its own venue-local history across multiple lookback windows.

Required cone features:

- realized volatility cone percentile
- DEX premium cone percentile
- active-liquidity cone percentile
- active-position share cone percentile
- swap-flow imbalance cone percentile
- fee APR cone percentile
- volume cone percentile

Cone features must be causal:

- no future samples in percentile calculations
- explicit lookback windows and/or EWMA half-lives
- explicit minimum observation counts
- explicit max-age thresholds for joined external data
- separate feature timestamp and source timestamp fields

Store cone features in derived tables or files rather than recomputing them ad hoc inside individual hypothesis scripts. The feature builder should report missingness, stale-source rates, and percentile stability by pool.

### 8. Stress Slices and Stability Diagnostics

Walk-forward tests should keep swap-count windows for event-density fairness. Add calendar stress slices for regime tests:

- high realized-volatility cone buckets
- extreme DEX-premium cone buckets
- thin active-liquidity or high active-share buckets
- extreme swap-flow imbalance buckets
- high fee APR or volume buckets

Parameter stability diagnostics are required for promoted configs:

- selected parameter values by walk-forward window
- parameter jump size between neighboring windows
- measured regime-feature changes across the same boundary
- penalty or rejection when parameters jump without a measured regime explanation

This prevents the research process from selecting unstable grid winners that are likely objective-function noise.

## What This Enables

After this refactor, we can test:

- whether early profit-taking beats boundary-only resets
- whether profitable exits occur after small range traversal
- whether centered in-range positions dominate for our pools
- whether Base and BSC need different width/exit/sizing regimes
- whether DEX premium and venue-local flow imbalance explain LP outcomes
- whether paper-style LP metrics transfer from real LP behavior to our virtual strategy simulations
- whether volatility-cone selected ranges beat fixed EWMA widths
- whether LP sizing should shrink under active-share, liquidity, or DEX-premium stress
- whether rebalance and defend policies help only in high-stress buckets rather than globally

## What This Does Not Claim

This refactor does not make DEX prices Fair Value labels.

It also does not prove that real LP population behavior transfers to our strategy. The paper-faithful ledger supports that hypothesis test; it does not assume the answer.

## Implementation Order

1. Add pool-history validation and canonical swap-flow fields.
2. Add the pool snapshot bridge into `price_snapshots`.
3. Add event-time price replay for pool history rows.
4. Add the LP lifecycle ledger with token id and owner tracking.
5. Add burn+collect matching and FIFO episode reconstruction.
6. Add the full position taxonomy and exact realized-PnL win-score.
7. Add receipt/gas sidecar joins for net-of-cost reporting.
8. Add causal cone and stress feature tables.
9. Add opportunity-screen versus full-costed-backtest reporting.
10. Add parameter stability diagnostics and stress-slice reports.
11. Re-run walk-forward and paper-style studies only after coverage reports pass.
