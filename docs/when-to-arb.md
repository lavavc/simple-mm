# Fees and Arbitrage

Perhaps you look at the dashboard and see the two DEXes far away from each other and wonder why we're not surfacing that as an arbitrage opportunity? Aerodrome is at 1437.80 and Pancakeswap is at 1429.36: why is no-one arbing these!?

Here's why...

## The Math

  ┌─────────────┬─────────────────────┬───────────────────┐
  │    Venue    │ NGN/USD (dashboard) │ cNGN/USD (engine) │
  ├─────────────┼─────────────────────┼───────────────────┤
  │ Aerodrome   │ 1437.80             │ 0.0006955         │
  ├─────────────┼─────────────────────┼───────────────────┤
  │ PancakeSwap │ 1429.36             │ 0.0006996         │
  └─────────────┴─────────────────────┴───────────────────┘

Spread Calculation
  - Gross spread: 59 bps (0.59%)
  - DEX-to-DEX fees: 100 bps (30 swap + 20 slippage × 2 legs)
  - Net spread: -41 bps (unprofitable after fees)

Thresholds
  - min_spread_bps = 150 → 59 bps fails this check
  - min_net_profit_bps = 50 → -41 bps fails this check

The 8.44 NGN difference looks large in absolute terms, but as a percentage it's only ~0.6%. After accounting for swap fees on both DEXs, executing this trade would actually lose money.

## Options

1. Lower the thresholds if the fees are actually lower:

```
  # In config.py or via API
  arbitrage_min_spread_bps = 75  # Lower threshold
```
  
2. Reduce fee estimates if the pools have lower fees:

```
  dex_swap_fee_bps = 20  # Instead of 30
  dex_slippage_bps = 10  # Instead of 20
```

3. Accept that this isn't a real opportunity - the current thresholds are conservative for a reason. A 59 bps gross spread with 100 bps in fees means we'd lose ~40 bps on every trade.