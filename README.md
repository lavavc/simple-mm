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

## Local Checks

```bash
source .venv/bin/activate
python -m mypy engine --no-error-summary
python -m pytest -x -q --ignore=tests/test_dex_fork.py
python -m pytest -q tests/test_dex_fork.py -v
```

- CI runs the same strict `mypy` check and the default pytest suite on pull requests and pushes to `main`.
- `mypy` covers `engine/`, the production Python package, and intentionally ignores `tests/` and `dashboard/` because test doubles are looser by design and frontend code can be checked by its own toolchain.
- The default pytest run covers the fast local suite and intentionally skips `tests/test_dex_fork.py`, because those tests need Foundry's `anvil` plus RPC-backed fork access.

---

## Dashboard

A Next.js dashboard for real-time monitoring.

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

**How to Run Backtester Module**
```
python3 -m backtester.run --csv data/Aerodrome\&Pancakeswap_HistoricalData.csv --pool both --walkforward
```
