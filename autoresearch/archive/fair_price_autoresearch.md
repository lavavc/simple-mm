# cNGN Fair Price Autoresearch Guide

Use this guide to test CEX-led fair-price hypotheses for cNGN/USD. The goal is
to find a price estimator that is useful for forward-looking rebalancing and
market making while keeping DEX/AMM prices as monitored venues, not as primary
price-discovery truth.

## Current Position

- Quidax is the only executable CEX venue currently implemented.
- Bybit P2P is a reference feed, not an executable exchange.
- DEX prices are often lagging or impactable and must not be allowed to become
  the dominant source of cNGN price discovery.
- Existing fair-price tiers in `engine/market/fair_price.py`:
  - `MarketFairPrice`: multi-factor venue VWAP using volume, liquidity, spread,
    and recency.
  - `ExecutableFairPrice`: short-horizon microprice adjustment using order-flow
    imbalance.
  - `StrategyFairPrice`: inventory-skewed reservation price for quoting and LP
    management.

## Progress So Far

- Added capture metadata persistence for Quidax depth and Bybit P2P filter/depth
  diagnostics in `price_snapshots.metadata_json`.
- Added a capture-only research collector that does not start the trading
  scheduler or execution adapters.
- Added Quidax depth-walk markout export for 10-600s horizons.
- Added a markdown analyzer for estimator errors, side-specific errors, label
  lag, feed cadence, current executable-price availability, and Bybit reference
  age.
- Fixed the capture loop to use fixed-rate scheduling. `--interval` now targets
  start-to-start cadence instead of sleeping after each fetch completes.
- Fixed analyzer lag handling so `0ms` label lag is counted as observed data.
- Added research export fields for Quidax book imbalance, cNGN/USD pressure,
  order-weighted average price, microprice, and DEX premium/age controls.
- Added analyzer probability buckets so imbalance features can be tested for
  event calibration, not only point-estimator error.

Live smoke findings:

- With the default Quidax source cache, 10s labels were mostly invalid because
  cached quotes reused the same `timestamp_ms` and collided on the
  `(source, timestamp_ms)` unique key.
- With `--quidax-cache-seconds 0`, 10s labels became valid, but mixed
  Quidax+Bybit capture made Quidax cadence uneven because slow Bybit P2P
  requests delayed the loop.
- Quidax-only capture improved cadence. Before fixed-rate scheduling, a 5s
  interval produced a median Quidax gap of about `7168ms`; after fixed-rate
  scheduling, a short smoke produced a median gap of about `4547ms`.
- A later low-stress Quidax-only capture at 15s persisted `40/40` attempted
  rows with complete metadata and executable depth. Median source gap was about
  `14873ms`; max gap was about `20281ms`.
- Short smoke samples had no Quidax price/book movement, so they validate the
  pipeline but do not support estimator ranking yet.

Session closeout on 2026-06-19:

- Before restarting forward collection, `data/cngn.db` contained saved
  fair-price feed history only from the current research session: `109` Quidax
  rows from `2026-06-19 12:26:40` to `2026-06-19 14:22:04` UTC, and `7` Bybit
  P2P rows from `2026-06-19 12:26:49` to `2026-06-19 14:22:24` UTC.
- All pre-forward Quidax rows had metadata; `108/109` had executable depth at
  the tested `100 USD` size.
- The saved Quidax ticker mid has `1` distinct value so far. Treat these rows
  as capture/feed-quality evidence, not estimator-ranking evidence.
- No public Quidax historical OHLCV, candles, trades, or order-book backfill
  endpoint has been confirmed from the current API docs or SDK. Historical
  values should be treated as available only from our own `price_snapshots`
  capture unless Quidax provides an institutional export.
- Forward collection is running in detached `screen` sessions:
  `fair_price_quidax` writes to `logs/fair_price_quidax_capture_screen.log`
  every 15s for 5760 iterations, and `fair_price_bybit` writes to
  `logs/fair_price_bybit_capture_screen.log` every 60s for 1440 iterations.
  Check status with `screen -ls`; stop a session with
  `screen -S fair_price_quidax -X quit` or
  `screen -S fair_price_bybit -X quit`.

## Primary Markout Target

Mark out every candidate estimator to future CEX executable value, not to DEX
mid or blended cross-venue fair value.

