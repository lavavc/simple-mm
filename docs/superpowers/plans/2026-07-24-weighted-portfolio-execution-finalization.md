# Weighted Portfolio Execution Finalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two invalid joint-portfolio execution seams with a fixed-cost-safe entry planner and globally funded terminal settlement, then publish versioned diagnostics that make every failure and execution decision reproducible.

**Architecture:** Ordinary entry/exit action settlement remains unchanged. Entry eligibility is frozen once into typed intents and the largest feasible point is selected from a declared 1/256 scale lattice without assuming monotone netting. Terminal settlement uses terminal-only removal intents, pools every sleeve into one physical portfolio wallet, charges one removal cost per open position plus one exact-input inventory swap, and deterministically mutualizes individually underfunded sleeves only when the aggregate portfolio remains solvent.

**Tech Stack:** Python 3.12, dataclasses and typed literals, pytest, strict mypy, canonical JSON checkpoints, deterministic CSV publication.

**Binding protocol amendment:**
[`../specs/2026-07-24-weighted-portfolio-execution-finalization.md`](../specs/2026-07-24-weighted-portfolio-execution-finalization.md)

## Global Constraints

- Preserve the frozen 10% aggregate synthetic-liquidity cap; the 15% cap remains a separately labelled sensitivity.
- Preserve the 10% sleeve cap, 35% family cap, causal windowing, residual cash, and Base/BSC separation.
- Preserve exact token conservation, action-kind exactness, joint inventory netting, and the exogenous historical pool path.
- Base uses cNGN token0; BSC uses cNGN token1. Terminal code must use `cngn_to_stable` and existing orientation helpers, never token-index branches.
- Only open positions incur per-position removal fixed costs. Loose cNGN incurs no per-sleeve fixed cost.
- Terminal settlement performs one additional portfolio-level exact-input cNGN sale with one fixed cost and one aggregate variable cost, after synthetic liquidity has been removed.
- Fail terminal settlement only for aggregate insolvency or malformed execution state; never convert an invalid path to cash.
- No v2 checkpoint compatibility shim. The corrected run uses protocol `2026-07-24` and artifact schema `weighted-portfolio-artifacts/v3`.
- Do not modify `.gitignore`, raw pool histories, feature histories, or the current v2 evidence packages.
- Terra subagents remain read-only. Sol Ultra performs all edits, verification, and commit authorization.

---

### Task 1: Freeze entry intents and a non-monotone scale contract

**Files:**
- Modify: `research/backtester/simulator.py`
- Modify: `research/backtester/position_runtime.py`
- Modify: `research/backtester/portfolio_simulator.py`
- Test: `research/tests/test_portfolio_simulator.py`
- Test: `research/tests/test_position_runtime.py`

**Interfaces:**
- Produces: `EntryIntent`, `SleeveRuntime.propose_entry_intent(...)`, and `SleeveRuntime.materialize_entry(intent, scale)`.
- Produces: `ENTRY_SCALE_STEPS = 256` and `EntryActionPlan(actions, scale, intended_entry_count)`.
- Preserves: `SleeveRuntime.propose_entry(...)` as the single-sleeve full-scale wrapper.

- [x] **Step 1: Add failing fixed-cost intent tests**

```python
def test_scaled_fixed_cost_intent_keeps_the_frozen_entry() -> None:
    intent = runtime.propose_entry_intent(event, active_liquidity)
    assert intent is not None

    action = runtime.materialize_entry(intent, scale=1 / 256)

    assert action.sleeve_id == intent.sleeve_id
    assert action.transaction_cost.gas_cost == pytest.approx(0.05)
    assert action.position_after is not None
    assert action.wallet_after.value_usd(runtime.current_price) >= 0.0
```

Add companion tests proving the overlay, sizing policy, range selection, and expected-fee admission execute once when the intent is frozen and are not re-evaluated during scale trials.

