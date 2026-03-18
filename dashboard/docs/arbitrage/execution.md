---
title: Execution
order: 5
---

## Single trade at a time

A single boolean flag `_arb_executing` on `ArbitrageEngine` serialises all execution across both pipelines. If a CEX-DEX trade is in flight and a DEX-DEX signal fires, the DEX-DEX trade is skipped (not queued). This is intentional: opportunity signals are continuous; missing one cycle is fine.

## CEX-DEX execution

1. Buy leg first — always. Either `executor.execute_cex_buy` (market order on Quidax) or `executor.execute_dex_buy` (on-chain swap), depending on direction.
2. The buy trade returns the actual cNGN amount received (not the theoretical amount).
3. Sell leg uses the exact cNGN amount from step 2 — not the original estimate.
4. `inventory.record_trade_start` is called before both legs to log the trade in progress.

## DEX-DEX execution

Same structure: buy leg (on one chain), then sell leg (on the other chain from existing inventory). Both legs are `execute_dex_buy` / `execute_dex_sell` on different venue adapters.

After the buy leg confirms, the opportunity DB record is updated to `executing` status. After the sell leg confirms, it moves to `completed`.

## Half-open trades

If the buy leg succeeds but the sell leg fails, the engine is in a **half-open** position: cNGN was acquired but not sold, or vice versa. This is the highest-severity failure mode.

On sell-leg failure, the engine:
1. Logs `cex_dex_half_open` or `dex_dex_half_open` at error level with the buy tx hash
2. Calls `inventory.record_trade_failure` (increments `consecutive_failures`)
3. Broadcasts a `critical` alert to the dashboard WebSocket channel
4. Releases the `_arb_executing` flag so the system can resume

The alert message includes the buy tx hash so the position can be manually closed. A half-open trade does not trigger the circuit breaker automatically — it requires operator review.

## Circuit breaker

`consecutive_failures >= max_consecutive_failures` (default 3) activates the circuit breaker, blocking all further trading until it is manually reset via `POST /api/arbitrage/reset-circuit-breaker` or until UTC midnight.
