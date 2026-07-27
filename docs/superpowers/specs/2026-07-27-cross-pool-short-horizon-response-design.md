# Cross-Pool Short-Horizon Response Study Design

**Date:** 2026-07-27

**Status:** Approved research design; implementation pending

**Scope:** Post-hoc event-conditioned Base/BSC response study at 30 seconds
through 15 minutes, followed by evidence sealing, article integration, a CTO
brief, and controlled repository cleanup

## Decision

Implement a new, independently sealed exploratory extension of the reviewed
cross-pool package. Do not rewrite, augment, or reclassify
`research/results/cross_pool_lead_lag/`.

The extension answers a narrower question than the frozen predictive study:
after a qualifying move in one pool, when does the other pool next update and
how does its observed state change over the next 30 seconds to 15 minutes? It
does not reopen the one-hour predictive decision, the DTW interpretation, or
the frozen LP economic test.

## Frozen Parent Boundary

The reviewed parent is:

```text
path: research/results/cross_pool_lead_lag/article_manifest.json
sha256: adf4fd71fd33604cee70fcba7aa90d2b7763715d3cba68a868d26347c599abfe
schema_version: 2.0.0
artifact_status: reviewed
article_branch: leadership_unresolved
economic_class: no_net_return_improvement
allowed_claim: aggregate_leadership_unresolved
```

The extension must bind that exact manifest and fail if its bytes, status,
article branch, economic class, or allowed claims differ. It must also require
the exact feature inputs already bound by the parent:

```text
Base features: 47bae897266c00d92823fcae8d004debf9e50d22639ab52c326b121c9b7da23a
BSC features:  aba2dbef0548d4bca4882c26f220da14f963c1727bff855cd7532784c2cfaa5c
```

No RPC scan, pool-history export, LP-ledger export, predictive refit, DTW run,
or portfolio run is required.

## Research Role And Claims

The study is `post_hoc_exploratory`. Its complete seven-horizon family must be
reported for both directions. No horizon may be selected after results are
observed.

Permitted conclusions after separate evidence review are limited to:

- the measured short-horizon response, update-incidence, latency, staleness,
  and fee-only cross-venue gap profiles; and
- whether those profiles are compatible or incompatible with Girum's result
  under explicitly different estimands.

The extension may not change `leadership_unresolved` or support claims of
causal price discovery, a unique leader, deployable alpha, toxic flow, external
LP profitability, or an economically executable trade after gas, slippage,
inventory, and stablecoin-basis costs.

## Inputs And Price Orientation

Use only:

- `research/data/derived/uni_base_pool_features.csv`;
- `research/data/derived/uni_bsc_pool_features.csv`;
- the reviewed parent manifest; and
- `research/autoresearch/cross-venue-lead-lag-2026-07-15.md` as hash-bound
  contextual evidence, not as an estimator input. Its current SHA-256 is
  `e401f2ddb91305d209d2183745d61e89e437ae3db054b0d4c11178c7db1f3261`.

`raw_sqrt_mid` is the canonical statistical price. Both feature tables already
normalize price to stablecoin per cNGN and require
`fee_adjusted_bid < raw_sqrt_mid < fee_adjusted_ask`. The analysis therefore
does not branch on token ordering: Base USDC/cNGN and BSC USDT/cNGN have the
same economic orientation even though BSC stores USDT as token0 and cNGN as
token1.

The fee-aware view assumes USDC/USDT parity. Effects below 10 basis points are
not economically interpreted without a historical stablecoin-basis series.

## Events And Common Support

Preserve the parent event definition exactly:

- source move: cumulative raw-mid simple return of at least 5 basis points from
  the source state as of 15 minutes earlier;
- crossing: retain the first inclusive threshold crossing;
- clustering: start a half-open 15-minute refractory interval at that crossing;
- directions: BSC to Base and Base to BSC; and
- horizons, in canonical order:
  `(30_000, 60_000, 120_000, 180_000, 300_000, 600_000, 900_000)` milliseconds.

Use a common-support cohort within each direction: a shock is eligible only if
both streams have an as-of state at the shock and remain observed through the
15-minute endpoint. Every eligible shock must therefore produce all seven
horizons. This prevents horizon-specific tail attrition from masquerading as a
response-time pattern.

The 15-minute unconditional rows, shock identities, response values, and
summary point estimates must reconcile exactly with the reviewed parent. The
expected parent anchors are 123 BSC-to-Base shocks and 197 Base-to-BSC shocks.
Failure to reproduce the anchor blocks publication.

## Estimands

### 1. Unconditional as-of response

For target pool `T`, shock time `t`, and horizon `h`, let `T(u)` be the last
target state at or before `u`:

```text
response_bps(t, h) = 10,000 * (raw_mid_T(t + h) / raw_mid_T(t) - 1)
```

