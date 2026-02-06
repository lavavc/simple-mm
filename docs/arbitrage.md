# Arbitrage Detection System

Cross-venue arbitrage detection for the CNGN trading system. Monitors price divergences between DEX pools, CEX orderbooks, and reference rates to identify profitable opportunities.

## Architecture

```
engine/core/arbitrage/
├── __init__.py       # Module exports
├── engine.py         # ArbitrageEngine - main orchestrator
├── detector.py       # PriceNormalizer + ArbitrageDetector
├── executor.py       # ArbitrageExecutor (stub for Phase 1)
└── inventory.py      # InventoryTracker - risk management
```

## Components

### PriceNormalizer

Converts prices from different venues to a common basis: **cNGN/USD** (how many USD per 1 cNGN).

| Source | Raw Format | Conversion |
|--------|-----------|------------|
| Bybit P2P (reference) | USDT/NGN (e.g., 1650) | `1 / rate` → ~0.000606 |
| Aerodrome DEX | cNGN/USDC pool price | Direct (USDC ≈ USD) |
| Quidax CEX | cNGN/USDT orderbook | Direct (USDT ≈ USD) |

### ArbitrageDetector

Scans all venue pairs for price divergences:

1. Fetches normalized prices from all venues
2. Compares each pair (both directions)
3. Calculates gross spread in basis points
4. Estimates fees (swap fees, slippage, taker fees)
5. Filters opportunities meeting `min_net_profit_bps` threshold
6. Returns sorted by expected profit

### InventoryTracker

Manages risk through limits and circuit breakers:

- **Daily volume cap** - Stops trading after reaching limit
- **Inventory imbalance** - Prevents one-sided exposure
- **Daily loss limit** - Circuit breaker on losses
- **Consecutive failures** - Circuit breaker after N failures
- Resets daily at midnight UTC

### ArbitrageExecutor

**Phase 1 (Current)**: Detection-only mode. Logs opportunities but does not execute trades.

**Phase 2 (Future)**: DEX swap execution with slippage protection.

**Phase 3 (Future)**: Full cross-venue execution with CEX orders.

## Configuration

### Environment Variables

```bash
# Enable/disable arbitrage scanning
ARBITRAGE_ENABLED=true

# Phase 1: detection only (no trades)
ARBITRAGE_EXECUTION_ENABLED=false

# Scan interval in seconds
ARBITRAGE_SCAN_INTERVAL=30

# Thresholds
ARBITRAGE_MIN_SPREAD_BPS=150           # 1.5% minimum gross spread
ARBITRAGE_MIN_NET_PROFIT_BPS=50        # 0.5% minimum after fees
ARBITRAGE_MAX_SINGLE_TRADE_USD=1000    # Max per opportunity
ARBITRAGE_MAX_DAILY_VOLUME_USD=10000   # Daily volume cap
ARBITRAGE_MAX_INVENTORY_IMBALANCE_USD=5000  # Max one-sided exposure
```

### ArbitrageParams

Full parameter model (can be updated via API):

```python
class ArbitrageParams(BaseModel):
    # Detection thresholds
    min_spread_bps: int = 150          # 1.5% minimum gross spread
    min_net_profit_bps: int = 50       # 0.5% minimum after fees

    # Fee estimates (basis points)
    dex_swap_fee_bps: int = 30         # DEX swap fee
    dex_slippage_bps: int = 20         # Expected slippage
    cex_taker_fee_bps: int = 25        # CEX taker fee

    # Position limits
    max_single_trade_usd: Decimal = Decimal("1000")
    max_daily_volume_usd: Decimal = Decimal("10000")
    max_inventory_imbalance_usd: Decimal = Decimal("5000")

    # Timing
    scan_interval_seconds: int = 30

    # Circuit breakers
    max_consecutive_failures: int = 3
    max_daily_loss_usd: Decimal = Decimal("500")
```

## API Endpoints

