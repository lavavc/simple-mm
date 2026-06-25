# Fair Price Autoresearch

## Goal

Find a forward-looking cNGN/USD estimator for quoting, rebalancing, and market making without letting low-liquidity DEX prices become the truth label.

The live engine has three pricing layers in `engine/market/fair_price.py`:

- `MarketFairPrice`: neutral multi-factor market price.
- `ExecutableFairPrice`: short-horizon imbalance-adjusted executable price.
- `StrategyFairPrice`: inventory-skewed reservation price.

The research pipeline tests candidate estimators before any live behavior changes.

## Current Status

Implemented:

- Quidax and Bybit capture metadata in `price_snapshots.metadata_json`.
- Capture-only collector: `research/scripts/capture_fair_price_feeds.py`.
- Markout export: `research/scripts/export_fair_price_markouts.py`.
- Feed quality report: `research/scripts/report_fair_price_feed_quality.py`.
- Markout analyzer: `research/scripts/analyze_fair_price_markouts.py`.
- Quidax book features: top-1/top-N imbalance, cNGN/USD pressure, OWA, microprice.
- DEX context features: previous-or-equal `uni-base_pool` / `uni-bsc_pool` premium versus Quidax executable mid.
- Walk-forward probability buckets for midpoint and side-specific adverse movement.

Current data caveat: existing Quidax captures have mostly flat prices. They validate plumbing and feed quality, not estimator quality.

DEX context caveat: the markout exporter expects previous-or-equal `uni-base_pool` and `uni-bsc_pool` rows in `price_snapshots`. Those rows must come from the pool-history bridge described in `research/autoresearch/data-methodology-refactor.md`; Quidax and Bybit capture jobs do not create them.

## Primary Label

Use future CEX executable value, currently Quidax depth-walk value at the tested size:

- midpoint label: future executable midpoint
- buy label: future cost to buy cNGN
- sell label: future proceeds from selling cNGN

Do not label against DEX mid, blended price, Bybit P2P, or Blockradar.

## Hypotheses

1. Quidax depth-adjusted executable price beats ticker mid for 10-120s horizons.
2. P2P feeds improve slow anchoring at 300-600s, not fast markouts.
3. DEX divergence is a control feature, not truth.
4. Side-specific labels beat a symmetric midpoint for rebalancing.
5. Book imbalance, OWA, and microprice improve short-run direction hit rate.
6. DEX premium explains LP rebalance outcomes but must not replace CEX labels.
7. DEX premium cone percentiles explain LP outcomes conditionally, but should not improve CEX executable-label prediction enough to become a label proxy.
8. Fair Value estimator improvements should be stress-conditioned; improvements that appear only through unstable global parameter jumps should not be promoted.

## DEX Pool Context Requirements

Before testing DEX premium hypotheses, import pool-history swap prices into `price_snapshots` as:

- `uni-base_pool`
- `uni-bsc_pool`

Each imported row should use sqrt-derived cNGN/USD as `mid`, fee-adjusted infinitesimal executable prices as `bid` and `ask`, and include block number, transaction hash, log index, active liquidity, fee rate, quote model, signed cNGN flow fields, `stored_cngn_usd_price`, and `stored_price_model` in metadata. The importer must be idempotent, must reject unexplained stored-price rows, and must not rewrite CEX labels.

The DEX quote convention is:

- `mid = sqrt_mid`
- `bid = sqrt_mid * (1 - fee_rate)`
- `ask = sqrt_mid / (1 - fee_rate)`

These DEX bid/ask values include pool fee only. They do not include price impact, gas, routing, hooks, or tick crossing.

## Cone and As-Of Feature Requirements

Fair Value research should consume causal cone features only through as-of joins:

- CEX book imbalance and pressure features with source age
- DEX premium cone percentile with DEX source age
- pool liquidity, volume, and flow cone percentiles with pool source age

Every markout export should report missing counts and max-age statistics for CEX labels, DEX context, and pool-derived features. If DEX or pool features are stale beyond the configured max age, bucket-level conclusions should be reported as unavailable rather than silently backfilled.

## Standard Workflow

Inspect saved history before collecting:

```bash
sqlite3 -header -column data/cngn.db \
  "select source, count(*) as rows, datetime(min(timestamp_ms)/1000,'unixepoch') as first_utc, datetime(max(timestamp_ms)/1000,'unixepoch') as last_utc from price_snapshots group by source order by source;"

python research/scripts/report_fair_price_feed_quality.py \
  --db data/cngn.db \
  --out research/data/fair_price_feed_quality_current.md \
  --target-usd 100
```

Recommended separated capture:

```bash
python research/scripts/capture_fair_price_feeds.py \
  --db data/cngn.db \
  --sources quidax \
  --interval 15 \
  --iterations 5760 \
  --quidax-cache-seconds 0

python research/scripts/capture_fair_price_feeds.py \
  --db data/cngn.db \
  --sources bybit \
  --interval 60 \
  --iterations 1440
```

Export and analyze:

```bash
python research/scripts/export_fair_price_markouts.py \
  --db data/cngn.db \
  --out research/data/fair_price_markouts.csv \
  --horizons 10,30,60,120,300,600 \
  --target-usd 100 \
  --max-label-lag-seconds 15

python research/scripts/analyze_fair_price_markouts.py \
  --csv research/data/fair_price_markouts.csv \
  --out research/data/fair_price_markout_report.md
```

## Promotion Gate

Before changing `ExecutablePriceCalculator` or route sizing:

- current executable prices must move enough for ranking to be meaningful
- each horizon must report label count, missing labels, median lag, and max lag
- validation buckets must remain stable out of sample
- side-specific errors must improve for the relevant execution direction
- Quidax execution latency must support the chosen markout horizon
- parameter-cone and stress-bucket improvements must survive out-of-sample validation
- estimator parameters must be stable across neighboring windows unless a measured regime shift explains the jump

Archive detail: `research/autoresearch/archive/fair_price_autoresearch.md` and `research/autoresearch/archive/FAIR_PRICE_PROGRESS.md`.
