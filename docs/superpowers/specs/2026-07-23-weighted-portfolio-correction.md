# Weighted LP Portfolio Correction Contract

> **Historical contract:** the July 24 execution-finalization amendment
> [`2026-07-24-weighted-portfolio-execution-finalization.md`](2026-07-24-weighted-portfolio-execution-finalization.md)
> supersedes this document's entry-scaling, terminal-settlement, diagnostics,
> checkpoint-compatibility, and source-freeze clauses. Preserve this document as
> v2 audit provenance. The binding successor is the v4 shared-exit-funding
> design
> [`2026-07-24-weighted-portfolio-v4-shared-exit-funding-design.md`](2026-07-24-weighted-portfolio-v4-shared-exit-funding-design.md);
> do not use this document's conflicting clauses for a v3 or v4 run.

**Date frozen:** 2026-07-23
**Historical status as of 2026-07-23; do not action:** approved amendment;
implementation complete, final verification and frozen Base/BSC execution
pending
**Supersedes:** only the conflicting weighted-portfolio clauses identified below in
`2026-07-10-weighted-parameterization-portfolio-design.md` and its implementation
plan. All other July 10 research constraints remain binding.

## Why This Amendment Exists

The first full Base/BSC execution began before a pre-publication implementation
audit found four material mismatches between the July 10 design and the running
code:

1. forty exact paper/frozen parameter pairs were separately fundable;
2. every validation window reset to the reference bankroll and omitted boundary
   reallocation costs;
3. exits and entries were executed in separate same-event batches;
4. the comparator and family files contained standalone candidate returns rather
   than the promised comparators and joint attribution.

The running output is therefore an **implementation audit**, not a frozen
portfolio result. It must be quarantined and may not be used to tune parameters,
ownership, thresholds, or comparator selection. This amendment is driven by the
pre-existing design contracts and accounting defects, not by observed Base or
BSC performance.

## Frozen Decisions

### Canonical economic sleeves

Parameter sleeves are canonical within a pool by their complete serialized
`BacktestParams` fingerprint. Route-aware directional policies remain distinct
because routing is part of their economic behavior.

The catalog keeps every declaration as immutable provenance, but only canonical
economic IDs may enter eligibility, allocation, simulation, PBO, comparator
selection, removal sensitivity, or attribution.

The only approved cross-family collision is an exact paper/frozen pair:

- the canonical sleeve's allocation family is `frozen`;
- the paper declaration remains an alias;
- `paper_exclusive` contains only paper configurations not owned by `frozen`;
- any other future cross-family collision fails catalog construction until a new
  dated ownership rule is frozen.

Expected catalog counts per pool are:

| Object | Count |
| --- | ---: |
| EWMA canonical sleeves | 2,640 |
| paper-exclusive canonical sleeves | 2,200 |
| static canonical sleeves | 10 |
| frozen-owned canonical sleeves | 40 |
| route-aware directional policies | 5 |
| allocator-facing economic units | 4,895 |
| retained declarations including policies | 4,935 |

Aliases never receive a second weight, family budget, runtime, cost, PBO row, or
attribution row.

### Training and selection capital

All training eligibility and rank-one selection use the pool's common frozen
reference bankroll, not the realized capital of any carried method. This keeps
the information set and candidate selection identical across allocation rules
and comparators. Training simulations settle to USD at the training boundary so
their cost contract matches validation.

Eligibility requires all four training metrics to be finite, positive net
return, at least one completed episode, fee-to-transaction-cost ratio at least
the frozen threshold, and a coherent non-positive drawdown. Missing or invalid
metrics are ineligible; they are never imputed.

Rank-one selection is training-only and deterministic:

1. net return, descending;
2. fee-to-transaction-cost ratio, descending;
3. maximum drawdown, descending, so the less negative value wins;
4. canonical economic ID, ascending.

For a directional policy, the current validation boundary's causal entry state
first selects one archetype. That selected component is then evaluated over the
training slice for eligibility and rank-one scoring. This is explicitly a
`boundary_selected_component` training rule, not a claim that the route was
historically replayed inside training. The route may use training-derived state
but no validation value. Its additional in-sample model-selection burden remains
visible in candidate PBO. A boundary `no_position` route is ineligible for
funding and is a valid zero-return validation observation for that policy.

### One ordered event and one action per sleeve

"Same timestamp" means one ordered swap observation. Logs or transactions that
share a wall-clock timestamp are not combined. Every runtime is observed against
the same pre-action pool and synthetic-liquidity state.

