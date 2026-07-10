# Flow-Gated cNGN LP Plan

Date: 2026-06-27

## Decision

Do not deploy any full-grid rank-1 DEX LP stream. Continue with a reduced
Base-first research test: paper-style fixed-width LP, entered only when the
entry-time pool state supports conditional cNGN exposure.

BSC remains diagnostic. The current BSC opportunity set is not strong enough to
generalize the Base result.

## Evidence Update

Source files checked:

- `research/autoresearch/dex-only-rerun-status.md`
- `research/autoresearch/lp.md`
- `research/results/extended_dex_only_full/*`
- `research/data/derived/uni_base_pool_features.csv`
- `research/data/derived/uni_bsc_pool_features.csv`

The full walk-forward already rejected the four train-rank-1 streams:

| Pool | Style | Selected windows | Sum validation return | Mean window return | Positive windows |
| --- | --- | ---: | ---: | ---: | ---: |
| Base | Paper | 26 | -0.460% | -0.018% | 65.4% |
| Base | EWMA | 26 | -1.256% | -0.048% | 53.8% |
| BSC | Paper | 37 | -2.086% | -0.056% | 35.1% |
| BSC | EWMA | 37 | -1.755% | -0.047% | 32.4% |

Validation returns are still dominated by directional pool-price movement:

| Pool | Style | Corr(return, validation price return) | Mean return in up windows | Mean return in down windows |
| --- | --- | ---: | ---: | ---: |
| Base | Paper | 0.756 | +0.133% | -0.224% |
| Base | EWMA | 0.675 | +0.052% | -0.185% |
| BSC | Paper | 0.550 | +0.028% | -0.155% |
| BSC | EWMA | 0.473 | +0.016% | -0.122% |

The prompt's 7-window exploratory gate is directionally right but should be
tightened before becoming a frozen test. Using a strict causal entry join
defined as the last train-swap feature row before the first validation swap:

- Gate: `entry swap_flow_imbalance_cone_pct_1h >= 0.90` and
  `train_price_return <= 0`.
- Base Paper selected rows: 5 windows, mean +0.142%, sum +0.709%, worst
  +0.047%, positive 100.0%.
- Base Paper non-gated selected rows: 21 windows, mean -0.056%, sum -1.169%,
  worst -0.821%, positive 57.1%.
- Base EWMA under the same gate: 5 windows, mean +0.021%, positive 40.0%.
- BSC Paper under the same gate: 10 windows, mean +0.005%, worst -0.250%,
  positive 50.0%.

Strict Base Paper gate windows:

| Window | Validation start | Validation end | Selected width | Return | Train price return | Validation price return |
| ---: | --- | --- | ---: | ---: | ---: | ---: |
| 0 | 2026-03-12T15:00:55+00:00 | 2026-03-15T10:23:37+00:00 | 2.00% | +0.269% | -1.250% | +0.198% |
| 1 | 2026-03-16T08:17:25+00:00 | 2026-03-18T12:46:35+00:00 | 2.00% | +0.122% | -0.366% | +0.316% |
| 6 | 2026-03-26T05:45:57+00:00 | 2026-03-30T12:49:09+00:00 | 0.50% | +0.047% | -0.914% | +0.012% |
| 7 | 2026-03-30T13:15:17+00:00 | 2026-04-02T02:20:51+00:00 | 1.00% | +0.127% | -1.378% | +0.686% |
| 23 | 2026-06-04T11:11:25+00:00 | 2026-06-09T16:39:09+00:00 | 0.25% | +0.143% | -0.075% | -0.390% |

The existing `swap_flow_imbalance` feature is an event-sign imbalance:
`signed_usd_notional / abs(signed_usd_notional)`. Its cone percentile is useful
as a direction-pressure sensor, but it is not yet the QTS rolling notional flow
model described in the hypothesis.

## Next Research Test

Build a reduced, frozen-family validation harness that evaluates only causal
entry decisions and low-dimensional paper-style LP choices.

Required comparisons:

1. No-position baseline.
2. Passive static LP baseline.
3. Existing selected full-grid rank-1 stream.
4. Paper-style fixed-width family without the flow gate.
5. Paper-style fixed-width family with the strict causal flow gate.
6. Same families on BSC as diagnostics only.

Primary metric: costed validation return on capital.

Secondary metrics:

- positive-window rate
- worst window
- max drawdown
- fee/cost ratio
- churn and rebalance count
- missed-window return
- return versus holding cNGN inventory
- return attribution by pool-price direction

## Implementation Tasks

### Task 1: Freeze Entry-State Audit

Create a reusable audit script that joins each swap-count validation window to
the last causal train-swap feature row.

Suggested file:

- `research/scripts/evaluate_flow_gated_lp.py`

Inputs:

- `--windows research/results/extended_dex_only_full/uni_base_paper_swapwf_windows.csv`
- `--matrix research/results/extended_dex_only_full/uni_base_paper_matrix.csv`
- `--features research/data/derived/uni_base_pool_features.csv`
- `--pool uni-base`
- `--train-swaps 200`
- `--val-swaps 50`
- `--stride-swaps 50`
- `--out-dir research/results/flow_gated_lp`