- [x] **Step 2: Run the tests and verify the old implementation fails for the reproduced reason**

Run:

```bash
.venv/bin/pytest -q \
  research/tests/test_position_runtime.py::test_scaled_fixed_cost_intent_keeps_the_frozen_entry \
  research/tests/test_portfolio_simulator.py::test_entry_scale_trials_freeze_admission_once
```

Expected: failure because `EntryIntent` and intent materialization do not exist.

- [x] **Step 3: Split fixed-cost payment from variable-cost routing**

Refactor `_route_wallet_to_position(...)` through a funded-wallet helper with this contract:

```python
def _route_funded_wallet_to_position(
    funded_wallet: PortfolioComposition,
    fixed_entry_cost: TransactionCostBreakdown,
    tick_lower: int,
    tick_upper: int,
    current_tick: int,
    current_sqrt_price_x96: int,
    cngn_usd_price: float,
    active_liquidity: int,
    fee_rate: float,
    pool_config: PoolConfig,
    params: BacktestParams,
) -> PositionEntryRoute:
    """Route post-fixed-cost capital while retaining the full cost breakdown."""
```

The helper reconciles `deployed + remaining == funded value - variable cost`; the wrapper continues to reconcile against the original wallet after fixed and variable costs.

- [x] **Step 4: Implement frozen entry intents**

```python
@dataclass(frozen=True)
class EntryIntent:
    sleeve_id: str
    full_action: SleeveAction
    funded_deployment_wallet: PortfolioComposition
    fixed_payment_wallet: PortfolioComposition


def materialize_entry(self, intent: EntryIntent, scale: float) -> SleeveAction:
    if scale == 1.0:
        return intent.full_action
    funded = _scale_wallet(intent.funded_deployment_wallet, scale)
    deploy = _add_wallets(intent.fixed_payment_wallet, funded)
    idle = _subtract_wallet(intent.full_action.wallet_before, deploy)
    # Route `funded`, reuse the frozen range, and add `idle` back to the sleeve.
```

Reject non-finite scales and scales outside `(0, 1]`. A positive-scale intent that cannot materialize raises `ExecutionAccountingError`; it is never silently omitted.

- [x] **Step 5: Add failing non-monotone planner tests**

```python
def test_scale_lattice_can_select_positive_scale_when_exits_only_is_infeasible() -> None:
    plan = _plan_affordable_actions(runtimes, event, active_liquidity, pool)
    assert plan.scale > 0.0
    assert plan.intended_entry_count > 0


def test_scale_lattice_selects_largest_feasible_declared_point() -> None:
    assert plan.scale * ENTRY_SCALE_STEPS == int(plan.scale * ENTRY_SCALE_STEPS)
    assert _trial_is_feasible(plan.scale)
    if plan.scale < 1.0:
        assert not _trial_is_feasible(plan.scale + 1 / ENTRY_SCALE_STEPS)
```

Also test mapping-order invariance and complete non-mutation when every declared scale is infeasible.

- [x] **Step 6: Replace monotone bisection with the finite scale lattice**

```python
ENTRY_SCALE_STEPS = 256

for numerator in range(ENTRY_SCALE_STEPS, 0, -1):
    scale = numerator / ENTRY_SCALE_STEPS
    candidate = materialize_frozen_intents(scale)
    try:
        _prepare_action_settlements(candidate, runtimes, active_liquidity, pool_config)
    except JointActionAffordabilityError:
        continue
    return EntryActionPlan(candidate, scale, len(intents))

exits_only = tuple(sorted(exits.values(), key=lambda action: action.sleeve_id))
_prepare_action_settlements(exits_only, runtimes, active_liquidity, pool_config)
return EntryActionPlan(exits_only, 0.0, len(intents))
```

Only `JointActionAffordabilityError` denotes an infeasible lattice point. Non-finite costs, incompatible models, reconciliation failures, and reconstruction failures propagate.

- [x] **Step 7: Run entry tests and the existing simulator suite**

