---
title: Overview
order: 1
---

The arbitrage system follows a canonical algo-trading pipeline:  

**Market Data → Signal → Risk → Execution → Post-Trade**.

That is, we gather data from multiple sources, process it to identify the most profitable opportunities, test those opportunities against our risk paramaters, execute trades if they pass, and then make sure everything went as expected and document it for auditability. Each piece is documented individually in this section.

# Too Long, Didn't Read

The core principles are simple. There are 3 kinds of arbitrage trade we can make:

1. **DEX <> DEX** - this is where the price on decentralised exchanges diverges enough to warrant buying cNGN on one and selling it on the other. Given that this requires two onchain transactions, and hence two gas fees, this is the least likely trade the system will make. It is, however, worth noting that less popular DEXes like the one on AssetChain do often diverge significantly and so are ripe for this sort of arbitrage. The AssetChain route is not currently enabled.
2. **CEX <> DEX** - where the prices on a centralized and decentralized exchange diverge enough to warrant a trade. This is the most regularly occuring trade, both because it is free to execute via API on a CEX, and because prices update continuously on centralized exchanges and discretely on decentralised exchanges, so they tend to drift almost every block in actively traded pairs. There are four possible trades: buy/sell cNGN on the CEX, or buy/sell cNGN on the DEX.
3. **CEX <> CEX** - this is somewhat unique to cNGN, given that we can set prices in secondary venues like BlockRadar. Which means we can buy or sell on a CEX and set prices on BlockRadar or other venues like it at an automatic profit. This is not yet implemented.

Read on to find out how we decide between the three routes, how we think about risk generally, and how we execute quickly and log each action.

## Configuration reference

All thresholds are set in `engine/config.py` which is where you need to go to change how the engine behaves:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ARB_EXECUTE_CEX_DEX_ENABLED` | `false` | Enable live CEX-DEX execution |
| `ARB_EXECUTE_DEX_DEX_ENABLED` | `false` | Enable live DEX-DEX execution |
| `ARBITRAGE_MAX_DAILY_VOLUME_USD` | 10,000 | Rolling 24h volume cap |
| `ARBITRAGE_MAX_INVENTORY_IMBALANCE_USD` | 5,000 | Per-account target deviation cap; corrective trades remain eligible |
| `ARBITRAGE_MAX_DAILY_LOSS_USD` | 500 | Circuit breaker loss threshold |
| `ARBITRAGE_MAX_CONSECUTIVE_FAILURES` | 3 | Circuit breaker failure count |
| `ARBITRAGE_MIN_ACCOUNT_STABLECOIN_USD` | 10 | Min stablecoin per venue before pausing |
| `ARBITRAGE_INVENTORY_WEIGHT` | 0.02 | Weight of inventory improvement in route preference |
| `ARBITRAGE_INVENTORY_MAX_AGE_SECONDS` | 30 | Maximum balance and price-reference age |
| `ARBITRAGE_QUIDAX_TARGET_CNGN_RATIO` | 0.5 | Quidax arb account cNGN target by value |
| `ARBITRAGE_UNI_BASE_TARGET_CNGN_RATIO` | 0.5 | Base arb account cNGN target by value |
| `ARBITRAGE_UNI_BSC_TARGET_CNGN_RATIO` | 0.5 | BSC arb account cNGN target by value |

Fee assumptions for arb detection are not currently environment-configurable. Quidax taker fee and route-specific on-chain fee assumptions live in the detection modules and route math, not in `engine/config.py`.
