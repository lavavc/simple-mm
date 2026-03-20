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

LP deployment is controlled by `deploy_token0` and `deploy_token1` on `DexParams` — see [Overview](overview). These are set in `engine/config.py`. Setting both to `0` prevents new LP minting after the next rerange without touching the current position.

## Pausing and resuming trading

Via the Telegram bot:

- `/pause` → confirm → trading halted globally (persisted across restarts)
- `/resume` → confirm → trading resumes

## Withdrawing liquidity

Via the Telegram bot:

- `/withdraw uni-base` — removes the active LP position on Base
- `/withdraw uni-bsc` — removes the active LP position on BSC
- `/withdraw all` — removes both

After withdrawal, no re-mint occurs until deploy amounts are set and a rebalance triggers.

## Stopping the engine

Via the Telegram bot `/shutdown`:

- **Unwind + Stop** — removes all LP positions on-chain, then stops the engine
- **Stop only** — stops immediately, positions remain deployed

Resume after either stop with:

```bash
docker compose -f /opt/repo/docker-compose.yml start
```

The engine resumes from its last persisted state.
