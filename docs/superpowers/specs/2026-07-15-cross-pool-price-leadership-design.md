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
historical methodology boundary through an early/late regime sensitivity split
at:

- Base block `45848255`
- BSC block `97799490`

The derived feature tables recompute canonical `raw_sqrt_mid` consistently and
currently label every row `sqrt_mid`, including rows before these blocks. The
split therefore tests stability across the historical boundary; it does not
compare two price fields and must not reconstruct legacy amount-ratio prices.

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
Define each trailing return over the same horizon from the state as of `t - h`
to the state as of `t`. Define the Base-BSC gap as
`10,000 * log(Base raw mid / BSC raw mid)` and reverse the target/source
projection consistently for the falsification direction.

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

The first refit is the first UTC Monday at or after 14 elapsed days from the
common interval start. Later refits occur at UTC Monday boundaries. At refit
time `r`, a training row is eligible only when its label is observable:
`t + h <= r`. Standardize non-intercept features on training rows only and fit
OLS with an intercept using an SVD least-squares solve. Fail on non-finite
inputs, zero-variance features, deficient rank, or a standardized design
condition number above `1e12`.

Report:

- out-of-sample MAE and RMSE;
- out-of-sample R-squared relative to the Base-only model;
- directional accuracy conditional on an absolute target move of at least 10
  basis points;
- paired UTC-day block-bootstrap confidence intervals for incremental loss and
  conditional directional accuracy, using 2,000 resamples and deterministic
  seed `20260715`.

Use paired target-day UTC resampling, two-sided 95-percent percentile
intervals, and identical sampled days for the nested models. Define positive
loss improvement as baseline loss minus cross-pool loss. Report MAE and MSE
improvement intervals, RMSE point estimates, and baseline-relative conditional
directional-accuracy gain. Relative out-of-sample R-squared is
`1 - SSE_cross / SSE_baseline`.

On the conditional subset `abs(actual_bps) >= 10`, a directional hit requires
actual and predicted moves to have the same nonzero sign; a zero prediction is
a miss. Use sign comparisons rather than multiplication so extreme finite
inputs cannot overflow. The threshold includes exact positive and negative
10-basis-point moves.

Pin the resampling implementation to NumPy `Generator(PCG64(seed))` and sample
one `int64` day-index matrix in row-major order. Percentile endpoints use the
nearest-rank empirical order statistics without interpolation. For 2,000 draws
at 95 percent, the zero-based lower and upper indices are 49 and 1949. Compute
the ranks with exact decimal arithmetic parsed from the confidence-level text;
binary floating-point subtraction must not choose an adjacent order statistic.
Convert the parsed Decimal to an exact rational for rank arithmetic so the
ambient Decimal precision cannot round tiny accepted confidence levels.
Byte-identical bootstrap streams are guaranteed only within a matching NumPy
version, build configuration, machine, and byte order. Record those fields,
plus the Python version, bit generator, and draw dtype, in provenance rather
than claiming cross-environment stream stability.
Relative out-of-sample R-squared is unavailable, rather than non-finite, when
the baseline sum of squared errors is zero. Store the mathematically identical
`1 - cross_mse / baseline_mse` form so component reconciliation is exact.

Typed predictive inference is authoritative rather than advisory: derived loss
and directional fields, evidence class, frozen bootstrap settings, regime
flags, and regime-partition row counts must reconcile at construction. A
shared public structural validator applies the same ordering, horizon, target,
fold, refit, and finiteness rules to statistical reporting and the later
economic prediction loader.

Treat the primary inference as underpowered, but still data-valid, when it has
fewer than 20 target UTC days or when conditional directional accuracy has
eligible observations on fewer than 10 target UTC days. Loss inference remains
reportable when only the conditional component is underpowered. Never replace
an unavailable directional interval with zero, 50 percent, NaN, or an infinite
bound.

Apply the same classification rule independently to the primary and reverse
directions. Evaluate the branches in the order listed:

- positive evidence when the cross-pool model improves both absolute and
  squared loss at one hour, both improvement intervals have lower bounds above
  zero, and cross-pool conditional direction has a lower confidence bound above
  50 percent;
- affirmative null evidence only when the upper confidence bounds are below a
  one-basis-point MAE improvement and a five-percentage-point directional gain;
- suggestive when both MAE and MSE point improvements are positive but the
  result satisfies neither the positive-evidence nor affirmative-null rule;
- inconclusive otherwise.

An insignificant coefficient is not acceptance of the null.

## Event Study

Define a source-pool shock as a cumulative raw-mid move of at least 5 basis
points within 15 minutes. Cluster repeated shocks within the same 15-minute
interval so bursts do not become independent observations.

