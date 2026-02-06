# CNGN Trading System: Technical Specification

## 1. Overview

A Python trading engine with FastAPI backend and static Next.js dashboard that automates CNGN market-making operations across multiple venues (starting with four, but built to be extensible).

**Core Capabilities:**
- Unified USDT/NGN price feed with proper filtering
- Real-time position tracking across all venues
- Automated DEX LP range management (SD-based tick ranges)
- Automated CEX order ladder management
- Manual controls with pause/resume at global and venue level
- Cross-venue arbitrage detection and execution

**Architecture:**
```
┌──────────────────────────────────────────────────────────────────┐
│                         Ubuntu Server                            │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                 Python Trading Engine                       │ │
│  │                      (FastAPI)                              │ │
│  │                                                             │ │
│  │  ┌─────────────────────────────────────────────────────┐    │ │
│  │  │                   Venue Adapters                    │    │ │
│  │  │  ┌───────────┐ ┌───────────┐ ┌───────┐ ┌─────────┐  │    │ │
│  │  │  │ Aerodrome │ │ Pancake   │ │Quidax │ │Blockrdr │  │    │ │
│  │  │  │  (Base)   │ │  (BSC)    │ │ (CEX) │ │ (Wallet)│  │    │ │
│  │  │  └─────┬─────┘ └─────┬─────┘ └───┬───┘ └────┬────┘  │    │ │
│  │  │        │             │           │          │       │    │ │
│  │  │        └──────┬──────┘           │          │       │    │ │
│  │  │               │                  │          │       │    │ │
│  │  │        ┌──────┴──────┐           │          │       │    │ │
│  │  │        │  Shared DEX │           │          │       │    │ │
│  │  │        │    Layer    │           │          │       │    │ │
│  │  │        │ (from mm-bot)           │          │       │    │ │
│  │  │        └─────────────┘           │          │       │    │ │
│  │  └──────────────────────────────────┴──────────┴───────┘    │ │
│  │                         │                                   │ │
│  │  ┌──────────────────────┴───────────────────────────────┐   │ │
│  │  │                    Orchestrator                      │   │ │
│  │  │  • Scheduler (APScheduler)                           │   │ │
│  │  │  • Price Feed (Bybit P2P + on-chain)                 │   │ │
│  │  │  • Rebalancer (delta-neutral targeting)              │   │ │
│  │  │  • Arbitrage Engine                                  │   │ │
│  │  └──────────────────────┬───────────────────────────────┘   │ │
│  │                         │                                   │ │
│  │  ┌──────────────────────┴───────────────────────────────┐   │ │
│  │  │              FastAPI Server                          │   │ │
│  │  │  • REST API (controls, positions, config)            │   │ │
│  │  │  • WebSocket (real-time updates)                     │   │ │
│  │  │  • Webhook handlers (Quidax)                         │   │ │
│  │  └──────────────────────────────────────────────────────┘   │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                              │                                   │
│  ┌───────────────────────────┴────────────────────────────────┐  │
│  │              Static Next.js Dashboard                      │  │
│  │            (served by nginx or FastAPI)                    │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐                              │
│  │   SQLite     │  │    cache/    │                              │
│  │   (state)    │  │ (price data) │                              │
│  └──────────────┘  └──────────────┘                              │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. Technology Stack

| Component | Technology | Rationale |
|-----------|------------|-----------|
| **Trading Engine** | Python 3.11+ | Good testing infra, typically used for trading, quant etc |
| **Web Framework** | FastAPI | Async, auto-docs, Pydantic validation |
| **On-chain** | web3.py 6.x | Mature, good docs & examples |
| **Scheduling** | APScheduler | Robust, supports async jobs |
| **Database** | SQLite + aiosqlite | Async SQLite, zero dependencies |
| **HTTP Client** | httpx | Async HTTP for external APIs |
| **Real-time** | WebSocket (FastAPI built-in) | Push-based streaming to dashboard |
| **Dashboard** | Next.js (static export) | React DX, static = simple serving |
| **Logging** | structlog | Structured logging, Python-native |

---

## 3. Core Components

### 3.1 Price Architecture

Prices are fetched per-venue, normalized to a common basis (cNGN/USD), and then aggregated into blended reference prices. See [engine/core/venue_prices.py](../engine/core/venue_prices.py) for the source implementations and [engine/core/price_aggregation.py](../engine/core/price_aggregation.py) for normalization and blending.

**Venue price sources:**

| Source | Pair | Method | File |
|--------|------|--------|------|
| Bybit P2P | USDT/NGN | HTTP (public ads, fraud-filtered VWAP) | [engine/core/venue_prices.py](../engine/core/venue_prices.py) |
| Quidax | cNGN/USDT | HTTP (public market summary, no auth) | [engine/core/venue_prices.py](../engine/core/venue_prices.py) |
| Aerodrome | cNGN/USDC | On-chain `slot0()` via Base RPC | [engine/venues/dex/base.py](../engine/venues/dex/base.py) |
| PancakeSwap | cNGN/USDT | On-chain `slot0()` via BSC RPC | [engine/venues/dex/base.py](../engine/venues/dex/base.py) |
| Blockradar | cNGN/NGN | HTTP (API, not yet integrated) | [engine/core/venue_prices.py](../engine/core/venue_prices.py) |

**DEX price reads** use `PoolPriceReader`, a lightweight read-only class that calls `slot0()` on any UniswapV3-style pool. It uses raw `eth_call` with just the function selector (no typed ABI), making it protocol-agnostic across Aerodrome, PancakeSwap, and standard UniswapV3. No private keys are needed — only a public RPC URL. See [engine/venues/dex/base.py](../engine/venues/dex/base.py).

Pool configs with on-chain token ordering and decimal info live in [engine/venues/dex/aerodrome.py](../engine/venues/dex/aerodrome.py) and [engine/venues/dex/pancakeswap.py](../engine/venues/dex/pancakeswap.py).

**Normalization** converts all venue prices to cNGN/USD. Venues reporting USDT/NGN (Bybit) are inverted; venues reporting cNGN/USDT or cNGN/USDC are used directly.

**Blended prices** (TWAP, VWAP) are computed from the normalized venue prices for global portfolio management and delta-neutrality checks. Venue-specific prices are used for per-venue LP rebalancing and arbitrage detection.

**Token decimals:** cNGN has **6 decimals** on both Base and BSC. This is important for the sqrtPriceX96 → human price conversion.

### 3.2 DEX Position Management

Both Aerodrome (Base) and PancakeSwap (BSC) use concentrated liquidity (UniswapV3-style). The system:

1. **Monitors positions** — checks if liquidity is still in range
2. **Calculates optimal range** — uses standard deviation of recent prices
3. **Rebalances when needed** — removes old position, creates new one with updated range

The SD-based range calculation ensures positions adapt to market volatility. The shared base class lives in [engine/venues/dex/base.py](../engine/venues/dex/base.py); protocol-specific ABIs and configs in [engine/venues/dex/aerodrome.py](../engine/venues/dex/aerodrome.py) and [engine/venues/dex/pancakeswap.py](../engine/venues/dex/pancakeswap.py).

### 3.3 CEX Order Ladder (Quidax)

Maintains buy and sell orders across price levels:

- **Ladder levels** — configurable number of price increments (default: 10)
- **Spread per level** — price increment in NGN (default: 1.0)
- **Liquidity distribution** — percentage of balance at each level

Orders are cancelled and recreated when the reference price changes significantly. The adapter is in [engine/venues/cex/quidax.py](../engine/venues/cex/quidax.py).

### 3.4 Wallet Rate Sync (Blockradar)

Sets B2C swap rates for the wallet system:

- Calculates buy/sell rates from reference price ± spread
- Supports both CNGN/USDT and CNGN/USDC pairs
- Configurable spread in basis points (default: 15 bps)

See [docs/block-radar-questions.md](block-radar-questions.md) for open questions and remaining work.

### 3.5 Scheduler

Orchestrates all automated tasks. See [engine/core/scheduler.py](../engine/core/scheduler.py). All task intervals are configurable via environment variables.

| Task | Default Interval | Description |
|------|------------------|-------------|
| Price update | 30s | Fetch all venue prices and compute blended |
| Position sync | 60s | Fetch positions from all venues |
| DEX rebalance check | 120s | Check if LP needs rebalancing |
| CEX order sync | 300s | Sync Quidax order ladder |
| Rate sync | 300s | Sync Blockradar swap rates |

### 3.6 WebSocket Streaming

The engine pushes real-time events to all connected dashboard clients via WebSocket (`/ws`). The scheduler already emits structured events at every state change — the WebSocket endpoint streams them to clients as JSON.

**Event types:**

| Event | Trigger | Dashboard effect |
|-------|---------|------------------|
| `venue_prices` | Price update job | Refreshes prices, blended, status |
| `positions` | Position sync job | Refreshes status |
| `portfolio_delta` | Delta check job | Refreshes global position |
| `alert` / `refill_alert` | Any alert | Refreshes alerts list |
| `system` | Pause/resume | Refreshes status, health |
| `account_balances` | Balance check job | Refreshes account balances |
| `arbitrage_opportunity` | Arb scan | Refreshes opportunities, arb status |
| `arbitrage_completed` | Arb execution | Refreshes opportunities, arb status |

The dashboard connects via a single WebSocket and invalidates the matching React Query cache keys on each event, eliminating the need for polling. Reconnection uses exponential backoff with jitter.

See [engine/ws.py](../engine/ws.py) for the server and [dashboard/lib/hooks/useEventStream.ts](../dashboard/lib/hooks/useEventStream.ts) for the client.

---

## 4. Data Storage

### Database Tables

| Table | Purpose |
|-------|---------|
| `system_state` | Key-value store (trading_enabled, etc.) |
| `price_snapshots` | Historical price data |
| `positions` | Venue position history |
| `actions` | All trading actions taken |
| `venue_config` | Per-venue configuration |
| `alerts` | System alerts and notifications |

---

## 5. API

### WebSocket
- `WS /ws` — Real-time event stream (prices, positions, alerts, arbitrage)

### REST Endpoints

| Group | Endpoint | Description |
|-------|----------|-------------|
| Status | `GET /api/status` | System status, uptime, venue prices |
| Status | `GET /api/health` | Health check |
| Prices | `GET /api/prices` | All venue prices |
| Prices | `GET /api/prices/blended` | VWAP/TWAP composite |
| Prices | `GET /api/prices/normalized` | cNGN/USD per venue |
| Positions | `GET /api/positions/global` | Aggregated portfolio |
| Trading | `POST /api/trading/pause` | Pause all trading |
| Trading | `POST /api/trading/resume` | Resume trading |
| Venues | `POST /api/venues/{v}/pause` | Pause venue |
| Venues | `PUT /api/venues/{v}/params` | Update params |
| Arbitrage | `GET /api/arbitrage/status` | Arb engine status |
| Arbitrage | `POST /api/arbitrage/scan` | Manual scan trigger |
| Accounts | `GET /api/accounts/balances` | All account balances |
| Alerts | `GET /api/alerts` | Recent alerts |

---

## 6. Security

### Server Hardening

- SSH on non-standard port (2222)
- Key-only authentication (no passwords)
- UFW firewall (only SSH + HTTPS)
- Fail2ban for brute-force protection
- Automatic security updates

### Key Management

All secrets are stored as environment variables in `.env` (loaded by Pydantic Settings). No separate keys file — keep it simple.

### Key Hierarchy

| Key | Purpose | Typical Funding |
|-----|---------|-----------------|
| Aerodrome LP | LP position management | ~$5k |
| Aerodrome Trade | Swaps for rebalancing | ~$2k |
| PancakeSwap LP | LP position management | ~$5k |
| PancakeSwap Trade | Swaps for rebalancing | ~$2k |
| Quidax API | CEX order management | N/A |
| Blockradar API | Rate setting | N/A |

---

## 7. Deployment

### Systemd Service

The trading engine runs as a systemd service with:
- Automatic restart on failure
- Restricted permissions (no root, limited filesystem access)
- Environment loaded from `.env` file

### Nginx Reverse Proxy

- HTTPS with Let's Encrypt certificates
- WebSocket proxying for `/ws` (Connection: Upgrade)
- Optional IP whitelisting for additional security

---

## 8. Configuration

Key configuration options (via `.env` file):

| Setting | Description | Default |
|---------|-------------|---------|
| `PRICE_UPDATE_INTERVAL` | Price fetch frequency | 30s |
| `DEX_CHECK_INTERVAL` | LP position check frequency | 120s |
| `CEX_SYNC_INTERVAL` | Order ladder sync frequency | 300s |
| `TARGET_DELTA_RATIO` | Target CNGN/total ratio | 0.5 |
| `REBALANCE_THRESHOLD_PERCENT` | Deviation before rebalancing | 5% |

---

## 9. Extensibility

### Adding a New DEX

**For price reading only** (no trading):

1. Create a `PoolReadConfig` with the pool address, RPC URL, token decimals, and `invert_price` if needed
2. Wire a `PoolPriceReader` into the aggregator in [engine/main.py](../engine/main.py)

**For full trading:**

1. Create a new adapter file extending `BaseDexAdapter` in [engine/venues/dex/base.py](../engine/venues/dex/base.py)
2. Provide contract ABIs and a `PoolConfig` with all addresses
3. Register in the venue factory

The shared base class handles all UniswapV3-style operations (mint, burn, swap, range calculation).

### Adding a New CEX

1. Create new adapter implementing position and order methods
2. Add webhook handler if the exchange supports it
3. Register in the venue factory

### Adding a New Trading Pair

1. Update venue configuration with new pool/market addresses
2. Add price feed source if needed
3. Update position tracking

---

## 10. Rollout Plan

### Phase 1: Read-Only
- Deploy with trading disabled
- Verify price feed accuracy
- Verify position tracking
- Dashboard verification

### Phase 2: Blockradar
- Enable rate syncing (lowest risk)
- Monitor for 1 week
- Verify rates stay accurate

### Phase 3: Quidax
- Enable order ladder sync
- Start with wider spreads
- Monitor fill rates and P&L

### Phase 4: DEXs
- Enable Aerodrome first, then PancakeSwap
- Start with conservative range settings
- Gradually tighten as confidence builds

### Phase 5: Cross-Venue
- Enable global rebalancing
- Enable arbitrage detection
- Start with manual approval, then automate