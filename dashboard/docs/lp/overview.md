---
title: Overview
order: 1
---

## Two pools

The engine manages two Uniswap V4 concentrated liquidity pools:

| Venue | Chain | Pair | Tick spacing |
|-------|-------|------|-------------|
| `uni-base` | Base (8453) | cNGN / USDC | 100 |
| `uni-bsc` | BSC (56) | cNGN / USDT | 200 |

## LP vs trade wallet separation

Each venue has two dedicated HD-wallet accounts:

- **LP account** (`uni-base-lp`, `uni-bsc-lp`) — holds the liquidity position NFT and the tokens deployed as LP. Never used for arb swaps.
- **Trade account** (`uni-base-trade`, `uni-bsc-trade`) — holds stablecoin and cNGN for arb swap legs. Never used for LP minting.

This keeps on-chain history clean and prevents arb execution from accidentally draining LP reserves.

## How V4 position state is tracked

V4 pools do not emit a convenient `PositionUpdated` event. The engine reconstructs position state from first principles:

1. **Find the LP NFT**: scan `Transfer` events on the PositionManager contract to find the `tokenId` owned by the LP address.
2. **Decode PositionInfo**: call `PositionManager.getPositionInfo(tokenId)` which returns a packed `bytes32`. `tickLower` is at bits 8–31, `tickUpper` at bits 32–55.
3. **Get liquidity**: call `StateView.getPositionLiquidity(poolId, ..., tickLower, tickUpper)`.
4. **Compute amounts**: use exact tick math to convert our position's liquidity + current sqrtPriceX96 → token amounts. See below.

Pool state itself (sqrtPriceX96, current tick, in-range liquidity) is updated inline from V4 Swap events — zero RPC calls during normal operation.

## Position value and fee share

The dashboard shows **Position Value** — the USD value of the tokens held in the LP position — computed from exact tick math using the position's `tickLower`, `tickUpper`, and the current `sqrtPriceX96`:

- If the current price is below the position's range, the position holds only token0 (cNGN on Base, USDT on BSC).
- If above the range, it holds only token1 (USDC on Base, cNGN on BSC).
- If in range, both tokens are held in proportion to how far through the range the current price sits.

Token amounts are converted to USD using the sqrtPriceX96-derived cNGN/USD price.

### Active liquidity and fee earnings

V4 tracks the total liquidity active at the current tick — the sum of all LP positions whose tick range currently includes the price. The dashboard shows **Our Share %**, which is our position's liquidity divided by this active total.

Fee earnings are proportional to active share. If our position is in range and we hold 40% of active liquidity, we earn 40% of swap fees while the price stays in range. When the price moves outside our range, our share drops to zero and fee accrual stops until the price re-enters. Widening the tick range increases the chance of staying in range but dilutes the concentration (and therefore the share of active liquidity relative to other LPs).

The engine does not auto-compound fees. Collected fees sit in the LP wallet until manually redeployed.

## Capital deployment

LP deployment is controlled by two explicit fields on `DexParams`:

| Field | Default | Meaning |
|-------|---------|---------|
| `deploy_token0` | `0` | cNGN amount to use for LP |
| `deploy_token1` | `0` | USDC/USDT amount to use for LP |

Defaults to `0` — nothing is deployed until explicitly configured. The engine caps each value to the actual wallet balance, so setting a large number is safe.

Configure in `engine/config.py` and restart the engine. See [Operations](operations) for runtime controls.
