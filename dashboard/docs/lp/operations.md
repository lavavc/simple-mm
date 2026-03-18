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

## Deploying and withdrawing liquidity

Set deploy amounts via API (auth required):

```
PATCH /api/venues/uni-base/params
{"deploy_token0": "500000", "deploy_token1": "600"}
```

Setting both to `"0"` prevents any new LP minting. The engine will not remove an existing position when deploy amounts are zeroed — it will simply not re-mint after the next rerange. To fully exit, trigger a rerange (e.g. by setting a tight range that the current price is outside of) and then set deploy amounts to `0` before the re-mint executes.

## Emergency stop

To halt all trading immediately:

```bash
ssh root@<server-ip> "docker compose -f /opt/repo/docker-compose.yml stop"
```

The engine and all scheduled jobs stop. On-chain positions are unaffected — LP positions remain deployed. Resume with:

```bash
ssh root@<server-ip> "docker compose -f /opt/repo/docker-compose.yml start"
```

The engine resumes from its last persisted state on restart.