Run:

```bash
.venv/bin/pytest -q \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_simulator.py
```

Expected: all tests pass, including the historical fixed-cost and full-wallet conservation regressions.

---

### Task 2: Implement globally funded terminal portfolio settlement

**Files:**
- Modify: `research/backtester/position_runtime.py`
- Modify: `research/backtester/portfolio_simulator.py`
- Modify: `research/backtester/portfolio_path.py`
- Test: `research/tests/test_position_runtime.py`
- Test: `research/tests/test_portfolio_simulator.py`
- Test: `research/tests/test_portfolio_path.py`

**Interfaces:**
- Produces: `TerminalRemovalIntent`, `TerminalSleeveSettlement`, and `TerminalPortfolioSettlement`.
- Produces: `SleeveRuntime.propose_terminal_removal(...)` and `SleeveRuntime.apply_terminal_settlement(...)`.
- Preserves: standalone `SleeveRuntime.propose_terminal_liquidation(...)` and `simulate_pool()` economics.

- [x] **Step 1: Add failing global-terminal tests**

Cover these independent behaviors:

```python
def test_terminal_portfolio_pools_loose_cngn_into_one_swap() -> None:
    result = simulate_portfolio(..., settle_to_cash=True)
    assert result.terminal_loose_cngn_settlement_count == 2
    assert result.terminal_position_settlement_count == 0
    assert result.terminal_inventory_swap_count == 1
    assert result.terminal_open_position_count == 0
    assert result.terminal_cngn_amount == 0.0


def test_individually_underfunded_sleeve_settles_when_portfolio_is_solvent() -> None:
    result = simulate_portfolio(..., settle_to_cash=True)
    assert result.settled_to_cash
    assert result.final_value >= 0.0


def test_aggregate_terminal_insolvency_fails_before_mutation() -> None:
    with pytest.raises(TerminalLiquidationError, match="aggregate portfolio"):
        _prepare_terminal_portfolio_settlement(...)
    assert snapshots_after == snapshots_before
```

Parameterize the pooled-swap test for `UNISWAP_BASE_POOL` and `UNISWAP_BSC_POOL`.

- [x] **Step 2: Verify the new tests fail against per-sleeve liquidation**

Run:

```bash
.venv/bin/pytest -q research/tests/test_portfolio_simulator.py -k 'terminal_portfolio or individually_underfunded or aggregate_terminal'
```

Expected: failure with the old per-sleeve fixed-cost liquidation behavior.

- [x] **Step 3: Add pure per-sleeve removal intents**

```python
@dataclass(frozen=True)
class TerminalRemovalIntent:
    sleeve_id: str
    event: Event
    wallet_before: PortfolioComposition
    gross_wallet_after: PortfolioComposition
    position_before: VirtualPosition | None
    removal_fixed_cost: TransactionCostBreakdown
```

`propose_terminal_removal(...)` collects position principal and fees without paying or swapping. It charges a fixed removal cost only when `position_before is not None`; loose wallet cNGN has zero per-sleeve fixed cost.

- [x] **Step 4: Add a pure aggregate terminal planner**

```python
@dataclass(frozen=True)
class TerminalPortfolioSettlement:
    sleeves: tuple[TerminalSleeveSettlement, ...]
    final_cash_usd: float
    total_cost: TransactionCostBreakdown
    external_input_value_usd: float
    external_output_value_usd: float
    funding_transfer_by_id: Mapping[str, float]
```

The planner:

1. freezes all removal intents;
2. sums gross stable and cNGN balances;
3. prices one exact-input `cngn_to_stable` swap against historical liquidity after synthetic removals;
4. charges open-position fixed removals plus the swap fixed and variable costs once;
5. rejects non-finite values, incompatible variable-cost models, variable cost greater than swap input, or negative aggregate final cash;
6. allocates the portfolio swap by marked cNGN contribution;
7. mutualizes negative raw sleeve balances over every positive sleeve and the cash account using one deterministic pro-rata haircut;
8. assigns any floating residual to the final lexicographic positive account.