Outputs:

- `entry_state_windows.csv`
- `selected_rank1_gate_summary.csv`
- `fixed_family_gate_summary.csv`
- `flow_gated_lp_report.md`

Acceptance:

- The Base selected rank-1 summary reproduces the strict 5-window result above.
- The script never uses a feature row from the first validation swap or later.
- Window counts match the source walk-forward summaries: Base 26, BSC 37.

### Task 2: Add Real QTS Flow Features

Add rolling pre-trade flow and adaptive flow-to-markout diagnostics before
promoting any gate.

Signals:

- `F_tau_signed_cngn`
- `F_tau_signed_usd`
- `F_tau_signed_usd_z`
- `markout_h_raw_sqrt_mid_return`
- `beta_tau_h_ew`
- `predicted_markout_tau_h`

Start with swap-count horizons:

- `tau` in 20, 50, 100 swaps
- `H` in 10, 25, 50 swaps

Then add wall-clock variants only if the swap-count version survives.

Acceptance:

- Each row uses only prior swaps for `F_tau` and `beta`.
- Markouts use the pool state at `t + H`, not the next future event if that
  event is outside the horizon.
- Static beta, adaptive beta, volatility-only, and flow-only variants are all
  reported side by side.

### Task 3: Run Frozen Base-First Validation

Evaluate a small paper-style family, not the full grid:

- center: `spot`
- widths: 0.25%, 0.50%, 1.00%, 1.50%, 2.00%
- harvest upward range fraction: 0.04
- exit confirmation swaps: 1
- exit price mode: `spot`
- overshoot: 0.00
- profit take: 0.50%, 1.00%
- stop loss: -0.25%, -0.50%, -1.00%, -2.00%

Gate variants:

- no gate
- strict current gate: high entry flow sign-cone and flat/down train price
- QTS gate: adaptive predicted markout supports cNGN exposure
- stress cone + QTS gate

Acceptance:

- The selected frozen rule beats no-position and passive LP after costs.
- Improvement is not explained by one or two windows.
- Leave-one-active-window-out remains non-negative.
- The strategy still beats the hold-cNGN comparator on risk-adjusted terms.

### Task 4: Keep BSC As A Diagnostic

Run the same report on BSC, but do not tune Base rules to make BSC work.

Acceptance:

- If BSC stays weak, document it as a pool-specific opportunity-set failure.
- If BSC turns positive only under validation-oracle configuration selection,
  keep it rejected.
- Promote cross-pool transfer only if the same gate logic improves both pools
  without pool-specific threshold fitting.

### Task 5: Decide Promotion, Demotion, Or Data Extension

Promote only if:

- costed validation return improves versus all baselines
- missed participation is acceptable
- exact gas, ratio-swap, and unwind costs do not erase the edge
- no active month or single window explains most of the gain
- the signal is causally available at decision time

Demote if:

- the strict gate collapses when shifted by one swap or one block
- QTS beta adds no out-of-sample value over volatility and price-trend filters
- BSC remains negative and the Base rule cannot be justified as pool-specific
  rather than overfit
- the best result is just long cNGN exposure in rising pool-price windows

## Immediate Recommendation

Implement Task 1 first. It is the smallest useful next step because it turns
the current exploratory observation into a reproducible gate audit and prevents
the 7-window exploratory result from being treated as a confirmed causal test.

If Task 1 passes, implement Task 2 before spending more time on sizing. Dynamic
sizing should be tested only after the entry signal is a real rolling flow and
markout model rather than the current event-sign cone percentile.

## Task 1 Audit Result

Status: completed 2026-06-27.

Implemented:

- `research/scripts/evaluate_flow_gated_lp.py`
- `research/tests/test_evaluate_flow_gated_lp.py`

Generated outputs:

- `research/results/flow_gated_lp/uni_base/entry_state_windows.csv`
- `research/results/flow_gated_lp/uni_base/selected_rank1_gate_summary.csv`
- `research/results/flow_gated_lp/uni_base/fixed_family_gate_summary.csv`
- `research/results/flow_gated_lp/uni_base/flow_gated_lp_report.md`
- `research/results/flow_gated_lp/uni_bsc/entry_state_windows.csv`
- `research/results/flow_gated_lp/uni_bsc/selected_rank1_gate_summary.csv`
- `research/results/flow_gated_lp/uni_bsc/fixed_family_gate_summary.csv`
- `research/results/flow_gated_lp/uni_bsc/flow_gated_lp_report.md`

Verification:

```bash
pytest research/tests/test_evaluate_flow_gated_lp.py -q
```

Result: 4 passed.

Base selected rank-1 strict gate:

