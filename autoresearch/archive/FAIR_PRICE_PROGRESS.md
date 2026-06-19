# Fair Price Pipeline — Progress & Next Steps

## What Has Been Built

### Tier 1 — MarketFairPrice (`engine/market/fair_price.py`)

Multi-factor VWAP that replaces the old simple VWAP in `price_aggregation.py`.

**Weight formula per venue:**
```
w_v = volume_weight × liquidity_weight × spread_quality × recency_decay
```
- `recency_decay = exp(-λ × age_seconds)`, half-life ≈ 87s; venues >10 min stale → ~0 weight
- `spread_quality = 1 / bid_ask_spread_bps`
- Bybit excluded from all fair-value math (`FAIR_VALUE_EXCLUDED` set)
- Output: `MarketFairPrice(price, weights, confidence, timestamp)`

### Tier 2 — ExecutableFairPrice (`engine/market/fair_price.py`)

Stoikov microprice adjustment for short-horizon directional pressure.

```
imbalance ∈ [-1, 1]  (from DEX signed swap flow or CEX LOB)
executable_price = market_price × (1 + fee_rate × imbalance)
```
- Output: `ExecutableFairPrice(price, market_price, imbalance_signal, ...)`

### Tier 3 — StrategyFairPrice (`engine/market/fair_price.py`)

Avellaneda-Stoikov reservation price adjusted for LP inventory position.

```
normalized_imbalance = (net_cNGN - target_cNGN) / max_scale
raw_skew_bps = -β × normalized_imbalance
skew_bps = max_skew × tanh(raw_skew_bps / max_skew)   # tanh cap ≤ ±20 bps
strategy_price = executable_price × (1 + skew_bps / 10_000)
```
- Parameters: `beta_bps=10`, `max_skew_bps=20`, `target_cngn=0`, `max_scale=1_000_000`
- Output: `StrategyFairPrice(price, executable_price, skew_bps, net_cNGN, timestamp)`

### Production Engine Fixes

| File | Change |
|---|---|
| `engine/lp/strategy.py` | `compute_ewma_stats` switched to **log-return variance** + 3 bps floor; bounds now log-space `mean * exp(±range * skew)` |
| `engine/market/price_aggregation.py` | `bybit` added to `FAIR_VALUE_EXCLUDED` |
| `engine/lp/rebalancer.py` | All rebalance methods accept `strategy_fair_price: StrategyFairPrice | None = None` |

### Backtester Sync (`backtester/strategy.py`) ← Phase 1 complete

`EWMACalculator` and `calculate_tick_range` now match production exactly:
- Variance on **log-returns** (not raw price deviations): `r = log(x / x_prev)`
- **3 bps std floor**: `std = max(sqrt(var), 3e-4)`
- **Log-space bounds**: `lower = mean * exp(-range * skew)`, `upper = mean * exp(+range * (1-skew))`
- **`center_price` parameter** on `calculate_tick_range` — hook for fair price injection in Phase 2

### Plotting Scripts (Phase 0 complete)

**`scripts/fetch_cngn_balance_history.py`**
Scans ERC-20 Transfer logs for cNGN on Base + BSC for the two LP accounts,
reconstructs a running balance time-series, outputs to CSV.
```
python scripts/fetch_cngn_balance_history.py \
    --base-addr 0xB25dB46588634D1153c058407D08361AbC6323fE \
    --bsc-addr  0x71A39D4663d52FFEb2EC78CAa3FC73d4Cc7E9302 \
    --out data/cngn_balance_history.csv
```

**`scripts/plot_fair_prices.py`**
Replays the full swap event CSV through all three fair price tiers,
generates a 3-panel chart: prices / imbalance+skew / net inventory.
```
python scripts/plot_fair_prices.py \
    --csv "data/Aerodrome&Pancakeswap_HistoricalData.csv" \
    --format legacy \
    --balance-csv data/cngn_balance_history.csv \
    --out fair_prices.png
```