This is the reviewed parent's exact event-response estimator. It is retained
verbatim so the 15-minute anchor can reconcile byte-for-value; it must not be
silently replaced by the predictive panel's log-return convention.

An unchanged as-of state is a valid zero response. It is not proof of a new
target observation and must be interpreted with the update diagnostics below.

Record:

- target start and endpoint timestamps;
- `target_start_age_ms = t - target_start_timestamp_ms`;
- `target_end_age_ms = t + h - target_end_timestamp_ms`;
- zero-response indicator; and
- whether response and source move have the same nonzero sign.

### 2. First-post-shock update

The first update is the first target event with timestamp `u` satisfying:

```text
t < u <= t + h
```

If observed, record its timestamp, exact delay, raw-mid response from `T(t)` to
that event, and direction agreement. A price-unchanged event is still an
observed update with zero response.

If no target event occurs by `t + h`, retain the unconditional row and mark the
first-update outcome `right_censored`. Do not encode censoring as a conditional
zero or an exclusion.

A target event at exactly the shock timestamp belongs to the as-of start state
and never counts as post-shock. Record a same-timestamp flag and report a fixed
sensitivity excluding those shocks because cross-chain block timestamps do not
establish within-timestamp ordering.

### 3. Fee-only cross-venue gap

Let all bids and asks be stablecoin per cNGN. Determine the trade direction
from the source shock sign and keep it fixed through the horizon.

For a positive source move, source is the putative rich venue:

```text
gap(u) = 10,000 * log(source_bid(u) / target_ask(u))
```

For a negative source move, target is the putative rich venue:

```text
gap(u) = 10,000 * log(target_bid(u) / source_ask(u))
```

Report the signed gap at the shock and horizon, its positive part, and
`gap_closure_bps = gap(t) - gap(t + h)`. The horizon gap uses contemporaneous
as-of states from both pools, so closure is a cross-venue convergence measure,
not solely a target-response measure.

This metric includes the two pool fees already encoded in bid/ask. It excludes
gas, slippage, latency, inventory constraints, and USDC/USDT basis and therefore
must be called a **fee-only cross-venue gap**, not net executable profit.

Constant pool fees cancel from same-side within-pool price ratios, whether the
move is expressed as a simple or log return. The raw-mid
response remains the correct statistical response; no fee-adjusted midpoint is
constructed.

## Summaries And Inference

For every direction and horizon, report:

- eligible event and UTC-day counts;
- unconditional mean and median response with pointwise 95% UTC-day bootstrap
  intervals;
- direction agreement and zero-response share;
- update count, right-censored count, update incidence, and observed-update-day
  count;
- conditional first-update mean and median response;
- conditional first-update median and p95 delay;
- median and p95 target start and endpoint age;
- mean signed fee-only gap at shock and horizon, positive-gap share, and mean
  gap closure; and
- same-timestamp sensitivity results.

Use 2,000 UTC shock-day block resamples, NumPy `Generator(PCG64(20260715))`,
and the parent's nearest-rank 95% percentile convention. Within each direction,
the same sampled day indices must be used across all seven horizons so nested
horizons remain paired.

Pointwise intervals are descriptive. To make the seven-horizon search explicit,
also construct within-direction 95% max-z simultaneous bands for:

- unconditional mean response;
- update incidence; and
- conditional first-update mean response when every horizon has at least 10
  observed update days, positive finite bootstrap variance, and at least 1,900
  valid joint resamples.

Every family requires at least 1,900 joint-valid draws. For conditional means,
each horizon additionally requires at least 10 distinct shock UTC days with an
observed update. For each metric and horizon, let `s_h` be the sample standard
deviation (`ddof=1`) of its joint-valid bootstrap estimates. For each joint
draw, compute
`max_h(abs((estimate_bh - estimate_h) / s_h))`; the nearest-rank 95th
percentile is the common critical value, using
`ceil(0.95 * valid_draw_count) - 1` as its zero-based index. The band is
`estimate_h +/- critical * s_h`, intersected with `[0, 1]` for incidence. Any
zero or non-finite `s_h` makes that metric's simultaneous family unavailable
with an explicit reason. A malformed or unsupported horizon family is instead
a hard contract failure before inference begins.

If a simultaneous band is unavailable, record the exact support failure rather
than substituting a pointwise interval. Do not produce p-values or label an
individual horizon significant. Both directions and all horizons remain visible
regardless of sign.

## Girum Reconciliation

The report must compare methods before comparing numbers. Girum's analysis uses
per-swap moves of at least 10 basis points, 3- and 30-minute responses, a
different sample/tape construction, own-trade exclusions, a 10-minute
de-clustering sensitivity, and circular-shift inference. This extension uses
the parent's cumulative 5-basis-point/15-minute shock and UTC-day bootstrap.

