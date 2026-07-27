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
CPL_EDITORIAL_STATUS: EVIDENCE_REVIEWED
CPL_PRIMARY_CLASS: inconclusive
CPL_REVERSE_CLASS: inconclusive
CPL_ARTICLE_BRANCH: leadership_unresolved
CPL_ECONOMIC_CLASS: no_net_return_improvement
CPL_ROBUSTNESS_STATUS: complete
CPL_ROBUSTNESS_FLAGS: dtw_band_unstable
CPL_SOURCE_MANIFEST: research/results/cross_pool_lead_lag/article_manifest.json
```

```text
CSH_EDITORIAL_STATUS: EVIDENCE_REVIEWED
CSH_RESEARCH_ROLE: post_hoc_exploratory
CSH_PARENT_DECISION: leadership_unresolved
CSH_PARENT_DECISION_UNCHANGED: true
CSH_SOURCE_MANIFEST: research/results/cross_pool_short_horizon_v1/short_horizon_manifest.json
CSH_MANIFEST_SHA256: 9145738bb6dd4aa84512b3f62625d779e6e4ef223f5b614e1f9facc502ab7509
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
- Result: the pre-specified directional policy did not transfer across pools;
  no live LP promotion is justified.

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
- The local Bybit sample covers none of the relevant Base validation marks at a
  usable age.

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

The completed directional-policy transfer conclusion is diagnostic:

| Pool | Active windows | Directional sum | Worst directional | Positive directional | Directional minus hold | Best static sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Base | 4 | +1.039% | +0.118% | 100.0% | +0.228 pp | +0.796% |
| BSC | 7 | -1.272% | -0.842% | 14.3% | -1.602 pp | +0.400% |

Base has a sparse DEX-internal regime worth preserving as a hypothesis. The
pre-specified diagnostic directional LP policy did not transfer across pools;
this is a pool-separated research result, not a live recommendation. Its
aggregate return was +1.039% across four Base windows and -1.272% across seven
BSC windows.

That transfer finding does not answer whether either pool contributes stable
incremental information about the other. Because no usable non-pool comparator
covers the relevant windows, the article should not call this live LP alpha.

#### The final portfolio test

Earlier liquidity experiments generated useful hypotheses, but an individual
range is not a portfolio. Multiple ideas can overlap economically, capital can
carry from one evaluation period to the next, and an isolated result can change
once it shares one accounting frame with the rest of the portfolio.

The final study therefore evaluates canonical economic candidates with joint
portfolio accounting. It keeps Base and BSC separate, distinguishes carried
capital paths from reset-only diagnostics, and tests candidate completeness,
path validity, and comparator evidence as separate questions. A failed gate is
reported as a failure rather than repaired with a replacement result.

> **Portfolio result — integrity-attested non-result.** Both frozen pool-local
> evidence packages passed integrity attestation after Base and BSC completed
> their full evaluation horizons. The candidate reset matrices were complete,
> and candidate-level overfitting diagnostics were computed. However, the
> allocation-rule reset matrices and carried portfolio paths did not satisfy
> the pre-specified completeness gate. This study therefore reports no
> weighted-portfolio performance result. The failure is retained as a
> diagnostic research result, not converted into a substitute claim or
> live-promotion rationale. The missing non-pool inventory comparator remains
> an independent limit on economic interpretation.

<!-- PORTFOLIO_RESULT: INTEGRITY_ATTESTED_NONRESULT -->

### 7. The separate cross-pool information-transfer experiment

The failed directional-policy transfer does not answer whether one pool contains
incremental information about the other. The reviewed experiment uses each
pool's canonical `raw_sqrt_mid` and checks stability across the known
early/late methodology boundaries rather than reconstructing legacy prices.

At each epoch-aligned decision time, a causal as-of panel carries forward only
the last state already observable in each pool and reports that state's age.
The preregistered horizons are a one-hour primary test with 15-minute and
four-hour sensitivities. The confirmatory direction compares nested models: the
cross-pool model adds BSC features to a Base-only baseline after a 14-day warmup
and expanding weekly walk-forward refits. A reverse Base-to-BSC falsification
uses the same construction.

The reviewed statistical result is deliberately narrow. Both one-hour
directional tests were `inconclusive`, and the constrained-DTW lag was unstable
across the preregistered bands. The manifest therefore selects
`leadership_unresolved`. That permits one aggregate claim: this experiment did
not resolve a stable directional leader. It does not establish either
directional lead, bidirectional incremental evidence, or an affirmative finding
that no material lead exists.

The unconditional frozen-policy evaluation also produced a negative result. On
the same 23 reset-capital windows, aggregate net return was +0.515% for the
original Base policy and +0.310% for its forecast-gated variant. The gated
variant had a worse worst window, while worst within-window drawdown was
unchanged. The reviewed economic class is `no_net_return_improvement`.

The comparison assumes USDC/USDT parity, so effects below 10 basis points remain
economically uninterpretable without a historical basis series. Even these
reviewed results cannot establish causal price discovery, toxic flow,
external-LP profitability, or deployable alpha from two pool histories.
Implementation-specific parameters and execution details remain outside the
article.

#### A reviewed post-hoc short-horizon extension

The reviewed extension asks a narrower question: after a shock in one pool,
what does the other pool's recorded as-of state look like from 30 seconds to 15
minutes? It reuses the parent's event and common-support construction, covers
all seven horizons, and applies 2,000 paired UTC shock-day bootstrap resamples.
It is explicitly post-hoc and does not change the reviewed parent conclusion of
`leadership_unresolved`.

