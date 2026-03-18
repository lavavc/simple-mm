---
title: Operations
order: 4
---

## Monitoring

Position state for each DEX venue is available at:

```
GET /api/venues/uni-base/position
GET /api/venues/uni-bsc/position
```

Returns current tick range, token amounts, liquidity, and whether the position is in-range. The dashboard LP tab shows this in real time via WebSocket.

Portfolio delta across all venues is at `/api/arbitrage/status` (`delta_ratio` field) and broadcast as `portfolio_value` WebSocket events every 2 minutes.

## Balance alerts

Each LP and trade account has configurable minimum balance thresholds. When any balance falls below threshold, a `refill` alert is created, broadcast to the dashboard, and logged. Thresholds are set via:

```
PUT /api/accounts/{role}/thresholds
{"min_balance_eth": "0.001", "min_balance_tokens": {"cNGN": "50000", "USDC": "500"}}
```

Default thresholds by role are set in `engine/venues/account_manager.py`. Keep hot wallet balances minimal — only enough for daily operations. Bulk funds stay in the treasury multisig and are transferred manually when alerts fire.

## Deploying liquidity

Set deploy amounts via API (auth required):

```
PUT /api/venues/uni-base/params
{"deploy_token0": "500000", "deploy_token1": "600"}
```

Setting both to `"0"` prevents any new LP minting after the next rerange without touching the current position.

## Withdrawing liquidity

To remove the active position on a venue immediately:

```
POST /api/venues/uni-base/withdraw
```

This calls `remove_position` on the adapter and returns the transaction result. The position is removed on-chain; no re-mint will occur until deploy amounts are set and a rebalance triggers. To withdraw both venues at once, call both endpoints in sequence.

To withdraw and prevent any future minting, zero the deploy amounts afterward:

```
POST /api/venues/uni-base/withdraw
PUT /api/venues/uni-base/params
{"deploy_token0": "0", "deploy_token1": "0"}
```

## Stopping the engine

**Stop without unwinding** — positions remain deployed on-chain, engine stops:

```
POST /api/shutdown
```

**Stop and unwind** — removes all LP positions before stopping:

```
POST /api/shutdown?unwind=true
```

The unwind option calls `withdraw` on each active DEX venue sequentially, waits for confirmation, then shuts down. Use this when you need a clean exit. Use plain stop when you want to restart quickly and leave positions in place.

Resume after either stop with:

```bash
docker compose -f /opt/repo/docker-compose.yml start
```

The engine resumes from its last persisted state.