Use horizons:

```text
10s, 30s, 60s, 120s, 300s, 600s
```

Labels:

- Mid-price label: future depth-adjusted CEX executable midpoint.
- Buy-cNGN label: future side-specific cost to buy cNGN on CEX.
- Sell-cNGN label: future side-specific proceeds from selling cNGN on CEX.

If only Quidax has executable depth, the first benchmark label is Quidax depth.
When new executable CEXs become available, define the label as the best
reliable cross-CEX executable value at the target size.

## Hypotheses To Test

### H1: Quidax Depth Dominates Short-Horizon Truth

Hypothesis: For 10-120s horizons, a Quidax depth-adjusted executable price
beats ticker mid, DEX mid, and old blended fair value on absolute markout error.

Mechanism: Thin cNGN venues are jumpy; top-of-book mid ignores size, while
depth-walk value captures the price available to the bot.

Success criteria:

- Lower MAE and median absolute error at 10s/30s/60s/120s.
- Lower signed bias by side.
- Better direction hit rate for quote shifts.

Failure criteria:

- Depth is too stale or sparse to beat ticker mid.
- Errors concentrate around our own ladder updates, implying self-quote leakage.

### H2: P2P Reference Feeds Improve Slow Anchoring, Not Fast Markouts

Hypothesis: Bybit P2P and future P2P/reference feeds improve 300-600s anchor
stability but should receive little or no weight at 10-60s horizons.

Mechanism: P2P ads update more slowly and include fraud/ad-quality noise, but
they may reflect broader NGN/USDT clearing pressure.

Success criteria:

- Adding filtered P2P features improves 300s/600s error without harming
  10s/30s error.
- P2P residuals are slow-moving and mean-reverting versus Quidax.

Failure criteria:

- P2P deviations are mostly stale or one-sided and worsen CEX executable labels.

### H3: DEX Divergence Is a Control Signal, Not a Truth Input

Hypothesis: DEX-vs-CEX divergence predicts when to rebalance or pause quoting,
but including DEX prices directly in fair value worsens markout accuracy.

Mechanism: AMMs are low-liquidity and often lag CEX. Their price tells us where
inventory risk is accumulating, not where fair value should be set.

Success criteria:

- CEX-only estimators outperform blended CEX+DEX estimators.
- DEX divergence features improve decisions only as gating/skew variables.

Failure criteria:

- DEX leads CEX in statistically significant windows after removing wash/dust
  events and liquidity shocks.

### H4: Side-Specific Labels Beat Mid Labels For Rebalancing

Hypothesis: The best estimator for rebalancing is side-specific executable
value, not a single symmetric mid.

Mechanism: A rebalance has a direction. The realized opportunity cost is the
future bid or ask we could execute against, including depth and fees.

Success criteria:

- Side-specific markouts explain realized rebalance PnL better than mid labels.
- Inventory-skewed `StrategyFairPrice` reduces adverse selection and overtrading.

Failure criteria:

- Side labels add noise because available depth changes faster than our reaction
  time.

### H5: Book Imbalance Predicts Short-Run Executable Markouts

Hypothesis: Quidax order-book imbalance, OWA, and microprice features improve
10-120s direction hit rate and side-specific execution error versus raw top-of-
book mid.

Mechanism: A native USDT/cNGN book that is bid-heavy predicts higher
cNGN-per-USDT and therefore lower cNGN/USD. The research export records both
native imbalance and sign-flipped cNGN/USD pressure so the analyzer can test the
direction explicitly rather than relying on intuition.

Success criteria:

- OWA or microprice estimators improve direction hit rate without worsening MAE.
- Walk-forward probability buckets show stable event probabilities in the
  validation split, especially at high absolute imbalance.
- Side-specific buckets identify when buy cost or sell proceeds are likely to
  worsen before execution.

Failure criteria:

- Bucket probabilities collapse toward 50% out of sample.
- Signal only works in flat-book samples or is dominated by stale label lag.

### H6: DEX Divergence Is A LP-Rebalance Feature, Not A Fair-Value Label

Hypothesis: Previous-or-equal `uni-base_pool` and `uni-bsc_pool` premiums versus
Quidax executable mid explain LP rerange and rebalance outcomes, but should not
replace the CEX-led executable label.

