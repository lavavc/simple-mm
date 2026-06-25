# DEX LP Strategy Optimization

## Current Strategy Summary

SD-Bollinger band concentrated LP: calculate mean and standard deviation of recent cNGN/USD prices, set LP tick range to `mean ± (sd_multiplier × stdev)`, rebalance when price exits range. Full remove + re-mint on each rebalance.

Historical implementation note: this plan pre-dates the current LP split. The
current production implementation lives in
[engine/lp/strategy.py](../../engine/lp/strategy.py) for range math and
[engine/lp/rebalancer.py](../../engine/lp/rebalancer.py) for lifecycle
orchestration.

---

## 1. Grid Search Parameters

| Parameter | Range | Step | Rationale |
|-----------|-------|------|-----------|
| `sd_multiplier` | 0.5 – 3.0 | 0.25 | Below 0.5 is too tight; above 3.0 is basically full-range |
| `preemptive_rebalance` | true, false | discrete | `false` = reactive (rebalance after exiting range by threshold). `true` = pre-emptive (rebalance when price is within threshold of range edge, still in range) |
| `rebalance_threshold_percent` | 1, 3, 5, 10, 15 | discrete | How far past (reactive) or before (pre-emptive) the range boundary to trigger |
| `ewma_lambda` | 0.95, 0.975, 0.99, 0.999 | discrete | EWMA decay rate for mean and SD. Lower = more reactive, higher = smoother |
| `downside_skew` | 0.5, 0.6, 0.7, 0.8 | discrete | Fraction of range below the new center-tick which will be set to the EWMA mean price. 0.5 = symmetric, 0.8 = 80% downside |
| `venue_divergence_rebalance_bps` | 100, 200, 300, 500 | discrete | Second rebalance trigger, fires independently of out-of-range check |

~11 × 5 × 2 × 4 × 4 × 4 = **7,040 combinations**. Use walk-forward validation (section 4) to catch overfitting.

### Fixed guardrails (not searched)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `min_tick_width` | 50 | Prevents degenerate <5% width positions |
| `max_tick_width` | 1000 | Prevents full-range positions that defeat the purpose of CL |
| `max_slippage_percent` | 1.0 | Execution constraint, not strategy |

---

## 2. Objective Function

### P&L components for concentrated LP

| Component | Direction | Depends on |
|-----------|-----------|------------|
| Fee income | + | Time in range, swap volume, fee tier, L_position / L_active share |
| Impermanent loss | - | Price movement while in range |
| Rebalance cost | - | Gas + slippage per rebalance × frequency |
| Capital efficiency | multiplier | Narrower range = more fees per $ deployed |

`net_return = fee_income - impermanent_loss - rebalance_costs`

### Why not just Sharpe + max drawdown?

- **Sharpe** penalizes upside equally with downside and only uses the first two moments — misleading for carry-like strategies with negative skew and fat tails (QTS Lecture 06).
- **Max drawdown** is a single-point measure — nothing about the shape of the loss distribution or recovery time.

### Better-suited metrics

| Metric | Formula | Why it fits |
|--------|---------|-------------|
| **Sortino ratio** | `E[r - rb] / sqrt(E[(r - rb)² | r < rb])` | Only penalizes downside deviation (QTS Lecture 03). Formally: the denominator conditions on returns *below* the benchmark, so big fee days don't drag the score. Scales with sqrt(time) like Sharpe. |
| **Calmar ratio** | `annualized_return / max_drawdown` | Directly captures the return-vs-drawdown tradeoff. |
| **Omega ratio** | `sum(returns above threshold) / sum(returns below threshold)` | Considers the entire return distribution, not just mean/variance. Captures the fat left tail and negative skew that define carry strategies. |
| **Time in range** | `periods_in_range / total_periods` | LP-specific: if you're not in range, you're earning zero fees. A strategy with great Sharpe but 20% time in range is useless. |
| **Rebalance cost ratio** | `total_rebalance_costs / total_fee_income` | If >50%, rebalancing is eating most of your profits. Strategy is too reactive. |