A sleeve may propose at most one action for that observation. Exit takes
precedence over entry. A sleeve that exits may re-enter no earlier than the next
ordered swap. This makes the action set immutable before joint execution and
avoids a within-event circular dependency between exit savings and replacement
position size.

### Mixed inventory netting

Every action is represented by a unique `(economic_id, phase)` inventory leg.
Entries are exact-output legs. Exits are exact-input legs. Opposing cNGN
requirements are internally crossed at the recorded sqrt-mid mark, pro rata on
each side, before any external execution cost is computed.

The remaining inventory is one external swap against historical active
liquidity plus all synthetic liquidity active before the action batch. Fixed gas
and failed-transaction costs remain action-specific and are never netted.

`B` is the absolute marked notional remaining after pro-rata internal matching;
`alpha` is the fraction of that residual belonging to exact-output legs. Variable
cost models are compatible only when `swap_slippage_bps`,
`latency_slippage_bps`, and `fallback_price_impact_bps` are exactly equal. Pool
fee, tick, sqrt price, and liquidity come from the common event state.

For a residual containing both exact-input and exact-output legs in the same
direction, variable execution cost is frozen as follows. Let `B` be residual
marked notional and `alpha` the share belonging to exact-output legs. Solve the
single-swap fixed point

```text
C = variable_cost_exact_input(B + alpha * C)
```

then allocate every variable-cost component pro rata by residual marked
notional. The resulting aggregate input is `B + alpha*C`; aggregate output is
`B - (1-alpha)*C`. Homogeneous exact-input and exact-output batches continue to
use their exact primitives directly. Non-convergence, non-finite values,
incompatible variable-cost models, negative balances, or failed token/cost
reconciliation invalidate the rule-window before any **action-batch** state is
mutated. Event observation and fee accrual precede proposal and remain part of
the processed historical observation; tests pin that atomicity boundary.

Report `external_marked_notional_usd`, `external_input_value_usd`, and
`external_output_value_usd` separately. `internal_cross_notional_usd` records
one side of the bilateral match and is not double counted. Per-action attribution
records signed internal cNGN value—positive for cNGN received, negative for cNGN
released—plus residual external marked notional and allocated variable cost.

Entry actions are initially proposed at their strategy sizing target. If joint
variable cost would make any entry wallet negative, all entry proposals in that
ordered event receive one common deployment scale while exit quantities remain
fixed. A deterministic bisection finds the largest feasible scale in `[0, 1]`;
every trial recomputes entry token requirements, netting, the external swap, and
cost allocation from the unchanged pre-action state. No removed capital is
redistributed—it remains in its sleeve wallet. Non-convergence invalidates the
action batch. The realized common entry scale is recorded for audit.

### Boundary liquidation and carried capital

Every validation method begins the first window with the frozen reference
bankroll. At the final valid swap of every window it jointly:

1. removes every open synthetic position;
2. collects accrued fees;
3. converts all remaining cNGN, including loose wallet inventory, to USD;
4. charges action-specific fixed costs and one net external variable cost;
5. verifies that no position or cNGN balance remains.

Boundary settlement overrides each strategy's `unwind_to_cash_on_exit` and
`close_position_on_end` flags. A sleeve with an open position pays one existing
remove-action fixed cost covering removal and its joined inventory route. A
sleeve with no position but positive loose cNGN pays one existing exit-action
fixed cost because the frozen model has no separate wallet-swap gas field. A
sleeve with neither emits a zero-cost terminal settlement record and no
transaction. These semantics are fixed before the run and reported separately
from ordinary rebalance costs.

Liquidation is mandatory even when the next window would select the same
weights. The settled USD value becomes that method's next opening capital. This
is an explicit amendment to the July 10 wording that charged reallocation only
when weights changed and to the later reset-only aggregate instructions.

Each allocation rule and each comparator has an independent carried path. The
first invalid outcome permanently blocks only that method. Trigger statuses are
`invalid_liquidity_cap`, `invalid_no_validation_swap`,
`invalid_execution_accounting`, `invalid_terminal_liquidation`, or
`invalid_opening_capital`; later rows use `blocked_prior_invalid` and cite the
triggering status/window. Opening capital for an evaluated window must be finite
and positive. A valid window may close at exactly zero USD; that zero is
checkpointed as its terminal economic result, and the next window triggers
`invalid_opening_capital`. A blocked path is never reset, rescaled, replaced by
cash, or resumed.

