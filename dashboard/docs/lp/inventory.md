---
title: Inventory
order: 3
---

## Inventory preferences

A roughly equal split by value between cNGN and stablecoins helps keep both sides funded. This is a soft preference, not a delta-neutral portfolio: cNGN still carries NGN exchange-rate exposure.

LP venues manage their own funds. [Arbitrage inventory management](/docs/arbitrage/inventory-balance) considers only arb accounts. Balances in separate accounts cannot fund each other's trades without a transfer, so keeping these decisions separate gives each strategy clear ownership of its available capital.

## DEX liquidity

A concentrated LP position changes composition as price moves and can become entirely one token. Reranging restores active liquidity; it does not restore or maintain a fixed 50/50 allocation.

Once price moves more than 1% beyond a position's boundary, the engine closes it, swaps the LP wallet's tokens to the ratio needed by a new range, and remints. The trade wallet is not involved. These actions are recorded so the lifecycle can be reconstructed.

Range skew uses recent price history as a mean-reversion heuristic, not a calibrated probability. It adjusts when a new range is calculated, rather than continuously moving an existing position. The skew fraction stays between 0.2 and 0.8; a higher value allocates more of the range below the mean in the range calculation's price orientation. This changes liquidity placement, not a guaranteed inventory outcome.

## Quidax liquidity

The ladder keeps the same configured number of levels and price offsets, but increases order sizes that spend the overweight asset. On Quidax's USDT/cNGN market, a USDT-heavy account therefore posts larger asks to sell USDT for cNGN. Available funds cap each side, so an exhausted asset can leave only one side quoted.

This encourages balance when orders fill. It cannot guarantee fills or an exact allocation, and it does not use the arb account's inventory.

## Portfolio reporting

The dashboard combines three distinct sources: managed on-chain wallets, deployed LP positions, and exchange holdings including funds locked in orders. Residual tokens in LP wallets count separately from tokens deployed in positions. Each source is included once.

HTTP and live updates share one report, refreshed every two minutes by default. Unreadable holdings or a missing valuation price make the report explicitly incomplete; unknown amounts are not presented as zero. The overall cNGN share describes the portfolio. It has no global target, imbalance alert, or influence on trading decisions.
