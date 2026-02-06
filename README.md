# CNGN Trading Engine

Automated market-making engine for CNGN stablecoin across DEXs, CEXs, and wallet systems.

## Getting Started

### Prerequisites

- Python 3.11+
- Access to Base/BSC RPC endpoints
- API keys for venues (Quidax, Blockradar, etc.)

### Quick Start

```bash
cd cngn

# Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env — at minimum set QUIDAX_API_KEY for live CEX prices

# Run the engine
python -m engine.main
```

The API will be at `http://localhost:8000/api/`, interactive docs at `http://localhost:8000/docs`.

DEX price reads work out of the box — they only need public RPC endpoints (pre-configured). Private keys / wallet mnemonic are only required for trading (LP minting, swaps).

### Running Tests

```bash
pytest
pytest --cov=engine  # with coverage
```

---

## Arbitrage Detection

The engine includes cross-venue arbitrage detection that monitors price divergences between:
- **DEX pools** — Aerodrome (Base), PancakeSwap (BSC)
- **CEX orderbooks** — Quidax (cNGN/USDT)
- **Reference rates** — Bybit P2P (USDT/NGN)

**Current status**: Detection-only mode (Phase 1). Opportunities are logged and broadcast but not executed.

### API Endpoints

```bash
GET  /api/arbitrage/status              # Engine status
GET  /api/arbitrage/opportunities       # List opportunities
POST /api/arbitrage/scan                # Manual scan trigger
POST /api/arbitrage/enable              # Enable scanning
POST /api/arbitrage/disable             # Disable scanning
PUT  /api/arbitrage/params              # Update parameters
```

See [docs/ARBITRAGE.md](docs/ARBITRAGE.md) for full documentation.

---

## Account Management

The engine uses **HD wallet derivation** to generate trading accounts from a single seed phrase. Each venue/role gets a dedicated account for clear audit trails.

### Account Roles

| Role | Purpose | Tokens |
|------|---------|--------|
| `aerodrome-lp` | DEX liquidity provision | cNGN, USDC |
| `aerodrome-trade` | Arbitrage swaps | cNGN, USDC |
| `blockradar` | Wallet system funding | cNGN, USDT, USDC |
| `quidax` | CEX deposits | cNGN, USDT |

### Balance Monitoring

The system monitors account balances and creates alerts when refills are needed:

```bash
GET  /api/accounts                    # List all accounts
GET  /api/accounts/balances           # Check all balances
GET  /api/accounts/{role}/balance     # Check specific account
PUT  /api/accounts/{role}/thresholds  # Update refill thresholds
```

**Treasury is managed off-server.** When alerts trigger, manually transfer funds from your cold treasury to the account address shown.

See [docs/ACCOUNTS.md](docs/ACCOUNTS.md) for full documentation.

---

## Dashboard

A Next.js dashboard for real-time monitoring and control.

### Running the Dashboard

```bash
cd dashboard

# Install dependencies
npm install

# Development mode (with hot reload)
npm run dev

# Production build
npm run build
npm start
```

The dashboard will be available at `http://localhost:3000`.

### Configuration

Create `dashboard/.env.local`:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000/api
NEXT_PUBLIC_WS_URL=ws://localhost:8000/ws
NEXT_PUBLIC_API_TOKEN=your-dashboard-token
```

### Features

- **Real-time streaming**: WebSocket connection pushes all updates instantly — no polling
- **System Status**: Trading state, uptime, venue health
- **Price Feed**: Live venue prices, blended VWAP/TWAP, cross-venue comparison
- **Venues**: Position details, LP status, parameter management
- **Arbitrage**: Opportunity detection, statistics, parameter tuning
- **Accounts**: HD wallet balances, refill alerts, threshold management
- **Alerts**: Notification management and acknowledgment

---

## DEX LP Strategy

Capital allocation is controlled through `DexParams`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_utilization_percent` | 80% | Maximum percentage of wallet balance to deploy |
| `min_reserve_token0` | 0 | Minimum cNGN to keep in wallet (not deployed) |
| `min_reserve_token1` | 0 | Minimum stablecoin to keep in wallet |
| `max_position_usd` | None | Hard cap on total position value in USD |

**Example configurations:**

```python
# Conservative: Keep significant reserves
DexParams(
    max_utilization_percent=Decimal("70"),
    min_reserve_token0=Decimal("50000"),   # Keep 50k cNGN
    min_reserve_token1=Decimal("100"),      # Keep $100 USDC
    max_position_usd=Decimal("10000"),      # Never deploy more than $10k
)

# Aggressive: Deploy most capital
DexParams(
    max_utilization_percent=Decimal("95"),
    min_reserve_token0=Decimal("1000"),     # Keep 1k cNGN for gas/emergencies
    min_reserve_token1=Decimal("10"),       # Keep $10 USDC
)
```

**How allocation is calculated:**

1. Start with wallet balance for each token
2. Apply `max_utilization_percent` cap (e.g., 80% of balance)
3. Subtract `min_reserve_tokenX` from each token's available amount
4. If `max_position_usd` is set, scale down proportionally to stay under cap

The `calculate_mint_amounts()` method returns the final amounts in raw token units ready for the mint transaction.

### Range Calculation

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `sd_multiplier` | Decimal | 1.5 | Standard deviations for range width |
| `min_tick_width` | int | 100 | Minimum tick range (prevents too-narrow positions) |
| `max_tick_width` | int | 1000 | Maximum tick range (prevents too-wide positions) |
| `lookback_points` | int | None | Limit price history for SD calculation |
| `rebalance_threshold_percent` | Decimal | 5.0 | % out of range before rebalancing |
| `max_slippage_percent` | Decimal | 1.0 | Max slippage for swaps |