| Section | Windows | Sum return | Mean active return | Mean all-window return | Worst return | Positive windows | Fee/cost ratio | Rebalances |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All | 26 | -0.460% | -0.018% | -0.018% | -0.821% | 65.4% | 1.213 | 8 |
| Gated | 5 | +0.709% | +0.142% | +0.027% | +0.047% | 100.0% | 1.488 | 0 |
| Non-gated | 21 | -1.169% | -0.056% | -0.045% | -0.821% | 57.1% | 1.144 | 8 |

BSC selected rank-1 strict gate:

| Section | Windows | Sum return | Mean active return | Mean all-window return | Worst return | Positive windows | Fee/cost ratio | Rebalances |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All | 37 | -2.086% | -0.056% | -0.056% | -0.559% | 35.1% | 0.779 | 13 |
| Gated | 10 | +0.053% | +0.005% | +0.001% | -0.250% | 50.0% | 0.996 | 9 |
| Non-gated | 27 | -2.139% | -0.079% | -0.058% | -0.559% | 29.6% | 0.698 | 4 |

Base sensitivity:

- Strict entry-row gate windows: 0, 1, 6, 7, 23.
- Entry shift -2 swaps: 2 windows, +0.316% sum, +0.047% worst.
- Entry shift -1 swap: 2 windows, +0.270% sum, +0.0004% worst.
- Entry shift +1 swap: 7 windows, +0.935% sum, -0.019% worst.
- Entry shift +2 swaps: 4 windows, +0.672% sum, -0.019% worst.
- Thresholds from 0.75 through 1.00 select the same 5 windows.
- Leave-one-active-window-out remains positive for the selected rank-1 gated
  stream: +0.440% to +0.662% remaining sum.

BSC sensitivity:

- Strict entry-row gate windows: 4, 8, 18, 19, 20, 21, 23, 25, 27, 36.
- Nearby entry shifts are near zero or negative, and positive-window rate
  remains weak.
- BSC remains diagnostic rather than promotable.

Validation-matrix clue:

- The best Base gated fixed-family row in the existing matrix is spot-centered
  1.00% width, harvest fraction 0.04, profit take 0.50%, stop loss -0.25%,
  zero overshoot. It returns +0.829% across the 5 active windows, with +0.044%
  worst active window, 100.0% positive active windows, no rebalances, and
  fee/cost ratio 1.868.
- This row is validation-oracle evidence from the matrix, not a deployable
  selector. It is useful only for freezing the next reduced family.

Updated next step:

Proceed to Task 2: build real QTS rolling flow and flow-to-markout features.
Do not test dynamic sizing until the entry signal uses rolling notional flow
and adaptive beta rather than the event-sign cone percentile alone.

## Task 2 QTS Flow/Markout Feature Result

Status: first pass completed 2026-06-27.

Implemented:

- `research/scripts/build_flow_markout_features.py`
- `research/tests/test_build_flow_markout_features.py`

Generated outputs:

- `research/data/derived/uni_base_flow_markout_features.csv` with 1,508 swap rows.
- `research/data/derived/uni_bsc_flow_markout_features.csv` with 3,105 swap rows.

The script computes:

- `F_{tau}_signed_cngn`
- `F_{tau}_signed_usd`
- `F_{tau}_signed_usd_z`
- `markout_{H}_raw_sqrt_mid_return`
- `beta_{tau}_{H}_ew`
- `predicted_markout_{tau}_{H}`

The initial grid is:

- `tau`: 20, 50, 100 swaps
- `H`: 10, 25, 50 swaps
- EW lambda: 0.94

Causality checks are test-backed:

- pre-trade flow excludes the current swap
- markout labels look forward
- adaptive beta at row `i` uses only matured markouts available by row `i`

Verification:

```bash
pytest research/tests/test_evaluate_flow_gated_lp.py research/tests/test_build_flow_markout_features.py -q
```

Result: 6 passed.

First overlay read:

| Pool | Gate | Active windows | Sum return | Mean return | Worst return | Positive windows |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Base | sign-cone strict gate | 5 | +0.709% | +0.142% | +0.047% | 100.0% |
| Base | sign gate and `predicted_markout_20_25 > 0` | 4 | +0.662% | +0.166% | +0.122% | 100.0% |
| Base | sign gate and `predicted_markout_100_25 > 0` | 3 | +0.540% | +0.180% | +0.127% | 100.0% |
| Base | train flat/down and `predicted_markout_20_25 > 0` | 7 | +0.779% | +0.111% | -0.019% | 85.7% |
| BSC | sign-cone strict gate | 10 | +0.053% | +0.005% | -0.250% | 50.0% |
| BSC | sign gate and `predicted_markout_20_25 > 0` | 7 | +0.191% | +0.027% | -0.057% | 42.9% |
| BSC | sign gate and `beta_50_10_ew > 0` | 4 | +0.271% | +0.068% | -0.057% | 75.0% |

Interpretation:

- QTS predicted-markout filters can improve average active-window quality on
  Base, but they also discard profitable strict-gate windows. They do not yet
  dominate the strict sign-cone gate on total costed return.