Output confirmed: 4,669 swap frames over 2025-02-16 → 2026-02-23.

---

## Commits on `beech` (this work)

| Hash | Description |
|---|---|
| `6562aea` | fix(backtester): sync EWMACalculator + calculate_tick_range to production |
| `0bc8da0` | fix: update tests to match log-return std and bybit VWAP exclusion |
| earlier | feat(fair-price): three-tier pipeline + rebalancer wiring |

---

## Next Steps

### Immediate — Fetch real inventory data (Phase 0 finish)

Run the balance fetcher to get actual cNGN history from the two LP accounts:
```
python scripts/fetch_cngn_balance_history.py \
    --base-addr 0xB25dB46588634D1153c058407D08361AbC6323fE \
    --bsc-addr  0x71A39D4663d52FFEb2EC78CAa3FC73d4Cc7E9302 \
    --out data/cngn_balance_history.csv
```
Then re-run the plot with `--balance-csv` to see the Tier 3 inventory panel and
confirm the skew signal is non-zero and plausible.

**Requires:** `ALCHEMY_KEY` in `.env` (or `BASE_RPC_URL`/`BSC_RPC_URL`).

---

### Phase 2 — β_bps validation grid

**Goal:** Given real historical inventory, which β coefficient minimizes divergent
loss without sacrificing fee income?

#### 2a. `FairPriceScenario` dataclass (`backtester/params.py`)

```python
@dataclass
class FairPriceScenario:
    beta_bps: float        # A-S inventory skew strength; 0 = baseline (no skew)
    max_skew_bps: float    # skew cap (default 20)
    target_cngn: float     # target inventory (default 0)
    max_scale: float       # normalization scale (default 1_000_000)
```

Grid: `beta_bps ∈ [0, 5, 10, 15, 20]` × fixed `max_skew_bps=20`.

#### 2b. Inventory injection hook (`backtester/simulator.py`)

Add `center_price_fn: Callable[[float, float], float] | None` parameter to
`simulate_pool()`. At each swap event, call it with `(current_price, net_cngn_at_t)`
to get the skewed center price, then pass it as `center_price` to `calculate_tick_range`.

The `net_cngn_at_t` comes from the `BalanceSeries` step-function lookup (same class
already implemented in `scripts/plot_fair_prices.py` — move to `backtester/data.py`).

#### 2c. `--fair-price-grid` mode (`backtester/run.py`)

```
python -m backtester.run \
    --csv data/uni_base_v4.csv \
    --pool uni-base \
    --dataset-format v4 \
    --walkforward \
    --balance-csv data/cngn_balance_history.csv \
    --fair-price-grid \
    --output results/fair_price_validation
```

Runs the top-N DexParams configs (from the DEX walk-forward output) × the
`FairPriceScenario` grid. Sanity check: `beta_bps=0` row must match the plain
walk-forward result for the same params.

#### 2d. New metrics

- `center_mae`: mean absolute error of range center vs. realised next price
- `skew_capture`: fraction of events where inventory skew direction matched
  subsequent price movement

#### 2e. Tests (`tests/test_backtester.py`)

- `center_price_fn=None` → identical output to current baseline
- `center_price_fn` with extreme inventory → ticks shift in expected direction
- `FairPriceScenario` grid generates expected number of scenarios

---

### Other DEX walk-forward session

The other agent session running the DEX walk-forward (`sd_multiplier`, `ewma_lambda`,
`downside_skew`, `rebalance_threshold_pct`) should be **paused** until Phase 1 was
merged — it was using the old linear-bounds backtester and producing params that would
not transfer to production. Now that `backtester/strategy.py` is synced, that session
can resume and its output will be trustworthy.

Phase 2 depends on the walk-forward output: use the top-N DexParams to fix the base
config, then sweep `beta_bps` to find the optimal skew coefficient.

---

### Dashboard items (separate, not blocking)

From `TODO.md`:
- Track LP position value at every sqrt price change → historical LP value time series
- Global cNGN delta / inventory imbalance on main page
