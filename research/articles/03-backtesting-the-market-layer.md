# Backtesting the Market Layer

## Working Title

Backtesting the Market Layer: What We Need to Prove Before Local Stablecoin Liquidity Can Scale

## One-Sentence Thesis

The honest way to build market infrastructure for local stablecoins is to turn every trading belief into a timestamped hypothesis, then publish the tests that survive contact with venue-specific data.

## Why This Fits LAVA

This should be the empirical capstone. LAVA has a precedent for data-first pieces that replicate external claims, inspect methodology, label actors, and separate count-based optimism from volume-based reality. This article should bring that same discipline to cNGN market making: no vibes, no hero charts without caveats, no promotion of research features into live policy until the evidence earns it.

## Narrative Progression

### 1. Start with the methodological trap

Open with the central mistake this repo is trying to avoid: using the market being optimized as its own truth label.

For example, if a fair-value model uses DEX premium as an input and then validates itself against future DEX mid, the test can accidentally reward circularity. The repo's stance is that short-horizon fair-price labels should be future Quidax executable value, while DEX premium remains a control and explanatory variable.

This makes a good LAVA-style opening because it challenges a lazy industry habit: publishing backtests that look rigorous but quietly validate against the wrong thing.

### 2. The three research tracks

Organize the repo's research into three tracks:

#### Track A: Fair-price markouts

Question: Which current features predict future executable cNGN/USD value over 10-600 seconds?

Inputs:

- Quidax ticker and order-book depth
- Quidax imbalance, pressure, OWA, microprice
- Bybit P2P reference feed
- previous-or-equal DEX premium and pool context

Labels:

- future Quidax executable midpoint
- future Quidax buy cNGN executable cost
- future Quidax sell cNGN executable proceeds

#### Track B: LP policy backtests

Question: Which range, sizing, and rebalance policies survive walk-forward validation after costs?

Inputs:

- venue-local pool history
- realized volatility and liquidity features
- swap-flow imbalance
- DEX premium and stress features once causal feature tables are complete

Primary discipline:

- keep `uni-base` and `uni-bsc` separate unless explicitly testing transferability
- use swap-count walk-forward windows when event density is uneven
- compare against EWMA baseline, static LP allocation, and hold inventory where available

#### Track C: Execution and routing diagnostics

Question: Which apparent opportunities remain profitable after depth, slippage, gas, inventory caps, and recovery risk?

Inputs:

- route registry definitions
- route-selected size and profit metadata
- gas oracle costs
- venue-local inventory
- realized execution records

### 3. What has already been implemented

Summarize current repo state:

- capture-only fair-price feed collector
- feed quality report
- fair-price markout exporter
- fair-price analyzer with estimator tables and probability buckets
- LP backtester with walk-forward outputs under `research/results/`
- LP research docs that distinguish virtual strategy backtests from paper-faithful LP reconstruction
- data methodology docs noting that historical V4 pool CSVs have mixed `cngn_usd_price` semantics and that `sqrt_price_x96` is the canonical marginal pool price

Be explicit that fair-price results are not yet ready for strong claims because recent Quidax captures were mostly flat.

### 4. What current backtest artifacts can support

Current LP artifacts can support a cautious process story:

- the repo has walk-forward validation output for Uniswap V4 LP strategies
- the BSC calibrated h7 summary covers 1,628 events from 2026-03-04 to 2026-05-12 with 17 valid windows
- aggregate CSVs compare paper-style and EWMA strategy variants across validation return, drawdown, fees, transaction costs, positive-window rate, and rebalance count

The article should avoid cherry-picking a top row as a durable result until the methodology is frozen. Use current results to show the shape of the reporting, not to claim final strategy superiority.

### 5. Result slots for the finished article

#### Fair-price markout tables

- rows analyzed
- timestamp coverage
- unique executable mids
- label count and lag per horizon
- MAE, signed bias, RMSE, and direction hit rate per estimator
- probability bucket tables for imbalance and pressure features

#### LP walk-forward tables

- dataset coverage by venue
- valid and skipped windows
- selected parameter stability across neighboring windows
- validation net return distribution
- positive-window rate
- worst-window drawdown and divergent loss
- fee-to-cost ratio
- rebalance count and churn
- capacity sensitivity
- PBO or overfitting diagnostics

#### Execution diagnostics

- expected vs realized route profit
- route family contribution
- rejected opportunity reasons
- inventory penalty impact
- half-open/recovery count
- gas sensitivity by chain

### 6. The hypotheses that would matter if confirmed

- If Quidax executable depth beats ticker mid, market makers should stop quoting local stablecoins from naive mids.
- If Bybit P2P only helps slower horizons, P2P should be treated as macro/reference context rather than fast execution truth.
- If DEX premium predicts LP outcomes but not CEX executable labels, DEX pools are useful stress sensors, not fair-value oracles.
- If side-specific labels outperform symmetric midpoint labels, local stablecoin market making should optimize buy and sell decisions separately.
- If parameter choices jump across walk-forward windows without a measured regime explanation, apparent alpha is probably overfit.
- If gas and rebalance penalties erase most raw spread, the market needs more venue-local inventory or better rebalancing rails, not more aggressive bots.

### 7. What could break the story

Include a LAVA-style "where this breaks" section:

- Quidax API latency or cache behavior could make 10-30 second labels unrealistic.
- Quidax price flatness could limit short-term estimator ranking.
- LP backtests can overstate performance if liquidity-operation prices use block-end state or if fees/gas are not attributed per episode.
- Route profitability can vanish once inventory localization and rebalancing costs are enforced.
- A market maker that improves spreads may still be too capital-intensive to matter at ecosystem scale.

### 8. Close with the public-good argument

The conclusion should tie empirical transparency to ecosystem formation. Local stablecoin markets need more than issuers and apps; they need shared measurement conventions, public failure modes, and comparable backtests. Open-sourcing the repo matters because it lets other builders copy the testing discipline, dispute assumptions, and run their own venue-specific versions.

## Likely Structure

1. The wrong label can make any strategy look smart.
2. What this repo tests.
3. What is already implemented.
4. The evidence we have versus the evidence still owed.
5. The results tables the final article should publish.
6. What would change our mind.
7. Why empirical infrastructure should be public.

## Evidence And Repo Anchors

- `research/autoresearch/fair-price.md`
- `research/autoresearch/lp.md`
- `research/autoresearch/data-methodology-refactor.md`
- `research/backtester/`
- `research/results/`
- `research/scripts/export_fair_price_markouts.py`
- `research/scripts/analyze_fair_price_markouts.py`
- `research/scripts/report_fair_price_feed_quality.py`
- `research/tests/test_export_fair_price_markouts.py`
- `research/tests/test_analyze_fair_price_markouts.py`
- `research/tests/test_pool_price_semantics.py`
- `research/tests/test_backtester.py`

## Claims To Avoid Until Proven

- Do not claim final LP profitability from preliminary aggregate CSVs.
- Do not compare Base and BSC as if they are one market unless transferability is explicitly tested.
- Do not use "next future event" labels for fixed 10-600 second markouts; use executable state as of `t+h`.
- Do not let DEX mid become the label for a CEX-led fair-price estimator.
- Do not publish charts without source-age, missing-label, and stale-feature caveats.