- `train <= 0` plus QTS prediction without the sign-cone gate admits more Base
  windows and slightly higher sum return, but it introduces a negative worst
  window. This is a candidate diagnostic, not a promotion.
- BSC remains too weak: QTS filters reduce the worst loss versus the raw strict
  gate in some variants, but positive-window rate is still poor and the result
  is too threshold/model-specific.

Updated next step:

Build the frozen-family experiment harness from Task 3 with the following gate
variants:

1. no gate
2. strict sign-cone gate
3. `train <= 0` plus `predicted_markout_20_25 > 0`
4. strict sign-cone gate plus `predicted_markout_20_25 > 0`
5. strict sign-cone gate plus `predicted_markout_100_25 > 0`

Promotion should require beating the strict sign-cone gate after no-position,
static LP, and hold-cNGN baselines are available. Until then, QTS features are
diagnostic evidence, not a deployable selector.

## Task 3 Frozen-Family Baseline Result

Status: completed 2026-07-02.

Implemented:

- `research/scripts/evaluate_frozen_family_lp.py`
- `research/tests/test_evaluate_frozen_family_lp.py`
- `strategy_mode="static"` support in the research backtester, for passive
  mark-at-window-end LP baselines.

Generated outputs:

- `research/results/flow_gated_lp/uni_base/frozen_family_window_results.csv`
- `research/results/flow_gated_lp/uni_base/frozen_family_gate_summary.csv`
- `research/results/flow_gated_lp/uni_base/frozen_family_report.md`
- `research/results/flow_gated_lp/uni_bsc/frozen_family_window_results.csv`
- `research/results/flow_gated_lp/uni_bsc/frozen_family_gate_summary.csv`
- `research/results/flow_gated_lp/uni_bsc/frozen_family_report.md`

The harness evaluates the frozen paper family from Task 3, passive static LP
widths, no-position, and hold-cNGN under five causal gate variants:

1. no gate
2. strict sign-cone gate
3. train flat/down plus `predicted_markout_20_25 > 0`
4. strict sign-cone plus `predicted_markout_20_25 > 0`
5. strict sign-cone plus `predicted_markout_100_25 > 0`

Base summary:

| Gate | Active windows | Best frozen paper sum | Best static LP sum | Hold-cNGN sum | Frozen worst | Frozen leave-one-out min | Interpretation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| no gate | 26 | +0.469% | +0.877% | +1.643% | -0.401% | +0.084% | LP does not beat hold-cNGN. |
| strict sign-cone | 5 | +0.829% | +0.829% | +0.822% | +0.044% | +0.450% | Positive and robust, but edge over hold is only +0.0066 percentage points. |
| train flat + QTS 20/25 | 7 | +0.922% | +0.922% | +1.248% | -0.019% | +0.558% | More total LP return than strict, but worse than hold and admits one negative LP window. |
| strict + QTS 20/25 | 4 | +0.796% | +0.796% | +0.811% | +0.027% | +0.432% | Cleaner active windows, but lower total and slightly below hold. |
| strict + QTS 100/25 | 3 | +0.662% | +0.662% | +0.494% | +0.027% | +0.298% | Beats hold on active return, but is too sparse and lower total than the strict gate. |

BSC summary:

| Gate | Active windows | Best frozen paper sum | Best static LP sum | Hold-cNGN sum | Frozen worst | Frozen leave-one-out min | Interpretation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| no gate | 37 | -2.933% | -0.948% | +0.897% | -1.328% | -3.178% | Frozen paper is rejected. |
| strict sign-cone | 10 | -1.183% | +0.339% | +1.888% | -1.328% | -1.428% | Hold dominates; paper remains negative. |
| train flat + QTS 20/25 | 11 | -1.635% | +0.093% | +0.050% | -1.391% | -1.783% | Static barely positive; paper rejected. |
| strict + QTS 20/25 | 7 | -1.304% | +0.400% | +0.330% | -1.328% | -1.412% | Static beats hold by a small amount, but paper is still negative. |
| strict + QTS 100/25 | 6 | -1.283% | +0.072% | +1.521% | -1.328% | -1.528% | Hold dominates. |

Interpretation:

- Base still has a conditional opportunity set, but the current frozen-family
  result is not a deployable LP policy. The best Base gated frozen-paper rows
  equal the passive static LP rows, which means the tested exit discipline is
  not adding value in those active windows.
- The strict Base gate remains the best simple LP gate by total active return
  and leave-one-active-window-out robustness. The QTS filters can improve
  selectivity, but they either lose total return or fail to beat hold-cNGN.
- Hold-cNGN is now a hard baseline. On Base, no-gate hold-cNGN beats all LP
  families; under the strict gate, frozen LP beats pool-mark hold by only
  0.0066 percentage points across five active windows.
- BSC remains diagnostic. Frozen paper is negative under every gate; occasional
  static-LP positives do not support promoting a paper-style strategy.

Current blocker:

First-pass close/unwind accounting and the harsh pool-routed cNGN comparator are
implemented. Active-window attribution now shows that the strict Base frozen
paper row adds no value over mark-only static LP (`lp_minus_static_mark = 0` in
all five active windows), while the total mark edge over pool-mark hold is only
`0.000066` return. Promotion still needs either a paper-exit policy that beats
static LP or a realistic non-pool cNGN inventory baseline.

Updated next step:

Do not run H12 as a promotion step. Next work should explain why the strict Base
gate should choose LP exposure instead of simpler cNGN inventory exposure under
a realistic non-pool route. Dynamic sizing is still premature.

## Task 3 Baseline-Hardening Follow-Up

Status: first pass completed 2026-07-02.

Implemented:

- `TransactionCostModel.close_position_on_end`
- terminal static-LP close/unwind accounting in the simulator
- `passive_static_lp_closed` rows in the frozen-family harness
- `hold_cngn_routed` rows that buy cNGN at validation entry and sell it at
  validation exit through the same pool-cost model

Base strict-gate result after adding the harder baseline rows:

| Comparator | Active windows | Sum return | Worst return | Leave-one-out min | Transaction cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| best frozen paper | 5 | +0.829% | +0.044% | +0.450% | $5.79 |
| best passive static LP, mark-only | 5 | +0.829% | +0.044% | +0.450% | $5.79 |
| best passive static LP, closed/unwound | 5 | +0.579% | -0.044% | +0.253% | $8.76 |
| hold-cNGN, pool mark | 5 | +0.822% | -0.390% | +0.136% | $0.00 |
| hold-cNGN, pool routed | 5 | -1.169% | -0.810% | -1.461% | $23.88 |
| no-position | 5 | 0.000% | 0.000% | 0.000% | $0.00 |

Interpretation:

- Terminal close/unwind costs do not erase the strict Base LP signal, but they
  do reduce the more realistic static baseline from +0.829% to +0.579%.
- The current frozen-paper strategy still does not add visible value over
  passive static LP in the active strict-gate windows.
- The pool-routed hold-cNGN comparator is very expensive and should be treated
  as a harsh DEX-only inventory route, not as a realistic CEX or external
  inventory route.
- The promotion blocker has narrowed: the strict Base gate beats no-position
  and a pool-routed hold path, but the tested active paper exits do not beat
  static LP. Promotion now needs an execution-costed non-pool cNGN inventory
  baseline.

Updated next step:

The next baseline is a realistic non-pool cNGN inventory route. The pool-routed
hold comparator is useful as a harsh lower bound, but it is too expensive to be
the deployment alternative.

## Task 4 LP-Versus-Inventory Attribution Follow-Up

Status: completed 2026-07-02.

Implemented:

- `lp_inventory_attribution.csv` in the frozen-family harness
- a Markdown attribution section in `frozen_family_report.md`
- strict active-window tests for comparator selection, delta calculation, and
  missing comparator failures

Base strict-gate attribution:

| Measure | Sum over five active windows |
| --- | ---: |
| best frozen paper net return | +0.829% |
| best passive static LP, mark-only | +0.829% |
| best passive static LP, closed/unwound | +0.579% |
| hold-cNGN, pool mark | +0.822% |
| hold-cNGN, pool routed | -1.169% |
| frozen paper minus mark-only static LP | 0.000 percentage points |
| frozen paper minus pool-mark hold | +0.0066 percentage points |
| closed static LP minus pool-mark hold | -0.243 percentage points |
| closed static LP minus pool-routed hold | +1.748 percentage points |
| frozen paper fee net component | +0.419% |
| frozen paper range/inventory residual | +0.410% |

Interpretation:

- The tested active paper exits still do not add value. The best frozen-paper
  row and the best mark-only static row are identical in every strict Base
  active window.
- The tiny mark edge over hold-cNGN is not a robust LP-specific edge. Pool-mark
  hold outperforms LP in two positive-price windows, while LP wins mainly by
  avoiding one negative hold window.
- Closed/unwound static LP remains positive but no longer beats pool-mark hold.
  It only beats routed hold because the same-pool inventory route is punitive.
- The fee/range split is balanced, not decisive: roughly half the mark-only LP
  return is fee net of transaction costs and half is residual range/inventory
  exposure.

Updated next step:

Build or source a non-pool cNGN inventory comparator, then rerun the same
strict active-window attribution. Do not promote H12 or dynamic sizing until LP
beats that comparator or the strategy is explicitly reframed as a pool-route
execution hedge rather than an LP alpha.

## Task 5 Directional-Paper LP Follow-Up

Status: first pass completed 2026-07-02.

Implemented:

- `research/scripts/evaluate_directional_paper_lp.py`
- `research/tests/test_evaluate_directional_paper_lp.py`
- reduced directional archetype configs for `upside_capture`,
  `dip_accumulator`, and `fee_box`
- causal routing rules over entry-state, flow-cone, fee/volume-cone, and QTS
  markout fields
