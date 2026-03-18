---
title: Risk
order: 4
---

## Route selection

When the fast path returns multiple profitable candidates (up to 4 CEX-DEX directions), the router (`engine/core/arbitrage/router.py`) selects the single best one. Scoring:

```
net_profit = expected_profit - estimated_gas - rebalance_cost_penalty
```

`estimated_gas` is provided by `engine/core/gas_oracle.py`, which refreshes every 30 s. Gas units are fixed constants measured from real on-chain swaps; the USD cost is computed dynamically:

```
gas_usd = gas_units × gas_price_gwei × 10⁻⁹ × native_token_usd
```

| Chain | Gas units | Source |
|-------|-----------|--------|
| Base (Uniswap V4) | 173,000 | `GAS_UNITS_BASE` |
| BSC (PancakeSwap V3) | 158,000 | `GAS_UNITS_BSC` |

Gas price (gwei) is fetched from each chain via `eth_gasPrice`. Native token prices (ETH/USD, BNB/USD) are fetched from the Alchemy Prices API (`tokens/by-symbol`). Both fall back to hardcoded defaults if a fetch fails. CEX-DEX routes use the per-chain cost; DEX-DEX round trips use the sum of both.

The rebalance cost penalty is computed per buy-side venue by `inventory.get_rebalance_cost_bps()`. It scales linearly from **0 bps** (venue fully stocked with stablecoin) to `cross_chain_rebalance_bps` (default 10 bps, configurable) as the stablecoin balance drains toward zero. This encodes the forward-looking cost of eventually needing to bridge or rebalance that venue.

When two routes have similar net profit, **inventory alignment** is used as a tiebreak: if we are net long cNGN (imbalance > $10), routes that sell cNGN to a CEX score higher; if net short, routes that buy cNGN from a CEX score higher. This nudges the system back toward balance without requiring explicit rebalancing trades.

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

The router also caps `optimal_size_usd` to the available stablecoin balance on the buy-side venue (`per_account_stable`, refreshed every scheduler cycle from the balance fetch). This ensures we never attempt a trade we can't fund. The min_out for the sell leg is then derived from the adjusted size:

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