### Status & Monitoring

```bash
# Get arbitrage engine status
GET /api/arbitrage/status

# List detected opportunities
GET /api/arbitrage/opportunities
GET /api/arbitrage/opportunities?status=detected&limit=50

# Get specific opportunity
GET /api/arbitrage/opportunities/{opportunity_id}
```

### Control

```bash
# Enable/disable scanning (requires auth token)
POST /api/arbitrage/enable
POST /api/arbitrage/disable

# Update parameters
PUT /api/arbitrage/params
Content-Type: application/json
{
  "min_spread_bps": 200,
  "max_single_trade_usd": "500"
}

# Manual scan trigger
POST /api/arbitrage/scan

# Reset circuit breaker
POST /api/arbitrage/reset-circuit-breaker
```

## Database Schema

### arbitrage_opportunities

```sql
CREATE TABLE arbitrage_opportunities (
    id TEXT PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    buy_venue TEXT NOT NULL,
    sell_venue TEXT NOT NULL,
    buy_price REAL NOT NULL,
    sell_price REAL NOT NULL,
    gross_spread_bps INTEGER NOT NULL,
    net_spread_bps INTEGER NOT NULL,
    recommended_size_usd REAL NOT NULL,
    expected_profit_usd REAL NOT NULL,
    status TEXT NOT NULL,  -- 'detected', 'executing', 'completed', 'abandoned', 'expired'
    actual_profit_usd REAL,
    reason TEXT
);
```

### arbitrage_trades

```sql
CREATE TABLE arbitrage_trades (
    id INTEGER PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    side TEXT NOT NULL,  -- 'buy', 'sell'
    amount REAL NOT NULL,
    price REAL,
    tx_hash TEXT,
    status TEXT NOT NULL,  -- 'pending', 'submitted', 'confirmed', 'failed'
    timestamp INTEGER NOT NULL,
    error TEXT
);
```

## WebSocket Events

Opportunities are broadcast to connected clients:

```json
{
  "type": "arbitrage_opportunity",
  "data": {
    "id": "uuid",
    "buy_venue": "aerodrome",
    "sell_venue": "reference",
    "gross_spread_bps": 175,
    "net_spread_bps": 85,
    "expected_profit_usd": 8.50,
    "recommended_size_usd": 1000,
    "timestamp": 1707235200000
  }
}
```

## Implementation Phases

### Phase 1: Detection Only (Current)

- Full detection infrastructure
- Opportunities logged to database
- WebSocket broadcasts
- No actual trades executed
- Run for 1-2 weeks to validate accuracy

### Phase 2: DEX Execution (Future)

- Swap execution on Aerodrome
- Slippage protection via `min_amount_out`
- Start with small amounts (~$100)
- Requires `ARBITRAGE_EXECUTION_ENABLED=true`

### Phase 3: Full Cross-Venue (Future)

- CEX order placement on Quidax
- State machine for pending fills
- Sequential execution (buy leg → wait → sell leg)
- Full circuit breaker integration

## Fee Estimation

The detector estimates total fees for each venue pair:

| Venue Type | Fee Components |
|------------|---------------|
| DEX (buy or sell) | `dex_swap_fee_bps` + `dex_slippage_bps` = 50 bps |
| CEX (buy or sell) | `cex_taker_fee_bps` = 25 bps |
| Reference | 0 (benchmark only, not tradeable) |

**Example**: DEX → CEX arbitrage = 50 + 25 = 75 bps total fees

An opportunity with 150 bps gross spread would have ~75 bps net profit.

## Validation Checklist

Before enabling execution (Phase 2+):

- [ ] Detection running for 1-2 weeks without issues
- [ ] Opportunities correlate with manual spot checks
- [ ] Fee estimates align with actual on-chain/CEX costs
- [ ] False positive rate acceptable
- [ ] Spread distribution understood (typical size, frequency)
- [ ] Circuit breakers tested
