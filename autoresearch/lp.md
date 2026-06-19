# DEX LP Autoresearch

## Goal

Improve Uniswap V4 LP range, sizing, and rebalance policy while preserving package boundaries: LP decisions are venue-local and must not depend on arb internals.

## Current Live Design

The live engine uses:

- `engine/lp/strategy.py` for pure EWMA/log-return range math.
- `engine/lp/rebalancer.py` for lifecycle orchestration.
- `engine/lp/research.py` for episode reconstruction.
- `engine/lp/policy.py` for the first deterministic policy layer.
- `scripts/analyze_lp_strategy.py` for DB-backed LP episode summaries.

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
- CLI analysis through `scripts/analyze_lp_strategy.py`
- backtester research log and capacity/sizing experiments archived for traceability

Known limitations:

- older snapshots may lack newer LP fields
- episode accounting is snapshot-based, not full transaction-native accounting
- fees, inventory PnL, gas, and ratio-swap costs are not yet cleanly attributed per episode
- policy thresholds are not yet venue-configurable

## Research Discipline

- Keep `uni-base` and `uni-bsc` separate unless a hypothesis explicitly tests cross-pool transferability.
- Validate token order and cNGN/USD inversion before every serious run.
- Compare against current EWMA baseline, static LP allocation, and hold inventory where available.
- Use swap-count walk-forward windows when event density is uneven.
- Report train/validation gap, drawdown, churn, fee/cost ratio, and PBO where applicable.

## LP Markout Features

Fair-price research now exports features that can be joined into LP rebalance markouts:

- Quidax executable midpoint and side-specific labels.
- Quidax imbalance, OWA, microprice, and cNGN/USD pressure buckets.
- Previous-or-equal `uni-base_pool` and `uni-bsc_pool` premiums versus Quidax executable mid.

For LP policy research, use DEX premium and venue-local swap-flow imbalance as explanatory variables. Do not use arb route state as an LP input.

## Next Implementation Candidates

1. Persist derived `lp_episode_features`.
2. Expose LP research summaries through API/dashboard views.
3. Make policy thresholds configurable per venue.
4. Add transaction-native episode accounting.
5. Integrate the policy scaffold into the backtester.
6. Re-run walk-forward studies after enough extended pool history is available.

Archive detail:

- `autoresearch/archive/autoresearch.md`
- `autoresearch/archive/LP_RESEARCH_IMPLEMENTATION_STATUS.md`
- `autoresearch/archive/lp-backtester-research-log.md`
- `autoresearch/archive/dex-lp-opti.md`