Mechanism: DEX pools are the venue-local state LP actually manages. Their
premium/discount versus executable CEX value is useful for LP autoresearch:
whether a range exit was caused by local pool flow, stale external price
discovery, or a real market-wide move.

Success criteria:

- Rebalance markouts differ meaningfully by DEX premium/discount bucket.
- Venue-local pool premium plus swap-flow imbalance identifies defensive
  reranges that would have avoided more loss than their gas and ratio-swap cost.

Failure criteria:

- DEX premium buckets mostly describe noise or self-impact and do not explain
  post-rebalance P&L.

## Data To Capture

Minimum fields per observation:

- `source`, `venue`, `pair`, `timestamp_ms`
- normalized bid, ask, mid in cNGN/USD or explicit pair basis
- raw venue bid/ask/last in venue-native units
- order-book levels when available
- top-N bid and ask notional depth
- spread bps
- 24h volume or credible depth proxy
- feed latency and error state
- whether the quote is executable, reference-only, or DEX-derived

Current sources:

- Quidax public ticker and order book depth.
- Bybit P2P filtered bid/ask, ad counts, filter counts, and depth proxy.
- Uniswap pools as DEX divergence features only.

Future collectors to add:

- Any CEX with a real cNGN or NGN-stablecoin order book and public depth API.
- Any P2P feed with enough ad quality fields to filter fraud/stale posters.
- OTC/manual quotes only if timestamped, source-attributed, and excluded from
  automated execution truth until validated.

## Estimator Families

### Baselines

- Quidax ticker mid.
- Quidax top-of-book mid.
- Quidax depth-walk executable mid at fixed sizes.
- Existing `BlendedPrice`.
- Existing `MarketFairPrice`.

### CEX-Led Estimators

- CEX executable midpoint: average of side-specific depth-walk prices at target
  size.
- CEX microprice: top-of-book or depth-weighted price adjusted by imbalance.
- Quidax OWA: square-root order-weighted average from top-book and top-N depth,
  using cNGN/USD bid/ask after normalizing the native USDT/cNGN book.
- Quidax pressure buckets: native top-N imbalance and sign-flipped cNGN/USD
  pressure, calibrated to future midpoint and side-specific adverse movement.
- CEX robust anchor: median or trimmed mean across executable CEXs once multiple
  CEX feeds exist.
- CEX plus slow reference: CEX executable value with low-frequency P2P residual
  correction at longer horizons only.

### Strategy Estimators

- `ExecutableFairPrice`: market fair value adjusted for signed flow imbalance.
- `StrategyFairPrice`: executable fair value adjusted for inventory.
- Side-specific reservation prices for buy/sell quoting.

## Methodology

1. Build an aligned observation table from `price_snapshots`.
2. Normalize all prices to cNGN/USD and preserve pair basis.
3. For each timestamp, compute estimator candidates using only information
   available at or before that timestamp.
4. For each horizon, find the first future valid label at or after
   `timestamp + horizon`.
5. Compute:
   - MAE, median absolute error, RMSE
   - signed bias
   - direction hit rate
   - side-specific execution error
   - walk-forward probability buckets for imbalance-driven midpoint and side
     adverse movement
   - error by spread/depth regime
   - error around our own ladder updates
6. Use walk-forward splits by observation count, not random rows.
7. Report all horizons; do not tune to one horizon and claim generality.

Feed and modeling soundness gates:

- Quidax executable rows must have current depth-walk prices at the tested
  target size. Missing depth is missing data, not zero.
- Each reported horizon must include label count, missing-label count, median
  label lag, and max label lag.
- Run both strict and operational lag reports. Use 5s max label lag for strict
  10s/30s validation and 15s max label lag for operational sensitivity.
- Do not compare estimators when current executable prices do not move across
  the sample. A flat-book sample only validates plumbing.
- Do not use random train/test splits. Use walk-forward windows and report
  morning/afternoon/evening or similar time blocks once there is enough data.
- Keep Bybit P2P as previous-or-equal reference data only. Do not let Bybit or
  DEX prices become labels for CEX executable value.
- Track Bybit age in the analyzer; stale Bybit rows can be tested as slow
  anchor features but must not contaminate short-horizon labels.

Guardrails:

- Exclude self-generated Quidax ladder quotes from labels where possible, or at
  least stratify results by own-order presence.
