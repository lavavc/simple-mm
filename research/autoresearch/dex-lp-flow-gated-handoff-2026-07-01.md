# DEX LP Flow-Gated Research Handoff

Date: 2026-07-01

## Current State

The current DEX-only LP research does **not** support deploying any full-grid
rank-1 strategy stream. The useful result is narrower:

- Base cNGN LP has a possible conditional opportunity set.
- BSC remains diagnostic and should not be generalized into a cross-pool rule.
- Returns are still dominated by directional pool-price exposure and regime
  selection, not by a stable global range policy.
- The next deployability question is whether a causal entry gate plus a frozen
  paper-style family beats no-position, passive LP, and hold-cNGN baselines
  after costs.

Primary research docs:

- `research/autoresearch/dex-only-rerun-status.md`
- `research/autoresearch/lp.md`
- `research/autoresearch/flow-gated-cngn-lp-plan.md`

Primary full-history artifacts:

- `research/results/extended_dex_only_full/`
- `research/data/derived/uni_base_pool_history_replay.csv`
- `research/data/derived/uni_bsc_pool_history_replay.csv`
- `research/data/derived/uni_base_pool_features.csv`
- `research/data/derived/uni_bsc_pool_features.csv`

Generated artifacts from the latest flow-gate pass are ignored by `.gitignore`
under `research/data/**` and `research/results/**`. Regenerate them from the
scripts instead of trying to commit them.

## Continuation Update

Status: frozen-family harness completed after this handoff.

New source files:

- `research/scripts/evaluate_frozen_family_lp.py`
- `research/tests/test_evaluate_frozen_family_lp.py`

Changed research support:

- `research/backtester/params.py`
- `research/backtester/run.py`
- `research/backtester/simulator.py`
- `research/scripts/evaluate_flow_gated_lp.py`
- `TransactionCostModel.close_position_on_end`

The frozen-family pass writes:

- `frozen_family_window_results.csv`
- `frozen_family_gate_summary.csv`
- `frozen_family_report.md`

Current read:

- Base strict sign-cone gate is still the best simple LP gate by total active
  return and leave-one-active-window-out robustness.
- The best Base strict-gated frozen-paper rows equal passive static LP and beat
  hold-cNGN by only `0.0065` percentage points across five active windows.
- After terminal close/unwind costs, the best Base strict-gated closed static LP
  row falls to `+0.579%` across five active windows, with `-0.044%` worst
  active window.
- The pool-routed hold-cNGN comparator is negative under the strict Base gate
  (`-1.169%`) because the DEX pool route is expensive. Treat this as a harsh
  DEX-only path, not as a realistic external inventory route.
- QTS overlays are still diagnostic. They improve selectivity in some subsets
  but do not dominate the strict sign-cone gate plus hold-cNGN baseline.
- BSC frozen paper is negative under every tested gate and remains diagnostic.

Next live question:

Do not run H12 as a promotion step yet. First-pass baseline hardening now exists.
The next question is attribution: why should Base choose active paper LP exits
over passive static LP, and why should it choose LP exposure over a non-pool
cNGN inventory route?

## Implemented Since Full Rerun

New source files:

- `research/scripts/evaluate_flow_gated_lp.py`
- `research/tests/test_evaluate_flow_gated_lp.py`
- `research/scripts/build_flow_markout_features.py`
- `research/tests/test_build_flow_markout_features.py`
- `research/scripts/evaluate_frozen_family_lp.py`
- `research/tests/test_evaluate_frozen_family_lp.py`

The flow-gate audit script writes:

- `entry_state_windows.csv`
- `selected_rank1_gate_summary.csv`
- `fixed_family_gate_summary.csv`
- `flow_gated_lp_report.md`

The QTS flow/markout feature script writes:

- `uni_base_flow_markout_features.csv`
- `uni_bsc_flow_markout_features.csv`

Verification commands:

