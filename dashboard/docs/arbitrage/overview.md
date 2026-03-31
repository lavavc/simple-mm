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

All thresholds are set in `engine/config.py` which is where you need to go to chang how the engine behaves:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ARB_EXECUTE_CEX_DEX_ENABLED` | `false` | Enable live CEX-DEX execution |
| `ARB_EXECUTE_DEX_DEX_ENABLED` | `false` | Enable live DEX-DEX execution |
| `ARBITRAGE_MAX_DAILY_VOLUME_USD` | 10,000 | Rolling 24h volume cap |
| `ARBITRAGE_MAX_INVENTORY_IMBALANCE_USD` | 5,000 | Max net directional exposure |
| `ARBITRAGE_MAX_DAILY_LOSS_USD` | 500 | Circuit breaker loss threshold |
| `ARBITRAGE_MAX_CONSECUTIVE_FAILURES` | 3 | Circuit breaker failure count |
| `ARBITRAGE_MAX_DELTA_RATIO` | 0.60 | Portfolio cNGN% ceiling |
| `ARBITRAGE_MIN_ACCOUNT_STABLECOIN_USD` | 10 | Min stablecoin per venue before pausing |
| `ARBITRAGE_CROSS_CHAIN_REBALANCE_BPS` | 10 | Max rebalance penalty in route scoring |
| `ARBITRAGE_CEX_TAKER_FEE_BPS` | 10 | Quidax taker fee (0.1%) |
| `ARBITRAGE_DEX_SWAP_FEE_BPS` | 30 | Fallback DEX fee if pool read fails |