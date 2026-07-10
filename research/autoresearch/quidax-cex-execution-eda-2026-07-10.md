# Quidax CEX Execution EDA

Date: 2026-07-10

## Scope

Analyze the new Quidax top-of-book JSON files:

- `quidax_cngn_usdt.json`
- `data/quidax_changes.json`

The files contain the same Quidax cNGN/USDT top-of-book process at different
densities. `data/quidax_changes.json` is the exact compressed quote-state
sequence from `quidax_cngn_usdt.json`; it is not an independent second sample.

The sample can test feed quality, quote-update cadence, short-horizon top-book
markouts, and proxy behavior for the CEX ladder anchor modes. It cannot test
true Quidax depth-walk execution, order-book imbalance, OWA, microprice, fill
probability, or realized PnL because these JSON rows do not include depth levels,
order flow, order size, fills, or the Binance reference used by the cNGN team.

Important market-structure caveat: the only active market makers on Quidax are
the cNGN team, and they manually update best bid and offer from a Binance
mid-price. Quidax top-of-book is therefore a policy-managed quote stream, not an
independent market-clearing truth label.

## Data Quality

| Metric | Value |
|---|---:|
| Dense rows | 255,846 |
| Compressed quote states | 102 |
| Complete bid/ask/mid states | 99 |
| First timestamp UTC | 2026-04-08T11:33:53.112Z |
| Last timestamp UTC | 2026-07-03T08:27:23.088Z |
| Coverage | 85.87 days |
| Median dense row gap | 37.786s |
| 95th percentile dense row gap | 44.109s |
| Max dense row gap | 53.27h |
| Non-increasing timestamp gaps | 0 |
| Rows unchanged from previous row | 99.9605% |
| Rows with missing ask | 297 |
| Quote states with missing ask | 3 |
| Unique mid values | 87 |
| Unique quote tuples | 89 |

Quote-change timing is highly uneven:

| Metric | Value |
|---|---:|
| Median quote-state duration | 72.6s |
| 90th percentile quote-state duration | 79.4h |
| Max quote-state duration | 17.10d |
| Median gap between quote-state starts | 69.9s |
| 90th percentile gap between quote-state starts | 79.4h |

Spread depends heavily on whether each quote state is weighted equally or by
time. Equal state weighting overweights brief manual-update episodes; dense rows
approximate duration weighting.

| Spread view | Value |
|---|---:|
| Median complete-state spread | 113.6 bps |
| 10th percentile complete-state spread | 24.5 bps |
| 90th percentile complete-state spread | 402.7 bps |
| Time-weighted complete-state spread | 42.3 bps |

## Fixed-Horizon Top-Book Markouts

For each complete quote state, labels use the first dense observation at or after
`t + horizon`, with a maximum label lag of 120 seconds. The three tested
top-book execution views are:

- `midpoint`: current midpoint valued against future midpoint.
- `buy_cngn`: buy cNGN with USDT at the current native bid and value at future
  midpoint.
- `sell_cngn`: sell cNGN for USDT at the current native ask and value against
  future midpoint.

Positive `buy_cngn` means buying cNGN now beats waiting until the future
midpoint. Positive `sell_cngn` means selling cNGN now beats waiting until the
future midpoint.

### State-Based Inference

Block bootstrap confidence intervals resample UTC days. This is the better
inferential view because the 255k dense rows are mostly repeated quotes.

| Horizon | States | Days | Future mid changed | Midpoint mean bps (95% CI) | Buy cNGN mean bps (95% CI) | Sell cNGN mean bps (95% CI) |
|---:|---:|---:|---:|---:|---:|---:|
| 10s | 97 | 23 | 46 | -2.5 [-10.1, 3.3] | -93.7 [-133.6, -54.8] | -90.4 [-123.9, -53.9] |
| 30s | 98 | 23 | 47 | -2.5 [-9.5, 3.3] | -92.9 [-134.6, -52.6] | -89.6 [-122.9, -54.8] |
| 60s | 98 | 23 | 50 | -0.7 [-13.4, 8.8] | -91.2 [-134.0, -52.7] | -91.4 [-125.1, -54.4] |
| 120s | 97 | 23 | 53 | 9.6 [-4.0, 22.6] | -80.1 [-118.5, -45.9] | -100.5 [-141.0, -60.2] |
| 300s | 98 | 23 | 56 | 19.7 [-11.1, 48.4] | -69.3 [-115.2, -37.5] | -109.3 [-161.9, -59.8] |
| 600s | 98 | 22 | 61 | 22.8 [-3.9, 49.0] | -67.9 [-106.6, -37.9] | -114.1 [-165.2, -60.6] |

