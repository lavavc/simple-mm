---
title: Risk
order: 4
---

## Route selection

When multiple profitable candidates exist, `engine/arb/routing/router.py`
selects the single route to execute. Direction metadata, venue names, pipeline
type, leg type, and `cngn_effect` come from
`engine/arb/routing/route_registry.py`; other modules should not duplicate
direction lists or classify routes independently.

Route selection has three steps:

1. **Filter** candidates that are not profitable or cannot fit inside venue-local inventory limits.
2. **Score** remaining candidates with adjusted expected profit:

   ```
   net_profit = expected_profit - gas - rebalance_cost_penalty
   ```

   Gas costs come from `engine/market/gas_oracle.py`. Gas price is read from
   each chain via `eth_gasPrice`, native token prices come from Alchemy Prices,
   and CEX-DEX / DEX-DEX routes use the relevant per-chain cost.

   `rebalance_cost_penalty` grows as the route pushes a venue closer to an
   inventory imbalance. This makes a nominally profitable route less attractive
   when it increases future rebalancing pressure.
3. **Tiebreak** with inventory alignment. When routes have similar net profit,
   long-cNGN inventory prefers routes that sell cNGN; short-cNGN inventory
   prefers routes that buy cNGN.

`SelectedRoute` stores the selected routed size and profit metadata. Execution
does not trust routed token amounts; live token amounts are derived again at
execution time.

## Pre-trade risk gates

Before any execution task is created,
`engine/arb/risk/inventory.py` checks the following:

| Check | Parameter | Default |
|-------|-----------|---------|
| Circuit breaker active | — | Blocks all trades |
| Rolling 24h volume | `max_daily_volume_usd` | $10,000 |
| Inventory imbalance | `max_inventory_imbalance_usd` | $5,000 |
| Daily loss | `max_daily_loss_usd` | $500 |
| Buy-side stablecoin low | `min_account_stablecoin_usd` | $10 |
| Portfolio delta ratio | `max_delta_ratio` | 60% cNGN |

The 24h volume uses a **rolling window** (not a midnight reset) to prevent exposure bursts at day boundaries.

Inventory is venue-local. Quidax, `uni-base`, and `uni-bsc` balances are capped
independently; a fill on one venue does not make inventory available on another.

## Size adjustment

The router caps `optimal_size_usd` to the available stablecoin balance on the
buy-side venue and to sell-side cNGN inventory where applicable. This prevents
routes that cannot be funded on both legs.

The minimum output for the sell leg is derived from the adjusted size:

```
min_out_usd = adjusted_size * (1 - slippage_tolerance_bps / 10_000)
```

Default `slippage_tolerance_bps` = 10 (0.1%).

## Portfolio delta

Portfolio delta is monitored separately from arb inventory:

```
delta_ratio = cNGN_usd_value / total_portfolio_usd_value
target = 0.5 (50/50)
```

If delta deviates more than `delta_alert_threshold_percent` (10%) from target, an alert is raised and broadcast to the dashboard. The `max_delta_ratio` parameter (60%) acts as a hard gate in `can_trade` — no new trades are taken if the portfolio is already too heavy in cNGN.
