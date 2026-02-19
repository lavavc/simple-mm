# CNGN Trading System: Technical Specification

## 1. Overview

A Python trading engine with FastAPI backend and static Next.js dashboard that automates CNGN market-making operations across multiple venues.

**Core Capabilities:**
- Unified cNGN/USD price feed aggregated from multiple sources
- Real-time position tracking across all venues
- Automated DEX LP range management (SD-based tick ranges)
- Automated CEX order ladder management (Quidax)
- Cross-venue arbitrage detection and execution
- Portfolio delta monitoring

**Architecture:**
```
┌──────────────────────────────────────────────────────────────────┐
│                         Ubuntu Server                            │
│                                                                  │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │               Docker Compose                              │   │
│  │                                                           │   │
│  │  ┌────────────────────────────────────────────────────┐   │   │
│  │  │             Python Trading Engine (FastAPI)        │   │   │
│  │  │                                                    │   │   │
│  │  │  Venue Adapters:                                   │   │   │
│  │  │  • Aerodrome (Base) — cNGN/USDC LP + price        │   │   │
│  │  │  • PancakeSwap (BSC) — cNGN/USDT LP + price       │   │   │
│  │  │  • Quidax (CEX) — order ladder + arb execution    │   │   │
│  │  │  • Blockradar (Wallet) — rate setting             │   │   │
│  │  │                                                    │   │   │
│  │  │  Orchestrator:                                     │   │   │
│  │  │  • Scheduler (APScheduler)                        │   │   │
│  │  │  • Price aggregation + blending                   │   │   │
│  │  │  • Arbitrage engine                               │   │   │
│  │  │  • Account / balance monitoring                   │   │   │
│  │  │                                                    │   │   │
│  │  │  API: REST + WebSocket (/ws)                       │   │   │
│  │  │  Dashboard: static Next.js (served by FastAPI)     │   │   │
│  │  └──────────────────────────┬─────────────────────────┘   │   │
│  │                             │ 127.0.0.1:8000               │   │
│  │  ┌──────────────────────────┴─────────────────────────┐   │   │
│  │  │         cloudflared tunnel → app.domain.com        │   │   │
│  │  └────────────────────────────────────────────────────┘   │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                  │
│  SQLite (state + history)      GitHub Actions runner (CI/CD)     │
└──────────────────────────────────────────────────────────────────┘
```

Access is gated behind **Cloudflare Zero Trust** (identity verification required). The engine port is bound to `127.0.0.1` — not reachable from the public internet directly.

---

## 2. Technology Stack

| Component | Technology |
|-----------|------------|
| Trading engine | Python 3.11+ |
| Web framework | FastAPI (async, Pydantic validation) |
| On-chain | web3.py 6.x |
| Scheduling | APScheduler |
| Database | SQLite + aiosqlite |
| HTTP client | httpx (async) |
| Real-time | WebSocket (FastAPI built-in) |
| Dashboard | Next.js (static export, served by FastAPI) |
| Logging | structlog |
| Container | Docker + docker-compose |
| Tunnel | cloudflared (Cloudflare Zero Trust) |
| CI/CD | GitHub Actions (self-hosted runner on server) |

---

## 3. Core Components

### 3.1 Price Architecture

Prices are fetched per-venue, normalized to cNGN/USD, then aggregated into blended references. See [engine/core/venue_prices.py](../engine/core/venue_prices.py) and [engine/core/price_aggregation.py](../engine/core/price_aggregation.py).

**Venue price sources:**

| Source | Pair | Method |
|--------|------|--------|
| Bybit P2P | USDT/NGN | HTTP — fraud-filtered VWAP of public P2P ads |
| Quidax | cNGN/USDT | HTTP — public market summary (no auth) |
| Aerodrome | cNGN/USDC | On-chain `slot0()` via Base RPC |
| PancakeSwap | cNGN/USDT | On-chain `slot0()` via BSC RPC |
| Blockradar | cNGN/NGN | HTTP (not yet integrated) |

**DEX price reads** use `PoolPriceReader` — a read-only class that calls `slot0()` on any UniswapV3-style pool via raw `eth_call`. No private keys needed.

**Normalization** converts all venues to cNGN/USD. USDT/NGN sources (Bybit) are inverted; cNGN/USDT and cNGN/USDC sources are used directly.

**Blended prices** (TWAP 5m/1h, VWAP) are computed across normalized venue prices. Used for portfolio delta monitoring and LP rebalancing divergence checks.

**Token decimals:** cNGN has **6 decimals** on both Base and BSC.

### 3.2 DEX Position Management

Aerodrome (Base) and PancakeSwap (BSC) both use concentrated liquidity (UniswapV3-style). The system:

1. Monitors positions — checks if the LP tick range is still active
2. Calculates optimal range — standard deviation of recent prices × `sd_multiplier`
3. Triggers rebalance on two conditions:
   - Position out of range
   - Venue price diverged from blended fair value by > `venue_divergence_rebalance_bps`

Capital to deploy is configured explicitly per venue:

