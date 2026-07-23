# Fair Price Autoresearch

## Goal

Find a forward-looking cNGN/USD estimator for quoting, rebalancing, and market making without letting low-liquidity DEX prices become the truth label.

The live engine has three pricing layers in `engine/market/fair_price.py`:

- `MarketFairPrice`: neutral multi-factor market price.
- `ExecutableFairPrice`: short-horizon imbalance-adjusted executable price.
- `StrategyFairPrice`: inventory-skewed reservation price.

The research pipeline tests candidate estimators before any live behavior changes.

Current status: Fair Price/Quidax hypothesis testing is closed as diagnostic
research. We have historical Quidax top-of-book snapshots, but no additional
Quidax depth, trade, fill, or order-book history is expected. Binance `USDTNGN`
does not overlap the 2026 Quidax or Uniswap v4 windows, and the one-pass Bybit
P2P check did not produce historical coverage for the relevant timestamps.

Fair Price research should therefore stop waiting for a promotion-grade label.
The result is feed-quality and market-structure evidence: Quidax can be studied
as a manually managed top-book quote surface, but not as a depth-executable
fair-value target.

## Current Status

Implemented:

- Quidax and Bybit capture metadata in `price_snapshots.metadata_json`.
- Capture-only collector: `research/scripts/capture_fair_price_feeds.py`.
- Markout export: `research/scripts/export_fair_price_markouts.py`.
- Feed quality report: `research/scripts/report_fair_price_feed_quality.py`.
- Markout analyzer: `research/scripts/analyze_fair_price_markouts.py`.
- Binance/Quidax policy analyzer: `research/scripts/analyze_binance_fair_price.py`.
- Quidax book features: top-1/top-N imbalance, cNGN/USD pressure, OWA, microprice.
- DEX context features: previous-or-equal `uni-base_pool` / `uni-bsc_pool` premium versus Quidax executable mid.
- Walk-forward probability buckets for midpoint and side-specific adverse movement.

Current data caveat: existing Quidax captures are top-of-book only. They can
support quote-cadence, spread, quote-state markout, and managed-surface
diagnostics. They cannot support depth-walk execution, OWA, microprice,
imbalance, fill-probability, or realized CEX PnL tests.

DEX context caveat: the markout exporter expects previous-or-equal `uni-base_pool` and `uni-bsc_pool` rows in `price_snapshots`. Those rows must come from the pool-history bridge described in `research/autoresearch/data-methodology-refactor.md`; Quidax and Bybit capture jobs do not create them.

## Primary Label

The old target was future CEX executable value, ideally Quidax depth-walk value
at the tested size:

- midpoint label: future executable midpoint
- buy label: future cost to buy cNGN
- sell label: future proceeds from selling cNGN

Do not label against DEX mid, blended price, Bybit P2P, or Blockradar.

With no more Quidax depth history and no accepted overlapping external
reference, split the labels:

- `ExternalReferencePrice`: independent timestamped reference for cNGN/USD or
  native NGN/USDT, if a future source exists.
- `QuidaxManagedTopBook`: observed managed bid, ask, and midpoint.
- `TopBookExecutableProxy`: side-specific top-of-book bid/ask proxy, explicitly
  size-free and not depth-walk executable.

Promotion claims must use the label name. The current dataset supports
`QuidaxManagedTopBook` diagnostics only. It must not be described as a
depth-executable or independent fair-value model.

## Deferred Hypotheses

No longer testable without Quidax depth:

- Quidax depth-adjusted executable price beats ticker mid.
- Book imbalance, OWA, and microprice improve short-run direction hit rate.
- Side-specific depth-walk labels beat a symmetric midpoint.

No longer testable with currently available data:

1. Quidax top-of-book midpoint is a lagged, manually managed transform of an
   external Binance or Bybit reference.
2. Quidax spread width and update latency widen during external-reference moves.
3. A freshness-aware external anchor explains future Quidax midpoint better than
   stale Quidax midpoint at 60-600s horizons.
4. DEX premium versus an external reference explains LP stress and inventory
   markout better than DEX premium versus Quidax midpoint.

Still defensible as diagnostics:

1. Quidax quote cadence, spread, and top-book markout against its own future
   managed midpoint.
2. Quidax/Uniswap v4 overlap as a DEX-context sanity check.
3. Source-availability and label-discipline reporting for future data
   collection.

These diagnostics are not realized CEX execution tests.

## Binance Source Check

Status: checked 2026-07-10 after routing traffic through Switzerland.

