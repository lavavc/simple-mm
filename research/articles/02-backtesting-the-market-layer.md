# Backtesting The Market Layer

## Working Title

Backtesting The Market Layer: What We Tried To Prove Before Scaling cNGN
Liquidity

## One-Sentence Thesis

The honest way to build market infrastructure for local stablecoins is to turn
every trading belief into a timestamped hypothesis, then publish the tests that
survive contact with venue-specific data.

## Why This Fits Lava

This is the empirical sequel to the supervised-liquidity essay. Article 1 makes
the policy case for growing cNGN liquidity inside a supervised perimeter. This
article shows the research discipline required before turning that argument into
live market-making policy.

The point is not that the research branch discovered a profitable strategy ready
to deploy. It mostly did the opposite: it found that the available data can
support diagnostic market-structure claims, but not a promoted Fair Price model
or a live DEX LP strategy. That is the article's value. It demonstrates how to
avoid circular labels, stale references, and attractive backtests that do not
survive venue-specific constraints.

```text
CPL_EDITORIAL_STATUS: DESIGN_APPROVED_RESULTS_PENDING
CPL_PRIMARY_CLASS: UNAVAILABLE
CPL_REVERSE_CLASS: UNAVAILABLE
CPL_ARTICLE_BRANCH: UNAVAILABLE
CPL_ECONOMIC_CLASS: UNAVAILABLE
CPL_ROBUSTNESS_STATUS: UNAVAILABLE
CPL_SOURCE_MANIFEST: research/results/cross_pool_lead_lag/article_manifest.json
```

## Narrative Progression

### 1. The wrong label can make any strategy look smart

Open with the methodological trap. If a model uses DEX premium as a feature and
then validates itself against future DEX mid, the test can reward circularity.
If a market maker quotes from top-book midpoint without knowing depth, fills, or
account inventory, the backtest can look cleaner than execution reality.

The repo's standard is stricter: separate market context from truth labels,
disclose source age, and reject a result when the comparator is not good enough.

### 2. The repo tested three tracks

Fair Price:

- Question: can current data identify a reliable cNGN fair-price series for
  short-horizon execution or LP decisions?
- Desired label: future executable cNGN value from a non-circular, timestamped
  source.
- Result: not enough independent data to promote the model.

DEX LP:

- Question: can directional Uniswap v4 LP ranges improve risk-adjusted outcomes
  after walk-forward validation?
- Desired comparator: a non-pool cNGN inventory mark covering the relevant
  validation windows.
- Result: the Base strict-QTS policy did not transfer to BSC; no live LP
  promotion is justified.

CEX execution-mode feasibility:

- Question: can Quidax anchor, DEX VWAP, or blended modes be evaluated against
  the available Quidax sample?
- Desired data: top-book plus depth, fills, and an independent Binance reference
  mark.
- Result: only top-book proxy tests are possible. Crossing either side of the
  Quidax top book has reliably negative future-mid markout, which is spread cost
  rather than alpha.

### 3. The external-reference attempt failed cleanly

State the source availability facts directly:

- Binance `USDTNGN` exists but is in `BREAK`.
- The latest available Binance 1-minute kline opens at
  `2024-03-07T02:59:00+00:00`, close `1518.40000000`.
- The Quidax JSON window starts on `2026-04-08`, so Binance contributes zero
  overlapping reference observations.
- Bybit P2P exposes current online ads, not a historical archive.
- The local Bybit sample has only 171 snapshots from
  `2026-06-19T12:26:49.707000+00:00` through
  `2026-06-20T01:09:16.620000+00:00`.
- Bybit covers `0/8` strict Base edge marks under the one-hour max-age rule.

This is a publishable negative result. It prevents the article from pretending
there is a clean non-pool reference series when there is not.

### 4. What Quidax can and cannot prove

The Quidax JSON sample is large but low-dimensional:

- 255,846 dense rows
- 102 compressed quote states
- top-book only
- no depth ladder
- no fills
- no order sizes
- no independent Binance mark in the sample
- manually managed best bid and offer from the cNGN team, based on Binance mid

The statistically rigorous conclusion is narrow. State-based inference found no
reliable midpoint directional edge at 10-600 second horizons; all midpoint
confidence intervals include zero. Crossing the best bid or offer has negative
future-mid markout at each tested horizon, which is the expected cost of paying
spread in a mostly managed quote surface.

### 5. What Quidax and Uniswap v4 show together

The overlap evidence is useful but should not be overclaimed:

| Pool | Joined rows <=15m | Median DEX-Quidax bps | p10 bps | p90 bps | Median abs bps | Within 50 bps | Within 100 bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Base | 714 | -10.7 | -48.3 | +51.3 | 25.7 | 80.4% | 95.7% |
| BSC | 2,934 | -7.7 | -40.0 | +49.2 | 21.1 | 88.3% | 99.4% |