**Carry trade diagnostics** (QTS Lectures 06, 11):

| Metric | What to look for |
|--------|-----------------|
| **Win rate** | Should be >60%. If not, fee income doesn't justify tail risk. |
| **Profit factor** (`gross_profits / gross_losses`) | PF of 1.3 with 90% win rate means each loss is ~12x the average win — classic carry. |
| **Return skew** | Negative skew = carry signature. If not negative, simulation is probably underestimating IL. |
| **CVaR** (`E[loss \| loss > VaR_α]`) | Average loss in worst α% of periods. Better than max drawdown for tail risk. |

### Recommended composite objective

```
objective = sortino_ratio × (1 - max_drawdown_pct) × time_in_range_pct
```

**Why this works:**
- Sortino captures risk-adjusted return without penalizing upside
- `(1 - max_drawdown_pct)` applies a multiplicative penalty for drawdowns: a 5% max drawdown gives 0.95x, a 30% gives 0.70x
- `time_in_range_pct` ensures we don't select strategies that look great on paper but are rarely earning fees

**Alternative simpler option:** Just use Calmar ratio with a time-in-range floor (e.g., discard any parameter set where time_in_range < 60%).

### Practical Sortino calculation

**Measurement period**: Daily. The strategy rebalances on the order of hours to days — daily returns are long enough to be meaningful and short enough to capture rebalance costs. Shorter periods (hourly) would be dominated by noise from IL fluctuations.

**Mark-to-market**: At each daily boundary, compute position value using CL position math (see section 4 for fee income and IL formulas). This includes:
- Current value of the two tokens given price P and position range [p_a, p_b]
- Accrued but uncollected fees (estimated from swap volume × fee share)
- Any rebalance costs incurred during the period

```
value_t = position_value(P_t, L, p_a, p_b) + accrued_fees_t
return_t = (value_t - value_{t-1} - rebalance_costs_t) / value_{t-1}
```

**Benchmark (r_b)**: 0 (capital preservation). The alternative — using stablecoin lending yield as opportunity cost — is more principled but adds a data dependency. Start with 0.

**Computation**:
```
downside_returns = [min(0, r_t) for all t]
downside_deviation = sqrt(mean(downside_returns²))
sortino_daily = mean(all_returns) / downside_deviation
sortino_annual = sortino_daily × √365
```

**Edge case**: If a parameter set produces zero negative returns (unlikely but possible with very wide ranges and low vol), downside deviation is 0 and Sortino is undefined. Cap at a high value or flag for manual review.

### What to track during backtest (even if not in objective)

- Number of rebalances per period
- Average IL per rebalance
- Fee income per day in range
- Longest out-of-range streak
- Gas cost per rebalance (estimate from historical Base gas prices)
- Return skew and kurtosis (carry trade signature diagnostics)
- Win rate and profit factor per parameter set
- CVaR at 5% and 1% levels
- Position age at each rebalance (time since last mint)

---

## 3. Strategy Critique

### Problems with the current approach

**3.1 Simple SD is slow to adapt**

`statistics.stdev()` equally weights all observations. A sudden volatility spike (e.g., NGN devaluation event) doesn't change the SD meaningfully until enough new data points accumulate. This means:
- Entering volatile periods: range too narrow → frequent rebalancing → high costs
- Leaving volatile periods: range too wide → poor capital efficiency → low fee capture

With `lookback_points=None` (current default), the SD uses the entire price history, which could be months of data. This is far too sluggish for a strategy that rebalances on 120s check intervals.

**3.2 Symmetric ranges ignore directional bias**

The range is `mean ± k × SD`, which assumes equal probability of price moving up or down. But cNGN/USD moves with NGN/USD, which has a persistent depreciation trend. The fix is an asymmetry parameter `α` (`downside_skew`) that shifts the range:

```
lower = mean - sd_multiplier × 2α × SD
upper = mean + sd_multiplier × 2(1-α) × SD
```

