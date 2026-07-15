# Cross-Pool cNGN Price Leadership Experiment Design

## Goal

Test whether BSC Uniswap v4 price information improves out-of-sample prediction
of Base Uniswap v4 price changes beyond Base's own history, then test whether the
same information improves the frozen Base directional LP policy after costs.

The experiment distinguishes four claims:

1. Confirmatory: BSC contains incremental predictive information for Base.
2. Economic: a fixed BSC signal gate improves the existing Base LP policy.
3. Exploratory: constrained dynamic time warping and event studies indicate the
   same direction of temporal precedence.
4. Contextual: venue activity and LP concentration may explain the observed
   data-generating process.

The experiment cannot identify causal price discovery, toxic flow, or another
LP's profitability from pool prices alone.

## Data Contract

Use the existing pool-feature tables:

- `research/data/derived/uni_base_pool_features.csv`
- `research/data/derived/uni_bsc_pool_features.csv`

`raw_sqrt_mid`, derived from `sqrt_price_x96`, is the canonical marginal pool
price. Historical `cngn_usd_price` values are diagnostic metadata because they
straddle amount-ratio and sqrt-mid storage methodologies.

Use the full common Base/BSC interval. Preserve the sample while guarding the
stored-price transition through a pre/post sensitivity split at:

- Base block `45848255`
- BSC block `97799490`

Normalize Base USDC/cNGN and BSC USDT/cNGN prices under an explicit USDC/USDT
parity assumption. Effects below 10 basis points are not economically
interpretable without a historical USDC/USDT basis series. Use raw marginal
mids for statistical co-movement and each pool's fee-adjusted bid/ask band for
executable-price interpretation; do not construct a fictitious fee-adjusted
mid.

The current histories provide approximately 106 common days, 1,508 Base swaps,
and 3,105 BSC swaps. Median update gaps are approximately 13-14 minutes, while
the 90th-percentile gap is approximately 4.8 hours on Base and 1.4 hours on BSC.
Regular-clock evaluation and explicit state-age diagnostics must prevent BSC's
higher event count from mechanically creating apparent leadership.

## Causal As-Of Panel

Build a regular as-of panel from the two event streams. At every decision time,
use only the last pool state at or before that timestamp. A pool price remains
the executable marginal state until the next swap; its age remains a separate
diagnostic variable.

Use fixed horizons chosen before inspecting leadership results:

- primary: one hour;
- sensitivity: 15 minutes and four hours.

Use non-overlapping UTC-aligned decision times at each horizon. Define the
forward target as the log return in basis points from the pool state as of `t`
to the pool state as of `t + h`; never use the next future swap as a label.

## Primary Predictive Test

The primary direction is BSC to Base. The reverse Base-to-BSC analysis is a
pre-specified falsification and asymmetry test, not a second primary result.

Compare two nested ordinary-least-squares models with an intercept:

- Base-only baseline: trailing Base return and time since the last Base swap.
- Cross-pool model: the Base-only fields plus trailing BSC return, the current
  Base-BSC raw-mid gap, and time since the last BSC swap.

Use a 14-day initial training period followed by expanding weekly walk-forward
refits. Fit preprocessing on training rows only. Do not shuffle time, tune the
horizons, add features, or select thresholds after seeing outcomes.

Report:

- out-of-sample MAE and RMSE;
- out-of-sample R-squared relative to the Base-only model;
- directional accuracy conditional on an absolute target move of at least 10
  basis points;
- paired UTC-day block-bootstrap confidence intervals for incremental loss and
  conditional directional accuracy, using 2,000 resamples and deterministic
  seed `20260715`.

Classify the primary result as:

- positive evidence when the cross-pool model improves both absolute and
  squared loss at one hour, the paired loss interval excludes zero, and
  conditional direction has a lower confidence bound above 50 percent;
- suggestive when point estimates improve but uncertainty includes zero;
- affirmative null evidence only when confidence bounds exclude at least a
  one-basis-point MAE improvement and a five-percentage-point directional gain;
- inconclusive otherwise.

An insignificant coefficient is not acceptance of the null.

## Event Study

Define a source-pool shock as a cumulative raw-mid move of at least 5 basis
points within 15 minutes. Cluster repeated shocks within the same 15-minute
interval so bursts do not become independent observations.

For both BSC-to-Base and Base-to-BSC directions, measure the target pool's
as-of response after 15 minutes, one hour, and four hours. Report event counts,
median response, mean response, direction agreement, and UTC-day block-bootstrap
intervals. No event-study horizon may replace the primary one-hour predictive
test after results are observed.

