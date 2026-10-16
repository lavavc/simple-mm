---
title: Risk
order: 4
---

## Route selection

Feasible routes must meet the minimum monetary-profit threshold after fees and gas. Inventory scoring then favours trades that improve the balance of the two affected arb accounts. Both DEX directions are considered. See [Inventory Balance](inventory-balance) for the rationale.

## Pre-trade risk gates

Before execution, routes must pass these checks:

| Check | Parameter | Default |
|-------|-----------|---------|
| Circuit breaker active | — | Blocks all trades |
| Rolling 24h volume | `max_daily_volume_usd` | $10,000 |
| Projected deviation from each arb account target | `max_inventory_imbalance_usd` | $5,000; existing breaches may improve |
| Daily loss | `max_daily_loss_usd` | $500 |
| Buy-side stablecoin low | `min_account_stablecoin_usd` | $10 |
| Fresh balances and price references | `inventory_max_age_seconds` | 30 seconds |

The 24h volume uses a **rolling window** (not a midnight reset) to prevent exposure bursts at day boundaries.

## Size adjustment

The router also caps `optimal_size_usd` to the available stablecoin balance on the buy-side venue. This ensures we never attempt a trade we can't fund. The min_out for the sell leg is then derived from the adjusted size:

```
min_out_usd = adjusted_size * (1 - slippage_tolerance_bps / 10_000)
```

Default `slippage_tolerance_bps` = 10 (0.1%).

## Portfolio reporting

The global report describes holdings across wallets, LP positions, and exchange accounts. It has no target or deviation alerts and does not affect route eligibility or scoring. Only arb-account inventory affects arbitrage decisions; LP funds remain separate.
