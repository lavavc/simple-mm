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
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐            │
│  │   SQLite     │  │  encrypted   │  │    cache/    │            │
│  │   (state)    │  │  keys.json   │  │ (price data) │            │
│  └──────────────┘  └──────────────┘  └──────────────┘            │
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
| **Dashboard** | Next.js (static export) | React DX, static = simple serving |
| **Logging** | structlog | Structured logging, Python-native |

---

## 3. Core Components

### 3.1 Price Feed

Aggregates USDT/NGN price from Bybit P2P with fraud filtering:

- **Skip first N ads** (typically 5) - often fraudulent "bait" prices (this to be checked with Azza, HoneyCoin etc)
- **Reputation filter** - require 100+ completed orders, 95%+ completion rate
- **Outlier rejection** - remove prices >2% from median
- **VWAP calculation** - volume-weighted average of remaining ads

The price feed refreshes every 30 seconds and stores snapshots for historical analysis.

### 3.2 DEX Position Management

Both Aerodrome (Base) and PancakeSwap (BSC) use concentrated liquidity (UniswapV3-style). The system:

1. **Monitors positions** - checks if liquidity is still in range
2. **Calculates optimal range** - uses standard deviation of recent prices
3. **Rebalances when needed** - removes old position, creates new one with updated range

The SD-based range calculation ensures positions adapt to market volatility.

### 3.3 CEX Order Ladder (Quidax)

Maintains buy and sell orders across price levels:

- **Ladder levels** - configurable number of price increments (default: 10)
- **Spread per level** - price increment in NGN (default: 1.0)
- **Liquidity distribution** - percentage of balance at each level

Orders are cancelled and recreated when the reference price changes significantly.

### 3.4 Wallet Rate Sync (Blockradar)

Sets B2C swap rates for the wallet system:

- Calculates buy/sell rates from reference price ± spread
- Supports both CNGN/USDT and CNGN/USDC pairs
- Configurable spread in basis points (default: 15 bps)

### 3.5 Scheduler

Orchestrates all automated tasks:

| Task | Default Interval | Description |
|------|------------------|-------------|
| Price update | 30s | Fetch and broadcast price |
| Position sync | 60s | Fetch positions from all venues |
| DEX rebalance check | 120s | Check if LP needs rebalancing |
| CEX order sync | 300s | Sync Quidax order ladder |
| Rate sync | 300s | Sync Blockradar swap rates |

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

## 5. API Endpoints

### System Control
- `GET /api/status` - System status and uptime
- `POST /api/trading/pause` - Pause all trading
- `POST /api/trading/resume` - Resume trading

### Price
- `GET /api/price` - Current price
- `GET /api/price/history` - Historical prices

### Positions
- `GET /api/positions` - All venue positions
- `GET /api/positions/global` - Aggregated portfolio view

### Venue Control
- `POST /api/venues/{venue}/pause` - Pause specific venue
- `POST /api/venues/{venue}/resume` - Resume specific venue
- `PUT /api/venues/{venue}/params` - Update venue parameters
- `POST /api/venues/{venue}/sync` - Force immediate sync

### Alerts
- `GET /api/alerts` - Recent alerts
- `POST /api/alerts/{id}/acknowledge` - Acknowledge alert

---

## 6. Security

### Server Hardening

- SSH on non-standard port (2222)
- Key-only authentication (no passwords)
- UFW firewall (only SSH + HTTPS)
- Fail2ban for brute-force protection
- Automatic security updates

### Key Management

All private keys and API secrets are stored encrypted using:
- PBKDF2 key derivation (480,000 iterations)
- Fernet symmetric encryption
- File permissions restricted to owner only

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
- WebSocket support for real-time updates
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

1. Create new adapter file extending the shared DEX base class
2. Provide contract ABIs and pool configuration
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
