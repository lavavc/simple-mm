---
title: Risk
order: 4
---

## Route selection

Route selection compares eligible opportunities, including both DEX directions:

1. Cap trade size to available stablecoins on the buying venue, available cNGN on the selling venue, and the single-trade limit. Recalculate expected profit when sizing changes.
2. Require fresh balances and prices, sufficient monetary profit after fees and gas, and passing risk limits.
3. Score each route as monetary profit plus a weighted inventory improvement across both affected arb accounts. Choose the highest score, using monetary profit to break ties.

Inventory improvement measures how much the projected trade reduces each account's deviation from its own target. Larger imbalances earn a stronger preference for corrective trades; overshooting reduces the benefit. Inventory credit never replaces the monetary-profit requirement. If no route qualifies, no trade is selected.

See [Inventory Balance](/docs/arbitrage/inventory-balance) for the reasons behind the policy and its separation from LP inventory.

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