## Dynamic Time Warping

DTW is exploratory corroboration, not the confirmatory estimator.

- Apply derivative or log-price-innovation DTW, not raw price-level DTW.
- Constrain the primary warping band to one hour.
- Repeat with 15-minute and four-hour bands as sensitivity checks.
- Estimate paths within weekly blocks rather than forcing one global path.
- Compare normalized path cost and signed matched lags with day-block
  time-shift nulls that preserve within-series clustering.
- Do not use the DTW path to choose model features, horizons, LP parameters, or
  the claimed direction.

If the signed lag changes materially across bands or weeks, report leadership
as unstable even if the global path looks persuasive.

## Frozen Economic Test

Run one pre-specified Base policy variant regardless of statistical
significance, provided data QA and causal alignment pass. This avoids
conditioning the economic result on the predictive p-value.

Freeze the current Base directional strategy family, selected configuration,
validation windows, gas costs, and transaction-cost assumptions. Do not retune
ranges, exits, sizing, leverage, or the cross-pool threshold.

At each Base entry decision after the 14-day model warmup, use the one-hour
walk-forward forecast as an eligibility gate:

- `upside_capture` is eligible above `+5` basis points;
- `dip_accumulator` is eligible below `-5` basis points;
- `fee_box` is eligible inside `+/-5` basis points;
- disagreement with the existing route becomes `no_position`.

Compare both the original and signal-gated policies only on the same post-warmup
windows. Report pre-warmup windows as excluded rather than treating them as
`no_position` observations.

Compare the signal-gated policy against:

- the frozen original directional policy;
- centered static LP;
- pool-mark hold-cNGN;
- no position.

Report net return after costs, incremental return, worst window, positive-window
rate, maximum drawdown, total fees, transaction costs, fee-to-cost ratio, and
rebalance count. Do not report Sharpe or Sortino from the small active-window
sample.

Even a positive economic result remains diagnostic because the evidence covers
only two pools and lacks an independent executable cNGN inventory mark.

## Market-Structure Diagnostics

Report venue characteristics that can affect the analysis:

- swap and meaningful-move counts;
- median and tail update gaps;
- fee tiers;
- active-liquidity and volume summaries;
- anonymized LP-owner counts and gross opening-capital concentration.

The owner-distribution analysis is post hoc and appears after the performance
results. It may motivate hypotheses about why transferability differs, but it
must not be used to claim that concentration caused leadership or that the
other BSC LP is informed, toxic, or profitable.

## Outputs

Keep implementation research-only. Produce deterministic, machine-readable
outputs under an ignored `research/results/cross_pool_lead_lag/` directory:

- panel and data-quality summaries;
- predictive and reverse-direction metrics;
- event-study rows;
- DTW weekly paths and null comparisons;
- frozen-policy attribution;
- a Markdown report;
- article-ready price-gap, event-response, DTW-lag, and LP-performance figures.

Generated results remain ignored. Commit source, tests, and durable design or
methodology documentation only.

## Testing And Failure Behavior

Write synthetic tests before implementation for:

- recovery of a known BSC-to-Base lead;
- a true no-lead process that does not manufacture leadership;
- as-of joins with no future leakage;
- asynchronous timestamps and long flat intervals;
- token orientation and fee-adjusted bid/ask construction;
- non-overlapping targets and clustered shock events;
- DTW paths that cannot escape the configured time band;
- walk-forward preprocessing that never sees validation rows;
- signal-gated LP routing and `no_position` disagreement;
- hard failure on non-monotonic timestamps, duplicate event identities,
  missing canonical prices, or unexplained units.

Stop and report rather than widening the search when:

- QA invalidates price comparability;
- a result depends on one day or one validation window;
- the stored-methodology split reverses the primary result;
- DTW direction is band-sensitive;
- the primary model is underpowered or inconclusive.

## Article Claim Boundaries

The article may report one of four honest outcomes:

- BSC contains incremental short-horizon information for Base in this sample;
- the reverse test is stronger, rejecting the proposed BSC-to-Base direction
  and suggesting Base contains incremental information for BSC;
- the pools co-move but neither provides a reliably actionable lead;
- the sample resolves price alignment but not leadership.

It must not translate predictive precedence into causal price discovery, toxic
flow, external-LP alpha, or deployable strategy alpha.

The prose should disclose the hypotheses, horizons, baselines, validation
design, and aggregate outcomes. It should omit fitted coefficients, per-event
signal traces, leverage, sizing, and execution tactics. Public research code
may expose the exact evaluation construction, but it must not become a
production signal adapter.