- routed-policy, component-archetype, static LP, hold-mark, and no-position
  rows in one window-result table
- directional attribution rows with down-window deltas and missing-comparator
  failures

Generated outputs:

- `research/results/flow_gated_lp/uni_base/directional_paper_window_results.csv`
- `research/results/flow_gated_lp/uni_base/directional_paper_gate_summary.csv`
- `research/results/flow_gated_lp/uni_base/directional_paper_attribution.csv`
- `research/results/flow_gated_lp/uni_base/directional_paper_report.md`
- `research/results/flow_gated_lp/uni_bsc/directional_paper_window_results.csv`
- `research/results/flow_gated_lp/uni_bsc/directional_paper_gate_summary.csv`
- `research/results/flow_gated_lp/uni_bsc/directional_paper_attribution.csv`
- `research/results/flow_gated_lp/uni_bsc/directional_paper_report.md`

Run command:

```bash
python3 research/scripts/evaluate_directional_paper_lp.py --pool all
```

Verification:

```bash
pytest research/tests/test_evaluate_directional_paper_lp.py research/tests/test_evaluate_flow_gated_lp.py research/tests/test_evaluate_frozen_family_lp.py -q
python3 -m ruff check research/scripts/evaluate_directional_paper_lp.py research/tests/test_evaluate_directional_paper_lp.py
python3 -m py_compile research/scripts/evaluate_directional_paper_lp.py
python3 research/scripts/evaluate_directional_paper_lp.py --pool all
```

Result: 19 focused tests passed; ruff passed; py_compile passed; the full
directional harness wrote Base and BSC outputs.

Base route summary:

| Archetype | Windows |
| --- | ---: |
| `upside_capture` | 4 |
| `dip_accumulator` | 1 |
| `fee_box` | 1 |
| `no_position` | 20 |

Base directional-active result:

| Comparator | Active windows | Sum return | Worst return | Leave-one-out min | Fee/cost ratio | Active minus hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| best static LP | 6 | +1.002% | +0.027% | +0.638% | 2.304 | +0.069 percentage points |
| directional routed policy | 6 | +0.940% | -0.009% | +0.536% | 1.299 | +0.0068 percentage points |
| hold-cNGN, pool mark | 6 | +0.933% | -0.390% | +0.247% | n/a | baseline |
| no-position | 6 | 0.000% | 0.000% | 0.000% | n/a | -0.933 percentage points |

BSC directional-active result:

| Comparator | Active windows | Sum return | Worst return | Leave-one-out min | Fee/cost ratio | Active minus hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| hold-cNGN, pool mark | 11 | +1.857% | -0.303% | +1.601% | n/a | baseline |
| directional routed policy | 11 | -0.897% | -0.842% | -1.682% | 0.430 | -2.754 percentage points |
| no-position | 11 | 0.000% | 0.000% | 0.000% | n/a | -1.857 percentage points |

Interpretation:

- The reduced directional family is now reproducible and causal, but it is not
  promotable. Base directional routing beats no-position and barely beats
  pool-mark hold on the six routed windows, but it loses to the best static LP
  comparator and has one negative active window.
- The strict sign-cone view is slightly cleaner for Base: routed directional LP
  sums to +0.951% across five active windows, versus +0.829% for the best
  static LP and +0.822% for hold. That result is still too thin for promotion
  because the full directional route admits an extra weak window and the edge is
  small.
- BSC remains a falsification pool. Directional routing is negative under the
  active route and strict sign-cone gates while hold-cNGN is positive, so the
  Base result should not be generalized cross-pool.
- The down-window attribution is useful but not yet decisive. Base routed LP
  avoids some hold-mark downside in negative price windows, but the fee/range
  split is inconsistent and the static LP comparator is still competitive.

Updated next step:

Keep H12 blocked. The next useful research branch is either a stricter Base
directional route that refuses the extra weak directional-active window, or a
realistic non-pool cNGN inventory comparator that tests whether LP exposure is
better than simply owning cNGN when the causal signal is active.

## Task 6 Directional Fixed-Family Expansion

Status: first pass completed 2026-07-02.

Implemented:

- five route-aware policy profiles in `directional_archetype_configs()`
- 15 total directional shapes: one `upside_capture`, `dip_accumulator`, and
  `fee_box` shape per profile
- routed-policy rows for each profile, so the gate summary compares complete
  predeclared route policies rather than validation-oracle per-window choices
- component rows for all 15 shapes, so shape failures can be inspected
  separately from routing failures

Policy profiles:

- `balanced_v1`
- `upside_wide_v1`
- `upside_tight_v1`
- `dip_wide_v1`
- `fee_tight_v1`

Expanded Base directional-active result:

