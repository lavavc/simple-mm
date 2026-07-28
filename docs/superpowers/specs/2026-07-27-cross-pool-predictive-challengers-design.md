# Cross-pool predictive challengers: post-hoc design

**Date:** 2026-07-27

**Research role:** Post-hoc exploratory robustness study

**Parent decision:** `leadership_unresolved`, unchanged

## Purpose

The reviewed Base/BSC lead-lag study found that its specified one-hour
cross-pool OLS model forecast worse than its target-only comparator in both
directions. This extension tests four narrowly specified alternatives that
address the main observed data properties: missing target dynamics, nonlinear
freshness effects, heavy-tailed errors, and a large mass of unchanged target
prices.

This is not a second chance to search the reviewed sample for a positive
result. It is a separately sealed diagnostic. Even a strong challenger result
can only motivate fresh-data replication.

## Immutable parent boundary

The challenger package must validate and bind these reviewed parent bytes:

| Parent artifact | SHA-256 |
|---|---|
| `article_manifest.json` | `adf4fd71fd33604cee70fcba7aa90d2b7763715d3cba68a868d26347c599abfe` |
| `panel_15m.csv` | `58c2f41ff3d344c092bf1e88a4836e8638ca03bb44a602f4b7b9317b3bf6e550` |
| `panel_1h.csv` | `4abd93587f4d0fd6613c4f7189425e5cc382f02bb188b85a247c1f79fdd3862b` |
| `panel_4h.csv` | `aa7f7feb8a6b30d9daed2fdb5c81a8018e54eb4cba041a6482b5b72eda94da65` |
| `predictive_predictions.csv` | `182b76606395890aceec96f5388945508961d8b11539022874c8b9e890af157a` |

The extension must not modify the parent pipeline, contracts, manifest,
artifacts, evidence class, LP gate, or article decision. It consumes the
canonical `raw_sqrt_mid` panels and reuses the parent's exact validation rows,
fold indices, refit timestamps, directions, and horizons.

## Causal sample

The study covers both directions at 15 minutes, one hour, and four hours.
For a target pool $T$, source pool $S$, origin $t$, and horizon $h$:

\[
Y_{t,h}=10{,}000\log\left(M_T(t+h)/M_T(t)\right).
\]

An update is defined from observed state timestamps, not from a nonzero return:

\[
U_{t,h}=1\{\tau_T(t+h)>\tau_T(t)\}.
\]

That distinction preserves price-neutral state updates and the dataset's
as-of methodology.

All folds retain the parent schedule: 14 initial UTC days, Monday UTC refits,
expanding training only, and labels available by the refit boundary. Every
transform, scale, knot, coefficient, and robust weight is fitted from that
fold's training rows only.

The immediately preceding regular panel row supplies the second target lag.
Lags are derived before any freshness slice is applied. A filtered row is never
treated as adjacent to another filtered row. The first panel row is excluded
from ARX(2) training because its predecessor is unavailable; no lag is
fabricated. Every parent OOS row must have its exact regular predecessor or the
ARX(2) cell fails closed.

## Direction-relative feature blocks

Let $r^T_0$ be the target return over $[t-h,t]$, $r^T_1$ the target return
over $[t-2h,t-h]$, $r^S_0$ the source return over $[t-h,t]$, $g$ the signed
target-minus-source log-price gap, and $a^T,a^S$ the decision-time state ages.

Every estimator has three matched variants:

1. `target_only`
2. `source_age`: target-only features plus $a^S$
3. `full_source`: source-age features plus $r^S_0$ and $g$

The primary incremental-information contrast is:

\[
\Delta_{price}=L(\text{source_age})-L(\text{full_source}).
\]

Positive values mean that source price information lowered loss after source
freshness was already known. The source-age and total-source contrasts remain
reported as decomposition diagnostics.

## Estimators

### Sealed OLS reference

The parent target-only and full-source predictions remain the OLS reference.
The extension fits only the missing source-age middle variant and verifies that
an independent refit of the endpoint variants reproduces the sealed predictions
with `rtol=0` and `atol=1e-10` bps.