| Param | Default | Meaning |
|-------|---------|---------|
| `deploy_token0` | `0` | Absolute cNGN amount to use for LP |
| `deploy_token1` | `0` | Absolute USDC/USDT amount to use for LP |

Defaults to `0` — nothing is deployed until explicitly configured. The engine caps each to actual wallet balance.

Set via: `PATCH /api/venues/aerodrome/params`

See [engine/venues/dex/base.py](../engine/venues/dex/base.py) for the shared base class and [engine/venues/dex/aerodrome.py](../engine/venues/dex/aerodrome.py) / [engine/venues/dex/pancakeswap.py](../engine/venues/dex/pancakeswap.py) for protocol configs.

### 3.3 CEX Order Ladder (Quidax)

Quidax serves two independent roles:

| Role | Method | Order type | When |
|------|--------|------------|------|
| **Liquidity provision** | `sync_order_ladder()` | Limit orders | Scheduled — keeps the book filled |
| **Arb execution** | `place_market_order()` | Market order | On-demand — captures a detected spread |

**Order ladder formula:**

Orders are placed at fixed NGN offsets from the current NGN/USDT reference rate. Default offsets: `[1, 3, 5, 10]` NGN.

- Sell orders (cNGN more expensive): `price = 1 / (rate - offset)` USDT per cNGN
- Buy orders (cNGN cheaper): `price = 1 / (rate + offset)` USDT per cNGN

Each level gets a fixed amount, not a proportion of balance:

| Param | Default | Meaning |
|-------|---------|---------|
| `ladder_enabled` | `false` | Gate — ladder does nothing until enabled |
| `ladder_offsets_ngn` | `[1, 3, 5, 10]` | NGN offsets from current rate |
| `order_size_cngn` | `0` | cNGN per sell order (0 = no sell orders) |
| `order_size_usdt` | `0` | USDT budget per buy order (0 = no buy orders) |

Configure via: `PATCH /api/venues/quidax/params`

See [engine/venues/cex/quidax.py](../engine/venues/cex/quidax.py).

### 3.4 Wallet Rate Sync (Blockradar)

Sets B2C swap rates for the wallet system — buy/sell rates at reference price ± spread. Configurable spread in basis points (default: 15 bps).

See [engine/venues/blockradar.py](../engine/venues/blockradar.py).

### 3.5 Arbitrage

Cross-venue arbitrage detection and execution. See [docs/arbitrage.md](arbitrage.md) for full detail.

**Detection:** scans all venue pairs for price divergences, estimates fees, filters by `min_net_profit_bps`.

**Execution:**
- DEX leg: `venue.swap()` — synchronous, waits for on-chain confirmation
- CEX leg: `venue.place_market_order()` — market order for guaranteed fill at taker fee

Buy leg always executes first; sell leg uses the exact cNGN amount received.

Gated by `ARBITRAGE_EXECUTION_ENABLED`. Start with `false` (detection only) to validate opportunity quality before enabling trades.

### 3.6 Scheduler

All automated tasks. See [engine/core/scheduler.py](../engine/core/scheduler.py).

| Task | Default interval |
|------|-----------------|
| Price update | 30s |
| Position sync | 60s |
| DEX rebalance check | 120s |
| Portfolio delta check | 120s |
| CEX order sync | 300s |
| Account balance check | 300s |
| Arbitrage scan | 30s |

### 3.7 WebSocket Streaming

Real-time push to dashboard clients via `/ws`. The scheduler emits structured events at every state change; the WebSocket endpoint streams them as JSON.

| Event | Trigger |
|-------|---------|
| `venue_prices` | Price update job |
| `positions` | Position sync job |
| `portfolio_delta` | Delta check job |
| `alert` / `refill_alert` | Any alert |
| `account_balances` | Balance check job |
| `arbitrage_opportunity` | Arb scan |
| `arbitrage_completed` | Arb execution |

The dashboard connects once and invalidates React Query cache keys on each event — no polling needed. WebSocket URL is derived from `window.location` at runtime (`wss://` over HTTPS, `ws://` for local dev).

See [engine/ws.py](../engine/ws.py) and [dashboard/lib/hooks/useEventStream.ts](../dashboard/lib/hooks/useEventStream.ts).

---

## 4. Account Structure

Six HD wallet accounts derived from one BIP44 mnemonic. See [docs/accounts.md](accounts.md) for full detail.

| Role | Path | Chain | Tokens |
|------|------|-------|--------|
| `aerodrome-lp` | m/44'/60'/0'/1/0 | Base (8453) | ETH, cNGN, USDC |
| `aerodrome-trade` | m/44'/60'/0'/1/1 | Base (8453) | ETH, cNGN, USDC |
| `blockradar` | m/44'/60'/0'/2/0 | Base (8453) | ETH, cNGN, USDC |
| `quidax` | m/44'/60'/0'/3/0 | Ethereum (1) | ETH, cNGN, USDT |
| `pancakeswap-lp` | m/44'/60'/0'/4/0 | BSC (56) | BNB, cNGN, USDT |
| `pancakeswap-trade` | m/44'/60'/0'/4/1 | BSC (56) | BNB, cNGN, USDT |