Operationally, compare each source event with the source state as of 15 minutes
earlier. The first inclusive threshold crossing begins a half-open 15-minute
refractory interval; retain that first crossing rather than a later peak. The
5-basis-point threshold is statistical only because moves below 10 basis points
remain economically uninterpretable under the parity assumption.

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

Use a 15-minute UTC as-of innovation grid and complete UTC Monday-Sunday weeks.
Standardize each pool's innovations independently within each week using the
population standard deviation, use squared local cost, and allow `(1,1)`,
`(1,0)`, and `(0,1)` steps. Resolve equal-cost predecessors in that exact order,
so ties prefer the diagonal, then a source advance, then a target advance, and
normalize cost by path length. The 15-minute, one-hour, and four-hour bands are
one, four, and sixteen grid steps. For each direction, hold the standardized
source fixed and form the six null alignments by left-rotating the standardized
target by one through six whole UTC days. Define positive signed lag as target
time minus source time. Leadership is band-unstable when the aggregate lag sign
reverses across bands or fewer than two-thirds of complete weeks share the
primary-band sign.

## Frozen Economic Test

Run one pre-specified Base policy variant regardless of statistical
significance, provided data QA and causal alignment pass. This avoids
conditioning the economic result on the predictive p-value.

Freeze the current Base route-aware directional policy `upside_tight_v1`, its
validation windows, gas costs, and transaction-cost assumptions. Do not retune
ranges, exits, sizing, leverage, or the cross-pool threshold. Exclude whole
windows 0-2 because they overlap the model warmup. Compare both policies on
windows 3-25; the frozen routed-active identities are window 7
`upside_capture`, window 18 `fee_box`, window 23 `upside_capture`, and window 25
`dip_accumulator`. All other post-warmup windows remain explicit zero-return
cash observations.

At each Base entry decision after the 14-day model warmup, use the one-hour
walk-forward forecast as an eligibility gate:

- `upside_capture` is eligible above `+5` basis points;
- `dip_accumulator` is eligible below `-5` basis points;
- `fee_box` is eligible inside `+/-5` basis points;
- disagreement with the existing route becomes `no_position`.

The forecast gate is a veto-only entry overlay. It cannot create a route,
change ranges or sizing, force an open position to exit, or reallocate denied
capital. Re-entry after a normal exit consults the then-current forecast. Use
only the latest hourly forecast at or before the entry timestamp; a missing,
future-dated, non-finite, or at-least-one-hour-old forecast is a QA failure, not
a synthetic `no_position`.

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

Compute replay activity on the inclusive common first-to-last-swap interval so
neither venue receives extra calendar support. Retain same-timestamp swaps as
distinct ordered observations and include their zero update gaps. Treat active
liquidity as venue-native V4 liquidity units and volume as the exporter's
stablecoin-notional USD proxy, not comparable executable depth or audited
turnover. Ledger concentration covers historical gross exact additions from
pool inception through the common activity end; it is not current capital or a
position-level profitability measure. Each positive-liquidity action is a gross
addition, including repeated additions to the same position, before aggregation
by normalized owner. Require the frozen pool-inception block and fee rate rather
than accepting a truncated ledger or a uniformly wrong replay fee. Undefined
concentration denominators are QA failures rather than zeros.

Treat ledger tail coverage as a separate input contract. Each ledger must have
an adjacent sidecar that binds its exact hash to a full inclusive RPC scan from
pool inception through at least the common replay cutoff, including endpoint
block hashes, timestamps, and numeric chain ID. Full discovery unions
target-pool PoolManager `ModifyLiquidity` logs with PositionManager ERC-721
`Transfer` logs; its canonical candidate set and digest are producer-attested.
Fail if a discovered target-pool action cannot be represented by the configured
PositionManager decoder. Bracket all RPC data reads with exact endpoint-header
snapshots, require transaction and receipt hash/location agreement, and reject
any endpoint change. Stage the ledger and sidecar as one validated pair under
an output-scoped cooperating-exporter lock. The last observed LP action is not
a coverage watermark. Candidate-list and fixture exports are unverified and
cannot support `market_structure.status="complete"`; missing, stale, or short
coverage fails closed.

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

Also produce `article_manifest.json` with schema version `2.0.0`. Generated
code may set only `generated_unreviewed` or `qa_blocked`; it cannot mark its own
evidence `reviewed`. The manifest records provenance, QA, aggregate predictive,
event-study, DTW, market-structure, robustness, and economic results; artifact
hashes; publication branches; allowed and forbidden claims; and figure
identifiers. It excludes coefficients, per-event signal traces, leverage,
sizing, and execution tactics.

Provenance includes Python and NumPy versions, a SHA-256 of the
`numpy.show_config(mode="dicts")` value serialized as sorted, compact JSON with
UTF-8 encoding, no ASCII escaping, no non-finite values, and no trailing
newline, machine architecture, byte order, `PCG64`, and `int64`. Byte-stable
regeneration is asserted only when those runtime fields match.