The primary estimand is the **unconditional response** across every eligible
shock. If the target has no new observation by a horizon, its as-of price is
unchanged and contributes a valid zero response. Pointwise intervals describe
each horizon separately; the amber simultaneous max-z bands cover the
seven-horizon family. Every primary unconditional simultaneous band includes
zero.

![Unconditional short-horizon response profile](../results/reports/cross_pool_short_horizon_v1/short_horizon_response.png)

*How to read the response graph:* each panel follows one direction. The blue
points are mean unconditional responses, the blue region is the pointwise 95%
interval, and the amber region is the simultaneous 95% band. At three minutes,
the estimates are +0.204359 bps for BSC to Base and +0.035202 bps for Base to
BSC. At 15 minutes they are +0.878230 bps and +0.039554 bps. The
unconditional-response simultaneous max-z bands include zero, so the visible
oscillations are descriptive response shapes, not evidence of leadership,
causality, or alpha.*

The next graph separates four different measurements. **Update incidence** is
the fraction of eligible shocks followed by an observed target update by the
horizon. A **conditional response** averages only the selected subset with an
observed target update; a shock without one is right-censored, not a conditional
zero and not a survival estimate. The latency panel times observed updates only,
while state staleness measures how old the last recorded target state is. The
figure contains point estimates only; inferential intervals remain in the
reviewed summaries and manifest.

![Target update, latency, and staleness diagnostics](../results/reports/cross_pool_short_horizon_v1/short_horizon_updates.png)

*How to read the update graph:* by 15 minutes, incidence reaches 39/123 (31.7%)
for BSC to Base and 63/197 (32.0%) for Base to BSC. Conditional 15-minute means
are +2.382769 bps and -1.237692 bps, compared with unconditional means of
+0.878230 bps and +0.039554 bps. That contrast is selection, not a stronger
market-wide effect. Median observed-update delays are 138 and 228 seconds.
Median target-state ages at 15 minutes are 23.5 and 35.6 minutes; these diagnose
data freshness, not causal transmission latency or proof that the market itself
was stale. Base-to-BSC conditional simultaneous inference is unavailable because
too few UTC days contain observed updates at the shortest horizon.*

The last graph applies both pool fees to a signed cross-venue bid/ask log gap
under USDC/USDT parity. It is **not net executable profit** and excludes gas,
slippage, latency, inventory constraints, fill risk, and stablecoin basis.

![Fee-only cross-venue gap](../results/reports/cross_pool_short_horizon_v1/short_horizon_fee_gap.png)

*How to read the fee-gap graph:* the dashed line is the mean signed gap at the
shock, the blue line is its mean at each horizon, and the red line is the
algebraic start-minus-end difference. At 15 minutes, BSC-to-Base moves from
-14.774555 to -19.003638 bps and Base-to-BSC from -18.949015 to -20.881117 bps.
The corresponding red values are positive, but both signed gaps remain negative;
that does not mean the gap closed toward zero, became executable, or generated
profit.*

The extension also does not replicate Girum's directional magnitude pattern:

| Three-minute study | Base to BSC | BSC to Base | Interpretation |
| --- | ---: | ---: | --- |
| Girum de-clustered | +3.4 bps | +0.4 bps | Different shocks, samples, clustering, and inference |
| This reviewed extension | +0.035202 bps | +0.204359 bps | Same signs, opposite magnitude ordering |

Sign agreement is descriptive. It does not corroborate Base leadership. Taken
together, the extension does not establish causal price discovery, does not
establish a unique directional leader, does not establish deployable alpha, and
does not establish net executable profit.

### 8. Why the failures are the point

The research branch produced reusable discipline:

- reject circular labels
- preserve source-age and stale-reference reporting
- separate top-book proxy tests from depth-walk execution
- keep Base and BSC separate unless transferability is being tested explicitly
- document why data was rejected, not just what data was used
- allow a QA-valid result to remain unresolved when robustness fails
- run a frozen economic test without turning a negative result into a new search
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
6. The Base diagnostic slice and the failed directional-policy transfer to BSC.
7. The reviewed cross-pool result: leadership unresolved and no net-return
   improvement from the frozen economic gate.
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
- `research/results/cross_pool_lead_lag/article_manifest.json`
- `research/results/cross_pool_lead_lag/statistical_report.md`
- `research/results/cross_pool_lead_lag/frozen_policy_report.md`
- `research/results/cross_pool_short_horizon_v1/short_horizon_manifest.json`
- `research/results/reports/cross_pool_short_horizon_v1/short_horizon_manifest.json`

## Claims To Avoid

- Do not claim Fair Price is solved.
- Do not claim Quidax top book proves execution quality.
- Do not claim Uniswap v4 is the fair-value truth label.
- Do not claim the Base diagnostic slice is live LP alpha.
- Do not merge Base and BSC into one market unless transferability is the claim.
- Do not call Bybit P2P a historical executable comparator.
- Do not publish generated plots or aggregate LP curves as proof of in-house LP
  performance without owner-level attribution.
- Do not claim either pool has established incremental predictive leadership.
- Do not convert `leadership_unresolved` into a claim that no material lead
  exists.
- Do not treat constrained DTW as directional evidence when its lag is
  band-unstable.
- Do not claim the forecast-gated policy improved LP economics.
- Do not replace the unconditional response with the selected conditional subset.
- Do not read update incidence or observed-update delay as causal transmission.
- Do not read the signed fee-only gap as executable PnL.
- Do not treat sign agreement with Girum as evidence of a directional leader.
- Do not infer causal price discovery, toxic flow, or external-LP profitability
  from price histories or owner concentration.
- Do not publish selected identifiers, portfolio weights, operational
  thresholds, implementation-specific signals, model coefficients,
  capital-allocation parameters, leverage, or execution details.
