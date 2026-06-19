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
- Capture-only collector: `scripts/capture_fair_price_feeds.py`.
- Markout export: `scripts/export_fair_price_markouts.py`.
- Feed quality report: `scripts/report_fair_price_feed_quality.py`.
- Markout analyzer: `scripts/analyze_fair_price_markouts.py`.
- Quidax book features: top-1/top-N imbalance, cNGN/USD pressure, OWA, microprice.
- DEX context features: previous-or-equal `uni-base_pool` / `uni-bsc_pool` premium versus Quidax executable mid.
- Walk-forward probability buckets for midpoint and side-specific adverse movement.

Current data caveat: existing Quidax captures have mostly flat prices. They validate plumbing and feed quality, not estimator quality.

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

## Standard Workflow

Inspect saved history before collecting:

```bash
sqlite3 -header -column data/cngn.db \
  "select source, count(*) as rows, datetime(min(timestamp_ms)/1000,'unixepoch') as first_utc, datetime(max(timestamp_ms)/1000,'unixepoch') as last_utc from price_snapshots group by source order by source;"

python scripts/report_fair_price_feed_quality.py \
  --db data/cngn.db \
  --out data/fair_price_feed_quality_current.md \
  --target-usd 100
```

Recommended separated capture:

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
  --iterations 1440
```

Export and analyze:

```bash
python scripts/export_fair_price_markouts.py \
  --db data/cngn.db \
  --out data/fair_price_markouts.csv \
  --horizons 10,30,60,120,300,600 \
  --target-usd 100 \
  --max-label-lag-seconds 15

python scripts/analyze_fair_price_markouts.py \
  --csv data/fair_price_markouts.csv \
  --out data/fair_price_markout_report.md
```

## Promotion Gate

Before changing `ExecutablePriceCalculator` or route sizing:

- current executable prices must move enough for ranking to be meaningful
- each horizon must report label count, missing labels, median lag, and max lag
- validation buckets must remain stable out of sample
- side-specific errors must improve for the relevant execution direction
- Quidax execution latency must support the chosen markout horizon

Archive detail: `autoresearch/archive/fair_price_autoresearch.md` and `autoresearch/archive/FAIR_PRICE_PROGRESS.md`.

