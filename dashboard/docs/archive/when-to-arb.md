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

Spread Calculation (with on-chain reserve data available)
  - Gross spread: 59 bps (0.59%)
  - DEX-to-DEX fees: swap fee (Aerodrome) + swap fee (PancakeSwap) + rebalance cost
    — Aerodrome: pool fee read from chain (e.g. 5–30 bps depending on pool tier)
    — PancakeSwap: pool fee read from chain = 100 bps (1% pool)
    — Cross-chain rebalance: 0–10 bps inventory-weighted
  - Net spread: deeply negative (unprofitable)

Even in the most optimistic case (Aerodrome at 5 bps), the PancakeSwap leg alone costs 100 bps — nearly twice the gross spread.

Thresholds
  - min_spread_bps = 150 → 59 bps fails this check
  - min_net_profit_bps = 50 → net spread fails this check

## How Fees Are Estimated

### Swap fees — read from chain, not hardcoded

Each DEX pool exposes a `fee()` function. The engine calls it once per pool at startup and caches the result. This means:
- No global `dex_swap_fee_bps` guess applied uniformly across all DEXes
- PancakeSwap's pool fee is 1% for volatile, low liquidity pools.
- Aerodrome's fee reflects whatever tier the specific pool was deployed with

If the RPC call fails, the engine falls back to `params.dex_swap_fee_bps` as a conservative estimate.

### Slippage — captured in sizing, not fees

For DEX+DEX pairs, trade size is computed from on-chain pool reserves using the constant-product formula:

```
Δcngn_opt = (sqrt(k_B) · cngn_A − sqrt(k_A) · cngn_B) / (sqrt(k_A) + sqrt(k_B))
```

This finds the size where marginal profit equals marginal price impact, so there is no separate slippage estimate. When reserve data is unavailable (e.g. a DEX+CEX pair), trade size falls back to `max_single_trade_usd`.

### CEX legs

CEX fees use `params.cex_taker_fee_bps` (CEX taker fees are fixed and don't benefit from reserve data).

### Cross-chain rebalancing

For DEX↔DEX arb across different chains, a rebalancing cost is added. This scales with inventory drain via `InventoryTracker` — see [inventory.py](../engine/core/arbitrage/inventory.py).

### Profit Curve and Ternary Search

If you want to be safe without multi-tick math, a coarse scan of ~20 log-spaced sizes to find the rough peak, then ternary search in that neighbourhood, handles the multi-peak case cheaply.
