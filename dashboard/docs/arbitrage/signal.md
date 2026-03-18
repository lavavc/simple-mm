---
title: Signal
order: 3
---

## Two pipelines

The engine runs two independent detection pipelines:

**CEX-DEX** — triggered on every Quidax depth update. Evaluates four directions:
- `QUIDAX_TO_UNI_BSC` — buy cNGN on Quidax, sell on Uniswap BSC
- `UNI_BSC_TO_QUIDAX` — buy cNGN on Uniswap BSC, sell on Quidax
- `QUIDAX_TO_UNI_BASE` — buy cNGN on Quidax, sell on Uniswap Base
- `UNI_BASE_TO_QUIDAX` — buy cNGN on Uniswap Base, sell on Quidax

**DEX-DEX** — triggered on V4 swap events (plus a timer fallback). Evaluates two directions:
- `UNI_BSC_TO_UNI_BASE_DELTA_BALANCE` — buy on BSC, sell from inventory on Base
- `UNI_BASE_TO_UNI_BSC_DELTA_BALANCE` — buy on Base, sell from inventory on BSC

DEX-DEX is a **delta-balance** trade: there is no bridge. As the cNGN issuer with permanent inventory on both chains, we can buy on one chain and sell from existing inventory on the other. No cross-chain transfer is required — the two legs are fully independent.

## Finding the optimal size: ternary search

For any given direction, profit is a concave function of trade size. It rises to a peak (where marginal profit equals marginal price impact) then falls as slippage overwhelms the spread.

The fast path (`find_optimal_arb` for CEX-DEX, `find_optimal_dex_arb` for DEX-DEX) finds this peak using **ternary search** — similar to binary search but for unimodal functions. Each call narrows the search interval by one third, converging in ~56 iterations.

Before running the full search on a given direction, we apply a **short-circuit check**: evaluate the profit function at $5. At that size, slippage on both legs is negligible — the result is essentially the raw spread. If the spread is already ≤ 0 at $5, slippage can only make it worse at larger sizes (monotonic), so the direction is skipped entirely. This avoids ~114 eval calls per dead direction and keeps the event loop unblocked during quiet markets.

Both fast paths are run in `asyncio.get_running_loop().run_in_executor(None, ...)` — they are synchronous CPU loops and would otherwise block the event loop, making the engine deaf to incoming WebSocket updates during computation.

## Checking against CEX order book depth

For CEX legs, the eval function simulates full order-book execution rather than using a mid-price. `walk_orderbook_bids` and `walk_orderbook_asks` iterate through the actual bid/ask levels in price order, consuming each level until the trade size is filled. The result is a realistic average fill price that degrades naturally as the trade size grows — no separate slippage estimate is needed.

## Checking against DEX slippage

For DEX legs, `swap_token0_for_token1` and `swap_token1_for_token0` in `pool_state.py` compute the constant-product AMM output for a given input using the cached sqrtPriceX96 and liquidity. This is the same math as the on-chain swap, so slippage is captured exactly in the output amount. Again, no separate slippage model is needed — it falls out of the sizing.

## Slow path: full profit curve

Separately from the fast path (which returns only the optimal point), a slow path runs in the background via `run_in_executor`. This generates a 1,000-point profit curve over the full trade size range, which is broadcast to the dashboard as `quidax_dex_arb_curve` / `dex_arb_curve` and rendered in the Arbitrage tab chart. The slow path does not block execution.