Interpretation:

- The midpoint has no statistically reliable directional edge at any tested
  horizon. Every midpoint confidence interval includes zero.
- Crossing the top of book has a reliably negative future-mid markout at every
  horizon. That result is robust in both buy and sell directions.
- The negative side-specific result is not a strategy discovery; it is the
  expected spread cost when no independent signal is present.

### Duration-Weighted View

Dense rows are not independent, but they approximate the experience of a random
arrival over clock time. Under this view, the midpoint is essentially flat and
crossing either side costs about half the time-weighted spread.

| Horizon | Dense labels | Future mid changed share | Midpoint mean bps | Buy cNGN mean bps | Sell cNGN mean bps |
|---:|---:|---:|---:|---:|---:|
| 10s | 255,326 | 0.0505% | -0.001 | -21.39 | -21.45 |
| 30s | 255,496 | 0.0513% | -0.001 | -21.38 | -21.44 |
| 60s | 255,430 | 0.0756% | 0.0005 | -21.38 | -21.45 |
| 120s | 255,364 | 0.1057% | 0.003 | -21.38 | -21.45 |
| 300s | 255,343 | 0.1946% | 0.005 | -21.36 | -21.44 |
| 600s | 255,066 | 0.3246% | 0.017 | -21.36 | -21.45 |

Interpretation:

- The dense sample is mostly a heartbeat of unchanged quotes.
- A naive row-count-based test would overstate confidence because almost all rows
  are duplicates of the same few quote states.
- For random arrival, expected spread-crossing cost is about 21 bps. For
  quote-change-state inference, the cost looks larger because brief high-spread
  manual-update states get equal weight.

## CEX Ladder Anchor Modes

The repo has three CEX ladder anchor modes in `CexParams.anchor_source`:

- `quidax`: anchor to Quidax top-book midpoint.
- `dex_vwap`: anchor to DEX-only VWAP.
- `blended`: anchor to the blended market reference.

The JSON files directly support only the `quidax` anchor. The other two require
as-of DEX pool joins, volume/TWAP state, and live source metadata. This EDA uses
a proxy only:

- `dex_vwap_equal_proxy`: equal-weight as-of Base and BSC sqrt-derived pool
  mids, then inverted to native cNGN per USDT.
- `blended_equal_proxy`: equal-weight Quidax plus Base plus BSC normalized
  cNGN/USD mids, then inverted to native cNGN per USDT.

The proxy sample is limited to the overlap before the DEX pool histories end:
83 complete Quidax states across 17 UTC days. Median as-of age was 0.94h for
Base and 0.38h for BSC; max as-of age was 8.63h for Base and 2.31h for BSC.

### Anchor Error Versus Future Quidax Mid

| Horizon | Anchor | MAE bps | Bias bps | RMSE bps | Median abs error bps |
|---:|---|---:|---:|---:|---:|
| 10s | `quidax` | 13.7 | -3.5 | 43.6 | 0.0 |
| 10s | `dex_vwap_equal_proxy` | 68.2 | -58.9 | 85.4 | 43.1 |
| 10s | `blended_equal_proxy` | 49.9 | -40.6 | 63.6 | 29.5 |
| 60s | `quidax` | 17.3 | -1.3 | 46.4 | 0.7 |
| 60s | `dex_vwap_equal_proxy` | 66.4 | -56.8 | 84.5 | 42.4 |
| 60s | `blended_equal_proxy` | 49.8 | -38.4 | 63.5 | 30.0 |
| 600s | `quidax` | 50.1 | 31.1 | 79.2 | 18.4 |
| 600s | `dex_vwap_equal_proxy` | 37.0 | -24.8 | 58.7 | 14.9 |
| 600s | `blended_equal_proxy` | 39.1 | -6.3 | 53.8 | 23.5 |

Paired day-block bootstrap on absolute error differences:

| Horizon | Difference | Mean bps | 95% CI | Interpretation |
|---:|---|---:|---:|---|
| 10s | DEX proxy minus Quidax | +54.5 | [+37.0, +73.7] | Quidax anchor is closer |
| 10s | Blended proxy minus Quidax | +36.2 | [+24.2, +48.8] | Quidax anchor is closer |
| 60s | DEX proxy minus Quidax | +49.2 | [+32.9, +64.8] | Quidax anchor is closer |
| 60s | Blended proxy minus Quidax | +32.5 | [+21.0, +42.6] | Quidax anchor is closer |
| 600s | DEX proxy minus Quidax | -13.1 | [-50.7, +26.8] | Not statistically resolved |
| 600s | Blended proxy minus Quidax | -11.0 | [-35.3, +15.2] | Not statistically resolved |