Where `α ∈ [0.5, 0.8]`. Total range width = `2 × sd_multiplier × SD` regardless of α, so capital efficiency is unchanged — only directional coverage shifts. At `α = 0.5` this is the current symmetric formula. At `α = 0.7`, 70% of the range is below the mean, giving more room for depreciation moves before triggering a rebalance.

**3.3 No regime awareness**

Parameters are static. Optimal behavior differs by regime: tight ranges in low-vol, wide in high-vol, withdraw in shocks. A single parameter set is always a compromise. **Volatility cones** (Burghardt 1990, QTS Lecture 04) can serve as a cheap regime indicator: current EWMA vol vs historical quantiles at multiple timescales.

**3.4 Simple mean as range center**

`statistics.mean()` equally weights all observations, so in a trending market the mean lags the current price. The range center is stale, causing asymmetric risk: you're closer to one bound than the other.

### Baseline improvements (included in grid search)

**A. EWMA for mean and SD**

Replace `statistics.mean()` and `statistics.stdev()` with EWMA versions (QTS Lecture 04). Online update requiring only two floats of state:

```
mean_{n+1}  = λ × mean_n  + (1 - λ) × x_{n+1}
var_{n+1}   = λ × var_n   + (1 - λ) × (x_{n+1} - mean_{n+1})²
```

This directly fixes problems 3.2 and 3.6. The `ewma_lambda` parameter replaces `lookback_points` — continuous, principled, O(1) per update, no price history buffer.

**B. Asymmetric ranges via `downside_skew`**

The `downside_skew` parameter `α` (formalized in 3.3 above) is included directly in the grid search. The backtest will reveal whether NGN depreciation bias justifies asymmetry or whether symmetric (`α = 0.5`) wins. No separate implementation phase needed — just read the grid results.

### Future improvements (not in initial grid)

- **Multi-position**: 2-3 overlapping NFT positions at different ranges, only replace the furthest. Reduces rebalance frequency but adds significant implementation complexity. Consider as v2 after finding good base parameters.
- **Regime-switching presets**: Conservative/moderate/aggressive parameter sets, switch based on EWMA vol quantiles. If the grid search results show optimal parameters differ sharply by vol regime, this is the natural next step.

---

## 4. Backtest Design Notes