### Reset matrices and CSCV/PBO

Path-dependent carried returns are not CSCV observations. Separate reset
simulations use the common reference bankroll and the same mandatory terminal
liquidation solely for complete-matrix diagnostics:

- canonical standalone candidates feed candidate-sleeve CSCV/PBO;
- the three joint reset allocation rules feed allocation-rule CSCV/PBO;
- any cap-invalid allocation row makes allocation-rule PBO
  `invalid_incomplete_matrix`;
- Base and BSC are always computed and reported separately;
- family-mean standalone PBO is removed because the raw families are nested and
  not additive portfolios.

Every canonical candidate must have exactly one finite reset-validation row per
completed window. Directional `no_position` is the sole explicit zero-return
policy observation. Any other missing, duplicate, failed, or non-finite row makes
candidate PBO `invalid_incomplete_matrix`; it is never dropped or cash-filled.

Reset returns may be summarized only as reset diagnostics. They may not be
stitched or compounded into an economic equity curve.

### Allocation rules

The predeclared rules remain `equal_config`, `equal_family`, and `shrinkage`.
They operate on canonical economic IDs and the disjoint allocation families
`ewma`, `paper_exclusive`, `static`, `frozen`, and `directional`. The frozen 10%
sleeve cap, 35% family cap, shrinkage constants, no-redistribution rule, and
residual-cash rule are unchanged.

### Comparator contract

Every pool and completed window emits exactly these comparator methods:

| Comparator | Frozen behavior |
| --- | --- |
| `cash` | Full carried USD balance; zero return and zero cost. |
| `hold_cngn_mark` | Frictionless cNGN exposure from first to last validation sqrt-mid; carries its marked USD equivalent and is labelled an idealized mark-only benchmark. |
| `hold_cngn_pool_routed` | Buys at the first validation swap and sells at the last using the existing pool-route cost model; labelled a harsh DEX execution diagnostic. |
| `static_spot_w0025` | Existing predeclared static configuration, funded at the 10% sleeve cap with residual cash. |
| `ewma_rank_one` | Training-selected eligible EWMA canonical sleeve, funded at 10% with residual cash. |
| `paper_exclusive_rank_one` | Training-selected eligible paper-exclusive canonical sleeve, funded at 10% with residual cash. |
| `frozen_rank_one` | Training-selected eligible frozen canonical sleeve, funded at 10% with residual cash. |
| `directional_rank_one` | Training-selected eligible route-aware policy, funded at 10% with residual cash. |

An LP comparator with no eligible candidate becomes cash for that window with
`selection_status=no_eligible`; this is not a fabricated LP return. LP
comparators obey the same aggregate-liquidity cap and carried-path blocking
rules. Hold comparators are not synthetic LP positions and are exempt from that
cap.

### Joint attribution and sensitivity

For every valid carried joint result, output canonical sleeve, disjoint family,
and cash rows that reconcile exactly to portfolio opening capital, closing USD,
PnL, fees, fixed costs, variable costs, external notional, and internal notional.
`portfolio_return_contribution` means additive dollar PnL divided by opening
capital. Drawdown is not additive under joint netting and must not be described
as a family or sleeve contribution.

Best-sleeve removal remains a clearly labelled ex-post sensitivity. Select the
canonical sleeve by mean reset validation return across all completed windows,
with canonical ID as the final tie-break. Re-run each carried allocation after
removing that economic ID; its weight remains cash. The sensitivity cannot alter
selection, weights, PBO, or the primary result.

### Value paths and drawdown

Every path begins with an opening-capital sample immediately before the first
validation swap, records value after each ordered swap's fees and action batch,
and appends terminal settled USD after boundary liquidation. Maximum drawdown is
the minimum signed value of `value / running_peak - 1`, so it is finite and
non-positive. Training, reset, carried, and comparator paths use this same rule.
Reset-window paths are never stitched. Carried methods use their genuinely
continuous sequence of opening, event, and terminal samples across windows.

## Checkpoint and Publication Contract

Each pool has an independent checkpoint directory. Run identity binds:

- this protocol version and artifact schema version;
- input bytes and pool configuration;
- complete source closure used for simulation and orchestration;
- canonical catalog and aliases;
- window specification and reference bankroll;
- allocation and comparator constants.

One canonical JSON checkpoint is atomically published per completed contiguous
window. A checkpoint contains all candidate metrics, reset outcomes, carried
outcomes, attribution, and closing capitals needed to resume without recomputing
completed windows. Resume rejects missing prefixes, identity drift, malformed
values, or carried-capital mismatch.

