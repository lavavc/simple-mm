---
title: Risk
order: 4
---

## Route selection

When we find multiple profitable candidates, the router (`engine/core/arbitrage/router.py`) selects the single best one.

We do this via the following 3 steps:

1. We **filter** trades: is the trade proditable, does it stay within the bounds of inventory paramaters?
2. Then we **score** trades: `net_profit = expected_profit - gas - rebalance_cost_penalty`. The highest net profit is prioritised.
    1. `gas` is provided by `engine/core/gas_oracle.py`, which refreshes every 30s. Gas units are conservative constants measured from real on-chain swaps, currrently set to 200k gas per DEX swap. The USD cost is computed dynamically:`gas_usd = gas_units × gas_price_gwei × 10⁻⁹ × native_token_usd`.   
    Gas price (gwei) is fetched from each chain via `eth_gasPrice`. Native token prices (ETH/USD, BNB/USD) are fetched from the Alchemy Prices API (`tokens/by-symbol`). CEX-DEX routes use the per-chain cost; DEX-DEX round trips use the sum of both.  
    2. The `rebalance_cost_penalty` is a special term we add that dynamically adjusts to inventory levels. That is, as one of our accounts on a given chain/platform moves into an imbalanced state, this penalty scales, because the need to rebalance is closer and so the cost of trading from that account is subsequently higher.
3. When two routes have similar net profit, **inventory alignment** is used as a tiebreak: if we are net long cNGN (imbalance > $10), routes that sell cNGN to a CEX score higher; if net short, routes that buy cNGN from a CEX score higher. This nudges the system back toward balance without requiring explicit rebalancing trades.

## Pre-trade risk gates

Before any execution task is created, `inventory.can_trade(size_usd, buy_venue, sell_venue)` checks the following in order:

| Check | Parameter | Default |
|-------|-----------|---------|
| Circuit breaker active | — | Blocks all trades |
| Rolling 24h volume | `max_daily_volume_usd` | $10,000 |
| Inventory imbalance | `max_inventory_imbalance_usd` | $5,000 |
| Daily loss | `max_daily_loss_usd` | $500 |
| Buy-side stablecoin low | `min_account_stablecoin_usd` | $10 |
| Portfolio delta ratio | `max_delta_ratio` | 60% cNGN |

The 24h volume uses a **rolling window** (not a midnight reset) to prevent exposure bursts at day boundaries.

## Size adjustment

The router also caps `optimal_size_usd` to the available stablecoin balance on the buy-side venue. This ensures we never attempt a trade we can't fund. The min_out for the sell leg is then derived from the adjusted size:

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
