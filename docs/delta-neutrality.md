# Delta Neutrality Strategy

This document describes how the CNGN trading system defines and maintains delta neutrality across all trading venues.

---

## Table of Contents

1. [Definition](#1-definition)
2. [How Delta Is Calculated](#2-how-delta-is-calculated)
3. [Configuration Parameters](#3-configuration-parameters)
4. [Mechanisms for Maintaining Delta Neutrality](#4-mechanisms-for-maintaining-delta-neutrality)
5. [Current Limitations](#5-current-limitations)
6. [Recommendations](#6-recommendations)
7. [USD vs NGN: Calculation Basis and Tradeoffs](#7-usd-vs-ngn-calculation-basis-and-tradeoffs)
8. [Alternative Delta Strategies](#8-alternative-delta-strategies)
9. [Liquidity, Solvency, and Profitability](#9-liquidity-solvency-and-profitability)
10. [Data Flow Diagram](#10-data-flow-diagram)
11. [Related Files](#11-related-files)
12. [Testing Considerations](#12-testing-considerations)

---

## 1. Definition

**Delta neutrality** in this system means maintaining a target balance between cNGN token holdings and stablecoin (USDC/USDT) holdings by USD value.

```
delta_ratio = cNGN_usd_value / total_portfolio_usd_value
target_delta = 0.5 (50% cNGN, 50% stablecoins)
```

A delta ratio of 0.5 means the portfolio holds equal USD-value in cNGN and stablecoins. This minimizes directional exposure to cNGN price movements while capturing trading profits from spreads and arbitrage.

---

## 2. How Delta Is Calculated

### 2.1 Portfolio-Level Delta

**File**: `engine/core/scheduler.py` (`_check_portfolio_delta` method)

```python
# Aggregate positions across all venues
total_cngn = sum(venue.get_position().balances.get("cngn", 0) for venue in venues)
total_usdt = sum(venue.get_position().balances.get("usdt", 0) for venue in venues)
total_usdc = sum(venue.get_position().balances.get("usdc", 0) for venue in venues)

# Convert cNGN to USD value using blended VWAP
cngn_usd_value = total_cngn * blended_vwap  # e.g., 100,000 cNGN * 0.0006 = $60

# Calculate delta ratio
total_stable_usd = total_usdt + total_usdc
total_usd_value = cngn_usd_value + total_stable_usd
delta_ratio = cngn_usd_value / total_usd_value
```

### 2.2 Arbitrage-Specific Inventory

**File**: `engine/core/arbitrage/inventory.py`

The arbitrage engine tracks its own inventory imbalance separately:

```python
@dataclass
class InventoryState:
    cngn_imbalance_usd: Decimal = Decimal("0")  # Positive = net long cNGN from arb trades
    daily_volume_usd: Decimal = Decimal("0")
    daily_profit_usd: Decimal = Decimal("0")
```

This tracks the net directional exposure from arbitrage trades specifically, independent of the overall portfolio.

### 2.3 API Response

**Endpoint**: `GET /api/positions/global`

```json
{
  "total_cngn": 825000,
  "total_usdt": 420,
  "total_usdc": 225.5,
  "total_usd_value": 1145.5,
  "delta_ratio": 0.48,
  "target_delta": 0.5
}
```

---

## 3. Configuration Parameters

| Parameter | Default | Location | Purpose |
|-----------|---------|----------|---------|
| `target_delta_ratio` | 0.5 | `config.py` | Target 50/50 cNGN/USD split |
| `delta_alert_threshold_percent` | 10.0 | `config.py` | Alert if deviation > 10% |
| `portfolio_delta_interval` | 120s | `config.py` | Check delta every 2 minutes |
| `venue_divergence_rebalance_bps` | 200 | `SchedulerConfig` | Rebalance DEX if venue price diverges 2% from fair value |

**Arbitrage-specific limits:**

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `max_inventory_imbalance_usd` | $5,000 | Max one-sided exposure from arb trades |
| `max_single_trade_usd` | $1,000 | Max per arbitrage opportunity |
| `max_daily_volume_usd` | $10,000 | Max daily arbitrage volume |
| `max_daily_loss_usd` | $500 | Circuit breaker: stop if losses exceed |
| `max_consecutive_failures` | 3 | Circuit breaker: stop after N failures |

---

## 4. Mechanisms for Maintaining Delta Neutrality

### 4.1 DEX LP Position Rebalancing

**Trigger conditions** (either triggers rebalancing):
1. LP position goes out of tick range (price moved beyond position bounds)
2. Venue price diverges more than 200 bps from blended fair value

**Action**: Remove existing LP position and create a new one with updated tick range.

```python
# engine/core/scheduler.py
async def _check_dex_rebalance(self):
    for venue in dex_venues:
        position = venue.get_position_state(token_id)

        # Trigger 1: Out of range
        if not position.in_range:
            await self._rebalance_dex_position(venue, token_id)

        # Trigger 2: Price divergence
        venue_price = venue.get_current_price()
        divergence_bps = abs(venue_price - blended_vwap) / blended_vwap * 10000
        if divergence_bps > 200:
            await self._rebalance_dex_position(venue, token_id)
```

**Tick range calculation** uses historical price volatility:

```python
# Range = mean ± (SD * multiplier)
lower_price = mean_price - (std_dev * 1.5)
upper_price = mean_price + (std_dev * 1.5)
```

### 4.2 Capital Allocation Controls

**File**: `engine/venues/dex/base.py` (`calculate_mint_amounts`)

When creating LP positions, three constraints apply:

```python
class DexParams:
    max_utilization_percent: Decimal = Decimal("80.0")  # Max % of balance to deploy
    min_reserve_token0: Decimal = Decimal("0")           # Min cNGN to keep in wallet
    min_reserve_token1: Decimal = Decimal("0")           # Min USDC to keep in wallet
    max_position_usd: Optional[Decimal] = None           # Hard cap on position size
```

The system applies these sequentially:
1. Apply utilization cap (80% of available balance)
2. Subtract minimum reserves
3. Apply USD hard cap if configured

### 4.3 Arbitrage Inventory Limits

**File**: `engine/core/arbitrage/inventory.py`

Before executing any arbitrage trade:

```python
def can_trade(self, trade_size_usd) -> tuple[bool, str | None]:
    # Check 1: Circuit breaker not active
    if self._state.circuit_breaker_active:
        return False, "Circuit breaker active"

    # Check 2: Daily volume limit
    if self._state.daily_volume_usd + trade_size_usd > self.params.max_daily_volume_usd:
        return False, "Would exceed daily volume limit"

    # Check 3: Inventory imbalance limit
    potential_imbalance = abs(self._state.cngn_imbalance_usd) + trade_size_usd
    if potential_imbalance > self.params.max_inventory_imbalance_usd:
        return False, "Would exceed inventory imbalance limit"

    # Check 4: Daily loss limit
    if self._state.daily_loss_usd >= self.params.max_daily_loss_usd:
        return False, "Daily loss limit reached"

    return True, None
```

### 4.4 Portfolio Delta Monitoring

**File**: `engine/core/scheduler.py` (`_check_portfolio_delta`)

Runs every 2 minutes to:
1. Aggregate positions across all venues
2. Calculate current delta ratio
3. Compare to target (0.5)
4. If deviation > 10%, create alert and broadcast to dashboard

```python
deviation_percent = abs(delta_ratio - target) / target * 100
if deviation_percent > self.config.delta_alert_threshold_percent:
    await db.insert_alert(
        severity="warning",
        category="delta",
        message=f"Portfolio delta {delta_ratio:.1%} deviates {deviation_percent:.1f}% from target"
    )
```

---

## 5. Current Limitations

### 5.1 No Active Portfolio Rebalancing

**Gap**: The system monitors delta and creates alerts, but has no mechanism to actively trade to rebalance the portfolio back to target.

**Current behavior**: If portfolio becomes 60% cNGN / 40% stables, an alert is raised but no trades execute.

**Workaround**: Rely on arbitrage trades to gradually correct imbalance (if arb opportunities favor the rebalancing direction).

### 5.2 DEX Rebalancing Is Not Delta-Aware

**Gap**: DEX LP position rebalancing triggers on tick range or venue divergence, not on portfolio delta deviation.

**Current behavior**: Even if portfolio is severely overweight cNGN, DEX rebalancing won't trigger unless the LP position goes out of range.

### 5.3 Arbitrage Inventory Tracking Is Approximate

**Gap**: The `cngn_imbalance_usd` in arbitrage inventory is a rough estimate, not a precise tracking of cNGN quantities traded.

**Current behavior**: System tracks directional exposure from arb trades, but the calculation depends on `cngn_delta` parameter being passed correctly by the executor (which is not yet implemented).

### 5.4 Capital Allocation Defaults Are Permissive

**Gap**: Default settings allow deploying up to 80% of capital with no minimum reserves.

**Risk**: Could leave insufficient capital for rebalancing trades or gas.

### 5.5 Daily Reset Discontinuity

**Gap**: Arbitrage inventory imbalance resets at midnight UTC.

**Risk**: If system accumulates $4,500 imbalance at 11:50 PM, it allows another $500. At midnight, resets to $0, allowing $5,000 more—effectively $5,500 in 20 minutes.

---

## 6. Recommendations

### 6.1 Implement Active Portfolio Rebalancing

Add a new method to execute rebalancing trades when delta deviates significantly:

```python
async def _rebalance_portfolio_delta(self):
    """Execute trades to bring portfolio back to target delta."""
    delta_ratio = await self._calculate_portfolio_delta()
    deviation = delta_ratio - self.config.target_delta_ratio

    if abs(deviation) < Decimal("0.05"):  # Within 5%, no action
        return

    if deviation > 0:  # Overweight cNGN, sell cNGN for stables
        rebalance_amount = deviation * total_usd_value
        await self._execute_rebalance_sell(rebalance_amount)
    else:  # Underweight cNGN, buy cNGN with stables
        rebalance_amount = abs(deviation) * total_usd_value
        await self._execute_rebalance_buy(rebalance_amount)
```

### 6.2 Make DEX Rebalancing Delta-Aware

Extend DEX rebalancing triggers to include portfolio delta:

```python
# Additional trigger: Portfolio delta deviation
portfolio_delta = await self._calculate_portfolio_delta()
if abs(portfolio_delta - target) > Decimal("0.15"):  # 15% deviation
    needs_rebalance = True
    rebalance_reason = "portfolio_delta_deviation"
```

### 6.3 Conservative Default Configuration

Recommend changing defaults:

```python
# More conservative capital allocation
max_utilization_percent: Decimal = Decimal("60.0")  # Down from 80%
min_reserve_token0: Decimal = Decimal("10000")       # Keep 10k cNGN
min_reserve_token1: Decimal = Decimal("50")          # Keep $50 stables
max_position_usd: Decimal = Decimal("500")           # Start small
```

### 6.4 Rolling Inventory Window

Replace daily reset with rolling 24-hour window:

```python
def _get_rolling_imbalance(self) -> Decimal:
    """Calculate imbalance over rolling 24-hour window."""
    cutoff = time.time() - 86400
    recent_trades = [t for t in self._trades if t.timestamp > cutoff]
    return sum(t.cngn_delta for t in recent_trades)
```

### 6.5 Position Sizing by Delta

Adjust LP position size based on current delta state:

```python
def calculate_mint_amounts(self, reference_price_usd, current_delta: Decimal):
    """Adjust position based on delta deviation."""
    base_utilization = self.params.max_utilization_percent

    # If overweight cNGN, deploy less cNGN and more stables
    if current_delta > Decimal("0.55"):
        token0_multiplier = Decimal("0.7")  # Reduce cNGN by 30%
        token1_multiplier = Decimal("1.3")  # Increase stables by 30%
    elif current_delta < Decimal("0.45"):
        token0_multiplier = Decimal("1.3")  # Increase cNGN
        token1_multiplier = Decimal("0.7")  # Reduce stables
    else:
        token0_multiplier = token1_multiplier = Decimal("1.0")
```

---

## 7. USD vs NGN: Calculation Basis and Tradeoffs

### 7.1 Current Implementation

The system uses **two different normalizations** depending on context:

| Component | Normalization | Example Value | Used For |
|-----------|---------------|---------------|----------|
| Engine (`price_aggregation.py`) | cNGN/USD | 0.000606 | Arbitrage detection, delta calculation, VWAP |
| Dashboard (`utils.ts`) | NGN/USD | 1650 | Human-readable display |

**Engine normalization (cNGN/USD):**
```python
# engine/core/price_aggregation.py
# All venues normalized to: "how many USD per 1 cNGN"

USDT_NGN (Bybit): cNGN/USD = 1 / 1650 = 0.000606
cNGN/USDC (Aerodrome): cNGN/USD = 0.000606 (direct)
cNGN/NGN (Blockradar): cNGN/USD = 1.0 / 1650 = 0.000606 (cross-rate)
```

**Dashboard normalization (NGN/USD):**
```typescript
// dashboard/lib/utils.ts
// All venues normalized to: "how many NGN per 1 USD"

USDT/NGN (Bybit): 1650 (direct)
cNGN/USDC (Aerodrome): 1 / 0.000606 = 1650 (inverted)
```

### 7.2 Venue Data Formats

| Venue | Raw Pair | Raw Example | Meaning |
|-------|----------|-------------|---------|
| Bybit P2P | USDT/NGN | 1650 | "1 USDT costs 1650 NGN" |
| Quidax | cNGN/USDT | 0.000606 | "1 cNGN costs 0.000606 USDT" |
| Aerodrome | cNGN/USDC | 0.000606 | "1 cNGN costs 0.000606 USDC" |
| PancakeSwap | cNGN/USDT | 0.000610 | "1 cNGN costs 0.000610 USDT" |
| Blockradar | cNGN/NGN | 1.0 | "1 cNGN = 1 NGN" (peg rate) |

### 7.3 Tradeoffs

**USD-based calculation (current engine approach):**

| Pros | Cons |
|------|------|
| Standard numeraire for risk limits | Small decimals (0.0006) prone to precision errors |
| Easier cross-venue comparison | Less intuitive for Nigerian operators |
| Consistent with DeFi conventions | Requires inversion for NGN-quoted venues |
| Risk limits naturally in USD | Blockradar cNGN/NGN needs cross-rate |

**NGN-based calculation (alternative):**

| Pros | Cons |
|------|------|
| Matches Bybit P2P format directly | NGN itself is volatile |
| Large numbers easier to reason about | Risk limits would drift with NGN/USD |
| More intuitive for local operators | Inconsistent with stablecoin venues |
| No inversion needed for P2P/CEX | Cross-venue spreads harder to compare |

### 7.4 Recommendation

**Keep USD as the primary calculation basis** because:

1. **Risk limits must be stable**: A $5,000 inventory limit means the same thing regardless of NGN movements
2. **Stablecoins are the hedge**: Our stable leg is USDC/USDT, not NGN
3. **Cross-venue comparison**: DEX pools quote in USD terms already
4. **Industry standard**: Most trading systems use USD as numeraire

**However, improve precision handling:**

```python
# Use quantize() for consistent decimal places
cngn_usd = (Decimal("1") / usdt_ngn_rate).quantize(Decimal("0.00000001"))
```

### 7.5 Blockradar Cross-Rate Dependency

Blockradar quotes cNGN/NGN (the peg rate, ~1.0). To normalize this to USD:

```
cNGN/USD = blockradar_mid / usdt_ngn_rate
```

**Risk**: If the USDT/NGN source (Bybit) is stale or unavailable, Blockradar prices cannot be normalized.

**Mitigation**: Track Blockradar peg health separately. A deviation from 1.0 indicates cNGN depegging from NGN, which is useful information regardless of USD conversion.

---

## 8. Alternative Delta Strategies

Beyond the simple 50/50 static delta target, consider these more sophisticated approaches:

### 8.1 Dynamic Delta Targeting

Instead of a fixed 50% target, adjust based on market conditions:

**Momentum-Based Delta:**
```python
# If cNGN trending up, allow slight overweight
if twap_1h > twap_24h * 1.02:  # 2% uptrend
    target_delta = Decimal("0.55")  # Allow 55% cNGN
elif twap_1h < twap_24h * 0.98:  # 2% downtrend
    target_delta = Decimal("0.45")  # Reduce to 45% cNGN
else:
    target_delta = Decimal("0.50")
```

**Volatility-Adjusted Bands:**
```python
# Widen tolerance during high volatility
price_std = calculate_std(prices_24h)
if price_std > normal_std * 1.5:  # High volatility
    delta_tolerance = Decimal("0.15")  # Allow 35-65%
else:
    delta_tolerance = Decimal("0.10")  # Normal 40-60%
```

### 8.2 Per-Venue Delta

Instead of global delta, maintain balance per venue:

```python
@dataclass
class VenueDelta:
    venue: str
    target_ratio: Decimal
    current_ratio: Decimal
    tolerance: Decimal

# Example: Keep Quidax more liquid for active trading
venue_targets = {
    "quidax": Decimal("0.4"),     # 40% cNGN (more stables for fills)
    "aerodrome": Decimal("0.5"),  # 50% (balanced LP)
    "blockradar": Decimal("0.6"), # 60% cNGN (B2C mostly sells cNGN)
}
```

### 8.3 Inventory Aging

Track how long positions have been held and force liquidation of stale inventory:

```python
@dataclass
class AgedInventory:
    token: str
    amount: Decimal
    acquired_at: int
    venue: str

def check_stale_inventory(max_age_hours: int = 24):
    """Force sell inventory held too long."""
    cutoff = time.time() - (max_age_hours * 3600)
    for inv in inventory:
        if inv.acquired_at < cutoff:
            logger.warning(f"Stale inventory: {inv.amount} {inv.token} held {max_age_hours}h")
            queue_liquidation(inv)
```

### 8.4 Mean Reversion Sizing

Size trades based on how far from target delta we are:

```python
def calculate_trade_size(current_delta: Decimal, base_size: Decimal) -> Decimal:
    """Larger trades when further from target."""
    deviation = abs(current_delta - Decimal("0.5"))

    if deviation < Decimal("0.05"):
        return base_size * Decimal("0.5")   # Small trades when close
    elif deviation < Decimal("0.10"):
        return base_size                     # Normal trades
    elif deviation < Decimal("0.15"):
        return base_size * Decimal("1.5")   # Larger trades
    else:
        return base_size * Decimal("2.0")   # Aggressive rebalancing
```

### 8.5 Hedge Ratios

Instead of 50/50 by value, optimize for minimum variance:

```python
# Minimum-variance hedge ratio
# h* = Cov(cNGN, USD) / Var(USD)
# This tells us the optimal hedge ratio to minimize portfolio variance

def calculate_optimal_hedge():
    cngn_returns = calculate_returns(cngn_prices)
    # For stablecoins, variance is near-zero, so optimal hedge ≈ 1.0
    # But if cNGN has significant vol, may want < 1.0
    return optimal_ratio
```

---

## 9. Liquidity, Solvency, and Profitability

### 9.1 Liquidity Management

**Definition**: Having enough of each asset to fulfill trading obligations.

**Current approach:**
- `min_reserve_token0/token1` in DexParams
- Position sync every 60s tracks available balances

**Improvements needed:**

1. **Venue-specific liquidity buffers:**
```python
liquidity_buffers = {
    "quidax": {"cngn": 50000, "usdt": 100},  # CEX needs fills
    "aerodrome": {"cngn": 10000, "usdc": 50},  # DEX needs gas + small swaps
    "blockradar": {"cngn": 100000, "usdc": 0},  # B2C mostly outflows
}
```

2. **Real-time liquidity alerts:**
```python
async def check_liquidity():
    for venue, buffers in liquidity_buffers.items():
        pos = await venue.get_position()
        for token, min_amount in buffers.items():
            if pos.balances.get(token, 0) < min_amount:
                await alert(f"{venue} low on {token}")
```

3. **Cross-venue liquidity rebalancing:**
```python
# If Quidax low on cNGN but Aerodrome has excess,
# withdraw from Aerodrome LP and transfer to Quidax
```

### 9.2 Solvency Constraints

**Definition**: Total assets exceed total liabilities; can meet all obligations.

**Key metrics:**

| Metric | Formula | Target |
|--------|---------|--------|
| Net Asset Value | Σ(holdings × price) | Always positive |
| Runway | NAV / daily_burn_rate | > 30 days |
| Drawdown | (Peak NAV - Current NAV) / Peak | < 10% |

**Implementation:**
```python
async def check_solvency():
    nav = await calculate_nav()
    peak_nav = await db.get_peak_nav()
    drawdown = (peak_nav - nav) / peak_nav

    if drawdown > Decimal("0.10"):
        await trigger_circuit_breaker("Max drawdown exceeded")

    if nav < Decimal("100"):  # Minimum NAV threshold
        await trigger_circuit_breaker("NAV below minimum")
```

### 9.3 Profitability Optimization

**Revenue sources:**
1. DEX LP fees (swap fees collected)
2. CEX spread capture (bid-ask spread)
3. Arbitrage profits (cross-venue price differences)
4. B2C markup (Blockradar spread)

**Profitability tracking:**
```python
@dataclass
class DailyPnL:
    date: str
    lp_fees_usd: Decimal
    arb_profit_usd: Decimal
    cex_spread_usd: Decimal
    b2c_markup_usd: Decimal
    gas_costs_usd: Decimal
    impermanent_loss_usd: Decimal  # Negative
    net_pnl_usd: Decimal
```

**Optimization strategies:**

1. **Fee tier selection** (for DEX LPs):
```python
# Choose fee tier based on expected volume and volatility
# Higher fees = more profit per trade but less volume
fee_tiers = {
    "low_vol": 100,    # 0.01% - high volume, low spread
    "medium": 500,     # 0.05% - balanced
    "high_vol": 3000,  # 0.30% - low volume, high spread
}
```

2. **Dynamic spread adjustment** (for CEX/B2C):
```python
def calculate_optimal_spread(volatility: Decimal, inventory_skew: Decimal) -> int:
    """Adjust spread based on market conditions."""
    base_spread_bps = 15

    # Widen spread during high volatility
    if volatility > normal_vol * 1.5:
        base_spread_bps += 10

    # Widen spread if inventory is skewed (incentivize balancing trades)
    if abs(inventory_skew) > Decimal("0.1"):
        base_spread_bps += 5

    return base_spread_bps
```

3. **Arbitrage opportunity scoring:**
```python
def score_opportunity(opp: ArbitrageOpportunity) -> Decimal:
    """Rank opportunities by risk-adjusted return."""
    expected_return = opp.expected_profit_usd / opp.recommended_size_usd
    execution_risk = estimate_slippage_risk(opp)
    venue_reliability = get_venue_success_rate(opp.buy_venue, opp.sell_venue)

    return expected_return * venue_reliability * (1 - execution_risk)
```

### 9.4 Combined Health Score

Aggregate liquidity, solvency, and profitability into a single health metric:

```python
@dataclass
class SystemHealth:
    liquidity_score: float    # 0-1: Can we trade?
    solvency_score: float     # 0-1: Are we solvent?
    profitability_score: float  # 0-1: Are we making money?
    delta_score: float        # 0-1: Are we balanced?
    overall_health: float     # Weighted average

def calculate_health() -> SystemHealth:
    return SystemHealth(
        liquidity_score=min(1.0, available_liquidity / required_liquidity),
        solvency_score=1.0 - min(1.0, drawdown / max_drawdown),
        profitability_score=sigmoid(rolling_7d_pnl / target_pnl),
        delta_score=1.0 - min(1.0, abs(delta - 0.5) / 0.5),
        overall_health=weighted_average([...])
    )
```

---

## 10. Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Delta Neutrality System                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐            │
│  │  Aerodrome   │     │   Quidax     │     │  Blockradar  │            │
│  │  (DEX LP)    │     │   (CEX)      │     │   (B2C)      │            │
│  └──────┬───────┘     └──────┬───────┘     └──────┬───────┘            │
│         │                    │                    │                     │
│         └──────────────┬─────┴────────────────────┘                     │
│                        ▼                                                │
│              ┌─────────────────────┐                                    │
│              │   Position Sync     │  (every 60s)                       │
│              │   get_position()    │                                    │
│              └─────────┬───────────┘                                    │
│                        ▼                                                │
│              ┌─────────────────────┐     ┌─────────────────────┐       │
│              │  Delta Calculator   │────▶│  Blended Price      │       │
│              │  Σ positions → δ    │     │  (VWAP reference)   │       │
│              └─────────┬───────────┘     └─────────────────────┘       │
│                        ▼                                                │
│              ┌─────────────────────┐                                    │
│              │  Delta Check        │  (every 120s)                      │
│              │  deviation > 10%?   │                                    │
│              └─────────┬───────────┘                                    │
│                        │                                                │
│         ┌──────────────┼──────────────┐                                 │
│         ▼              ▼              ▼                                 │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐                          │
│  │   Alert    │ │ DEX        │ │ Arbitrage  │                          │
│  │ (dashboard)│ │ Rebalance  │ │ Inventory  │                          │
│  └────────────┘ │ (if OOR)   │ │ Limits     │                          │
│                 └────────────┘ └────────────┘                          │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                     MISSING: Active Rebalancer                   │   │
│  │     (trades to restore delta when deviation exceeds threshold)   │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 11. Related Files

| File | Purpose |
|------|---------|
| `engine/config.py` | Delta configuration parameters |
| `engine/core/scheduler.py` | Delta monitoring, DEX rebalancing |
| `engine/core/arbitrage/inventory.py` | Arbitrage inventory tracking |
| `engine/venues/dex/base.py` | Capital allocation for LP positions |
| `engine/api/schemas.py` | `GlobalPosition`, `ArbitrageParams`, `DexParams` models |
| `engine/api/routes.py` | `/positions/global` endpoint |

---

## 12. Testing Considerations

Delta neutrality logic should be tested with:

1. **Unit tests**: Delta calculation math, inventory limit checks
2. **Integration tests**: Multi-venue position aggregation
3. **Scenario tests**:
   - Portfolio drifts to 70/30 - alert raised?
   - Arbitrage trade would exceed imbalance limit - blocked?
   - DEX position goes out of range - rebalancing triggered?

Current test coverage for delta logic is minimal. Priority tests to add:

```python
def test_delta_calculation_accuracy():
    """Verify delta ratio calculation is correct."""

def test_delta_alert_triggers_at_threshold():
    """Verify alert fires when deviation > 10%."""

def test_inventory_limit_blocks_trade():
    """Verify arbitrage trade blocked when it would exceed imbalance limit."""

def test_dex_rebalance_on_out_of_range():
    """Verify DEX position rebalancing when tick range exceeded."""
```
