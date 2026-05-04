# Fair Price And V4 Migration Plan

## Goal

Build a fair-price estimation and strategy research workflow for the new cNGN concentrated liquidity pools after the migration from the legacy pools to Uniswap v4.

The immediate objective is not to produce a final production model. It is to:

1. Ingest and verify the new pool state correctly.
2. Define a robust market fair-price estimate for `cNGN/USD`.
3. Separate market fair price from strategy fair price.
4. Adapt the backtester so it can evaluate two-pool event-driven policies on the new regime.

## New Pool Regime

The legacy pools are no longer the live market:

- `cNGN/USDC` v4 pool id: `0x84fa97768196067f0e5aa157709039a3897e219cba3002d9ad38bf44e300fe93`
- `USDT/cNGN` v4 pool id: `0x2268f03a28f37f16cd3610dc669536f8c815d9d4cb2906feeeba9150fb2d8596`

Known fee tiers:

- `cNGN/USDC`: `0.15%`
- `USDT/cNGN`: `0.12%`

Assumption:

- These identifiers are Uniswap v4 pool ids, not standalone pool contract addresses.

Implication:

- The old Aerodrome and PancakeSwap histories should be treated as prior information only.
- The new v4 pools define a new trading regime.
- Old and new data should not be pooled as if they came from one stationary process.

## Guiding Definitions

### Market Fair Price

The latent common `cNGN/USD` value implied by the live market after filtering venue-specific noise.

This should be independent of the strategy's inventory.

### Executable Fair Price

The market fair price adjusted for quote-asset basis and venue-specific frictions.

Examples:

- `USDT` vs `USDC` basis
- local pool liquidity shape
- fee tier differences
- stale pool updates

### Strategy Fair Price

The center actually used for recentering or capital-allocation decisions.

This may differ from executable fair price because of:

- inventory imbalance
- venue-level capital constraints
- preference for one chain or stablecoin over another

## Phase 1: V4 Data Plumbing

### Objective

Establish a reliable event and state pipeline for the new pools.

### Tasks

1. Identify the Uniswap v4 `PoolManager` deployment on the relevant chain or chains.
2. Reconstruct the full `PoolKey` for each pool:
   - `token0`
   - `token1`
   - `fee`
   - `tickSpacing`
   - `hooks`
3. Verify whether either pool uses:
   - dynamic LP fees
   - custom accounting hooks
   - custom oracle logic
   - nonstandard swap deltas
4. Add read-only support for v4 pool state:
   - current sqrt price
   - current tick
   - active liquidity
   - fee setting
   - hook address
5. Add event ingestion for:
   - swaps
   - liquidity additions
   - liquidity removals
   - fee updates, if dynamic fees are enabled
6. Persist the new pool histories separately from legacy pool histories.

### Deliverables

- v4 pool metadata registry
- raw event loader for the two live pools
- verified docs or code references showing how the pools are configured

### Notes

The current repo assumes v3-style standalone pools in places such as [engine/venues/dex/base.py](/Users/johnbeecher/Desktop/automated-infra/engine/venues/dex/base.py). That assumption should not be carried into the new v4 integration.

## Phase 2: Canonical Normalization Layer

### Objective

Normalize all observations to a common `cNGN/USD` basis.

### Tasks

1. Define exact normalization formulas:
   - `cNGN/USDC -> cNGN/USD`
   - `USDT/cNGN -> cNGN/USD`
2. Decide how to handle `USDT` basis relative to `USD` and `USDC`.
3. Add timestamp alignment and staleness logic:
   - event-time ordering
   - stale venue penalties
4. Build a canonical observation record:
   - timestamp
   - venue
   - normalized price
   - raw pool price
   - active liquidity
   - trade size
   - signed direction if inferable
   - fee tier
   - hook metadata

### Deliverables

- normalized event schema for both pools
- helper functions that convert pool-native prices into `cNGN/USD`

## Phase 3: First-Pass Fair Price Estimator

### Objective

Create a robust baseline fair-price model before attempting anything more ambitious.

### Recommended First Model

A robust weighted blend in event time.

### Proposed Formula

At each event time `t`, estimate:

`F_t = weighted_robust_blend(P_usdc_t, P_usdt_t)`

Where weights are based on:

- recency
- active liquidity near current tick
- recent swap intensity
- venue staleness
- estimated venue noise

### Robustness Rules

- If one venue is stale, downweight or ignore it.
- If one venue shows a sharp isolated move without supporting flow in the other venue, treat it as venue-specific noise until confirmed.
- If hook behavior makes one venue harder to interpret, increase its observation noise.

### Deliverables

- baseline fair-price estimator
- per-event diagnostic output showing:
  - venue observations
  - weights
  - resulting fair price

## Phase 4: Latent State Model

### Objective

Upgrade from a simple blend to a structured common-price model.