```bash
pytest research/tests/test_evaluate_flow_gated_lp.py research/tests/test_build_flow_markout_features.py -q
python3 -m ruff check research/scripts/evaluate_flow_gated_lp.py research/scripts/build_flow_markout_features.py research/tests/test_evaluate_flow_gated_lp.py research/tests/test_build_flow_markout_features.py
python3 -m py_compile research/scripts/evaluate_flow_gated_lp.py research/scripts/build_flow_markout_features.py
```

Last verified result:

- `pytest`: 6 passed
- `ruff`: all checks passed
- `py_compile`: passed

## Strategy Families Under Experiment

The research currently compares strategy *families*, entry rules, and baseline
comparators. Keep these concepts separate: the LP family defines how a position
is shaped and exited after entry; the entry gate defines whether capital is
deployed for a validation window; the baseline defines what the LP rule must
beat.

### EWMA LP Strategy

EWMA is the production-adjacent range policy family. It computes a venue-local
log-price EWMA center and volatility estimate from pool history, then chooses a
range around that center using parameters such as:

- `sd_multiplier`
- `ewma_lambda`
- `downside_skew`
- `preemptive_rebalance`
- `rebalance_threshold_pct`

In the full-grid rerun, EWMA acted more like a regime-fitting surface than a
stable deployable policy. Base EWMA and BSC EWMA sometimes selected broadly
similar parameter regions, but exact rank-1 choices jumped across neighboring
windows and selected OOS economics stayed negative after costs.

Use EWMA now as:

- the current dynamic-range comparison family
- a diagnostic for whether stress-cone state can explain width/threshold
  movement ex ante
- not as a deployable stream from the full-grid rank-1 output

Do not promote EWMA until parameter movement can be tied to causal DEX-side
features and it beats the frozen paper family after costs.

### Paper-Style LP Strategy

Paper-style LP is the policy family that most resembles the current promising
direction. It uses fixed percentage-width ranges and explicit exit discipline
instead of EWMA volatility ranges.

Important dimensions:

- `center_mode`: usually `spot`, sometimes `ewma`
- `fixed_width_pct`: tested across narrow and mid-width ranges
- `harvest_upward_range_fraction`: consistently 0.04 in selected rows
- `profit_take_return`
- `stop_loss_return`
- `out_of_range_overshoot_fraction`
- `exit_confirmation_swaps`: selected rows use one swap
- `exit_price_mode`: selected rows use `spot`

The full rerun rejected paper rank-1 streams as deployable, but the paper family
contains the clearest reduced hypothesis:

- shared exit discipline
- pool-specific width
- Base-first entry selection
- BSC as diagnostic only

The next frozen family should not rerun the full paper grid. It should test a
small paper-style family with `spot` center, 0.25% to 2.00% widths, one-swap
confirmation, spot exit mode, zero overshoot, and a small profit-take/stop-loss
set.

### Full-Grid Rank-1 Stream

The full-grid rank-1 stream is the old selection mechanism:

1. For each swap-count walk-forward window, evaluate the full parameter grid on
   the training slice.
2. Select the train rank-1 config.
3. Evaluate that selected config on the validation slice.

This stream is rejected for deployment. It is still useful as evidence because
it shows where the search surface concentrated, but it should not be used as a
capital allocation rule.

Failure modes:

- high parameter churn
- negative full-history selected OOS economics
- Base and BSC exact rank-1 choices are not stable enough
- PBO/rank stability alone did not overcome costed validation losses

### Frozen-Family Stream

The frozen-family stream is the replacement selection design. It does not ask
the training window to pick from the full grid. Instead, it tests a predeclared
small family against causal entry rules.

Current frozen paper family candidate:

- `center_mode = spot`
- widths: 0.25%, 0.50%, 1.00%, 1.50%, 2.00%
- `harvest_upward_range_fraction = 0.04`
- `exit_confirmation_swaps = 1`
- `exit_price_mode = spot`
- `out_of_range_overshoot_fraction = 0.00`
- `profit_take_return`: 0.50%, 1.00%
- `stop_loss_return`: -0.25%, -0.50%, -1.00%, -2.00%