- Never use future DEX state in a feature.
- Keep reference-only feeds out of short-horizon executable labels.
- Treat missing depth as missing data, not zero depth.

## First Agent Tasks

1. Confirm new Quidax metadata is being persisted in `price_snapshots`.
2. Export a one-day observation table with Quidax ticker/depth and Bybit P2P
   metadata.
3. Implement Quidax depth-walk labels for 10-600s horizons.
4. Compare Quidax ticker mid, Quidax top-of-book mid, Quidax depth-walk mid,
   current blended price, and `MarketFairPrice`.
5. Produce one markdown report with tables by horizon and a short conclusion on
   which estimator should anchor CEX market making.

Initial export command:

Capture-only collector command:

```bash
python scripts/capture_fair_price_feeds.py \
  --db data/cngn.db \
  --sources quidax,bybit \
  --interval 10 \
  --iterations 8640 \
  --quidax-cache-seconds 0
```

At a 10-second interval, `8640` iterations is one day of observations. This
script only polls research feeds and writes `price_snapshots`; it does not start
the trading scheduler or execution adapters. The Quidax cache override is
required for 10-second markouts because the production source default caches
Quidax quotes for 30 seconds. Capture loops use fixed-rate scheduling, so
`--interval` targets the start-to-start capture cadence; any fetch slower than
the interval is recorded as label lag rather than hidden by sleeping longer.

Export command:

```bash
python scripts/export_fair_price_markouts.py \
  --db data/cngn.db \
  --out data/fair_price_markouts.csv \
  --horizons 10,30,60,120,300,600 \
  --target-usd 100 \
  --max-label-lag-seconds 15
```

The export now includes Quidax book features and previous-or-equal DEX context:

- `quidax_imbalance_top1_usdt`, `quidax_imbalance_topn_usdt`
- `quidax_imbalance_topn_cngn`, `quidax_cngn_usd_pressure_topn`
- `quidax_owa_mid_top1`, `quidax_owa_mid_topn`, `quidax_microprice_top1`
- `uni_base_mid`, `uni_base_premium_bps`
- `uni_bsc_mid`, `uni_bsc_premium_bps`

Raw feed-quality command:

```bash
python scripts/report_fair_price_feed_quality.py \
  --db data/cngn.db \
  --out data/fair_price_feed_quality.md \
  --target-usd 100
```

Use this before modeling. It reports raw source cadence, metadata availability,
executable Quidax depth availability at the tested size, and distinct observed
mid prices directly from `price_snapshots`, independent of which rows survive
markout export.

Initial analysis command:

```bash
python scripts/analyze_fair_price_markouts.py \
  --csv data/fair_price_markouts.csv \
  --out data/fair_price_markout_report.md
```

The analyzer reports the new OWA/microprice estimators and walk-forward
probability calibration buckets. Treat those buckets as research evidence only.
Do not promote them into `ExecutablePriceCalculator` or Kelly-style sizing caps
until validation buckets remain stable over a full operational sample.

Recommended separated capture commands for forward collection:

```bash
python scripts/capture_fair_price_feeds.py \
  --db data/cngn.db \
  --sources quidax \
  --interval 15 \
  --iterations 5760 \
  --quidax-cache-seconds 0

python scripts/capture_fair_price_feeds.py \
  --db data/cngn.db \
  --sources bybit \
  --interval 60 \
  --iterations 60
```

Run Quidax as a separate label feed and Bybit separately at slower cadence as a
reference feature so P2P latency does not distort executable CEX labels. The
15s Quidax cadence is the current overnight/default collection setting because
it avoided the 5s stress-run failures while still supporting operational
10-600s markout analysis. Re-test 5s, 1s, and sub-1s horizons only after the
Quidax execution-latency benchmark and feed-capacity benchmark are complete.

## Minimum Report Template

```text
Hypothesis:
Estimator candidates:
Label:
Horizons:
Dataset window:
Rows after filters:
Leakage controls:

Results by horizon:
  MAE
  median absolute error
  signed bias
  direction hit rate
  side-specific execution error

Regime breakdown:
  tight vs wide spread
  shallow vs deep book
  DEX premium/discount buckets
  own-ladder-present vs not present

Conclusion:
Decision:
Next test:
```