The top-level manifest groups are:

- `schema_version`;
- `artifact_status`;
- `provenance`;
- `qa`;
- `robustness`;
- `predictive`;
- `event_study`;
- `dtw`;
- `market_structure`;
- `economics`;
- `publication`;
- `figures`;
- `artifacts`;
- `review`.

The manifest distinguishes three concepts. `artifact_status` records generated
versus human-reviewed state. `qa.status` records whether inputs and causal
alignment are valid. `robustness.status` records instability, unit dependence,
or inadequate inference support in otherwise valid evidence. Human review may
promote a `qa_blocked` artifact only to report `not_adjudicable_qa`, its failure
reason, provenance, and forbidden claims; it may not promote predictive or
economic performance claims from that artifact.

A reviewed manifest requires reviewer identity and UTC time plus a schema-valid
publication branch. Data-valid reviewed evidence requires complete aggregate
fields, exactly one publication branch, and one independent economic branch. A
human-reviewed QA failure requires `qa.status == "blocked"`, nonempty reasons,
both branches set to `not_adjudicable_qa`, unavailable result groups, and no
allowed performance claims. Serialization rejects NaN and Infinity and uses
sorted keys, stable arrays, and unit-bearing field names.

The bundled Draft 2020-12 JSON Schema and `validate_article_manifest()` are the
enforcement boundary. They pin required nested groups, enums, aggregate fields,
provenance and artifact hashes, review metadata, generated-versus-reviewed
state relationships, the separate QA-blocked shape, and the frozen statistical
and economic decision tables.

Publication branches are:

- `bsc_to_base_incremental`;
- `base_to_bsc_incremental`;
- `bidirectional_incremental_no_unique_leader`;
- `no_material_incremental_lead`;
- `leadership_unresolved`;
- `not_adjudicable_qa`.

Select exactly one branch with this frozen decision table:

- invalid data or causal alignment selects `not_adjudicable_qa`;
- underpowered, unit-dependent, regime-unstable, or DTW-band-unstable evidence
  in either direction selects `leadership_unresolved` before applying
  directional branches;
- otherwise, positive primary and non-positive reverse evidence selects
  `bsc_to_base_incremental`;
- positive reverse and non-positive primary evidence selects
  `base_to_bsc_incremental`;
- positive evidence in both directions selects
  `bidirectional_incremental_no_unique_leader`;
- affirmative-null evidence in both directions selects
  `no_material_incremental_lead`;
- every other data-valid combination, including suggestive or inconclusive
  evidence, selects `leadership_unresolved` and carries the applicable
  robustness flags.

Economic classification is independent: `pareto_improvement`,
`return_risk_tradeoff`, `no_net_return_improvement`, or
`not_adjudicable_qa`.

Compare the gated policy with the original policy on aggregate net return,
worst-window return, and worst within-window maximum drawdown. Higher return is
better; a higher worst-window return and a lower drawdown magnitude are safer.
Select `pareto_improvement` first when gated net return is strictly higher and
both risk measures are non-worse. Otherwise select `return_risk_tradeoff` when
at least one of return or risk improves and at least one other metric worsens
or is unchanged.
Select `no_net_return_improvement` when gated net return is non-higher and
neither risk measure improves. Invalid causal forecasts or economic inputs
select `not_adjudicable_qa`. Exact equality is non-improvement; do not introduce
an unstated tolerance.

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

Treat a result as unit-dependent when deleting one UTC day or one validation
fold reverses the MAE-improvement sign or changes the primary classification.
Incomplete portfolio rule/window matrices remain explicit and are ineligible
for PBO; invalid rows must never be filtered into an apparently complete
matrix.

## Article Claim Boundaries

The article may report one of the following honest outcomes:

- BSC contains incremental short-horizon information for Base in this sample;
- the reverse direction meets the positive-evidence rule while the primary
  direction does not, favoring Base-to-BSC incremental information in this
  sample without claiming that an inconclusive primary direction was rejected;
- the pools co-move but neither provides a reliably actionable lead;
- the sample resolves price alignment but not leadership.

If both directions satisfy the positive-evidence rule, report bidirectional
incremental information without claiming a unique leader. The earlier result
that the Base strict-QTS LP policy did not transfer to BSC is a policy-transfer
finding; it does not prejudge this information-transfer experiment.

It must not translate predictive precedence into causal price discovery, toxic
flow, external-LP alpha, or deployable strategy alpha.

The prose should disclose the hypotheses, horizons, baselines, validation
design, and aggregate outcomes. It should omit fitted coefficients, per-event
signal traces, leverage, sizing, and execution tactics. Public research code
may expose the exact evaluation construction, but it must not become a
production signal adapter.