The funding-transfer vector must sum to zero within `1e-10 * max(1, bankroll)`.

- [x] **Step 5: Apply the prepared terminal settlement atomically**

Validate every runtime snapshot and every non-negative final wallet before applying any settlement. Each runtime ends with no position, exactly zero cNGN, and its non-negative funded stable balance. Record position episodes exactly once. Append the terminal portfolio sample only after all applications succeed.

- [x] **Step 6: Reconcile cash and sleeve attribution**

Change `economic_result_from_portfolio(...)` so the cash row opens at:

```python
opening_cash = result.bankroll_usd - sum(
    item.capital_budget_usd for item in result.attribution.values()
)
```

and closes at `result.cash_value`. Add `terminal_funding_transfer_usd` to sleeve and cash attribution; its portfolio sum is exactly zero.

- [x] **Step 7: Run terminal, path, and orientation tests**

Run:

```bash
.venv/bin/pytest -q \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_simulator.py \
  research/tests/test_portfolio_path.py
```

Expected: all pass; standalone liquidation tests remain byte-for-byte behaviorally unchanged.

---

### Task 3: Persist execution traces and exact failure diagnostics

**Files:**
- Modify: `research/backtester/portfolio_path.py`
- Modify: `research/backtester/portfolio_evaluation.py`
- Modify: `research/backtester/portfolio_publication.py`
- Modify: `research/scripts/evaluate_parameter_portfolio.py`
- Test: `research/tests/test_portfolio_evaluation.py`
- Test: `research/tests/test_parameter_portfolio_checkpoint.py`
- Test: `research/tests/test_evaluate_parameter_portfolio.py`

**Interfaces:**
- Produces: `FailureDiagnostic(exception_type, reason)`.
- Produces: checkpointed `EntryScaleEvent` records and scalar execution diagnostics.
- Produces: artifact schema v3 with deterministic diagnostic fields.

- [x] **Step 1: Add failing diagnostic lifecycle tests**

```python
def test_invalid_reset_retains_exception_type_and_reason() -> None:
    outcome = _reset_outcome("equal_config", failing_evaluator)
    assert outcome.failure == FailureDiagnostic(
        "ExecutionAccountingError",
        "joint entry scale lattice has no feasible point",
    )


def test_blocked_path_repeats_originating_failure() -> None:
    first = state.evaluate_window(1, failing_evaluator)
    second = state.evaluate_window(2, valid_evaluator)
    assert second.failure == first.failure
    assert second.blocking_window_index == 1
```

Add checkpoint round-trip tests for failure diagnostics and ordered entry-scale events.

- [x] **Step 2: Implement bounded typed failures**

```python
@dataclass(frozen=True)
class FailureDiagnostic:
    exception_type: str
    reason: str

    def __post_init__(self) -> None:
        if not self.exception_type or not self.reason or len(self.reason) > 512:
            raise ValueError("failure diagnostic must be non-empty and bounded")
```

Attach it to training, candidate, reset, carried, blocked, and path-snapshot contracts. Valid and `no_position` records require `failure is None`; invalid records require it.

- [x] **Step 3: Persist exact entry execution diagnostics**

```python
@dataclass(frozen=True)
class EntryScaleEvent:
    block_time: datetime
    block_number: int | None
    tx_hash: str | None
    log_index: int | None
    scale: float
    intended_entry_count: int
    executed_entry_count: int
```

Store ordered events in `PortfolioResult` and `EconomicResult`. Derive and validate entry-batch count, scaled-batch count, and minimum scale from the trace rather than storing competing mutable totals.

- [x] **Step 4: Append v3 CSV fields without changing artifact cardinalities**

Add `failure_type` and `failure_reason` to every status-bearing matrix. Append these shared economic diagnostics:

```text
entry_action_batch_count
scaled_entry_action_batch_count
minimum_entry_execution_scale
terminal_position_settlement_count
terminal_loose_cngn_settlement_count
terminal_zero_settlement_count
terminal_inventory_swap_count
terminal_fixed_cost_usd
terminal_variable_cost_usd
terminal_external_marked_notional_usd
```

Add `terminal_funding_transfer_usd` to `joint_attribution.csv`. Preserve the existing fourteen-file package and all row-count formulas.

- [x] **Step 5: Bump protocol and artifact identities**

Set:

```python
PROTOCOL_VERSION = "2026-07-24"
ARTIFACT_SCHEMA_VERSION = "weighted-portfolio-artifacts/v3"
```

Keep source closure identical between runner and validator. Old v2 checkpoints must fail identity validation rather than receive defaults.

- [x] **Step 6: Run serialization and publication tests**

Run:

```bash
.venv/bin/pytest -q \
  research/tests/test_portfolio_evaluation.py \
  research/tests/test_parameter_portfolio_checkpoint.py \
  research/tests/test_evaluate_parameter_portfolio.py
```

Expected: all pass, deterministic payload bytes remain stable across resume.

---

### Task 4: Strengthen the publication validator and research contract

**Files:**
- Modify: `research/scripts/validate_parameter_portfolio_publication.py`
- Modify: `research/tests/test_validate_parameter_portfolio_publication.py`
- Create: `docs/superpowers/specs/2026-07-24-weighted-portfolio-execution-finalization.md`
- Modify: `docs/superpowers/specs/2026-07-23-weighted-portfolio-correction.md`
- Modify: `docs/superpowers/plans/2026-07-23-weighted-portfolio-correction-plan.md`
- Modify: `research/results/README.md`

**Interfaces:**
- Consumes: v3 diagnostics and funding-transfer attribution.
- Produces: fail-closed validation of trace/scalar, terminal, status/failure, and attribution identities.

- [x] **Step 1: Add failing validator tests**

Reject packages when:

- an invalid row lacks `failure_type` or `failure_reason`;
- a valid row contains a failure;
- a blocked row does not repeat its originating failure;
- entry scalar diagnostics disagree with checkpoint trace summaries;
- terminal fixed plus variable cost does not equal liquidation cost;
- terminal counts overlap or fail to cover funded sleeves;
- terminal funding transfers do not sum to zero;
- cash opening/closing values do not reconcile to the funding transfer;
- Base or BSC terminal swap accounting violates exact-input orientation-independent identities.

- [x] **Step 2: Run validator tests and confirm v2 assumptions fail**

Run:

```bash
.venv/bin/pytest -q research/tests/test_validate_parameter_portfolio_publication.py
```

Expected: the new tests fail before validator support is implemented.

- [x] **Step 3: Implement v3 validation without weakening claim gates**

Require diagnostics and their blankness/status relationships, validate all scalar identities, permit cash closing value to differ from opening only by terminal funding transfer, and preserve the existing complete-matrix, PBO, removal, carried-path, and comparator hard gates.

- [x] **Step 4: Amend the research specification**

Create the July 24 binding amendment and retain the July 23 document as linked
v2 audit provenance. Replace continuous monotone bisection with the declared
1/256 scale lattice. Replace per-sleeve loose-inventory liquidation with
per-position removals followed by one portfolio exact-input sale. Specify
post-removal liquidity, one swap fixed cost, global solvency, funding-transfer
attribution, failure diagnostics, v2 checkpoint rejection, and v3 provenance.

- [x] **Step 5: Run validator and documentation-contract tests**

Run:

```bash
.venv/bin/pytest -q \
  research/tests/test_validate_parameter_portfolio_publication.py \
  research/tests/test_cross_pool_article_contract.py
```

Expected: validator tests pass; article contract retains the current nonresult wording until fresh v3 evidence exists.

---

