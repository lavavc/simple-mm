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

## Deciding Between The Three

We use a simple method of determining which trade to make at any given moment.

1. We **filter** trades: is the trade proditable, does it stay within the bounds of inventory paramaters?
2. Then we **score** trades: `net_profit = expected_profit - gas - rebalance_cost_penalty`. The highest net profit is prioritised. The `rebalance_cost_penalty` is a special term we add that dynamically adjusts to inventory levels. That is, as one of our accounts on a given chain/platform moves into an imbalanced state, this penalty scales, because the need to rebalance is closer and so the cost of trading from that account is subsequently higher.
3. We also use a **tiebreak** term, which simply means that we prefer routes that reduce current inventory imbalance. If two trades are equally profitable, but one reduces USDT in an account that is already low, while the other increases cNGN in an account that is also low, we will prefer the one that increases cNGN.