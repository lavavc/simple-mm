---
title: Execution
order: 5
---

## Single trade at a time

A single boolean flag `_arb_executing` on `ArbitrageEngine` serialises all execution across both pipelines. If a CEX-DEX trade is in flight and a DEX-DEX signal fires, the DEX-DEX trade is skipped (not queued). This is intentional: opportunity signals are continuous; missing one cycle is fine.

## CEX-DEX execution

For directions where the sell leg is a DEX (Quidax → uni-base / uni-bsc), the engine runs a sell-side `eth_call` simulation before placing the Quidax buy order. If the simulation fails the buy is not placed, and the failure is classified into one of five categories with different responses (see Preflight error classification below).

The actual execution steps:
1. Simulate sell leg (DEX only) — abort if it would revert.
2. Buy leg — either `executor.execute_cex_buy` (market order on Quidax) or `executor.execute_dex_buy` (on-chain swap), depending on direction.
3. The buy trade records the actual cNGN amount received.
4. If the sell leg is a CEX leg, execution uses the exact buy fill from step 3. If the sell leg is a DEX leg, execution uses the preflight cNGN estimate computed at execution time.
5. `inventory.record_trade_start` is called after the preflight but before the buy.

**DEX→CEX directions:** When the buy is on a DEX and the sell is on Quidax (UNI_BSC_TO_QUIDAX / UNI_BASE_TO_QUIDAX), the sell leg is a REST API call and cannot be simulated. If the Quidax sell fails after a successful on-chain buy, the trade becomes half-open and must be recovered via the normal recovery flow — see Half-open trades below.

## DEX-DEX execution

Before executing either leg, both are simulated via `eth_call`. The sell-side simulation uses the cNGN amount from the detection signal (`cngn_transferred`). The buy-side simulation uses the USDC/USDT amount from the adjusted trade size. If either simulation fails, no on-chain transaction is sent, and the failure is classified (see Preflight error classification below).

**Known limitation — positive slippage:** The sell-side simulation uses the signal's estimated cNGN output, not the actual amount received from the buy. If the buy produces significantly more cNGN than estimated (positive slippage), the sell simulation may pass but the actual sell may revert due to the larger-than-simulated input. This scenario requires market conditions substantially better than expected, so it is treated as an acceptable residual risk rather than a gap requiring additional code.

Execution steps:
1. Simulate sell leg (cNGN → stable on sell chain) — abort; see Preflight error classification for how failures are handled.
2. Simulate buy leg (stable → cNGN on buy chain) — abort if it reverts.
3. Execute buy leg; record `buy_filled` with the actual cNGN received (`buy_amount_cngn`).
4. Execute sell leg using the preflight cNGN estimate from step 1. The actual buy fill from step 3 is persisted for recovery, not used for the live sell path.
5. Record `completed` with `actual_profit_usd`.

## Preflight error classification

When a preflight simulation fails, the error string is classified into one of five categories with different responses:

- **balance** — revert indicates insufficient cNGN balance (`transfer amount exceeds balance`, etc.). The venue's cNGN inventory is zeroed so the router stops sizing against it. Broadcasts a warning to Telegram.
- **rpc** — network or node error (timeout, connection refused, max retries). Inventory is not touched; the failure is transient. Broadcasts a warning to Telegram so operators can check node connectivity.
- **permit2** — Permit2 allowance expired or insufficient (`AllowanceExpired`, `InsufficientAllowance`). Inventory is not touched. Broadcasts a critical alert. In normal operation this should be rare because approvals are seeded during arb inventory bootstrap and also checked before each live V4 swap.
- **pool_paused** — pool is locked or not initialised (`LOK`, `PoolNotInitialized`, `paused`). Inventory is not touched. Trips the circuit breaker and broadcasts a critical alert for manual investigation.
- **unknown** — any other revert. Inventory is not touched; the circuit breaker is not tripped. Broadcasts a warning for visibility.

## Half-open trades

If the buy leg succeeds but the sell leg fails, the engine is in a **half-open** position. The preflight simulation prevents the most common causes (balance and approval failures), but network errors, node timeouts, or unexpected contract state after the simulation can still produce half-opens.

On sell-leg failure the engine:
1. Records status `half_open` in the DB together with the recovery-critical buy state, especially the buy tx hash and `buy_amount_cngn`.
2. Trips the circuit breaker immediately, blocking all further trading until manually reset.
3. Broadcasts a `critical` alert containing the opportunity ID and the `/recover <opp_id>` command. For DEX-DEX half-opens the alert also includes the sell account address for operator visibility.

**DEX-DEX recovery:** The `/recover` command attempts two paths in order: retry the sell if the simulation now passes, or reverse the buy (sell the cNGN back on the buy-side DEX) using the stored `buy_amount_cngn`. This ensures the reversal uses the actual received amount rather than a live balance query, preventing accidental sale of pre-existing inventory.

**CEX-DEX recovery:** The `/recover` command routes based on which leg is the CEX. For directions where the buy was on Quidax and the DEX sell failed (Case A), recovery sells the cNGN back on Quidax. For directions where the buy was on a DEX and the Quidax sell failed (Case B), recovery sells the cNGN back on the buy-side DEX. In both cases the stored `buy_amount_cngn` is used for the same reason as above. The Quidax `place_market_order` already retries internally five times before returning failure, so by the time a Case B half-open is recorded all retries are exhausted.

## Circuit breaker

`consecutive_failures >= max_consecutive_failures` (default 3) activates the circuit breaker, blocking all further trading. Any half-open trade also trips it immediately regardless of the failure count. Reset via `/reset_breaker` in the Telegram bot.

## WebSocket broadcast

Every signal, opportunity, execution, and alert is broadcast to connected dashboard clients in real time:

| Event type | Trigger |
|------------|---------|
| `quidax_dex_optimal_arb` | Every Quidax depth update — fast path result + portfolio valuation |
| `quidax_dex_arb_curve` | Background slow path — full `$1` to `$5,000` curve |
| `dex_arb_opportunity` | DEX-DEX fast path result |
| `dex_arb_curve` | DEX-DEX slow path curve |
| `arb_executed` | CEX-DEX trade completed |
| `dex_arb_executed` | DEX-DEX trade completed |
| `alert` (severity: critical) | Half-open trade detected |
| `arb_history_updated` | Any history event written — payload contains `opportunity_id` |
