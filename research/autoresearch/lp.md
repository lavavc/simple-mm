# DEX LP Autoresearch

## Goal

Improve Uniswap V4 LP range, sizing, and rebalance policy while preserving package boundaries: LP decisions are venue-local and must not depend on arb internals.

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
- simplified position taxonomy by start/end bucket
- win-score approximation
- finite-state policy scaffold: enter, hold, harvest, reset, defend
- CLI analysis through `research/scripts/analyze_lp_strategy.py`
- backtester research log and capacity/sizing experiments archived for traceability
- paper LP episode feature export with receipt-backed native gas fields

Known limitations:

- older snapshots may lack newer LP fields
- episode accounting is snapshot-based, not full transaction-native accounting
- fees, inventory PnL, gas, and ratio-swap costs are not yet cleanly attributed per episode
- historical pool CSV rows do not yet provide a paper-faithful LP owner/token-id ledger
- liquidity-operation prices must be reconstructed by event order, not block-end state
- policy thresholds are not yet venue-configurable
- gas-adjusted paper episode USD PnL requires an explicit native-token USD price input

## Research Discipline

- Keep `uni-base` and `uni-bsc` separate unless a hypothesis explicitly tests cross-pool transferability.
- Validate token order and cNGN/USD inversion before every serious run.
- Compare against current EWMA baseline, static LP allocation, and hold inventory where available.
- Use swap-count walk-forward windows when event density is uneven.
- Report train/validation gap, return on capital, drawdown, churn, fee/cost ratio, capacity, sGHO APY benchmark, and PBO where applicable.
- Separate opportunity screens from full costed backtests.
- Penalize configs whose chosen parameters jump across neighboring walk-forward windows without a measured regime-feature explanation.

## LP Markout Features

Fair-price research now exports features that can be joined into LP rebalance markouts:

- Quidax executable midpoint and side-specific labels.
- Quidax imbalance, OWA, microprice, and cNGN/USD pressure buckets.
- Previous-or-equal `uni-base_pool` and `uni-bsc_pool` premiums versus Quidax executable mid.

For LP policy research, use DEX premium and venue-local swap-flow imbalance as explanatory variables. Do not use arb route state as an LP input.

## Data Methodology Refactor

The active data refactor is documented in `research/autoresearch/data-methodology-refactor.md`.

The LP workflow needs two distinct research modes:

- **Virtual strategy backtests** use our own simulated positions over venue-local pool history.
- **Paper-faithful historical LP reconstruction** uses real owner/token-id lifecycle events, burn+collect matching, and realized PnL episodes.

Do not treat the current paper-style backtester as a full replication of Urusov et al. It is suitable for testing policy ideas, but the population-level methodology requires a separate LP lifecycle ledger with token id, owner, range, liquidity balance, collect payouts, and event-time pool price.

## Cone and Stress Features

LP research should add causal parameter-cone features before promoting dynamic policies:

- realized volatility cone percentile
- DEX premium cone percentile
- active-liquidity and active-share cone percentiles
- swap-flow imbalance cone percentile
- fee APR and volume cone percentiles

Stress-conditioned hypotheses to test:

- ranges selected from volatility-cone percentiles beat fixed EWMA widths after costs
- sizing shrinks under extreme active-share, thin-liquidity, or DEX-premium stress
- DEX premium explains LP outcomes conditionally, but never becomes a Fair Value label
- rebalance and defend policies improve high-stress buckets without degrading normal buckets

Keep swap-count walk-forward windows for primary selection. Add calendar stress slices only as diagnostics for regime behavior and failure modes.

## Next Implementation Candidates

1. Add canonical swap-flow fields and pool-history validation reports.
2. Add `uni-base_pool` and `uni-bsc_pool` snapshot import into `price_snapshots`.
3. Reconstruct event-time liquidity-operation prices without block-end lookahead.
4. Add the LP lifecycle ledger with token id and LP owner tracking.
5. Add burn+collect matching and FIFO transaction-native episode accounting.
6. Add causal cone and stress feature tables.
7. Split interim-collect gas across the affected open lots in `lp_episode_features`.
8. Add the full paper position taxonomy and exact realized-PnL win-score.
9. Add opportunity-screen versus full-costed-backtest reporting.
10. Add parameter stability diagnostics across walk-forward windows.
11. Expose LP research summaries through API/dashboard views.
12. Make policy thresholds configurable per venue.
13. Integrate the policy scaffold into the backtester.
14. Re-run walk-forward studies after enough extended pool history is available.

Archive detail:

- `research/autoresearch/archive/autoresearch.md`
- `research/autoresearch/archive/LP_RESEARCH_IMPLEMENTATION_STATUS.md`
- `research/autoresearch/archive/lp-backtester-research-log.md`
- `research/autoresearch/archive/dex-lp-opti.md`