After primary windows finish, a complete valid candidate reset matrix freezes
the selected ex-post removal economic ID and opens a separate
`best_sleeve_removal` checkpoint phase. Its per-window checkpoints bind that ID
and the completed primary-run identity. Interrupted sensitivity work resumes
only with the same frozen ID and never rewrites primary outcomes. If the
candidate matrix is incomplete or invalid, selection and removal do not run;
the complete primary evidence publishes fail-closed with
`not_run_incomplete_candidate_reset_matrix`.

`progress.json` is atomically updated after durable checkpoint publication and
contains exactly the pool, phase, total-window count, completed-window count,
and next-window index. It is deterministic and contains no timing metadata.
Live stderr reports training and candidate progress every 250 economic units,
the final unit in each phase, and every durable window. An interrupted run has
its identity, progress, contiguous `window-*.json` checkpoints, and the
implementation lock file; it has no published `run_manifest.json`.

Final artifacts are written to a staging directory, validated, hashed, and
atomically published only after every primary window is complete and, when the
candidate matrix is valid, every removal window is complete. The source closure
binds all executable research modules, the directly imported Base/BSC pool
configuration modules, the resolved pool configuration, and the Python
implementation/version.

Base and BSC may run concurrently because their identities, checkpoints, and
claims are independent.

## Required Artifacts

Exactly these 14 files are published per pool; no additional staged file is
allowed:

```text
run_manifest.json
configuration_catalog.csv
training_eligibility.csv
window_weights.csv
sleeve_validation_matrix.csv
reset_portfolio_validation_matrix.csv
carried_portfolio_path.csv
comparators.csv
joint_attribution.csv
concentration_and_contribution.csv
method_stability.csv
comparator_conclusions.csv
pbo_allocation_rules.json
summary.md
```

`joint_attribution.csv` explicitly replaces the former standalone-mean
`family_validation_matrix.csv`: it includes additive `family` rows derived from
the joint path and never revives family-mean PBO.

The shared economic suffix is:

```text
opening_capital_usd,closing_cash_usd,window_net_return,max_drawdown,
terminal_liquidation_cost_usd,total_fees_usd,total_fixed_cost_usd,
total_variable_cost_usd,external_marked_notional_usd,
external_input_value_usd,external_output_value_usd,
internal_cross_notional_usd,value_sample_count
```

The 11 analytic CSV contracts are frozen as follows; `ECONOMIC_FIELDS` means the
shared suffix above:

```text
configuration_catalog.csv:
schema_version,pool,economic_id,declaration_kind,allocation_family,
declared_family,config_name,source_constructor,behavioral_fingerprint

training_eligibility.csv:
schema_version,pool,window_index,window_start,window_end,economic_id,family,
routed_config_name,status,eligible,net_return,episode_count,
fee_to_transaction_cost_ratio,max_drawdown

window_weights.csv:
schema_version,pool,window_index,window_start,window_end,allocation_rule,
economic_id,family,weight,deployed_weight,cash_weight

sleeve_validation_matrix.csv:
schema_version,pool,window_index,window_start,window_end,economic_id,family,
routed_config_name,route_status,status,episode_count,ECONOMIC_FIELDS

reset_portfolio_validation_matrix.csv:
schema_version,pool,window_index,window_start,window_end,allocation_rule,status,
observed_share,cap,deployed_weight,cash_weight,ECONOMIC_FIELDS

carried_portfolio_path.csv:
schema_version,pool,window_index,window_start,window_end,method_id,method_kind,
status,blocking_status,blocking_window_index,ECONOMIC_FIELDS

comparators.csv:
schema_version,pool,window_index,window_start,window_end,comparator_id,status,
blocking_status,blocking_window_index,selection_status,selected_economic_id,
ECONOMIC_FIELDS

joint_attribution.csv:
schema_version,pool,window_index,window_start,window_end,allocation_rule,
row_type,economic_id,family,opening_value_usd,closing_value_usd,pnl_usd,
portfolio_return_contribution,total_fees_usd,total_fixed_cost_usd,
total_variable_cost_usd,external_marked_notional_usd,
external_input_value_usd,external_output_value_usd,internal_cross_notional_usd

concentration_and_contribution.csv:
schema_version,pool,window_index,window_start,window_end,allocation_rule,status,
deployed_weight,cash_weight,herfindahl,largest_weight,effective_sleeve_count,
best_sleeve_removed_economic_id,best_sleeve_removed_weight,removal_status,
removal_blocking_status,removal_blocking_window_index,
removal_opening_capital_usd,removal_closing_cash_usd,
removal_window_net_return,removal_max_drawdown

method_stability.csv:
schema_version,pool,method_id,method_kind,completed_windows,valid_windows,
invalid_windows,first_invalid_status,first_invalid_window_index,
cumulative_return,mean_window_return,median_window_return,
positive_window_rate,worst_window_return,continuous_max_drawdown,
total_fees_usd,total_fixed_cost_usd,total_variable_cost_usd

comparator_conclusions.csv:
schema_version,pool,allocation_rule,comparator_id,paired_valid_windows,
invalid_rule_windows,invalid_comparator_windows,mean_paired_excess_return,
median_paired_excess_return,worst_paired_excess_return,
positive_paired_excess_rate,candidate_pbo_status,allocation_pbo_status,
conclusion_status
```