The frozen-family stream is the next real promotion test, once the missing
baselines are available.

### No Entry Gate

The no-gate variant deploys the candidate LP family in every eligible
validation window. This is the plain benchmark for an LP policy: if the entry
gate cannot beat this after missed participation is counted, the gate is not
useful.

No-gate comparisons to run:

- paper fixed-width family with no gate
- selected full-grid rank-1 stream without any additional gate
- eventually static LP with no gate

### Strict Sign-Cone Entry Gate

The strict sign-cone gate is the current best simple Base entry rule:

- compute state causally at the last train-swap row before validation starts
- require `swap_flow_imbalance_cone_pct_1h >= 0.90`
- require `train_price_return <= 0`

Economically, positive swap flow means signed cNGN amount is positive, so cNGN
is leaving the pool. Traders are buying cNGN from the pool. The gate says to
only deploy after a flat/down training window when recent swap direction is at
the high end of its local cone.

This gate is currently the primary hurdle for QTS overlays. A QTS gate must beat
this rule after costs and baselines, not merely improve statistical fit.

### QTS Flow/Markout Entry Gates

QTS gates use rolling pre-trade flow and adaptive flow-to-markout estimates
rather than the event-sign cone alone.

Current QTS features:

- `F_{tau}_signed_cngn`
- `F_{tau}_signed_usd`
- `F_{tau}_signed_usd_z`
- `markout_{H}_raw_sqrt_mid_return`
- `beta_{tau}_{H}_ew`
- `predicted_markout_{tau}_{H}`

Current grid:

- `tau`: 20, 50, 100 swaps
- `H`: 10, 25, 50 swaps
- EW lambda: 0.94

QTS gate variants to carry forward:

1. `train <= 0` plus `predicted_markout_20_25 > 0`
2. strict sign-cone gate plus `predicted_markout_20_25 > 0`
3. strict sign-cone gate plus `predicted_markout_100_25 > 0`

Current read: QTS filters improve average active-window quality in some Base
subsets, but they also drop profitable strict-gate windows. Treat them as
diagnostic until they beat the strict sign-cone gate in the frozen-family
harness.

### Pool Scope: Base vs BSC

Base and BSC must remain separate.

Base is the only pool with a plausible current opportunity set. The Base test is
about whether a conditional LP exposure rule can be made defensible after
baselines and costs.

BSC is a diagnostic pool. It checks whether a rule transfers, but it should not
be used to tune thresholds right now. BSC staying weak is an acceptable and
important result.

### Baseline Comparators

The strategy family and gate are not enough. Promotion requires comparison
against baselines:

- **No-position:** capital stays idle for the window. This should be treated as
  zero return and zero costs unless a cash yield benchmark is explicitly added.
- **Passive static LP:** mint once at first eligible/gated event, no active
  rebalance, accrue fees, close or mark at window end.
- **Hold-cNGN:** convert starting capital into cNGN exposure at entry and mark
  it at window end. The current harness includes both a no-cost pool mark and a
  harsh DEX-pool routed entry/exit path.
- **Existing active LP:** the selected EWMA/paper streams from the current
  backtester.

No-position is easy to express conceptually. Passive static LP and hold-cNGN now
exist as first-pass harness baselines, but promotion still needs route-specific
non-pool inventory assumptions and LP-versus-inventory attribution.

## Research Findings

### Full-grid rank-1 streams are rejected

| Pool | Style | Windows | Sum validation return | Mean window return | Positive windows |
| --- | --- | ---: | ---: | ---: | ---: |
| Base | Paper | 26 | -0.460% | -0.018% | 65.4% |
| Base | EWMA | 26 | -1.256% | -0.048% | 53.8% |
| BSC | Paper | 37 | -2.086% | -0.056% | 35.1% |
| BSC | EWMA | 37 | -1.755% | -0.047% | 32.4% |

### Strict Base flow gate is reproducible

Strict causal gate:

- use the last train-swap feature row before the first validation swap
- `entry swap_flow_imbalance_cone_pct_1h >= 0.90`
- `train_price_return <= 0`

