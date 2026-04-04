---
title: Inventory
order: 3
---

## Delta-neutral target

The portfolio targets a **50/50 split** between USD-denominated assets (USDC, USDT) and NGN-denominated assets (cNGN).

The engine now computes one canonical global portfolio snapshot from three additive buckets:

1. **Managed on-chain wallets** from `account_manager.check_all_balances(...)`
2. **Deployed LP inventory** from explicitly registered LP venues
3. **Off-chain exchange balances** from explicitly registered exchange venues

Today that means:

- on-chain inventory includes `uni-base-trade`, `uni-bsc-trade`, `quidax-trade-fund`, `quidax-lp`, `blockradar`, and any rare residual balances still sitting in `uni-base-lp` / `uni-bsc-lp`
- deployed LP inventory is added from `uni-base` and `uni-bsc`
- off-chain exchange inventory is added from `quidax` only

This snapshot is exposed through both `/positions/global` and `/portfolio/exposure`, and the scheduler broadcasts it as `portfolio_delta` every 2 minutes by default.

The inclusion rule is explicit by design: if a new venue should affect global totals, it must get one entry in `engine/market/portfolio_registry.py`. Unregistered venue positions remain visible for diagnostics, but they do not silently change portfolio totals.

If delta deviates by more than `delta_alert_threshold_percent` (default 10%) from target, an alert is broadcast. If it exceeds `max_delta_ratio` (default 60% cNGN), `can_trade()` blocks new arb trades entirely.

## Interaction with arbitrage

Arb trades change per-chain stablecoin levels:
- A `QUIDAX_TO_UNI_BASE` trade increases USDC on Base, reduces USDT on Quidax
- A `UNI_BSC_TO_QUIDAX` trade reduces USDT on BSC, increases USDT on Quidax

The arb router's **inventory alignment tiebreak** uses this: if the portfolio is net long cNGN, it favours routes that sell cNGN to a CEX (reducing cNGN weight). If net short, it favours routes that buy cNGN. This creates passive delta management via arb flow — no explicit rebalancing trade is needed in routine operation.

## Automated LP rebalancing

When an LP position moves outside its tick range and the price has drifted more than `rebalance_threshold_percent` beyond the boundary, the engine rebalances automatically:

1. Close the out-of-range position and record the removal.
2. Swap LP wallet tokens to the ratio required by the pool at the new range (using exact tick math — the trade account is not involved).
3. Remint the position at a freshly calculated tick range.

All three steps are persisted in the action log so the LP lifecycle can be reconstructed from the database without relying on runtime logs.

The `downside_skew` adapts to mean-reversion probability: if the current price is 1σ above the EWMA mean, skew shifts up by 0.15 (more range above); if 1σ below, it shifts down. This is capped at ±0.8.