For the frozen full run, let `W` be completed windows, `C=4,895` canonical
economic units, `D=4,935` retained declarations, `R=3` allocation rules, `K=8`
comparators, and `F=5` disjoint allocation families. Smoke runs use their
manifest-declared `C` and `D` with the same formulas. Full-run row cardinalities
are:

| Artifact | Rows |
| --- | ---: |
| `configuration_catalog.csv` | `D` |
| `training_eligibility.csv` | `W*C` |
| `window_weights.csv` | `W*R*C` |
| `sleeve_validation_matrix.csv` | `W*C` |
| `reset_portfolio_validation_matrix.csv` | `W*R` |
| `carried_portfolio_path.csv` | `W*R` |
| `comparators.csv` | `W*K` |
| `joint_attribution.csv` | `C+F+1=4,901` per valid rule-window; 14,703 per all-valid window |
| `concentration_and_contribution.csv` | `W*R` |
| `method_stability.csv` | `R+K=11` |
| `comparator_conclusions.csv` | `R*K=24` |

Candidate-matrix status is `complete_valid` or `invalid_incomplete_matrix`.
Path status is one of `valid`, `blocked_prior_invalid`,
`invalid_liquidity_cap`, `invalid_no_validation_swap`,
`invalid_execution_accounting`, `invalid_terminal_liquidation`, or
`invalid_opening_capital`. Comparator selection status is `not_applicable`,
`predeclared`, `selected`, or `no_eligible`.

Training status is `valid`, `no_position`, `invalid_no_validation_swap`,
`invalid_execution_accounting`, or `invalid_terminal_liquidation`. Candidate
reset status is the strict subset `valid`, `invalid_no_validation_swap`,
`invalid_execution_accounting`, or `invalid_terminal_liquidation`. Published
manifest status is `completed` or
`completed_with_invalid_candidate_reset_matrix`. Candidate/allocation PBO status
is `computed`, `insufficient_complete_matrix`, or
`invalid_incomplete_matrix`; carried-path PBO is always
`not_applicable_path_dependent_carried_bankroll`.

`cumulative_return = final_closing_cash / initial_reference_bankroll - 1` only
for a never-blocked carried method. Stability moments use its valid sequential
window returns. Comparator conclusions pair the same pool/window only when both
carried rows are valid; otherwise invalid counts remain explicit.
`conclusion_status` is one of `diagnostic_only`,
`insufficient_complete_matrix`, or `not_evaluable_invalid_path`.

`comparators.csv` contains exactly eight unique comparator IDs per completed
window. A valid `no_eligible` LP selection records cash economics and a blank
`selected_economic_id`; invalid and blocked rows keep every economic field blank.
Selected IDs remain private evidence and never enter public prose.

Invalid rows retain identity and failure evidence while every economic field is
blank. Published `run_kind` is `smoke`, `completed_amended_protocol_run`, or
`completed_primary_invalid_candidate_matrix`. The last kind publishes primary
evidence only, with a null removed ID and no candidate selection,
candidate-sleeve PBO, removal, or conclusion dependent on the incomplete
candidate matrix. Its independently complete allocation-rule reset matrix may
still produce allocation-rule PBO.

## Publication Boundary

No current or corrected result authorizes live LP promotion. Public writing may
report aggregate, pool-separated hypotheses, gates, invalidity, and outcomes,
but must not expose selected IDs, weights, operational thresholds, coefficients,
or execution tactics. Any result remains diagnostic without the previously
required non-pool inventory comparator.
