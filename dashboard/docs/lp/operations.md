---
title: Operations
order: 4
---

## Monitoring

The dashboard LP tab shows position state for each venue in real time via WebSocket. Use the Telegram bot for quick operational checks:

- `/positions` — token IDs, balances, tick range, and LP snapshot status per venue
- `/balances` — account balances across all HD wallet roles
- `/status` — engine state, arb mode, circuit breaker

Portfolio holdings and cNGN share use a shared report, refreshed every two minutes by default. Missing holdings or valuation prices are shown as incomplete rather than zero. There is no global inventory target or deviation alert.

LP status surfaces use the same contract everywhere:

- **`live`** — current composition and value are available
- **`degraded`** — the LP NFT still exists, but composition/value are unavailable

This matters operationally: a transient RPC or cache failure should not be read as “no LP position”.

## Balance alerts

When any account balance falls below its minimum threshold, a `refill` alert fires: it appears on the dashboard, is pushed to the Telegram operator group, and is logged. Default thresholds by role are set in `engine/core/accounts.py`.

Keep hot wallet balances minimal — only enough for daily operations. Bulk funds stay in the treasury multisig and are transferred manually when alerts fire.

Use `/alerts` in the bot to see the last 5 alerts without opening the dashboard.

## Deploying liquidity

Fund the LP account with any balance of the pool's tokens — a single-token deposit (e.g. only cNGN, or only USDC) is fine. The engine will automatically swap to the correct ratio for the current tick range and mint the position within the next rebalance cycle (default every 2 minutes). No manual deployment amounts need to be configured.

## Quidax ladder inventory sizing

The Quidax LP ladder uses only its configured Quidax account's cNGN and USDT wallets. It has no dependency on arb account inventory or Uniswap LP positions. On the native `USDT/cNGN` market, bids spend cNGN to buy USDT; asks sell USDT to acquire cNGN.

The ladder keeps the configured level count and offsets. When both sides are enabled, it starts from equal per-level notionals, using the smaller of `order_size_cngn` and `order_size_usdt × reference_price`. It then scales sizes toward spending the overweight asset, using a soft 50/50 target by value:

```
imbalance = (USDT value - cNGN value) / total wallet value
bid cNGN budget = baseline × (1 - inventory_skew_strength × imbalance)
ask USDT size = baseline / reference_price × (1 + inventory_skew_strength × imbalance)
```

`inventory_skew_strength` defaults to `0.5` and accepts `0` through `0.5`. At `0`, notionals remain equal; at `0.5`, the size multipliers range from 0.5 to 1.5, giving at most a 3:1 bias before funding caps. For an account holding 90% USDT by value, the multipliers are 0.6 on bids and 1.4 on asks. This encourages balance through fills without forcing a market trade or requiring an exact inventory ratio. A configured zero size still disables that side.

Sizing includes available and locked balances so placing orders does not itself change the measured inventory mix. Before replacing a ladder, fresh wallet reads must succeed; existing orders must then be confirmed gone. Available funds are read again after cancellation. Each side's entire ladder is capped at 99% of its available spending asset, reserving 1% for fees and rounding. Orders that round to zero are omitted. An exhausted asset can therefore leave only the funded side quoted. Existing requote thresholds and cooldowns still apply.

## Pausing and resuming trading

Via the Telegram bot:

- `/pause` → confirm → trading halted globally (persisted across restarts)
- `/resume` → confirm → trading resumes

## Withdrawing liquidity

Withdrawals require an explicit destination address to prevent accidental re-deployment.

Via the Telegram bot:

- `/withdraw uni-base 0x...` — removes the active LP position on Base and sends tokens to the specified address
- `/withdraw uni-bsc 0x...` — same for BSC

The engine's own rebalance path (triggered by price moving out of range) sends tokens back to the LP account automatically for immediate reminting — no address needed for that path.

## Stopping the engine

Via the Telegram bot `/shutdown`:

- **Unwind + Stop** — removes all LP positions on-chain, then stops the engine
- **Stop only** — stops immediately, positions remain deployed

**Important:** "Unwind + Stop" sends withdrawn tokens to the LP wallet (not a cold wallet). On the next engine restart, the engine will detect the LP wallet balance and automatically remint the position within the first rebalance cycle. If you want to secure the funds rather than redeploy them, sweep the LP wallets to a cold address (via the manual `transfer.py` script) before restarting.

Resume after either stop with:

```bash
docker compose -f /opt/repo/docker-compose.yml start
```

The engine resumes from its last persisted state.