| Comparator | Active windows | Sum return | Worst return | Leave-one-out min | Fee/cost ratio | Active minus hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `upside_tight_v1` routed policy | 6 | +1.028% | -0.009% | +0.620% | 1.622 | +0.095 percentage points |
| best static LP | 6 | +1.002% | +0.027% | +0.638% | 2.304 | +0.069 percentage points |
| hold-cNGN, pool mark | 6 | +0.933% | -0.390% | +0.247% | n/a | baseline |
| no-position | 6 | 0.000% | 0.000% | 0.000% | n/a | -0.933 percentage points |

Expanded Base strict sign-cone result:

| Comparator | Active windows | Sum return | Worst return | Leave-one-out min | Fee/cost ratio | Active minus hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `upside_tight_v1` routed policy | 5 | +1.039% | 0.000% | +0.631% | 1.471 | +0.217 percentage points |
| best static LP | 5 | +0.829% | +0.044% | +0.450% | 1.868 | +0.0065 percentage points |
| hold-cNGN, pool mark | 5 | +0.822% | -0.390% | +0.136% | n/a | baseline |
| no-position | 5 | 0.000% | 0.000% | 0.000% | n/a | -0.822 percentage points |

Expanded BSC directional-active result:

| Comparator | Active windows | Sum return | Worst return | Leave-one-out min | Fee/cost ratio | Active minus hold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| hold-cNGN, pool mark | 11 | +1.857% | -0.253% | +0.155% | n/a | baseline |
| best static LP | 11 | +0.264% | -0.186% | +0.0004% | 1.303 | -1.592 percentage points |
| `fee_tight_v1` routed policy | 11 | -0.876% | -0.842% | -1.661% | 0.432 | -2.733 percentage points |
| no-position | 11 | 0.000% | 0.000% | 0.000% | n/a | -1.857 percentage points |

Profile ranking on Base directional-active windows:

| Profile | Sum return | Worst return | Positive window rate | Fee/cost ratio | Active minus hold |
| --- | ---: | ---: | ---: | ---: | ---: |
| `upside_tight_v1` | +1.028% | -0.009% | 66.7% | 1.622 | +0.095 percentage points |
| `dip_wide_v1` | +0.943% | -0.006% | 66.7% | 1.314 | +0.0098 percentage points |
| `balanced_v1` | +0.940% | -0.009% | 66.7% | 1.299 | +0.0068 percentage points |
| `upside_wide_v1` | +0.757% | -0.129% | 50.0% | 0.898 | -0.176 percentage points |
| `fee_tight_v1` | +0.684% | -0.257% | 66.7% | 1.153 | -0.249 percentage points |

Interpretation:

- The fixed-family expansion improved Base enough to justify one more
  targeted pass. `upside_tight_v1` is the first route-aware directional policy
  that beats both best static LP and pool-mark hold on the directional-active
  and strict sign-cone summaries.
- The result is still not promotable. The edge is small, the directional-active
  route still has one slightly negative routed window, and hold-cNGN still wins
  over all no-gate windows.
- The failure mode is clearer: wide upside ranges underperform, while a tighter
  upside range captures enough fee/range PnL to improve the strict Base slice.
  The next pass should focus on route selectivity around the weak windows rather
  than expanding shape count further.
- BSC remains a useful falsification pool. The best routed policy is still
  negative and far below hold-cNGN, so the Base result should remain explicitly
  pool-specific.

Updated next step:

Do not broaden the shape family again yet. Test whether the Base router can
reject the weak `fee_box` and `dip_accumulator` windows while preserving the
strict sign-cone upside windows, then compare the resulting route against
pool-mark hold and a non-pool cNGN inventory route.

## Task 7 Strict Upside Router Check

Status: fresh check completed 2026-07-10.

Run command:

```bash
python3 research/scripts/evaluate_directional_paper_lp.py --pool all
```

The existing `gate_strict_qts_20_25` slice is the stricter Base router check:
it keeps the strict sign-cone + positive QTS upside windows and rejects the
extra `fee_box` and `dip_accumulator` windows admitted by
`gate_directional_active`.

Base result:

| Gate | Active windows | Directional sum | Worst directional | Positive directional | Directional minus hold | Best static sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gate_directional_active` | 6 | +1.028% | -0.009% | 66.7% | +0.095 pp | +1.002% |
| `gate_strict_qts_20_25` | 4 | +1.039% | +0.118% | 100.0% | +0.228 pp | +0.796% |
| `gate_strict_qts_100_25` | 3 | +0.854% | +0.118% | 100.0% | +0.360 pp | +0.662% |

BSC under the same strict QTS gates remains rejected:

| Gate | Active windows | Directional sum | Worst directional | Positive directional | Directional minus hold | Best static sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gate_strict_qts_20_25` | 7 | -1.272% | -0.842% | 14.3% | -1.602 pp | +0.400% |
| `gate_strict_qts_100_25` | 6 | -0.583% | -0.842% | 33.3% | -2.105 pp | +0.072% |

Interpretation:

- The strict Base upside router passes the next narrow diagnostic: removing
  `fee_box` and `dip_accumulator` improves worst-window behavior and produces a
  cleaner edge versus both pool-mark hold and static LP on the active slice.
