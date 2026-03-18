---
title: Market Data
order: 2
---

## Sources

The engine pulls from four data sources continuously:

**Quidax order book — REST polling**

The Quidax CEX API is polled on a short interval (`price_update_interval`, default 10s) for the full order book depth on the cNGN/USDT pair. The raw bids and asks are stored in memory and passed directly into the signal layer. There is no local aggregation — we use the full order book.

Quidax has two API keys: one for the arb trading account (`QUIDAX_API_KEY`) and one for the LP account (`QUIDAX_LP_API_KEY`). Market data uses neither; it hits the public depth endpoint.

**Bybit P2P — REST + fraud filtering**

Bybit's P2P market is the primary NGN/USD reference rate. However, the first several listings on any P2P board are almost always fraudulent or manipulated — inflated to bait inattentive buyers, or undercut to poison reference data.

The engine filters the Bybit feed before using it:
- Minimum transaction count threshold (filters new/fake accounts)
- Minimum completion rate
- Ignores top-N listings regardless (the most common fraud position)

The result is a VWAP over the filtered mid-market. This is the reference that feeds the blended price and the CEX-DEX comparison.

**Uniswap V4 pools (Base + BSC) — WebSocket**

Pool state is maintained via Alchemy WebSocket subscriptions. The listener (`engine/core/arbitrage/listener.py`) subscribes to the PoolManager contract address on each chain, filtered by the V4 Swap topic and pool ID:

```
V4_SWAP_TOPIC = 0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f
```

Each swap event carries the new sqrtPriceX96 and liquidity values inline, so the pool state cache (`_POOL_CACHE` in `pool_state.py`, keyed by pool ID) is updated with **zero RPC calls** — pure event parsing. This is what makes CEX-DEX detection latency sub-second.

If a pool event arrives and the cache is cold (e.g. first startup), `seed_pool_states()` is called as a background task to perform the initial state fetch.

**Blockradar — REST**

Blockradar provides a quote API for its fixed-rate swap system. The engine polls this for price awareness but does not currently use it as a live arb signal source.

## Price Aggregation and Normalisation

All venues quote prices in different units. The normaliser converts everything to a common basis: **USD per 1 cNGN**.

| Source | Raw format | Conversion |
|--------|-----------|------------|
| Uniswap Base | sqrtPriceX96 (cNGN/USDC pool) | Unpack Q96 fixed-point → price |
| Uniswap BSC | sqrtPriceX96 (cNGN/USDT pool) | Unpack Q96 fixed-point → price |
| Quidax | cNGN/USDT order book | Direct (USDT ≈ USD) |
| Bybit P2P | NGN/USDT (e.g. 1650) | `1 / rate` → ~0.000606 |

The blended price is a **VWAP** (volume-weighted average price) across venues weighted by their liquidity depth — deeper venues get more weight. This is published on the prices WebSocket channel and used as the reference for delta-neutrality calculations.

A **TWAP** (time-weighted average price) is also maintained over a rolling window and used for LP tick-range calculations. VWAP weights by volume; TWAP weights by time. For LP range-setting we want a smoothed view of where price has actually been trading — TWAP is less sensitive to transient spikes, which prevents unnecessary LP resets after brief volatility events.

## The Numeraire

What unit do we measure profit in?

Consider a DEX↔DEX arbitrage:
- **Base leg**: spend USDC, receive cNGN
- **BSC leg**: spend cNGN, receive USDT

Net cNGN delta = zero. We've converted USDC on Base into USDT on BSC at a favourable rate. The profit is naturally USD-denominated: a stablecoin surplus.

So **USD is the numeraire** because it matches what we actually earn.

There is a deeper layer: as the cNGN issuer, cNGN we buy back is a liability we've extinguished at a discount. Each completed arb round-trip slightly reduces net cNGN supply. This is worth tracking for long-term reporting even if it doesn't affect short-term P&L. Since global cNGN delta is zero per trade, the real risk is not directional cNGN exposure — it is per-chain stablecoin exhaustion and the cross-chain rebalancing cost when BSC is full of USDT but Base is short of USDC.
