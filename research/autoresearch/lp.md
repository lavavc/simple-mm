# DEX LP Autoresearch

## Goal

Improve Uniswap V4 LP range, sizing, and rebalance policy while preserving package boundaries: LP decisions are venue-local and must not depend on arb internals.

Current research scope is DEX-only. Historical Quidax coverage is not yet
sufficient for CEX-label, DEX-premium, or Fair Value hypotheses, so those are
parked until the data exists. Near-term LP optimization should use only
venue-local pool history, LP ledger episodes, receipt/gas sidecars, and DEX-only
cone/stress features.

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

## Next Implementation Candidates

Latest DEX-only rerun status is tracked in
`research/autoresearch/dex-only-rerun-status.md`.

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
3. Keep H12 blocked. First-pass end-of-window static LP close/unwind costs,
   pool-routed hold-cNGN costs, and strict Base active-window attribution are
   implemented. The attribution shows no active paper-exit edge over static LP
   and only a 0.0066 percentage point mark edge over pool-mark hold.
4. Add a realistic non-pool cNGN inventory comparator, then rerun the same
   strict active-window attribution. QTS overlays remain diagnostic until they
   beat the strict gate and the relevant hold baseline after costs.
5. Run H12 capacity curves only on accepted frozen configs, or explicitly label
   them diagnostic if run before acceptance.
6. Test dynamic sizing against the best constant-capital policy out of sample.
7. Expose LP research summaries through API/dashboard views.
8. Make policy thresholds configurable per venue.
9. Integrate the policy scaffold into the backtester.

Archive detail:

- `research/autoresearch/archive/autoresearch.md`
- `research/autoresearch/archive/LP_RESEARCH_IMPLEMENTATION_STATUS.md`
- `research/autoresearch/archive/lp-backtester-research-log.md`
- `research/autoresearch/archive/dex-lp-opti.md`
