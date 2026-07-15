# DEX LP Autoresearch

## Goal

Improve Uniswap V4 LP range, sizing, and rebalance policy while preserving package boundaries: LP decisions are venue-local and must not depend on arb internals.

Current research scope is closed as diagnostic DEX-only evidence. Historical
Quidax coverage is top-book only, Binance `USDTNGN` does not overlap the 2026
pool windows, and the one-pass Bybit P2P check did not produce historical
coverage for the relevant validation edges. The current LP branch should
therefore preserve its Base/BSC diagnostic findings without promoting live LP
behavior.

## Current Live Design

The live engine uses:

- `engine/lp/strategy.py` for pure EWMA/log-return range math.
- `engine/lp/rebalancer.py` for lifecycle orchestration.
- `engine/lp/research.py` for episode reconstruction.
- `engine/lp/policy.py` for the first deterministic policy layer.
- `research/scripts/analyze_lp_strategy.py` for DB-backed LP episode summaries.

LP range width and rerange triggers come from venue-local pool history. The
current scheduler can pass a market-layer `StrategyFairPrice` as the range
center when available; the fallback center is the venue-local EWMA mean. Current
production LP behavior remains range-exit-first; early rerange ideas are
research-only until validated.

## Current Research Status

Implemented:

- persisted LP snapshots with range, price, fraction, and active-share fields
- DB helpers for LP action and position snapshot reconstruction
- episode reconstruction from snapshots and confirmed removal actions
- paper Figure 3 15-type position taxonomy for paper episodes
- Appendix A realized cumulative-PnL path win-score for LP episode features
- finite-state policy scaffold: enter, hold, harvest, reset, defend
- causal pool feature tables with lifetime and finite-lookback cone percentiles
- UTC calendar stress-slice reports over selected cone fields
- CLI analysis through `research/scripts/analyze_lp_strategy.py`
- backtester research log and capacity/sizing experiments archived for traceability
- paper LP episode feature export with receipt-backed native gas fields
- frozen-family flow-gated LP harness with no-position, static LP, and
  hold-cNGN baselines
- directional paper LP harness with route-aware profiles, strict QTS gates, and
  LP-versus-static-versus-hold attribution
- fail-closed external-reference cNGN inventory comparator hooks in the
  directional LP harness

Known limitations:

- older snapshots may lack newer LP fields
- episode accounting is snapshot-based, not full transaction-native accounting
- fees, inventory PnL, gas, and ratio-swap costs are not yet cleanly attributed per episode
- historical pool CSV rows do not yet provide a paper-faithful LP owner/token-id ledger
- liquidity-operation prices must be reconstructed by event order, not block-end state
- policy thresholds are not yet venue-configurable
- gas-adjusted paper episode USD PnL requires an explicit native-token USD price input
- frozen-family hold-cNGN now has both pool-mark and pool-routed variants, but
  the routed variant is a harsh DEX-only path rather than a realistic CEX or
  external inventory route
- Binance `USDTNGN` spot data does not overlap the 2026 Uniswap v4 pool window,
  so the external-reference comparator is implemented but not yet populated with
  a usable non-pool mark series
- local Bybit P2P coverage is too short for the strict Base validation edges:
  171 rows from `2026-06-19T12:26:49.707000+00:00` through
  `2026-06-20T01:09:16.620000+00:00`, covering `0/8` required strict
  start/end marks within 3600 seconds

## Research Discipline

- Keep `uni-base` and `uni-bsc` separate unless a hypothesis explicitly tests cross-pool transferability.
- Validate token order and cNGN/USD inversion before every serious run.
- Compare against current EWMA baseline, static LP allocation, and hold inventory where available.
- Use swap-count walk-forward windows when event density is uneven.
- Report train/validation gap, return on capital, drawdown, churn, fee/cost ratio, capacity, sGHO APY benchmark, and PBO where applicable.
- Separate opportunity screens from full costed backtests.
- Penalize configs whose chosen parameters jump across neighboring walk-forward windows without a measured regime-feature explanation.

## LP Markout Features

Active LP markout and regime tests should use DEX-side features only:

- realized volatility cone percentiles
- active-liquidity and active-share cone percentiles
- venue-local swap-flow imbalance cone percentiles
- fee-intensity and volume cone percentiles

Do not use arb route state as an LP input. Do not test DEX premium until a
historical Quidax reference sample exists.

## Data Methodology Refactor

The active data refactor is documented in `research/autoresearch/data-methodology-refactor.md`.

The LP workflow needs two distinct research modes:

- **Virtual strategy backtests** use our own simulated positions over venue-local pool history.
- **Paper-faithful historical LP reconstruction** uses real owner/token-id lifecycle events, burn+collect matching, and realized PnL episodes.

Do not treat the current paper-style backtester as a full replication of Urusov et al. It is suitable for testing policy ideas, but the population-level methodology requires a separate LP lifecycle ledger with token id, owner, range, liquidity balance, collect payouts, and event-time pool price.

## Cone and Stress Features

LP research should add causal parameter-cone features before promoting dynamic policies:

- realized volatility cone percentile
- active-liquidity and active-share cone percentiles
- swap-flow imbalance cone percentile
- fee APR and volume cone percentiles

Stress-conditioned hypotheses to test:

- ranges selected from volatility-cone percentiles beat fixed EWMA widths after costs
- sizing shrinks under extreme active-share, thin-liquidity, volatility, fee-intensity, or volume stress
- rebalance and defend policies improve high-stress buckets without degrading normal buckets

Keep swap-count walk-forward windows for primary selection. Add calendar stress slices only as diagnostics for regime behavior and failure modes.

## Research Closeout

Latest DEX-only rerun status is tracked in
`research/autoresearch/dex-only-rerun-status.md`.

Closeout decision: DEX LP remains diagnostic, not deployable.

What survives:

- Base has a sparse pool-internal directional regime worth preserving:
  `upside_tight_v1` under `gate_strict_qts_20_25` returns +1.039% across four
  active windows, worst +0.118%, 100.0% positive, and +0.228 percentage points
  versus pool-mark hold.
- The Base strict-QTS 20/25 directional LP policy did not transfer to BSC: it
  returned -1.272% across seven BSC windows, versus +1.039% across four Base
  windows.
- That result concerns policy transferability. It does not test whether lagged
  BSC pool prices contain incremental information about future Base price
  changes. The BSC run was worst -0.842%, 14.3% positive, and -1.602 percentage
  points versus hold.
- The external-reference comparator is implemented and fail-closed. It should be
  reused when genuinely new timestamped non-pool cNGN marks exist.

What does not survive:

- No full-grid rank-1 DEX LP stream is deployable after costed validation.
- H12 capacity curves and dynamic sizing are not final experiments until an
  accepted non-pool comparator exists.
- The Base slice is not live LP alpha. It is a useful market-structure
  diagnostic and a test harness for future data.

Completed path:

1. Run the full DEX-only extended walk-forward without `MAX_WINDOWS`, with H12
   still disabled until the winner set is re-frozen or rejected. Completed
   2026-06-25; none of the four full-grid rank-1 streams is deployable after
   full-history PBO and costed validation.
2. Collapse the search into reduced hypotheses: shared paper-style exit
   discipline with pool-specific width, and EWMA only where DEX-only stress
   features explain parameter movement ex ante. Completed as a first frozen
   family pass in `research/autoresearch/flow-gated-cngn-lp-plan.md`; Base is
   conditionally positive, but not promotable because static LP and hold-cNGN
   baselines explain too much of the result.
3. Keep H12 blocked. Directional paper LP now has one Base-only DEX-internal
   slice worth preserving: `upside_tight_v1` under `gate_strict_qts_20_25`
   returns +1.039% across four active windows, worst +0.118%, and +0.228
   percentage points versus pool-mark hold. This is still too sparse and too
   internally marked for promotion. The same strict-QTS policy did not transfer
   to BSC.
4. Source a realistic non-pool cNGN inventory comparator, then rerun strict
   active-window attribution through the implemented external-reference hooks.
   Completed 2026-07-10 as a rejection: Binance has no overlap; Bybit current
   ads are reachable but not historical; local Bybit covers `0/8` strict Base
   edge marks within the max-age rule.
5. Close the current DEX LP branch as a diagnostic result: Base has a promising
   pool-internal regime slice, but the evidence is insufficient for live LP
   promotion.

Deferred until new comparator data exists:

1. Run H12 capacity curves only on accepted frozen configs, or explicitly label
   them diagnostic if run before acceptance.
2. Test dynamic sizing against the best constant-capital policy out of sample.
3. Expose LP research summaries through API/dashboard views.
4. Make policy thresholds configurable per venue.
5. Integrate the policy scaffold into the backtester.

Archive detail:

- `research/autoresearch/archive/autoresearch.md`
- `research/autoresearch/archive/LP_RESEARCH_IMPLEMENTATION_STATUS.md`
- `research/autoresearch/archive/lp-backtester-research-log.md`
- `research/autoresearch/archive/dex-lp-opti.md`