Interpretation:

- `quidax` is the only statistically defensible short-horizon CEX ladder anchor
  from this sample, if the objective is to stay close to the cNGN team's current
  Quidax quote.
- This is partly tautological because the label is future Quidax and the cNGN
  team manually manages Quidax around an off-sample Binance reference.
- At 600s the DEX and blended proxies are not clearly worse, but the confidence
  intervals include zero and the proxy does not reconstruct live volume/TWAP
  weighting. Do not promote this as evidence that DEX anchoring is better.

## Statistically Rigorous Conclusions

1. The two JSON files provide one useful historical top-of-book sample, not two
   independent datasets.
2. Raw row count is misleading. The effective sample for inference is roughly
   99 complete quote states across 23 UTC quote-change days.
3. Quidax was mostly flat at 10-600 second horizons. In raw dense rows, future
   midpoint changed only 0.05% of 10s labels and 0.32% of 600s labels.
4. There is no statistically reliable midpoint directional edge in this sample.
5. Crossing the Quidax top of book without an independent signal has a reliably
   negative markout. This is spread cost, not alpha.
6. The `quidax` CEX anchor is statistically closer to future Quidax midpoint
   than the available DEX/blended proxies at 10s and 60s.
7. The 600s anchor comparison is unresolved; the DEX/blended proxies look
   competitive on point estimates but not with enough confidence to promote.
8. The sample does not support claims about depth-adjusted execution, OWA,
   microprice, imbalance, fill probability, or realized market-making PnL.
9. Because the cNGN team manually updates Quidax from Binance mid, an independent
   Binance reference is required before Quidax can be used as a fair-value truth
   label rather than a managed quote series.

## Closeout For Fair Price

Close Fair Price as feed-quality and market-structure evidence, not as a live
`ExecutableFairPrice` promotion.

Reason:

1. This JSON is ticker/top-book only, not depth-walk executable data.
2. Quidax depth history remains unavailable.
3. Binance `USDTNGN` does not overlap the 2026 Quidax window.
4. The one-pass Bybit P2P check found current ads and a short local June sample,
   but no historical coverage for the relevant Fair Price or LP windows.
5. The three CEX ladder anchor modes can be discussed as design modes, but this
   sample only validates `quidax` as a managed-top-book anchor to future Quidax
   top book. It does not validate executable CEX mode PnL.

No live Fair Price or CEX ladder policy should change from this dataset.

## Closeout For DEX LP

Close DEX LP as a diagnostic result.

Reason:

1. This Quidax sample lacks executable depth, independent CEX fair value, fills,
   and order sizes.
2. The strict Base directional LP slice is preserved as DEX-internal diagnostic
   evidence, not promotion evidence.
3. BSC remains the falsification pool and rejects the same strict QTS gates.
4. The external-reference comparator cannot be populated from currently
   available Binance or Bybit data.
5. H12 capacity curves, dynamic sizing, and live LP policy integration remain
   blocked until genuinely new timestamped non-pool cNGN marks arrive.

The Quidax sample is useful for explaining why data discipline matters. It is
not strong enough to change live Fair Price, CEX execution, or DEX LP behavior.

## Article Timing

The first Lava article, "Market making for the rest of the world," follows a
clear pattern:

- start from geographic decentralization and why local stablecoin markets matter
- explain the mechanism that gives local market makers durable advantage
- introduce the open-source market maker as a public standard, not a finished
  victory lap
- explicitly promise follow-up articles on the research branch: pricing,
  backtesting, lessons from the data, and more
- close with an ecosystem/public-good argument

Recommended writing posture:

1. Start the next article scaffold now, but do not publish an empirical results
   article yet.
2. The next publishable piece should be a methodology/history article, not an
   alpha-claim article. Working thesis: the wrong label can make any local
   stablecoin strategy look smart, so the research branch exists to make every
   assumption falsifiable.
3. Use this Quidax EDA as a negative-evidence example: the data is valuable, but
   it mainly proves why naive row-count confidence, Quidax-only labels, and
   spread-crossing simulations are insufficient.
4. Begin the full results draft only after one of two gates clears:
   - Fair Price gate: Binance reference plus Quidax top-of-book history supports
     a defensible anchor/freshness model.
   - DEX LP gate: the reduced Base directional policy either survives or fails
     the next static/hold/comparator pass.

The next article can be outlined immediately around the research branch's
history, but the evidence section should stay in placeholder/result-slot form
until one of those gates is closed.