Interpretation: Quidax and Uniswap v4 were broadly aligned over overlapping
timestamps, usually tens of basis points apart. The tails still reach roughly
160-174 bps. Uniswap v4 is therefore useful as DEX context and LP inventory
mark, but not as a clean Fair Price label.

### 6. What the DEX LP experiments can support

The completed strict-QTS policy-transfer conclusion is diagnostic:

| Pool | Gate | Active windows | Directional sum | Worst directional | Positive directional | Directional minus hold | Best static sum |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Base | `gate_strict_qts_20_25` | 4 | +1.039% | +0.118% | 100.0% | +0.228 pp | +0.796% |
| BSC | `gate_strict_qts_20_25` | 7 | -1.272% | -0.842% | 14.3% | -1.602 pp | +0.400% |

Base has a sparse DEX-internal regime worth preserving as a hypothesis. The
Base strict-QTS 20/25 directional LP policy did not transfer to BSC: it
returned -1.272% across seven BSC windows, versus +1.039% across four Base
windows.

That result concerns policy transferability. It does not test whether lagged
BSC pool prices contain incremental information about future Base price
changes. Because no usable non-pool comparator covers the relevant windows,
the article should not call this live LP alpha.

### 7. The separate cross-pool information-transfer experiment

The failed strict-QTS policy transfer does not answer whether one pool contains
incremental information about the other. The approved experiment uses each
pool's canonical `raw_sqrt_mid` and checks stability across the known
early/late methodology boundaries rather than reconstructing legacy prices.

At each epoch-aligned decision time, a causal as-of panel carries forward only
the last state already observable in each pool and reports that state's age.
The preregistered horizons are a one-hour primary test with 15-minute and
four-hour sensitivities. The confirmatory direction compares nested models: the
cross-pool model adds BSC features to a Base-only baseline after a 14-day warmup
and expanding weekly walk-forward refits. A reverse Base-to-BSC falsification
uses the same construction.

Once data QA and causal alignment pass, the economic follow-up is an
unconditional frozen-policy evaluation. It applies the one-hour forecast only
as an entry veto to the existing Base policy and does not retune routes, ranges,
sizing, exits, gas, or validation windows in response to the statistical
result.

The comparison assumes USDC/USDT parity, so effects below 10 basis points are
not economically interpretable without a historical basis series. Even reviewed
results cannot establish causal price discovery, toxic flow, external-LP
profitability, or deployable alpha from these two pool histories.

### 8. Why the failures are the point

The research branch produced reusable discipline:

- reject circular labels
- preserve source-age and stale-reference reporting
- separate top-book proxy tests from depth-walk execution
- keep Base and BSC separate unless transferability is being tested explicitly
- document why data was rejected, not just what data was used
- keep research-only features out of live execution policy until the evidence
  earns promotion

This is the article's constructive message. Local stablecoin markets need public
testing conventions as much as they need code.

## Likely Structure

1. The wrong label can make any strategy look smart.
2. The three tracks: Fair Price, DEX LP, and CEX execution modes.
3. The missing non-pool comparator.
4. What Quidax can rigorously show.
5. Quidax versus Uniswap v4 over the overlap.
6. The Base diagnostic slice and the failed strict-QTS policy transfer to BSC.
7. The separate cross-pool information-transfer method and pending status.
8. Why publishing disciplined non-results helps the market.

## Evidence And Repo Anchors

- `research/articles/evidence-pack-2026-07-cngn-market-making.md`
- `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md`
- `research/autoresearch/fair-price.md`
- `research/autoresearch/lp.md`
- `research/autoresearch/flow-gated-cngn-lp-plan.md`
- `research/autoresearch/quidax-cex-execution-eda-2026-07-10.md`
- `research/data/binance_fair_price_report.md`
- `research/data/quidax_uniswap_v4_overlap_report.md`
- `research/scripts/fetch_binance_reference.py`
- `research/scripts/analyze_binance_fair_price.py`
- `research/scripts/evaluate_directional_paper_lp.py`
- `research/tests/test_fetch_binance_reference.py`
- `research/tests/test_analyze_binance_fair_price.py`
- `research/tests/test_evaluate_directional_paper_lp.py`

## Claims To Avoid

- Do not claim Fair Price is solved.
- Do not claim Quidax top book proves execution quality.
- Do not claim Uniswap v4 is the fair-value truth label.
- Do not claim the Base strict-QTS slice is live LP alpha.
- Do not merge Base and BSC into one market unless transferability is the claim.
- Do not call Bybit P2P a historical executable comparator.
- Do not publish generated plots or aggregate LP curves as proof of in-house LP
  performance without owner-level attribution.