### Task 5: Verify reproduced failures and prepare the corrected frozen rerun

**Files:**
- Modify only if required by test evidence: files listed in Tasks 1–4.
- Preserve: `research/results/parameter_portfolio/**`
- Create during the later run, not implementation verification: a new v3 checkpoint/output root.

**Interfaces:**
- Consumes: corrected simulator and v3 publication contract.
- Produces: verified code ready for a fresh 10% baseline run and later 15% sensitivity.

- [ ] **Step 1: Run the focused reproduced windows in a temporary output root**

Exercise Base windows 1 and 4 and BSC windows 0 and 4. Assert that:

- no `preapproved entry disappeared during common scaling` diagnostic occurs;
- individually underfunded loose-cNGN sleeves settle when the aggregate portfolio is solvent;
- genuine 10% liquidity-cap breaches still return `invalid_liquidity_cap`;
- every valid result has zero terminal cNGN and no open positions.

- [x] **Step 2: Run the pre-freeze functional suite**

```bash
.venv/bin/pytest -q \
  research/tests/test_backtester.py \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_simulator.py \
  research/tests/test_portfolio_path.py \
  research/tests/test_portfolio_evaluation.py \
  research/tests/test_portfolio_comparators.py \
  research/tests/test_parameter_portfolio_checkpoint.py \
  research/tests/test_evaluate_parameter_portfolio.py \
  research/tests/test_validate_parameter_portfolio_publication.py \
  -k 'not source_closure_is_bound_to_the_frozen_commit and not run_identity_rebuilds_inputs_catalog_config_and_constants'
```

Expected: all selected functional tests pass. The two excluded tests are the
deliberately stale source-pin attestations and cannot pass before commit `X`.

- [x] **Step 3: Run static verification**

```bash
.venv/bin/mypy --strict \
  research/backtester/portfolio_simulator.py \
  research/backtester/position_runtime.py \
  research/backtester/portfolio_path.py \
  research/backtester/portfolio_evaluation.py \
  research/backtester/portfolio_publication.py \
  research/scripts/evaluate_parameter_portfolio.py \
  research/scripts/validate_parameter_portfolio_publication.py
git diff --name-only --diff-filter=ACM HEAD | rg '\.py$' | \
  xargs .venv/bin/ruff check
.venv/bin/python -m compileall -q research/backtester research/scripts
```

Expected: all commands exit zero. Repository-wide Ruff is not this change's
gate because the existing research tree has legacy violations outside the
changed-file scope.

- [x] **Step 4: Perform the Sol Ultra pre-commit review**

Review the complete diff and evidence for exact accounting, Base/BSC orientation, atomic failure, causal evaluation, comments, documentation, schema/version consistency, deterministic serialization, and preservation of the 10% cap. Resolve every material finding before committing.

- [x] **Step 5: Freeze the source closure in two commits**

Commit every changed source-closure file as immutable commit `X` while retaining
the old validator pin. In a follow-on commit outside the closure, update the
validator pin and documentation to the exact SHA `X`. Run final attestation only
from a worktree whose closure bytes equal `X`; do not claim the publication gate
is green between the two commits.

Frozen closure `X`: `07d5dfe1a2ae7a03eac068fe341655421ff6b984`.

- [ ] **Step 6: Run the complete post-freeze suite and attestation**

Rerun the complete focused suite from Step 2 without the `-k` exclusion, then
run the validator against a temporary v3 package. Both source-closure tests and
every functional test must pass from the follow-on worktree before any frozen
execution begins.

- [ ] **Step 7: Preserve and rerun evidence correctly**

Do not resume or overwrite v2 checkpoints. Reuse only the existing raw history, feature, and QTS bytes after hashing them into the new identity. Start a fresh v3 10% baseline checkpoint root, validate the complete packages, then execute the separately labelled 15% aggregate-cap sensitivity. Article claims remain blocked until both packages pass the normal claim gate.