Treasury is held off-server. Hot wallets contain only operational funds. The engine surfaces alerts when balances fall below configurable thresholds.

---

## 5. Data Storage

| Table | Purpose |
|-------|---------|
| `system_state` | Key-value store (trading_enabled, etc.) |
| `price_snapshots` | Historical price data |
| `positions` | Venue position history |
| `actions` | All trading actions taken |
| `venue_config` | Per-venue configuration |
| `alerts` | System alerts |
| `arbitrage_opportunities` | Detected and executed opportunities |
| `arbitrage_trades` | Individual trade legs |

---

## 6. API

### WebSocket
- `WS /ws` — Real-time event stream

### REST Endpoints

| Group | Endpoint | Auth | Description |
|-------|----------|------|-------------|
| Status | `GET /api/status` | — | System status, uptime, venue prices |
| Status | `GET /api/health` | — | Health check |
| Prices | `GET /api/prices` | — | All venue prices |
| Prices | `GET /api/prices/blended` | — | VWAP/TWAP composite |
| Prices | `GET /api/prices/normalized` | — | cNGN/USD per venue |
| Positions | `GET /api/positions/global` | — | Aggregated portfolio |
| Venues | `POST /api/venues/{v}/pause` | ✓ | Pause a venue |
| Venues | `POST /api/venues/{v}/resume` | ✓ | Resume a venue |
| Venues | `PATCH /api/venues/{v}/params` | ✓ | Update venue params |
| Venues | `POST /api/venues/{v}/sync` | ✓ | Force position sync |
| Arbitrage | `GET /api/arbitrage/status` | — | Arb engine status |
| Arbitrage | `GET /api/arbitrage/opportunities` | — | Opportunity list |
| Arbitrage | `POST /api/arbitrage/enable` | ✓ | Enable arb engine |
| Arbitrage | `POST /api/arbitrage/scan` | ✓ | Manual scan trigger |
| Arbitrage | `POST /api/arbitrage/reset-circuit-breaker` | ✓ | Reset circuit breaker |
| Accounts | `GET /api/accounts` | — | Account list |
| Accounts | `GET /api/accounts/balances` | — | All account balances |
| Accounts | `PUT /api/accounts/{role}/thresholds` | ✓ | Update refill thresholds |
| Alerts | `GET /api/alerts` | — | Recent alerts |
| Alerts | `POST /api/alerts/{id}/acknowledge` | ✓ | Acknowledge alert |

**Global pause/resume** is done at the server level — there is no API endpoint for it. See [docs/runbook.md](runbook.md).

---

## 7. Security

- Port `8000` bound to `127.0.0.1` — not internet-accessible
- **Cloudflare Zero Trust** — dashboard access requires identity verification
- Bearer token required for all mutating API calls (`DASHBOARD_API_TOKEN`)
- HD wallet mnemonic stored in `.env`, never logged or committed
- Infinite token approvals on DEX contracts are a known issue (see runbook)

---

## 8. Deployment

### CI/CD

Every push to `main`:
1. **test** — builds Docker `test` target; runs `pytest`
2. **deploy** — builds and pushes `production` image to `ghcr.io`, then SSHs to server and runs `docker compose pull && docker compose up -d`

### Server

`deploy/setup.sh` performs first-time setup:
- Installs Docker, GitHub Actions runner, cloudflared
- Configures Cloudflare tunnel
- Idempotent: skips steps already completed

See [docs/runbook.md](runbook.md) for operational procedures.

---

## 9. Configuration

Key environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `WALLET_MNEMONIC` | BIP39 seed phrase (12 or 24 words) | — |
| `DASHBOARD_API_TOKEN` | Bearer token for protected API calls | — |
| `BLOCKRADAR_API_KEY` | Blockradar dashboard key | — |
| `BLOCKRADAR_WALLET_ID` | Blockradar master wallet ID | — |
| `QUIDAX_API_KEY` | Quidax account key | — |
| `BASE_RPC_URL` | Base chain RPC | https://mainnet.base.org |
| `BSC_RPC_URL` | BSC RPC | https://bsc-dataseed.binance.org |
| `ARBITRAGE_EXECUTION_ENABLED` | Enable arb trade execution | `false` |
| `PRICE_UPDATE_INTERVAL` | Price fetch interval (seconds) | `30` |
| `DEX_CHECK_INTERVAL` | LP rebalance check interval | `120` |
| `CEX_SYNC_INTERVAL` | Order ladder sync interval | `300` |

---

## 10. Extensibility

### Adding a New DEX

**Price reading only:** add a `PoolReadConfig` and wire a `PoolPriceReader` into the aggregator in [engine/main.py](../engine/main.py).

**Full trading:** extend `BaseDexAdapter` in [engine/venues/dex/base.py](../engine/venues/dex/base.py), provide ABIs and a `PoolConfig`, and register in the venue factory.

### Adding a New CEX

Implement the `VenueAdapter` interface, add webhook handler if supported, register in the venue factory.

---