Base Paper selected rank-1 result:

| Section | Windows | Sum return | Mean active return | Worst return | Positive windows |
| --- | ---: | ---: | ---: | ---: | ---: |
| All | 26 | -0.460% | -0.018% | -0.821% | 65.4% |
| Gated | 5 | +0.709% | +0.142% | +0.047% | 100.0% |
| Non-gated | 21 | -1.169% | -0.056% | -0.821% | 57.1% |

Strict Base active windows:

- 0: 2026-03-12 to 2026-03-15
- 1: 2026-03-16 to 2026-03-18
- 6: 2026-03-26 to 2026-03-30
- 7: 2026-03-30 to 2026-04-02
- 23: 2026-06-04 to 2026-06-09

Sensitivity:

- thresholds from 0.75 through 1.00 select the same 5 Base windows
- nearby entry-row shifts stay positive on Base, though the strict gate is sparse
- leave-one-active-window-out remains positive for the Base selected rank-1
  gated stream

### BSC remains diagnostic

BSC strict-gated Paper selected rank-1:

- 10 active windows
- +0.053% sum return
- +0.005% mean active return
- -0.250% worst active window
- 50.0% positive windows

This is not a promotion signal. BSC can be used as a transferability check, but
not as a pool to tune against right now.

### QTS flow/markout features are diagnostic, not promotable

The QTS script computes:

- `F_{tau}_signed_cngn`
- `F_{tau}_signed_usd`
- `F_{tau}_signed_usd_z`
- `markout_{H}_raw_sqrt_mid_return`
- `beta_{tau}_{H}_ew`
- `predicted_markout_{tau}_{H}`

Initial grid:

- `tau`: 20, 50, 100 swaps
- `H`: 10, 25, 50 swaps
- EW lambda: 0.94

First overlay read:

| Pool | Gate | Active windows | Sum return | Mean return | Worst return | Positive windows |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Base | strict sign-cone gate | 5 | +0.709% | +0.142% | +0.047% | 100.0% |
| Base | sign gate + `predicted_markout_20_25 > 0` | 4 | +0.662% | +0.166% | +0.122% | 100.0% |
| Base | sign gate + `predicted_markout_100_25 > 0` | 3 | +0.540% | +0.180% | +0.127% | 100.0% |
| Base | train flat/down + `predicted_markout_20_25 > 0` | 7 | +0.779% | +0.111% | -0.019% | 85.7% |
| BSC | strict sign-cone gate | 10 | +0.053% | +0.005% | -0.250% | 50.0% |
| BSC | sign gate + `predicted_markout_20_25 > 0` | 7 | +0.191% | +0.027% | -0.057% | 42.9% |

Interpretation:

- QTS filters can improve average active-window quality on Base.
- They also discard profitable strict-gate windows.
- They do not yet dominate the strict sign-cone gate on total costed return.
- BSC remains weak and threshold/model-specific.

## Next Steps

### 1. Improve frozen-family baseline accounting

The frozen-family experiment harness now exists. The next step is to make the
baseline comparators realistic enough for promotion decisions.

Needed improvements:

- explicit static LP close or unwind costs at window end
- realistic hold-cNGN route costs
- route-specific cNGN inventory assumptions
- LP-versus-cNGN attribution for the strict Base gate

The tested comparison set was:

Frozen paper family:

- center: `spot`
- widths: 0.25%, 0.50%, 1.00%, 1.50%, 2.00%
- harvest upward range fraction: 0.04
- exit confirmation swaps: 1
- exit price mode: `spot`
- overshoot: 0.00
- profit take: 0.50%, 1.00%
- stop loss: -0.25%, -0.50%, -1.00%, -2.00%

Gate variants:

1. no gate
2. strict sign-cone gate
3. `train <= 0` plus `predicted_markout_20_25 > 0`
4. strict sign-cone gate plus `predicted_markout_20_25 > 0`
5. strict sign-cone gate plus `predicted_markout_100_25 > 0`

