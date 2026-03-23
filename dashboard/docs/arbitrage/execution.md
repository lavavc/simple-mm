---
title: Execution
order: 5
---

## Single trade at a time

A single boolean flag `_arb_executing` on `ArbitrageEngine` serialises all execution across both pipelines. If a CEX-DEX trade is in flight and a DEX-DEX signal fires, the DEX-DEX trade is skipped (not queued). This is intentional: opportunity signals are continuous; missing one cycle is fine.

## CEX-DEX execution

For directions where the sell leg is a DEX (Quidax → uni-base / uni-bsc), the engine runs a sell-side `eth_call` simulation before placing the Quidax buy order. If the simulation reverts — insufficient cNGN, missing approval, or any other on-chain error — the buy is not placed and the sell-side cNGN balance is marked zero in the inventory tracker so the router suppresses the same route until the next balance refresh cycle (~120 s).

The actual execution steps:
1. Simulate sell leg (DEX only) — abort if it would revert.
2. Buy leg — either `executor.execute_cex_buy` (market order on Quidax) or `executor.execute_dex_buy` (on-chain swap), depending on direction.
3. The buy trade returns the actual cNGN amount received (not the theoretical amount).
4. Sell leg uses the exact cNGN amount from step 3.
5. `inventory.record_trade_start` is called after the preflight but before the buy.

**Known limitation — DEX→CEX directions:** When the buy is on a DEX and the sell is on Quidax (UNI_BSC_TO_QUIDAX / UNI_BASE_TO_QUIDAX), the sell leg is a REST API call and cannot be simulated. If the Quidax sell fails after a successful on-chain buy, the result is a half-open trade with no automated recovery path.

## DEX-DEX execution

Before executing either leg, both are simulated via `eth_call`. The sell-side simulation uses the cNGN amount from the detection signal (`cngn_transferred`). The buy-side simulation uses the USDC/USDT amount from the adjusted trade size. If either simulation reverts, no on-chain transaction is sent. A sell-side failure also zeroes the cNGN balance in the inventory tracker, blocking the router from re-approving the same route until the next balance cycle.

**Known limitation — positive slippage:** The sell-side simulation uses the signal's estimated cNGN output, not the actual amount received from the buy. If the buy produces significantly more cNGN than estimated (positive slippage), the sell simulation may pass but the actual sell may revert due to the larger-than-simulated input. This scenario requires market conditions substantially better than expected, so it is treated as an acceptable residual risk rather than a gap requiring additional code.

Execution steps:
1. Simulate sell leg (cNGN → stable on sell chain) — abort and zero inventory if it reverts.
2. Simulate buy leg (stable → cNGN on buy chain) — abort if it reverts.
3. Execute buy leg; record `buy_filled` with the actual cNGN received (`buy_amount_cngn`).
4. Execute sell leg using the actual cNGN amount from step 3.
5. Record `completed` with `actual_profit_usd`.

## Half-open trades

If the buy leg succeeds but the sell leg fails, the engine is in a **half-open** position. The preflight simulation prevents the most common causes (balance and approval failures), but network errors, node timeouts, or unexpected contract state after the simulation can still produce half-opens.

On sell-leg failure the engine:
1. Records status `half_open` in the DB with the buy tx hash, sell error, and sell account address.
2. Trips the circuit breaker immediately, blocking all further trading until manually reset.
3. Broadcasts a `critical` alert containing the opportunity ID and the `/recover <opp_id>` command.

The `/recover` command in the Telegram bot attempts two paths in order: retry the sell if the simulation now passes, or reverse the buy (sell the cNGN back on the buy-side chain) using the stored `buy_amount_cngn`. This ensures the reversal uses the actual received amount rather than a live balance query, preventing accidental sale of pre-existing inventory.

## Circuit breaker

`consecutive_failures >= max_consecutive_failures` (default 3) activates the circuit breaker, blocking all further trading. A DEX-DEX half-open trips it immediately regardless of the failure count. Reset via `/reset_breaker` in the Telegram bot.
