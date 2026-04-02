---
title: Operations
order: 4
---

## Monitoring

The dashboard LP tab shows position state for each venue in real time via WebSocket. Use the Telegram bot for quick operational checks:

- `/positions` — tick range, token amounts, and in-range status per venue
- `/balances` — account balances across all HD wallet roles
- `/status` — engine state, arb mode, circuit breaker

Portfolio delta is broadcast as `portfolio_value` WebSocket events every 2 minutes.

## Balance alerts

When any account balance falls below its minimum threshold, a `refill` alert fires: it appears on the dashboard, is pushed to the Telegram operator group, and is logged. Default thresholds by role are set in `engine/core/accounts.py`.

Keep hot wallet balances minimal — only enough for daily operations. Bulk funds stay in the treasury multisig and are transferred manually when alerts fire.

Use `/alerts` in the bot to see the last 5 alerts without opening the dashboard.

## Deploying liquidity

Fund the LP account with any balance of the pool's tokens — a single-token deposit (e.g. only cNGN, or only USDC) is fine. The engine will automatically swap to the correct ratio for the current tick range and mint the position within the next rebalance cycle (default every 2 minutes). No manual deployment amounts need to be configured.

## Pausing and resuming trading

Via the Telegram bot:

- `/pause` → confirm → trading halted globally (persisted across restarts)
- `/resume` → confirm → trading resumes

## Withdrawing liquidity

Withdrawals require an explicit destination address to prevent accidental re-deployment.

Via the Telegram bot:

- `/withdraw uni-base 0x...` — removes the active LP position on Base and sends tokens to the specified address
- `/withdraw uni-bsc 0x...` — same for BSC

Via the API:

```
POST /venues/uni-base/withdraw
{"to_address": "0x...cold-wallet"}
```

After a manual withdrawal, the engine will not remint automatically (the LP account has no balance). To redeploy, fund the LP account again.

The engine's own rebalance path (triggered by price moving out of range) sends tokens back to the LP account automatically for immediate reminting — no address needed for that path.

## Stopping the engine

Via the Telegram bot `/shutdown`:

- **Unwind + Stop** — removes all LP positions on-chain, then stops the engine
- **Stop only** — stops immediately, positions remain deployed

Resume after either stop with:

```bash
docker compose -f /opt/repo/docker-compose.yml start
```

The engine resumes from its last persisted state.