### 2. Harden baselines before promotion

Current baselines:

- no-position
- passive static LP, mark-at-window-end
- hold-cNGN inventory, no-transaction-cost pool-price mark

Remaining blocker:

The baselines are now first-pass comparators, not yet promotion-grade
accounting. Static LP needs explicit close or unwind accounting. Hold-cNGN needs
route-cost and inventory-route assumptions.

### 3. Decide whether QTS gates are useful

Promotion requires the QTS gate to beat the strict sign-cone gate after all
baselines are included. Until then, QTS features are diagnostics only.

Required metrics:

- costed validation return
- positive active-window rate
- worst active window
- drawdown
- fee/cost ratio
- churn and rebalance count
- missed-window return
- return versus hold-cNGN
- leave-one-active-window-out

### 4. Keep BSC separate

Do not tune Base rules to make BSC work. Use BSC only to test transferability
after Base has a defensible frozen rule.

## Blockers

1. **Baselines are incomplete.**
   Static LP and hold-cNGN need simulator or harness support before any
   promotion claim.

2. **Generated research outputs are ignored.**
   `research/data/**` and `research/results/**` are ignored. Commit scripts,
   tests, and docs; regenerate CSV/Markdown result artifacts locally.

3. **BSC remains weak.**
   Any cross-pool generalization is premature.

4. **QTS overlay is not yet a selector.**
   It improves some subsets but does not dominate the strict sign-cone gate.

5. **Historical Quidax coverage remains deferred.**
   CEX-label, DEX-premium, and Fair Value hypotheses should stay parked until
   historical Quidax coverage exists.

## Notes On Merging To Main

Current local branch:

- `research`
- `origin/research` points at `a013f4d` before the uncommitted flow-gate work
- `origin/main` was fetched on 2026-07-01 and points at `39ca204`

Current dirty/untracked state:

- pre-existing/user changes:
  - `.Rhistory` deleted
  - `.gitignore` modified
  - `AGENTS.md` modified
- research handoff/work-in-progress files:
  - `research/autoresearch/flow-gated-cngn-lp-plan.md`
  - `research/scripts/evaluate_flow_gated_lp.py`
  - `research/tests/test_evaluate_flow_gated_lp.py`
  - `research/scripts/build_flow_markout_features.py`
  - `research/tests/test_build_flow_markout_features.py`
  - this handoff file

Important merge warning:

- `git diff origin/main...HEAD` currently fails with `fatal: origin/main...HEAD:
  no merge base`.
- `git rev-list --left-right --count origin/main...HEAD` reports `434 478`,
  so the branch histories are substantially divergent.
- Do not casually merge `research` into `main` until the history relationship is
  understood.

Recommended merge path:

1. Leave `.Rhistory`, `.gitignore`, and `AGENTS.md` out of the research commit
   unless their changes are deliberately part of the merge.
2. Commit only the research scripts, tests, and autoresearch docs.
3. Re-run:

   ```bash
   pytest research/tests/test_evaluate_flow_gated_lp.py research/tests/test_build_flow_markout_features.py -q
   python3 -m ruff check research/scripts/evaluate_flow_gated_lp.py research/scripts/build_flow_markout_features.py research/tests/test_evaluate_flow_gated_lp.py research/tests/test_build_flow_markout_features.py
   python3 -m py_compile research/scripts/evaluate_flow_gated_lp.py research/scripts/build_flow_markout_features.py
   ```

4. Because there is no merge base with current `origin/main`, prefer one of:
   - open a PR from `research` only after confirming GitHub can compare the
     histories correctly, or
   - cherry-pick the research commits onto a fresh branch from `origin/main`, or
   - create a patch from the research files and apply it to a fresh branch from
     `origin/main`.
5. Before merging, run the broader research/package tests that are practical for
   the final branch, at minimum the two focused test files and any import/layout
   tests touching `research/scripts`.

Do not include ignored generated CSV/result artifacts in the merge unless the
repo policy changes explicitly.