### Recommended Model

A small state-space model in event time:

- `F_t`: latent common `cNGN/USD` fair value
- `B_t`: latent `USDT` basis relative to `USDC`

Observation equations:

- `P_USDC,t = F_t + eps_usdc,t`
- `P_USDT,t = F_t + B_t + eps_usdt,t`

State equations:

- `F_t` evolves slowly
- `B_t` evolves even more slowly, unless evidence suggests otherwise

Observation noise should depend on:

- staleness
- active liquidity
- recent swap intensity
- hook complexity

### Why This Model

- It respects the two-pool structure.
- It separates common value from quote-asset basis.
- It can be updated online.
- It is modest enough for a short live-history regime.

### Deliverables

- event-time latent-price filter
- stored estimates for:
  - `market_fair_price`
  - `usdt_basis`
  - per-venue residuals

## Phase 5: CL Microprice Analog

### Objective

Capture short-horizon local pressure inside each concentrated liquidity pool.

### Idea

Build a pool-level microprice analog using concentrated liquidity structure rather than order-book depth.

### Candidate Inputs

- current pool price
- active liquidity at current tick
- liquidity within `N` ticks above and below
- recent signed swap flow
- local mint/burn changes near current tick
- recent imbalance in aggressive order flow

### Use

Do not replace the latent fair price with this model.

Instead use it as a short-horizon adjustment or residual feature:

- `executable_fair_price = market_fair_price + venue_local_adjustment`

### Deliverables

- per-pool local pressure metric
- short-horizon directional adjustment feature

## Phase 6: Strategy Fair Price

### Objective

Translate market fair price into a recentering target used by the strategy.

### Definition

`strategy_fair_price = executable_fair_price + inventory_adjustment + allocation_adjustment`

### Inventory Adjustment Ideas

- skew lower if globally long cNGN
- skew higher if globally short cNGN
- bias toward the venue with stronger expected fee-per-risk

### Allocation Adjustment Ideas

- prefer one stablecoin over another if treasury or settlement constraints matter
- prefer one chain if operational or funding friction differs materially

### Deliverables

- explicit formula for strategy center selection
- config surface for inventory and venue-preference skew

## Phase 7: Backtester Adaptation

### Objective

Extend the research stack from per-pool simulation to two-pool joint decision-making.

### New Backtest Principles

1. Event time, not fixed one-second bars.
2. Joint state across both pools.
3. Fair price estimated online using only information available at that time.
4. Old pool histories treated as priors, not pooled training data.

### Joint State Proposal

- current fair price `F_t`
- `USDT` basis estimate `B_t`
- per-pool deviation from fair
- per-pool distance to range edge
- recent event intensity by pool
- realized event-time volatility
- local active liquidity by pool
- recent per-pool fee opportunity proxy
- estimated reset cost by pool
- inventory or deployable capital by venue

### Action Proposal

Keep the action set small:

- hold
- recenter USDC pool
- recenter USDT pool
- recenter both
- optional width bucket choice with only 2 to 3 discrete widths

### Evaluation Metrics

Track at minimum:

- net return
- max drawdown
- time in range by pool
- rebalance count by pool
- total fees by pool
- rebalance cost ratio
- capital utilization
- longest out-of-range streak

### Deliverables

- joint two-pool simulator
- walk-forward evaluation on the live v4 regime

## Phase 8: Data Use Policy

### Old Pools

Use for:

- prior estimates
- volatility and re-entry heuristics
- initialization of reasonable parameter ranges

Do not use as if they are from the same live market regime.

### New Pools

Use for:

- model fitting
- validation
- policy comparison

## Phase 9: Immediate Implementation Order

1. Add a markdown registry of the two v4 pools and their verified metadata.
2. Build a v4 read-only state fetcher around `PoolManager` and `PoolKey`.
3. Ingest swap and liquidity-modification events for the two pools.
4. Normalize both pools to `cNGN/USD`.
5. Implement the robust blended fair-price estimator.
6. Add diagnostics and plots for pool prices versus fair price.
7. Implement the latent `F_t + B_t` state-space model.
8. Extend the backtester to joint two-pool decisions.

## Open Questions

These need to be resolved before the fair-price model should be trusted:

1. Which chain or chains host these exact v4 pools?
2. What are the full `PoolKey` values?
3. Are the fees static or dynamic in practice?
4. Do the pools use hooks, and if so what do those hooks do?
5. Is there a trustworthy external cNGN anchor to use as a slow-moving prior?
6. What is the best available treatment of `USDT` basis in this environment?

## Recommended Initial Research Standard

Do not attempt deep RL at this stage.

The first acceptable milestone is:

- verified v4 pool metadata
- reproducible event dataset for the two live pools
- a robust online fair-price estimate
- a simple threshold policy evaluated in walk-forward backtests

That is the correct foundation for any later model complexity.
