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

Both are Uniswap V4 pools on the respective chain's PoolManager. Aerodrome (Base) and PancakeSwap (BSC) remain in the codebase, but are not actvely used. If reference for handling V3 pools is required, those are the relevant files.

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
4. **Compute amounts**: use tick math (same as Uniswap SDK) to convert liquidity + current sqrtPriceX96 → token amounts.

If position lookup fails (e.g. no NFT found), the engine falls back to `balanceOf(poolManager)` for a rough estimate.

Pool state itself (sqrtPriceX96, current tick, liquidity) is updated inline from V4 Swap events — zero RPC calls during normal operation.

## Capital deployment

LP deployment is controlled by two explicit fields on `DexParams`:

| Field | Default | Meaning |
|-------|---------|---------|
| `deploy_token0` | `0` | cNGN amount to use for LP |
| `deploy_token1` | `0` | USDC/USDT amount to use for LP |

Defaults to `0` — nothing is deployed until explicitly configured. The engine caps each value to the actual wallet balance, so setting a large number is safe.

Set via the API (auth required):
```
PATCH /api/venues/uni-base/params
{"deploy_token0": "500000", "deploy_token1": "600"}
```