Binance spot market data is reachable from that network path, and
`USDTNGN` exists on Binance spot. Current `exchangeInfo` reports:

- symbol: `USDTNGN`
- base: `USDT`
- quote: `NGN`
- status: `BREAK`

The most recent available Binance `USDTNGN` klines are:

- `1m`: last open `2024-03-07T02:59:00+00:00`, close `1518.40000000`
- `1h`: last open `2024-03-07T02:00:00+00:00`, close `1518.40000000`

This does not line up with the local Uniswap v4 cNGN pool launch. The local
pool-history replay starts much later:

- Base initialize/swap: `2026-03-04T16:51:45+00:00`
- BSC initialize: `2026-03-04T16:15:30+00:00`
- BSC first swap: `2026-03-04T16:24:35+00:00`

So Binance `USDTNGN` was already inactive by almost two years before the local
Uniswap v4 pools in this dataset. It cannot explain, validate, or replace the
April-July 2026 Quidax top-book sample.

Implemented source artifacts:

- `research/scripts/fetch_binance_reference.py`
- `research/data/binance_usdtngn_1m_reference.csv`
- `research/data/binance_usdtngn_1m_reference_metadata.json`
- `research/data/binance_fair_price_policy_rows.csv`
- `research/data/binance_fair_price_report.md`

The overlap fetch for `2026-04-08T11:33:53.112000+00:00` through
`2026-07-03T08:27:23.088000+00:00` wrote zero Binance reference rows. The
policy report therefore has 255,846 Quidax rows and 0 Binance-reference
observations. This is a useful coverage result, not estimator evidence.

## External Anchor Choice

For Fair Price research, prefer Bybit P2P over Uniswap v4 as the external anchor
when both are available.

Reasoning:

- Uniswap v4 cNGN pools are the DEX surfaces whose stress, premium, LP exposure,
  and inventory effects we are trying to explain. Using them as the Fair Price
  truth label would validate against the same market being studied and would
  make DEX premium circular.
- Bybit P2P is external to the DEX pools and remains closer to the intended CEX
  or off-chain USDT/NGN reference role, even though it is an advert-based P2P
  surface rather than firm executable depth.
- The local `data/cngn.db` overlap is small: Bybit has 171 rows from
  `2026-06-19T12:26:49+00:00` to `2026-06-20T01:09:16+00:00`. Joined to Quidax
  within 15 minutes, there are 1,088 overlapping rows, with median Bybit age
  about 65.7 seconds and max age about 581 seconds.
- In that overlap, Quidax cNGN/USD is approximately 16-78 bps above inverse
  Bybit USDT/NGN, with a median offset around 31 bps.

Conclusion:

- If genuinely new overlapping data is supplied, prefer an external Bybit P2P
  series to a circular DEX label for fair-price sanity checks.
- Use Uniswap v4 only as DEX context: premium, pool stress, LP inventory mark,
  and route-specific comparator.
- Do not use Uniswap v4 as the primary Fair Price label.
- Do not treat the current Bybit sample as promotion-grade. It is too short and
  cannot support estimator promotion.

## Quidax And Uniswap v4 Overlap Check

Status: checked 2026-07-10.

Artifact:

- `research/data/quidax_uniswap_v4_overlap_report.md`
- `research/data/quidax_uniswap_v4_overlap_rows.csv`

Method:

- Use the dense `quidax_cngn_usdt.json` top-book series.
- Match each Uniswap v4 pool feature row to the previous-or-equal Quidax row.
- Require Quidax source age no greater than 15 minutes.
- Normalize both prices to `cNGN per USDT`:
  - Quidax JSON is already in this convention.
  - Uniswap `raw_sqrt_mid` is inverted as `1 / raw_sqrt_mid`.

Overlap result:

| Pool | Calendar-overlap pool rows | Joined rows <=15m | Median DEX-Quidax bps | p10 bps | p90 bps | Median abs bps | Within 50 bps | Within 100 bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Base | 718 | 714 | -10.7 | -48.3 | +51.3 | 25.7 | 80.4% | 95.7% |
| BSC | 2,934 | 2,934 | -7.7 | -40.0 | +49.2 | 21.1 | 88.3% | 99.4% |

Sign convention: negative `DEX-Quidax bps` means Uniswap implies fewer cNGN per
USDT than Quidax, equivalent to a higher USD-per-cNGN price on Uniswap.

Interpretation:

- Quidax and Uniswap v4 are broadly in line over their overlapping timestamps.
  Typical gaps are tens of basis points, not percentage points.