### Data requirements
- Historical cNGN/USD prices at 30s intervals (matches `PRICE_UPDATE_INTERVAL`)
- Historical swap volumes on Aerodrome/PancakeSwap pools (for fee income estimation)
- Historical active liquidity `L_active` at the current tick (from pool contract's `liquidity` field, via on-chain events or Allium/Dune)
- Historical Base gas prices (for rebalance cost estimation)
- Source: the engine's own `price_snapshots` SQLite table, supplemented by on-chain data

### Simulation logic
For each parameter combination:
1. Initialize position at first price point with the calculated tick range; compute L from capital and range
2. At each time step: check if price is in range. If yes, accrue fees based on `L_position / L_active` share. Mark position to market using CL position math.
3. If out of range by `rebalance_threshold_percent`: trigger rebalance (realize IL, incur costs, compute new range and L)
4. If venue divergence > `venue_divergence_rebalance_bps`: also trigger rebalance
5. Track daily returns, cumulative P&L, drawdown, time in range throughout

### Fee income estimation (concentrated liquidity)

In CL pools, fees are **not** pro-rata to total pool liquidity. They're pro-rata to *active* liquidity at the current tick — the sum of L from all positions whose ranges include the current price. This is the `liquidity` value in the pool's on-chain state.

**Your position's liquidity L**: For capital C deposited in range [p_a, p_b] at current price P:

```
L = Δy / (√P - √p_a)       (from the token1 side)
L = Δx × √P × √p_b / (√p_b - √P)  (from the token0 side)
```

Key: narrower range → higher L for the same capital → higher fee share when in range. This is the core CL tradeoff and why range width matters so much.

**Fee per period**:

```
fee = swap_volume × fee_rate × (L_position / L_active)
```

Where `L_active` is the pool's active liquidity at the current tick. This differs from V2 in two critical ways:
1. Only in-range LPs share fees (out-of-range positions earn nothing)
2. Concentrated positions get a disproportionate fee share — a position with half the capital but half the range width has roughly the same L and earns roughly the same fees

**If `L_active` data isn't available**: Use a constant fee-per-period-in-range proxy, calibrated from observed fee APRs on the pool. Less accurate but avoids the data dependency. Flag results that are sensitive to the fee income assumption.

### IL calculation

For a CL position with liquidity L in range [p_a, p_b], the token amounts at current price P (when p_a ≤ P ≤ p_b) are:

```
x = L × (√p_b - √P) / (√P × √p_b)
y = L × (√P - √p_a)
```

Position value at any point: `V(P) = x × P + y`. IL is the difference between this and holding the initial token mix:

IL = V_hold(P) - V_LP(P)
   = $2 * \frac{ \sqrt{\frac{new_price_ratio}{old_price_ratio}}}{1 + \frac{new_price_ratio}{old_price_ratio}} - 1$ 

If P exits the range: all value concentrates into one token (all x below p_a, all y above p_b), and IL is at its maximum for that range. This is the worst case that a rebalance realizes.

### Rebalance cost model

A rebalance is `decreaseLiquidity → collect → burn → approve → mint`. The costs are:

1. **Gas**: ~5 transactions at ~$0.01-0.10 each on Base L2. Fixed cost, independent of position size.
2. **Realized IL**: Captured by the IL formula above — not a separate cost, but crystallized on removal (can no longer revert if price returns).
3. **Token ratio swap**: The new range requires a specific token0/token1 ratio. If the amounts returned from removal don't match, you swap the delta. This is the only component with slippage, and it applies to the **delta** only, not the full position.

**Slippage on the ratio swap** can be computed exactly from the V3 math ([derivation](https://x3finance.medium.com/how-to-calculate-swap-slippage-of-uniswap-v3-d433ed6d74b0)). Within a single tick range:

```
Δy = L_active × (√P_new - √P_old)
→ √P_new = √P_old + Δy / L_active
→ slippage = P_new / P_old - 1
```

(Equivalently for token0: `√P_new = √P_old × L / (L + Δx × √P_old)`.)

If the swap crosses tick boundaries, segment the calculation per tick range since `L_active` changes at each boundary. For small ratio swaps on a deep pool, slippage is negligible. For large swaps on thin pools, it can be significant.

### Rebalance markout analysis

Per QTS Lecture 11: mark each rebalance with market state at decision time, measure the new position's P&L at 1hr/4hr/24hr post-rebalance. The **break-even time** (when fee income exceeds rebalance cost) is the key metric. Split by pre-emptive vs reactive, trigger type (threshold vs venue-divergence), vol regime, and direction.

The fair-price markout export now provides the cross-workflow features needed
for this split:

- Quidax executable midpoint and side-specific executable labels.
- Quidax top-N imbalance, OWA, microprice, and cNGN/USD pressure buckets.
- Previous-or-equal `uni-base_pool` and `uni-bsc_pool` premiums versus Quidax
  executable mid.

For LP autoresearch, use DEX premium and venue-local swap-flow imbalance as
explanatory variables for range exits and defensive reranges. Do not use
arbitrage route state or global blended fair value as LP decision inputs unless
the package boundary is deliberately changed and documented.


### Stress testing

**Volatility spike**: Simulate realized vol doubling for 48 hours then returning to normal. Measure rebalance frequency and cumulative cost.

### Backtest integrity

- **Lookahead bias**: EWMA calculations must use only data available at each simulated decision point.
- **Overfitting**: With 10k+ parameter combinations, some will look good by chance. Walk-forward validation: optimize on first 60%, validate on remaining 40%.
- **Survivorship bias**: Report results across the full data range, not just the best-looking window.