- The active slice is still only four windows, so this is not promotion
  evidence. It is evidence that the next comparator should be stronger, not that
  the route family should expand again.
- BSC falsifies cross-pool generalization. The same strict QTS gates make
  directional LP materially worse than hold-cNGN and static LP there.

Historical next-step proposal, now superseded by Tasks 8 and 9:

Keep H12 blocked. If a non-pool cNGN inventory comparator becomes available,
compare the strict Base upside router against:

1. no position,
2. best static LP,
3. pool-mark hold-cNGN,
4. external-reference hold-cNGN,
5. pool-routed hold-cNGN where available.

Only after the strict Base router beats the external-reference hold comparator
should capacity curves or dynamic sizing become final experiments.

Note: Task 8 supersedes this Binance-specific next step. Binance `USDTNGN`
does not overlap the 2026 pool window, so the external comparator now needs a
different non-pool reference series.

## Task 8 External Reference Comparator Implementation

Status: implementation completed 2026-07-10; Binance spot source checked and
not usable for the active overlap.

Implemented in `research/scripts/evaluate_directional_paper_lp.py`:

- `ReferencePricePoint` for external cNGN reference marks.
- `load_reference_price_csv()` for Binance/reference CSVs.
- `external_reference_hold_cngn_rows()` as a non-pool cNGN inventory comparator.
- Optional CLI flags:
  - `--external-reference-csv`
  - `--external-reference-source`
  - `--external-reference-max-age-seconds`
- Attribution columns:
  - `hold_external_config`
  - `hold_external_return`
  - `directional_minus_hold_external`

Reference CSV input accepts either `timestamp_ms` or `ts`, plus one of:

- `reference_price`
- `price`
- `mid`
- `binance_reference`
- `ngn_per_usdt`
- `usdt_ngn`

The comparator is intentionally fail-closed. If the supplied reference file does
not have a previous-or-equal price for every validation start and end timestamp
within the max age, the run raises instead of silently falling back to the pool
mark.

Fresh no-reference run:

```bash
python3 research/scripts/evaluate_directional_paper_lp.py --pool all
```

Result: the Base and BSC conclusions from Task 7 still hold. The new external
attribution columns are present but blank because no Binance reference file is
available in the checkout.

Historical run form if a future external reference file exists:

```bash
python3 research/scripts/evaluate_directional_paper_lp.py \
  --pool uni-base \
  --external-reference-csv research/data/<source>_reference.csv \
  --external-reference-source <source_name> \
  --external-reference-max-age-seconds 3600
```

Binance source result:

- `USDTNGN` exists on Binance spot, but current `exchangeInfo` reports status
  `BREAK`.
- The most recent available `USDTNGN` 1m kline opens at
  `2024-03-07T02:59:00+00:00`.
- The local Base v4 pool initialize/first-swap timestamp is
  `2026-03-04T16:51:45+00:00`; the BSC v4 pool initialize timestamp is
  `2026-03-04T16:15:30+00:00`.
- The Binance break therefore predates the local cNGN Uniswap v4 pools by almost
  two years and does not line up with their release.
- Fetching `USDTNGN` 1m klines over the Quidax JSON window wrote zero rows to
  `research/data/binance_usdtngn_1m_reference.csv`.
- Running the strict Base router with that sourced reference fails closed with:
  `missing external reference price for window=0 edge=start`.

Promotion remains blocked. For the LP-specific non-pool inventory comparator,
the next external anchor should be Bybit P2P once there is enough overlapping
forward data. Until then, keep the Base directional result framed as a
DEX-internal diagnostic, not a deployable LP alpha.

## Task 9 External Reference Attempt And Closeout

Status: completed 2026-07-10; comparator rejected and LP research closed as a
diagnostic result.

One-pass external reference result:

- Official Bybit P2P documentation exposes `POST /v5/p2p/item/online` as an
  online-ad endpoint. It is useful for current ads, not a historical archive.
- The legacy public Bybit endpoint is reachable for current `USDT`/`NGN` ads,
  but it cannot backfill March-June 2026 validation edges.
- Local `bybit_p2p` rows in `data/cngn.db` cover 171 snapshots from
  `2026-06-19T12:26:49.707000+00:00` through
  `2026-06-20T01:09:16.620000+00:00`.
- The Base strict-sign-cone-positive-QTS slice needs eight start/end marks from
  `2026-03-12T15:00:55+00:00` through
  `2026-06-09T16:39:09+00:00`.
- Bybit covers `0/8` of those strict edge marks within the 3600-second max-age
  rule.

Closeout conclusion:

- The non-pool comparator cannot be populated from currently available data.
- Fair Price is closed as feed-quality and managed-surface evidence.
- DEX LP is closed as diagnostic market-structure evidence: Base has a
  sparse pool-internal directional slice, BSC falsifies transferability, and no
  live LP promotion is justified.
- H12 capacity, dynamic sizing, and live policy integration remain blocked until
  genuinely new timestamped external data arrives.