The parent endpoint column order is preserved exactly: target-only is
$(r^T_0,a^T)$ and full-source is $(r^T_0,a^T,r^S_0,g,a^S)$. The new middle
variant is $(r^T_0,a^T,a^S)$. Ages remain in the parent's raw millisecond
representation before training-only standardization.

### 1. Direct ARX(2)

ARX(2) adds $r^T_1$ to each OLS variant. The source block remains
$a^S,r^S_0,g$. This tests whether the original target-only comparator omitted
a material second target lag without creating a distributed source-lag search.
It is fitted by checked OLS.

### 2. Restricted additive regression

This GAM-style regression keeps return terms linear. It transforms each age as
`log1p(age_ms / 60000)` and gives target age, source age, and the signed gap a
continuous piecewise-linear basis:

\[
B(x)=(x,(x-q_{1/3})_+,(x-q_{2/3})_+).
\]

Knots are computed from the training fold only at the predeclared one-third and
two-thirds quantiles. There are no interactions, penalties, adaptive knots,
smoothing searches, or hyperparameter searches. Duplicate knots, zero-variance
basis columns, rank failure, or excessive conditioning fail closed. Validation
values outside the training range are linearly extrapolated and counted in the
fold audit.

### 3. Huber regression

Huber regression uses the original OLS feature blocks and a fixed tuning
constant of 1.345. Training features are standardized, OLS initializes the
coefficients, and deterministic iteratively reweighted least squares updates
the coefficients and residual scale. At each iteration:

1. Compute residuals $e=y-X\beta$ and
   $s=1.482602218505602\,\mathrm{median}|e-\mathrm{median}(e)|$.
2. Set $u=e/s$ and $w=1$ for $|u|\le1.345$, otherwise
   $w=1.345/|u|$.
3. Solve unpenalized weighted least squares, including the intercept.
4. Stop when
   $\lVert\beta_{new}-\beta\rVert_2/\max(1,\lVert\beta\rVert_2)\le10^{-10}$.

The final coefficients are the converged weighted-least-squares update. The
maximum is 100 iterations.

Zero MAD scale, a singular weighted design, non-finite weights, excessive
conditioning, or non-convergence fails closed. The evidence reports the robust
scale, iteration count, and fraction of training rows downweighted.

### 4. Two-part update model

The first component estimates $P(U_{t,h}=1\mid X_t)$ with standardized ridge
logistic regression. The ridge penalty is fixed at 0.1 and excludes the
intercept. It minimizes mean negative log likelihood plus
$0.1\lVert\beta_{nonintercept}\rVert_2^2/2$. Coefficients start at zero. Each
iteration computes the exact gradient and Hessian, takes a Newton step, and
halves that step until the finite objective does not increase. Fifty failed
halvings or 100 iterations fails closed. The sigmoid uses a branch-stable
formula, and convergence requires the accepted step norm divided by
$\max(1,\lVert\beta\rVert_2)$ to be no greater than `1e-10`.

The second component fits the signed conditional return
$E[Y_{t,h}\mid U_{t,h}=1,X_t]$ with the specified Huber estimator. The combined
forecast is:

\[
\widehat{Y}_{t,h}=\widehat{P}(U=1\mid X_t)\,
\widehat{E}(Y\mid U=1,X_t).
\]

The product is a conditional-mean forecast and is not presented as the
MAE-optimal median of the zero-inflated mixture. The study therefore reports
combined MAE and MSE alongside component-aligned update Brier loss, update log
loss, calibration error, and conditional-update signed-return MAE.

Each logistic training fold must contain both update classes. Each conditional
Huber fit must have at least one more update row than design columns. Log-loss
probabilities alone are clipped to
`[numpy.finfo(float).eps, 1 - numpy.finfo(float).eps]`; fitted probabilities and
Brier loss are not clipped.

## Evaluation supports and metrics

