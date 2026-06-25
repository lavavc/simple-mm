# Literature

Research papers and reference PDFs live here. Active implementation guidance belongs in `dashboard/docs/` or `research/autoresearch/`.

## Market Microstructure And Fair Price

- `05_Tick_Level_Analysis.pdf` — tick data, OWA, Lee-Ready trade marking, VPIN, flow, EWMA returns, Epps effect.
- `BookImbalance__Lipton.pdf` — order-book imbalance, queue-depletion probabilities, near-side fill/adverse-selection model.
- `TradeSizing__Browne.pdf` — finite-horizon Kelly and probability-maximizing sizing.
- `fair_price_pipeline_plan.pdf` — historical fair-price design artifact.

## Backtesting And Parameter Reversion

- `03_Backtesting_Basics.pdf` — distinguishes rough return opportunity, strategy returns, and full costed backtests. The core lesson is that useful research must make execution costs, attainable size, capital usage, latency, stale positions, benchmarks, drawdowns, churn, capacity, and stress behavior explicit.
- `04-Parameter_Reversion_And_Cones.pdf` — frames stressed model parameters as opportunity only when they are economically meaningful, historically unusual, and tradable through liquid instruments. Parameter cones compare current values across lookback windows against historical quantiles; EWMA-style online estimators and regime-instability checks are natural complements.

Local research implications:

- Treat LP and Fair Value changes as falsifiable parameter-stress or execution-quality claims, not just grid-search winners.
- Report return on capital, drawdown, churn, fee/cost ratio, capacity, latency assumptions, and benchmarks such as passive LP, hold inventory, and sGHO APY.
- Add parameter-cone diagnostics for realized volatility, DEX premium, active-liquidity share, fee APR, and swap-flow imbalance.
- Test EWMA half-lives as online estimators, then stress-check them against cone quantiles and walk-forward stability.
- Penalize selected parameters that jump across neighboring windows unless the jump is explained by a measured regime change.

## Concentrated Liquidity

- `LPinginCLMMs.pdf` — LP strategy and concentrated-liquidity research background.
- `uniswap-v3-liquidity-math.pdf` — Uniswap V3/V4-compatible liquidity math reference.
