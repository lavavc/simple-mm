---
title: Price Range Management
order: 2
---

## Venue-local history

LP range-setting is based only on the venue's own pool history:

- `uni-base` uses `uni-base_pool` snapshots
- `uni-bsc` uses `uni-bsc_pool` snapshots

The LP subsystem does not use blended pricing or cross-venue fair-value estimates to decide when or how to rerange. That boundary is deliberate so LP can remain a separately shippable package.

## EWMA volatility estimation

Volatility is estimated using an **online EWMA** (exponentially weighted moving average) over that venue-local pool history:

The EWMA being "online" means it is not pre-seeded from historical data, so it adapts from startup. We use it to weigh recent prices more heavily, while old prices decay exponentially. A lower λ adapts faster to volatility changes but is noisier. Backtesting suggests using a high lambda, which means that we don't pay much attention to recent prices, therefore avoiding volatility spikes as this is a more profitable setup when looking at the data. Current values:

| Venue | `ewma_lambda` |
|-------|--------------|
| `uni-base` | 0.975 |
| `uni-bsc` | 0.975 |

## Range calculation

Given EWMA, mean, and σ, the tick range is:

```
total_width = σ × sd_multiplier × 2
lower_price = mean - total_width × downside_skew
upper_price = mean + total_width × (1 - downside_skew)
```

`downside_skew` controls the asymmetry of the range:
- `0.5` → symmetric (equal width above and below mean)
- `0.3` → 30% of width below, 70% above (bullish bias, more room for price to rise)
- `0.7` → 70% below, 30% above (defensive, more room for price to fall)

Current values:

| Venue | `sd_multiplier` | `downside_skew` |
|-------|----------------|----------------|
| `uni-base` | 2.75 | 0.45 |
| `uni-bsc` | 3.0 | 0.5 |

This means we provide liquidity across most of the range (high `sd_multiplier`), with a slight bullish lean on Base (skew 0.45 = more range above the mean) and a neutral position on BSC (skew 0.5 = symmetric).

Ticks are then aligned to the pool's `tick_spacing` (floor for lower, ceil for upper). The result is also clamped to `min_tick_width` and `max_tick_width` to prevent degenerate ranges.

## Rerange triggers

A rerange is considered when:
1. The current active tick exits the LP range, **and**
2. The price has moved at least `rebalance_threshold_percent` (default 10%) beyond the boundary.

The second condition prevents churning on brief range exits caused by momentary volatility. The check runs on the scheduler's LP management cycle.

## Future improvement: local early reranging

A venue-local early-rerange trigger is still a legitimate future improvement if ranges get tighter, pool trading gets heavier, or churn economics justify acting before price fully exits the range. The likely form would be a local EWMA or local historical-average trigger using the same venue-local pool history.

That is intentionally out of scope for the current production LP rollout. The live implementation today remains range-exit-only.

## Pool fees and their effects

Each pool has a fee tier (`pool_fee`), charged on every swap through the LP's active range:

| Venue | `pool_fee` |
|-------|-----------|
| `uni-base` | — (V4 hook-based, set at pool creation) |
| `uni-bsc` | — (V4 hook-based, set at pool creation) |

Pool fee has a dual role:

**As LP income**: every swap earns the LP a share of the fee proportional to their liquidity. Narrower ranges concentrate liquidity and earn more per unit of capital deployed, but go out-of-range more often.

**As arb cost**: pool fee is paid by the arb engine on every DEX swap leg. Higher fees reduce arb profitability and raise the minimum spread required for a trade to be worth executing. This creates a natural tension: the same fee that earns LP income also slows down arb execution.

**Effect on trade frequency and size**: in a tighter-fee pool, more arb opportunities cross the profitability threshold, but at smaller sizes. In a higher-fee pool, only large spread events are worth trading, but each trade is more profitable net of LP fee income.

## Parameters reference

LP strategy parameters are defined in `engine/config.py` as `uni_base_*` / `uni_bsc_*` fields and are the single source of truth. They can be overridden via environment variables.

| Parameter | uni-base | uni-bsc | Effect |
|-----------|----------|---------|--------|
| `sd_multiplier` | 2.75 | 3.0 | Range width in standard deviations. Higher = wider, fewer reranges |
| `ewma_lambda` | 0.975 | 0.975 | Volatility smoothing. Lower = adapts faster, noisier range |
| `downside_skew` | 0.45 | 0.50 | Fraction of range below mean. 0.5 = symmetric |
| `rebalance_threshold_percent` | 10.0 | 10.0 | % beyond range boundary before rerange triggers |
| `min_tick_width` | 100 | 100 | Floor on range width in ticks |
| `max_tick_width` | 1000 | 1000 | Ceiling on range width in ticks |
| `lookback_points` | None (all) | None (all) | Number of recent prices used for EWMA. None = full history |

> Potential future enhancement: a venue-local early-rerange trigger based on EWMA drift or local historical divergence. Not enabled in the current implementation.