Models are fitted once per parent fold. Freshness is evaluated without
refitting on three predeclared OOS supports:

- `all`
- `both_age_le_4h`
- `both_age_le_1h`

The two freshness supports require both decision-time pool states to satisfy
the threshold. Every cell reports rows, target UTC days, update rows, update
days, and actual update incidence.

Continuous-forecast metrics are MAE, MSE, RMSE, and directional accuracy when
the absolute realized move is at least 10 bps. Two-part component metrics are
reported separately and are not added together.

Calibration-in-the-large is mean predicted update probability minus observed
update incidence. When a cell has at least 25 rows, a descriptive reliability
table stably sorts by probability and timestamp, partitions the rows into five
equal-count groups, and reports each group's mean probability and incidence.

Diagnostics include fold condition numbers, convergence, Huber downweighting,
spline extrapolation, update calibration, fold-level loss differences,
day-level loss-difference autocorrelation, and early/mixed/late methodology
regime slices. Regime slices use the nine joint target-regime by source-regime
cells. MSE, RMSE, directional accuracy, log loss, calibration, reliability
groups, autocorrelation, and regime slices are descriptive unless a registered
contrast below explicitly gives them an interval.

## Dependence-aware inference

Inference conditions on the completed OOS forecasts; it does not refit models
inside bootstrap draws.

- Calendar block: seven target UTC days
- Scheme: circular moving-block bootstrap
- Generator: NumPy `PCG64`
- Seed: `20260715`
- Resamples: 10,000
- Confidence level: 95%

The package first constructs one ordered vector of the 88 parent OOS target
UTC days shared by all horizons and directions. It then constructs one global
`10000 x 88` draw matrix. Each row samples circular seven-day blocks until 88
indices are present and trims the final block. Every endpoint and support uses
that same matrix and aggregates only its rows for each sampled day. A sampled
day with no rows in a freshness support contributes zero count and zero loss;
if any complete draw has zero total rows for a cell, that cell is
`not_adjudicable`. Draws are never discarded or resampled conditionally.

Raw intervals are central nearest-rank percentile intervals: sorted draw
indices `ceil(0.025 * 10000) - 1` and `ceil(0.975 * 10000) - 1`. They are
reported for all three feature-block contrasts.

For each adjudicable source-price cell $j$, let $d_j$ be the observed loss
improvement, $d_{bj}$ its bootstrap values, and
$s_j=\mathrm{std}(d_{bj},\mathrm{ddof}=1)$. Define centered bootstrap values
$t_{bj}=(d_{bj}-d_j)/s_j$, observed $t_j=d_j/s_j$, and
$M_b=\max_j|t_{bj}|$. The 95% single-step max-$t$ critical value is nearest
rank `ceil(0.95 * 10000) - 1` of the sorted $M_b$. Simultaneous intervals are
$d_j\pm c s_j$ and two-sided adjusted p-values are
$(1+\sum_b 1\{M_b\ge|t_j|\})/(10000+1)$. This procedure covers:

- OLS reference, ARX(2), GAM-style, and Huber MAE:
  $4\times2\times3\times3=72$ cells.
- Two-part combined MAE, update Brier loss, and conditional-update MAE:
  $3\times2\times3\times3=54$ cells.
- Maximum family size: 126 cells.

Cells are registered even when they prove unavailable. A cell is
`not_adjudicable` when it lacks 42 OOS target days, lacks six seven-day blocks,
has incomplete matched variants, has a fit failure, or has zero bootstrap
variance. Conditional-update inference additionally requires at least 20 OOS
update-days. Point estimates and support are retained where meaningful, but no
interval or adjusted claim is substituted.

Complete-block support is deterministic and non-circular: mark each of the 88
ordered calendar days that contains at least one row in the evaluation support,
split those marks into maximal consecutive runs, and sum
`floor(run_length / 7)`. Endpoint-specific filtering, including the two-part
update condition, does not redefine this target-support count.

