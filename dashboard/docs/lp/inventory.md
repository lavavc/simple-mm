---
title: Inventory
order: 3
---

## Delta-neutral target

The portfolio targets a **50/50 split** between USD-denominated assets (USDC, USDT) and NGN-denominated assets (cNGN).

`portfolio_value()` computes the USD value of all positions across all venues — LP token amounts (from tick math), trade account balances, and CEX balances — and returns the aggregate delta ratio. This runs on a timer (default every 2 minutes) and on each CEX-DEX signal.

If delta deviates by more than `delta_alert_threshold_percent` (default 10%) from target, an alert is broadcast. If it exceeds `max_delta_ratio` (default 60% cNGN), `can_trade()` blocks new arb trades entirely.

## Interaction with arbitrage

Arb trades change per-chain stablecoin levels:
- A `QUIDAX_TO_UNI_BASE` trade increases USDC on Base, reduces USDT on Quidax
- A `UNI_BSC_TO_QUIDAX` trade reduces USDT on BSC, increases USDT on Quidax

The arb router's **inventory alignment tiebreak** uses this: if the portfolio is net long cNGN, it favours routes that sell cNGN to a CEX (reducing cNGN weight). If net short, it favours routes that buy cNGN. This creates passive delta management via arb flow — no explicit rebalancing trade is needed in routine operation.

## Automated LP rebalancing

LP positions may move outside the range, in which case we need to close them and remint a new position at a relevant range. We follow the logic below to do this:

1. Close the position and alert (always). 
2. Then check if there are sufficient funds of whichever token needs refilling in the trade account.   
3. If so, move those tokens, and automatically remint a new position, issuing a new alert. 
4. If there are not sufficient funds, then wait for manual refill and also automatically remint a     
position.

There is some subtlety to this though, because we may need to update our parameters if there has been a significant move in the range. EWMA and σ are naturally adaptive, but the `downside_skew` may need to be adjusted, depending on which way trading has moved the price. So, we measure how far the current price has deviated from the EWMA mean in units of σ, and use that to adjust skew proportionally. If the price is 1σ above mean, `downside_skew` shifts up by 0.15. If 2σ above, by 0.30, capped at 0.8. It is symmetric in the other direction.