Therefore:

- agreement in the Base-to-BSC three-minute direction is corroborating but not
  a direct replication;
- disagreement is an estimand or sample discrepancy to investigate, not grounds
  to discard either result; and
- a new directional-leadership claim would require a preregistered replication
  harmonizing triggers, sample, trade exclusions, clustering, horizons, and
  multiplicity.

## Package And Publication Contract

Publish to a new ignored directory:

```text
research/results/cross_pool_short_horizon_v1/
```

The exact candidate files are:

```text
short_horizon_events.csv
short_horizon_summaries.csv
short_horizon_qa.json
short_horizon_report.md
short_horizon_response.png
short_horizon_updates.png
short_horizon_fee_gap.png
short_horizon_manifest.json
```

Use an independent `short_horizon_manifest.schema.json` with schema version
`1.0.0`. Do not modify `article_manifest.schema.json`. The manifest binds:

- the parent manifest identity and decision boundary;
- exact feature and contextual-note hashes;
- frozen event, horizon, bootstrap, price, fee-gap, timestamp-tie, and common-
  support contracts;
- runtime and source provenance;
- QA counts and all summary values; and
- the exact bytes of every non-manifest artifact.

Source provenance uses an explicit dependency closure for the short-horizon
runner, its new modules, and the reused parent measurement/loading/bootstrap
modules. Every dependency file must be clean and hash-bound at run time.
Unrelated protected worktree changes outside that closure neither enter the
evidence nor block the run. Source-attestation failure is a pre-publication
hard stop because no truthful source provenance can be formed. QA-blocked
manifests are available only after source provenance and the frozen parent
identity have been established.

Generated code may emit only `generated_unreviewed` or `qa_blocked`. A separate
Sol Ultra evidence review is required to stamp `reviewed`. Atomic publication,
byte-identical rerun verification, credential redaction, and reviewed-output
immutability must match the parent package's fail-closed behavior.

The extension cannot be cited by the article or evidence pack until it is
`reviewed`. Sealing the extension does not itself seal the article evidence
package: article claims, figures, exact numbers, and provenance markers must be
updated and contract-tested in a separate commit.

## QA Gates

Publication fails closed unless all of the following hold:

1. Parent manifest bytes and decision fields match the frozen identity.
2. Feature hashes match both the local files and parent provenance.
3. Horizon order is exact, complete, and present for both directions.
4. Every common-support shock has exactly seven rows.
5. The 15-minute unconditional anchor reproduces the parent exactly.
6. Update and censor counts reconcile to eligible events.
7. Observed updates satisfy `shock < update <= endpoint`; censored rows have no
   update fields.
8. Ages and delays are nonnegative and reconcile exactly to timestamps.
9. Raw response, sign agreement, fee gaps, and gap closure recompute from bound
   prices.
10. Bootstrap settings, support, deterministic ordering, and valid-resample
    counts are explicit.
11. CSV/JSON values are finite; missing conditional values are structural nulls,
    never NaN or infinity.
12. Manifest hashes match candidate bytes, and reruns are byte-identical in the
    recorded environment.

## Article, CTO Brief, And Cleanup

After extension review:

1. Update `research/articles/02-backtesting-the-market-layer.md`,
   `research/articles/evidence-pack-2026-07-cngn-market-making.md`, and
   `research/articles/README.md` without changing the frozen parent conclusion.
2. Include only reviewed aggregate findings and article-ready figures; keep
   operational signals and implementation details out of the narrative.
3. Seal and contract-test the complete article evidence package.
4. Prepare `research/articles/cto-final-research-brief-2026-07.md` with the
   final hypotheses, methods, exact reviewed outcomes, failed gates, practical
   implications, limitations, and recommended next decisions. Preparing the
   brief does not authorize sending it externally.
5. Remove only task-created caches, staging directories, and obsolete generated
   scratch. Inventory pre-existing dirty, untracked, raw-data, and unfinished
   portfolio paths separately; do not delete or stage them without explicit
   approval. Never change `.gitignore` during cleanup.

## Rejected Alternatives

- **Rewrite the reviewed v2 package:** rejected because it destroys the frozen
  provenance and mixes post-hoc horizons into confirmatory evidence.
- **Add short horizons to predictive panels, DTW, or LP economics:** rejected
  because the request is an event-conditioned response study and such expansion
  would reopen settled methodology.
- **Publish only Girum's three-minute cell:** rejected because it selects a
  reported result and cannot distinguish stale quotes from observed responses.
- **Treat no update as a conditional zero:** rejected because it conflates a
  censored observation with a measured follower reaction.
- **Use a fee-adjusted midpoint:** rejected because a midpoint is not an
  executable direction and fixed fees cancel from within-pool returns.