A model or support limitation is a cell-level `not_adjudicable` result and
does not invalidate otherwise complete evidence. A parent hash or anchor
mismatch, noncanonical input or output, missing registered cell, artifact hash
mismatch, serialization failure, or nondeterministic rerun is package-level and
produces only a `qa_blocked` manifest.

## Evidence package

The immutable v1 and v1.1 candidates remain `generated_unreviewed`. A prior
audit found one Huber source-price contrast with a floating-point-scale point
estimate but a small adjusted p-value. The final audit found the same numerical
phenomenon on the four-hour freshness support. The review-capable replacement
is `research/results/cross_pool_challengers_v1_2/`. Its 11 statistical artifact
files must be byte-identical to v1.1; only the manifest and source provenance
may change.

The v1.2 package contains:

- `challenger_predictions.csv`
- `challenger_fold_audits.csv`
- `challenger_metrics.csv`
- `challenger_contrasts.csv`
- `challenger_diagnostics.json`
- `challenger_report.md`
- `model_error_comparison.png`
- `source_price_contrasts.png`
- `freshness_sensitivity.png`
- `two_part_calibration.png`
- `loss_dependence.png`
- `challenger_manifest.json`

The manifest binds the parent hashes, the explicit executable-source closure,
its Git commit, the SHA-256 of `git diff --binary HEAD` over that tracked
closure, configuration, artifact hashes, QA state, and review state. The
closure includes every transitively imported local module, executed package
initializer, and loaded JSON schema. A static closure test rejects omitted local
imports and dynamic imports. An untracked source dependency blocks execution.
Canonical evidence is generated
from a provenance checkpoint commit, so its scoped diff is empty and the
recorded commit contains the executable study. Generated evidence is
`generated_unreviewed`; a failed contract produces `qa_blocked`; only an
explicit review operation may produce `reviewed`. Publication is atomic and a
rerun against an existing directory must be byte-identical.

The v1.2 review manifest binds structured adjudications to the exact Huber
`full_source`, Base-to-BSC, one-hour, MAE source-price cells on the all and
both-age-at-most-four-hours supports. Each records the point estimate,
simultaneous interval, adjusted p-value, and classification `numerical_null`;
excludes the cell from substantive rejection counts; and states that no
statistical artifact was recomputed. The review CLI requires an explicit,
repeatable acknowledgement of both cells before it can stamp the package.

The review also binds the summary derived from the sealed contrast registry:
126 source-price cells, 78 adjudicable, 48 not adjudicable, 40 adjusted
rejections at p no greater than 0.05, two numerical-null exclusions, 38
substantive adverse rejections, and zero substantive favorable rejections.

The v1.2 manifest binds the immutable v1.1 manifest SHA-256 and all 11 artifact
SHA-256 values. Generation fails closed if any regenerated statistical artifact
differs. Both generation and review require and validate the on-disk v1.1
baseline supplied through an explicit CLI argument. Validation dispatches by
schema version: v1 remains verifiable only under its exact 1.0 contract, v1.1
under its exact one-adjudication 1.1 contract, and v1.2 under the strict
two-adjudication 1.2 contract.

## CTO brief boundary

The CTO brief is updated only from a QA-valid, reviewed challenger package. It
will explain each model, its assumptions, the data property it addresses, its
empirical fit diagnostics, exact OOS results, uncertainty, and figures in plain
language.

No challenger result may establish causal price discovery, a permanent venue
leader, executable alpha, trading profitability, or LP profitability. A
positive adjusted result means only that the specified source features lowered
the specified OOS loss within this sealed post-hoc sample. A null result means
only that these challengers found no robust incremental forecast evidence.

## Implementation boundary

Use NumPy and the repository's existing research infrastructure only. Do not
add SciPy, statsmodels, scikit-learn, pandas, or notebook-only logic. Keep the
frozen parent modules unchanged. New code must use separate challenger modules,
a separate CLI, independent typed contracts, deterministic canonical
serialization, and fail-closed tests for every non-obvious boundary above.
