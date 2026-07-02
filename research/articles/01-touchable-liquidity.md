# Touchable Liquidity: Why Local Stablecoins Need Market Makers

## Working Title

Touchable Liquidity: The Missing Market Layer for Local Stablecoins

## One-Sentence Thesis

Local stablecoins do not become infrastructure just because they are regulated, redeemable, or onchain; they become infrastructure when users can reliably enter, exit, hedge, and route through them at prices the market believes.

## Why This Fits LAVA

This should read like a market-structure sequel to LAVA's stablecoin and cNGN writing: start from the macro constraint, move into the rails, then show the boring machinery that lets the asset behave like money. Prior LAVA pieces frame African stablecoins around dollar scarcity, local monetary sovereignty, the currency perimeter, shadow OTC rails, and the need for open price discovery. This piece should add the missing operating layer: continuous CEX-DEX liquidity.

Useful prior LAVA anchors:

- "Stablecoins in Africa (Part I)" for USD scarcity and stablecoins as coordination infrastructure.
- "Stablecoins in Africa (Part II)" for local stablecoins, open systems, and intra-African trade.
- "How to Manufacture a Local Stablecoin" for cNGN as touchable money inside the currency perimeter.
- "Liquidity Will Find a Way" for the shadow rail, informal OTC liquidity, and deterministic settlement.

## Narrative Progression

### 1. The cold-start problem is not issuance; it is referenceability

Open with the cNGN premise: a local stablecoin can be compliant, onchain, and redeemable, but still fail to become a real settlement rail if users cannot quote it tightly or move size without guessing. The key question is not "can this token exist?" but "can the market treat it as a reliable NGN leg?"

Frame liquidity as the bridge between LAVA's "currency perimeter" argument and actual day-to-day usage. Regulated actors need defensible rails, but traders and fintechs need live prices, executable depth, and confidence that spreads will not explode at the moment they need to move.

### 2. Why CEX-DEX matters for African stablecoins

Explain the venues as distinct social and technical surfaces:

- Quidax order books are the currently executable CEX surface for cNGN/USDT.
- Uniswap V4 pools on Base and BSC are always-on public liquidity surfaces.
- Bybit P2P is a useful NGN/USD reference, but it is not directly executable for this engine.
- Blockradar and AssetChain are display/reference surfaces, not live fair-value truth.

The article should emphasize that CEX and DEX prices diverge for structural reasons: latency, fragmented inventory, different user bases, bank-hour constraints, gas, and local stablecoin novelty. The market maker's job is to compress those divergences when they are real opportunities, not to pretend one venue is always correct.

### 3. The repo's central design stance: do not let the noisiest venue become truth

Introduce the three price notions from `engine/market/fair_price.py`:

- `MarketFairPrice`: the neutral multi-factor market estimate.
- `ExecutableFairPrice`: the short-horizon, depth-aware executable estimate.
- `StrategyFairPrice`: the inventory-skewed reservation price for LP decisions.

The key editorial point: the engine treats "price" as plural because local stablecoin markets are not mature enough for a single canonical print. That is the market-structure lesson. The system separates where the market is, where it can actually trade, and where our inventory makes us willing to trade.

### 4. Hypotheses this repo is set up to test

- Quidax depth-adjusted executable price should beat ticker midpoint for 10-120 second markouts once there is enough price movement.
- Bybit P2P should help as a slower NGN/USD anchor at 300-600 seconds, but should not drive fast execution.
- DEX premium should explain local stress and LP outcomes, but should not become the truth label for fair value.
- Side-specific labels should matter because buying cNGN and selling cNGN are not symmetric when order-book depth is thin.
- Order-book imbalance, order-weighted average price, and microprice may improve short-run direction hit rate.

### 5. Results slot for the finished article

When markout data is mature, this section should include:

- feed-quality table: source counts, first/last timestamps, median row gaps, unique Quidax mids
- horizon table: 10s, 30s, 60s, 120s, 300s, 600s label counts and label lag
- estimator comparison: ticker mid vs top-book mid vs executable mid vs OWA vs microprice vs Bybit P2P vs DEX mids
- side-specific error table: buy cNGN and sell cNGN executable labels
- calibration bucket chart: imbalance/pressure buckets versus future adverse move probability

Until those results exist, the article should be explicit that current Quidax captures mostly validate plumbing and feed quality, not estimator superiority.

### 6. What this teaches the ecosystem

Tie back to the Flashbots analogy. Flashbots made a hidden market legible by releasing infrastructure and vocabulary around MEV search. This project can do something similar for African local stablecoin market making: show the data model, execution constraints, and research methodology instead of only publishing high-level claims that liquidity matters.

The call to action: more teams should publish market-making infrastructure, markout datasets, and venue-specific methodology so African stablecoins can compete on transparent liquidity rather than private screenshots and opaque OTC spreads.

## Likely Structure

1. The market does not believe a stablecoin until it can trade it.
2. The venues: CEX order book, DEX pool, P2P reference, fixed-rate quotes.
3. Why "fair price" has to be plural in immature markets.
4. What the current engine measures.
5. What the backtests/markouts will decide.
6. What open-sourcing changes.

## Evidence And Repo Anchors

- `engine/market/fair_price.py`
- `dashboard/docs/arbitrage/market-data.md`
- `research/autoresearch/fair-price.md`
- `research/scripts/capture_fair_price_feeds.py`
- `research/scripts/export_fair_price_markouts.py`
- `research/scripts/analyze_fair_price_markouts.py`
- `data/cngn.db` once publishing-ready counts are available

## Claims To Avoid Until Proven

- Do not claim the fair-price estimator beats alternatives before enough non-flat Quidax data exists.
- Do not claim DEX prices are reliable labels for NGN value.
- Do not claim Bybit P2P is executable.
- Do not claim the live engine is ready to promote research-only features into execution policy.
