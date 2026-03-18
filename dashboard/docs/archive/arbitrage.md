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
4. Estimates fees (swap fees read from chain, taker fees)
5. Filters opportunities meeting `min_net_profit_bps` threshold
6. Returns sorted by expected profit

### InventoryTracker

Manages risk through limits and circuit breakers:

- **Rolling 24h volume cap** - Stops trading after reaching limit (no midnight reset burst)
- **Inventory imbalance** - Prevents one-sided exposure
- **Daily loss limit** - Circuit breaker on losses (resets at midnight UTC)
- **Consecutive failures** - Circuit breaker after N failures

### ArbitrageExecutor

Executes detected opportunities across DEX and CEX venues:

- **DEX leg**: calls `venue.swap()` — synchronous, waits for on-chain confirmation
- **CEX leg**: calls `venue.place_market_order()` — market order for guaranteed fill at taker fee

Buy leg always executes first; sell leg uses the exact cNGN amount received. Either leg can be DEX or CEX depending on where the opportunity is.

## Configuration

### Environment Variables

```bash
# Enable/disable arbitrage scanning
ARBITRAGE_ENABLED=true

# Enable/disable live trading
ARBITRAGE_EXECUTION_ENABLED=false

# Scan interval in seconds
ARBITRAGE_SCAN_INTERVAL=30

# Thresholds
ARBITRAGE_MIN_SPREAD_BPS=150           # 1.5% minimum gross spread
ARBITRAGE_MIN_NET_PROFIT_BPS=50        # 0.5% minimum after fees
ARBITRAGE_MAX_SINGLE_TRADE_USD=100     # Fallback when pool reserves unavailable
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
    dex_swap_fee_bps: int = 30         # Fallback if on-chain fee() call fails
    cex_taker_fee_bps: int = 25        # CEX taker fee

    # Position limits
    max_single_trade_usd: Decimal = Decimal("100")   # Fallback when pool reserves unavailable
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
    "recommended_size_usd": 100,
    "timestamp": 1707235200000
  }
}
```

## Quidax: dual role

Quidax is used for two distinct and independent purposes:

| Role | Method | Order type | When |
|------|--------|-----------|------|
| **Liquidity provision** | `sync_order_ladder()` | Limit orders across a price range | Scheduled — keeps the book filled |
| **Arb execution** | `place_market_order()` | Market order | On-demand — captures a detected spread |

These never interfere: the order ladder runs on its own schedule, and arb market orders hit the best available price immediately regardless of what ladder orders are resting in the book.

## Cross-Chain DEX Arbitrage

As the cNGN issuer with permanent inventory on both Base (Aerodrome) and BSC (PancakeSwap), the two legs of a DEX↔DEX trade are **independent** — no bridge required:

- **Buy leg (Base):** Spend USDC, receive cNGN on Aerodrome
- **Sell leg (BSC):** Spend cNGN, receive USDT on PancakeSwap

Global cNGN delta = zero (bought on one chain, sold on the other). Profit is the USD spread: `(sell_price − buy_price) × amount_cNGN`.

### Fee model

| Component | Cost |
|-----------|------|
| DEX swap fees (both legs) | varies — read from pool on startup (PancakeSwap ≈ 100 bps, Aerodrome 5–30 bps) |
| Inventory-weighted rebalance cost | 0–10 bps |

The rebalance cost scales linearly from 0 bps (fully stocked) to `cross_chain_rebalance_bps` (10 bps, empty). With PancakeSwap's 100 bps pool fee, you need well over 150 bps gross spread to profit — see [when-to-arb.md](when-to-arb.md).

This is a fair proxy when trying to account for the cost to bridge per unit of inventory. There is a fixed gas cost for bridging, and we ideally want to do it in big batches, infrequently. The fee model here imposes no penalty when inventory is balanced, but scales the bps spread we need to be proitable as our inventory gets to levels where a rebalance and bridge event will be required.

### Per-account stablecoin tracking

`InventoryTracker` estimates stablecoin balances per venue after each trade leg. When a buy-side venue balance falls below `min_account_stablecoin_usd` ($1,000), that direction is automatically paused and surfaced in `low_inventory_venues` on the status response.

## Fee Estimation

The detector estimates total fees for each venue pair:

| Venue Type | Fee Components |
|------------|---------------|
| DEX (buy or sell) | Pool swap fee read from chain at startup (`dex_swap_fee_bps` = 30 bps fallback) |
| CEX (buy or sell) | `cex_taker_fee_bps` = 25 bps |
| Cross-chain DEX↔DEX | +0–10 bps rebalance cost (inventory-weighted) |
| Reference | 0 (benchmark only, not tradeable) |

Slippage is not modelled separately — it is captured in trade sizing via the constant-product optimal formula. See [when-to-arb.md](when-to-arb.md).

**Example**: Aerodrome (5 bps pool) → Quidax = 5 + 25 = 30 bps total fees. An opportunity with 150 bps gross spread would yield ~120 bps net.

## The Numeraire

What should the numeraire be in this system?

Let's consider a DEX <> DEX arb trade again:

- Leg 1 (Base): Spend USDC, receive cNGN → stablecoin down on Base, cNGN up on Base
- Leg 2 (BSC): Spend cNGN, receive USDT → cNGN down on BSC, stablecoin up on BSC

Global cNGN delta = zero (bought Y on one chain, sold Y on another). We've just converted USDC on Base into USDT on BSC at a favourable rate. The profit is naturally USD-denominated:

So the numeraire in our system is USD because it matches what we actually earn — a stablecoin surplus.

However, as the issuer, there's a deeper layer: cNGN we buy back is a liability we've extinguished at a discount. **It is worth considering how to account for this long-term**.

Since global cNGN delta is zero per trade, this cross-DEX arb type never changes the cNGN/stable ratio globally. It only changes per-chain distribution. The 60% limit therefore only. The real risk to track is per-chain stablecoin exhaustion and the cross-chain rebalancing cost when BSC is full of USDT but Base is short of USDC.
