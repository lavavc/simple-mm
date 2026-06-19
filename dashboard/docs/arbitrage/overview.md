---
title: Overview
order: 1
---

The arbitrage system follows a canonical algo-trading pipeline:

**Market Data → Signal → Risk → Execution → Post-Trade**.

That is, we gather data from multiple sources, process it to identify the most profitable opportunities, test those opportunities against risk parameters, execute trades if they pass, and then make sure everything was persisted for auditability. Each piece is documented individually in this section.

# Too Long, Didn't Read

The core principles are simple. There are three arbitrage families:

1. **DEX <> DEX** - price diverges between Uniswap Base and Uniswap BSC enough to buy on one venue and sell from inventory on the other. This is a delta-balance route, not a bridge route.
2. **CEX <> DEX** - Quidax executable order-book prices diverge from a DEX pool enough to buy on one venue and sell on the other. Four directions are currently evaluated through the route registry.
3. **CEX <> CEX** - CEX/API venue pricing could be used against another off-chain venue. This is not implemented.

Read on to find out how we decide between the three routes, how we think about risk generally, and how we execute quickly and log each action.

## Configuration reference

All thresholds are set in `engine/config.py` which is where you need to go to change how the engine behaves:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ARB_DETECTION_ENABLED` | `true` | Enable opportunity detection |
| `ARB_EXECUTE_CEX_DEX_ENABLED` | `true` | Enable live CEX-DEX execution |
| `ARB_EXECUTE_DEX_DEX_ENABLED` | `true` | Enable live DEX-DEX execution |
| `ARBITRAGE_MAX_DAILY_VOLUME_USD` | 10,000 | Rolling 24h volume cap |
| `ARBITRAGE_MAX_INVENTORY_IMBALANCE_USD` | 5,000 | Max net directional exposure |
| `ARBITRAGE_MAX_DAILY_LOSS_USD` | 500 | Circuit breaker loss threshold |
| `ARBITRAGE_MAX_CONSECUTIVE_FAILURES` | 3 | Circuit breaker failure count |
| `ARBITRAGE_MAX_DELTA_RATIO` | 0.60 | Portfolio cNGN% ceiling |
| `ARBITRAGE_MIN_ACCOUNT_STABLECOIN_USD` | 10 | Min stablecoin per venue before pausing |
| `ARBITRAGE_CROSS_CHAIN_REBALANCE_BPS` | 10 | Max rebalance penalty in route scoring |

Fee assumptions for arb detection are not currently environment-configurable. Quidax taker fee and route-specific on-chain fee assumptions live in the detection modules and route math, not in `engine/config.py`.