- BSC is slightly tighter than Base in this event-anchored comparison.
- The tails are still material: observed dislocations reach roughly 160-174 bps
  in absolute value.
- This supports using Uniswap v4 as DEX context and LP inventory mark.
- It still does not make Uniswap v4 a clean Fair Price label, because that would
  validate the estimator against the same DEX surface whose stress and LP
  economics are being studied.

## Research Closeout

Closeout decision: Fair Price is a diagnostic result, not a promoted estimator.

The one-pass external reference attempt rejected all currently available
promotion paths:

- Binance `USDTNGN` is reachable but in `BREAK`; the latest 1m kline opens at
  `2024-03-07T02:59:00+00:00`, while the local Quidax JSON window is
  `2026-04-08T11:33:53.112000+00:00` through
  `2026-07-03T08:27:23.088000+00:00`.
- The Binance overlap fetch wrote zero reference rows for the 255,846-row
  Quidax sample.
- Official Bybit P2P docs expose an online-ad endpoint, not a historical archive.
- The legacy public Bybit endpoint is reachable for current ads, but local
  Bybit rows cover only 171 snapshots from
  `2026-06-19T12:26:49.707000+00:00` through
  `2026-06-20T01:09:16.620000+00:00`.
- Bank, CBN, FMDQ/NAFEM, and fintech quote APIs are useful for slower context,
  but not for 60-600 second fair-price labels or LP edge marks.

The July 2026 branch no longer includes fintech quote APIs, CBN/FMDQ/NAFEM
rates, or renewed Bybit historical searches. External-reference hooks remain
available only for genuinely new timestamped overlapping data supplied later;
acquiring that data is not an open task.

Allowed conclusion:

- Quidax top-book data is useful for quote-cadence, spread, self-markout, and
  managed-surface analysis.
- Quidax top-book data is not enough for depth-walk execution, fill-probability,
  or realized CEX PnL claims.
- Uniswap v4 remains DEX context and LP inventory mark, not Fair Price truth.
- Among the evaluated candidates, Bybit P2P would be the preferred external
  anchor only if a dense, timestamped overlapping dataset is supplied later.

Rejected conclusion:

- Do not claim Fair Price is solved.
- Do not claim Binance, Bybit, Quidax, or Uniswap supplies an accepted
  promotion-grade label for the current historical windows.
- Do not promote `ExecutableFairPrice` from this dataset.

Implementation status:

- `research/scripts/analyze_binance_fair_price.py` now builds the
  Quidax/Binance as-of table and policy report from files.
- The Quidax input is the existing top-of-book JSON shape with `ts`, `bid`,
  `ask`, `mid`, and optional `spread_bps`.
- The Binance/reference input can be JSON or CSV. It needs `timestamp_ms` or
  `ts`, plus one of `reference_price`, `price`, `mid`, `binance_reference`,
  `ngn_per_usdt`, or `usdt_ngn`.
- The report labels future Quidax managed top-of-book midpoint only. It does
  not test Quidax depth-walk execution.

Historical command if a future timestamped external reference file becomes
available:

```bash
python3 research/scripts/analyze_binance_fair_price.py \
  --quidax-json quidax_cngn_usdt.json \
  --binance-reference research/data/binance_reference.csv \
  --out-csv research/data/binance_fair_price_policy_rows.csv \
  --out-report research/data/binance_fair_price_report.md \
  --horizons 60,120,300,600 \
  --max-reference-age-seconds 3600 \
  --max-label-lag-seconds 60
```

Historical readout if a future overlapping external reference is supplied:

- If `reference_price` has lower short-horizon error than `current_quidax_mid`,
  the external source is a better anchor for quote freshness.
- If the edge appears only when reference age is large or labels are sparse,
  keep the result as feed-quality evidence rather than estimator evidence.
- If Quidax offsets and spread widen after external-reference moves, treat
  Quidax as a managed quote surface and avoid using it as the Fair Price truth
  label.

Rejected paths:

- Do not wait for Quidax depth history.
- Do not infer depth-walk execution from top-of-book rows.
- Do not use future Quidax midpoint alone as market truth, because Quidax is a
  manually managed quote surface tied to Binance.
- Do not promote DEX premium as the Fair Price label. DEX remains explanatory
  context and LP stress context.

## DEX Pool Context Requirements

This bridge is deferred for active LP work. DEX-only LP research can use
pool-history replay and causal pool feature tables directly without importing
DEX context into `price_snapshots`.

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
