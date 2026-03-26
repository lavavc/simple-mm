---
title: Market Data
order: 2
---

## Sources

Six venues feed the price pipeline. Four contribute to fair-value calculations; two are display-only.

**Bybit P2P — REST + fraud filtering**

Bybit's P2P market is the primary NGN/USD reference rate. Raw listings contain manipulated and retail-noise prices, so the engine filters before using any data:
- Ads are filtered by merchant quality: minimum completed order count, completion rate ≥ 90%, and maximum release time
- Ads outside the ₦5M–₦20M total size band are excluded: below that is retail noise, above that is whale/outlier territory
- Prices more than 2% from the median of the remaining ads are removed
- The modal price (most frequently occurring integer NGN rate) of the survivors is used as the side price

The result is the rate the largest cohort of reputable mid-market merchants agree on. This is the primary input that determines the NGN leg of the blended price.

**Quidax order book — REST polling**

The Quidax CEX API is polled on a short interval (`price_update_interval`, default 10s) for the full order book depth on the cNGN/USDT pair. The raw bids and asks are stored in memory and passed directly into the signal layer. Market data hits the public depth endpoint; the two Quidax API keys (`QUIDAX_API_KEY`, `QUIDAX_LP_API_KEY`) are used only for trading.

**Uniswap V4 pools (Base + BSC) — WebSocket**

Pool state is maintained via Alchemy WebSocket subscriptions. The listener subscribes to the PoolManager contract on each chain, filtered by the V4 Swap topic and pool ID:

```
V4_SWAP_TOPIC = 0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f
```

Each swap event carries the new `sqrtPriceX96` and liquidity values inline, so the pool state cache is updated with zero RPC calls. This is what makes CEX-DEX detection latency sub-second. If the cache is cold at startup, `seed_pool_states()` is called as a background task to perform the initial state fetch.

**AssetChain — WebSocket (display only)**

AssetChain hosts a small cNGN/USDT pool. Its price is included in the dashboard price display but excluded from fair-value calculations — volume is negligible and including it would dilute the VWAP without improving accuracy.

**Blockradar — REST (display only)**

Blockradar provides a quote API for its fixed-rate swap system. Its price is included in the dashboard display but excluded from VWAP and TWAP fair-value calculations — it is a rate-setter, not a price-taker, so it reflects where Blockradar wants to trade, not where the market is.

## Price Normalisation

All venues quote in different units. The normaliser converts everything to a common basis: **USD per 1 cNGN**.

| Venue | Raw pair | Conversion |
|-------|----------|------------|
| Bybit P2P | USDT/NGN (e.g. 1600) | `1 / rate` → ~0.000625 |
| Quidax | cNGN/USDT | Direct (USDT ≈ USD) |
| Uniswap Base | sqrtPriceX96, cNGN/USDC pool | Unpack Q96 fixed-point |
| Uniswap BSC | sqrtPriceX96, cNGN/USDT pool, inverted | Unpack Q96 fixed-point, invert |
| AssetChain | sqrtPriceX96, cNGN/USDT pool | Unpack Q96 fixed-point, invert |
| Blockradar | cNGN/USDC | Direct |

Adding a new pair from any venue only requires adding its string to `CNGN_USD_PAIRS` or `INVERTED_PAIRS` in `price_aggregation.py`.

## Blended Price

The blended price combines a VWAP (current snapshot) with two TWAP windows (5-minute and 1-hour). It is published on the prices WebSocket channel and used as the reference for LP tick-range calculations and portfolio delta management.

### VWAP

The VWAP is computed across the four fair-value venues (Bybit, Quidax, uni-base, uni-bsc) with each venue weighted by its effective market depth or volume. The weights are not equal — they reflect how much liquidity each venue actually represents.

**Bybit** — Bybit does not expose a 24h traded P2P volume figure via its API. Instead we derive a depth proxy: fetch page 1 (200 ads) of the buy-side order book, sum `lastQuantity` (remaining USDT available on each ad), then extrapolate to the full ad count using `result.count`. This gives total listed depth across all active buy ads. We then apply a utilization factor (`depth_utilization`, default 5%) on the basis that most listed P2P depth is not actually traded. At typical book sizes (~$30–35M total listed buy-side depth) this produces a proxy of ~$1.5–1.8M, making Bybit the highest-weighted venue. The depth is refreshed every 5 minutes.

**Quidax** — The `/markets/tickers` response includes a `vol` field (24h traded volume in USDT). This is used directly as the VWAP weight.

**Uniswap Base** — 24h volume is tracked on-chain. Every V4 Swap event carries the stable-side token delta inline; the engine extracts the USDC amount from each event and accumulates it in a rolling 24h window. The window is seeded at startup and on WebSocket reconnect by scanning the last 24h of swap logs directly from the RPC. Live events keep it current with zero additional RPC calls.

**Uniswap BSC** — Same on-chain rolling window as uni-base, using the USDT delta from each swap event. Both pools now have independent real volume figures; the previous 0.33× ratio derived from uni-base is removed.

If a venue has no volume data and no derivable proxy, it is excluded from that VWAP cycle with a warning log. There is no silent fallback to equal weighting.

### TWAP

TWAP is computed from stored price snapshots in the database. The 5-minute window smooths out short-term noise; the 1-hour window provides a slower-moving anchor. Blockradar and AssetChain snapshots are excluded from TWAP for the same reasons they are excluded from VWAP. If either TWAP window has insufficient history (e.g. at startup), it falls back to the current VWAP.

### Confidence Score

The blended price carries a confidence score between 0 and 0.9 (never 1.0 — full confidence is never appropriate for NGN price data). It starts at 0.9 when all venues report successfully, and drops by 0.2 for each venue that fails to return a valid price. The total venue count is derived from the live fetch result each cycle, not hardcoded, so it self-corrects as venues are added or removed.

| Venues reporting | Confidence |
|-----------------|------------|
| All 6 | 0.90 |
| 5 | 0.70 |
| 4 | 0.50 |
| 3 | 0.30 |
| 2 | 0.10 |
| 1 or 0 | 0.00 |

## The Numeraire

What unit do we measure profit in?

Consider a DEX↔DEX arbitrage:
- **Base leg**: spend USDC, receive cNGN
- **BSC leg**: spend cNGN, receive USDT

Net cNGN delta = zero. We've converted USDC on Base into USDT on BSC at a favourable rate. The profit is naturally USD-denominated: a stablecoin surplus.

So **USD is the numeraire** because it matches what we actually earn.

There is a deeper layer: as the cNGN issuer, cNGN we buy back is a liability we've extinguished at a discount. Each completed arb round-trip slightly reduces net cNGN supply. This is worth tracking for long-term reporting even if it doesn't affect short-term P&L. Since global cNGN delta is zero per trade, the real risk is not directional cNGN exposure — it is per-chain stablecoin exhaustion and the cross-chain rebalancing cost when BSC is full of USDT but Base is short of USDC.
