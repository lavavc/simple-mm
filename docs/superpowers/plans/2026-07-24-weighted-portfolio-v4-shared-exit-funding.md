# Weighted Portfolio v4 Shared Exit Funding Implementation Plan

> **Execution rule:** The primary Sol Ultra agent implements this plan with
> test-driven development. Terra Extra High agents may perform read-only design,
> diff, and evidence reviews; they may not mutate the repository.

**Goal:** Produce a new frozen Base/BSC weighted-portfolio experiment whose
mandatory ordinary exits can consume residual portfolio cash without funding
entries, whose internal-cross turnover is gross and auditable, and whose v4
checkpoints and fourteen-file publication packages fail closed under the
approved July 24 protocol.

**Architecture:** Ordinary actions remain sleeve-owned until the joint simulator
prices and nets the complete event batch. A pure preparation boundary allocates
the final joint costs, reconciles every action, stages the exact exit-only cash
transfer vector, and validates all runtimes before any state is committed. The
path/evaluation layer carries typed failure evidence and independently preserves
gross internal receipts and signed conservation. Checkpoints retain detailed
traces; published artifacts expose only bounded aggregate economics. A two-commit
freeze binds the immutable implementation closure before a fresh concurrent
Base/BSC run.

**Tech stack:** Python 3.12, frozen dataclasses, canonical JSON/CSV, pytest,
strict mypy, Ruff, the existing DEX-only portfolio simulator and publication
validator.

## Binding Constraints

- Protocol is exactly `2026-07-24-r2`; artifact schema is exactly
  `weighted-portfolio-artifacts/v4`.
- The primary experiment retains the 10% aggregate synthetic-liquidity-share
  cap. The 15% result remains a separately labelled sensitivity.
- Entry allocation remains sleeve-local and restricted to the existing 1/256
  common-scale lattice. Residual cash may fund only mandatory ordinary exits.
- A funded exit pays the complete final allocated fixed and variable cost. Its
  canonical wallet remains non-negative and the residual cash account loses the
  exact reconciliation gap.
- An exits-only cash shortfall is a typed invalid execution. It cannot suppress
  an exit, relax a cap, or turn the result into cash.
- Batch preparation, proposal-state staging, runtime validation, and cash
  validation precede all mutation. Mapping order cannot affect results.
- Gross internal-cross notional is accumulated from positive action-time
  receipts. Signed internal flow is retained separately and sums to zero.
- Detailed reconciliation traces are checkpoint evidence only. Published
  failures remain the bounded pair `exception_type` and `reason`.
- The artifact package remains exactly fourteen files.
- Existing v2 and stopped v3 outputs, checkpoints, logs, and archives are
  immutable diagnostic evidence. V4 uses new roots and refuses v3 resumes.
- Raw histories, derived features, QTS inputs, and frozen catalogs may be reused
  only through the v4 run identity. No pool-history or LP-ledger export is rerun.
- Do not change `.gitignore` or the article evidence pack during this plan.

## File Map

- Modify `research/backtester/portfolio_errors.py`: typed reconciliation trace
  and typed action/cash errors.
- Modify `research/backtester/position_runtime.py`: pure pre-apply validation and
  stageable defensive-exit proposal state.
- Modify `research/backtester/portfolio_simulator.py`: ordinary funding events,
  pure batch preparation, atomic commit, cash-aware lattice, and gross action
  attribution.
- Modify `research/backtester/portfolio_path.py`: economic attribution, funding
  events, gross/signed reconciliation, and typed failure propagation.
- Modify `research/backtester/portfolio_evaluation.py`: summary fields,
  checkpoint codecs, comparisons, and blocked-path trace preservation.
- Modify `research/backtester/portfolio_publication.py`: schema-v4 package fields
  and bounded public failure projection.
- Modify `research/scripts/evaluate_parameter_portfolio.py`: r2 protocol, v4
  roots, and unchanged frozen source-closure membership.
- Modify `research/scripts/validate_parameter_portfolio_publication.py`: r2/v4
  structural and accounting validation, then the pin-only source commit.
- Modify focused tests under `research/tests/` and the result operations contract
  in `research/results/README.md`.

---

### Task 1: Implement Atomic Ordinary-Exit Funding

**Files:**

- Modify: `research/backtester/portfolio_errors.py`
- Modify: `research/backtester/position_runtime.py`
- Modify: `research/backtester/portfolio_simulator.py`
- Modify: `research/tests/test_position_runtime.py`
- Modify: `research/tests/test_portfolio_simulator.py`

- [ ] **Step 1: Add focused failing tests for the accounting boundary**

Add real-runtime regressions that construct tiny Base and BSC positions and pin
all of these outcomes:

1. a mandatory exit whose complete cost exceeds its sleeve value succeeds when
   residual cash covers the exact difference;
2. the same rule holds when joint netting increases the exit's variable cost
   after the standalone action was built;
3. insufficient cash raises the typed cash-funding error and leaves wallet,
   position, result counters, pending defensive confirmation, attribution, and
   residual cash unchanged;
4. exact cash closes to zero, while zero cash succeeds only when the required
   transfer is exactly zero;
5. multiple funded exits are ordered by event identity and sleeve economic ID,
   produce one equal-and-opposite cash debit, and are invariant to input mapping
   order;
6. a non-exit reconciliation mismatch remains an execution-accounting error.

Add a defensive-confirmation success-path regression: a qualifying no-action
batch advances the staged confirmation state, the next qualifying observation
triggers at the frozen threshold, and a rejected batch does not advance that
state.

Add a `SleeveRuntime.validate_action()` regression proving that an invalid
second action is detected without applying a previously valid first action.

Run and observe the expected red state:

```bash
.venv/bin/pytest -q \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_simulator.py \
  -k 'ordinary_exit or action_batch_is_atomic or validate_action'
```

- [ ] **Step 2: Add typed reconciliation evidence**

In `portfolio_errors.py`, define a frozen `ActionReconciliationTrace` with the
bounded event identity, sleeve/action/reason, before/expected/actual marked
values, complete allocated cost, tolerance, funding gap, available residual
cash, and aggregate required funding. Add
`ActionReconciliationError(ExecutionAccountingError)` and
`InsufficientOrdinaryExitFundingError(ActionReconciliationError)`; both expose
the immutable trace without accepting an untyped mapping.

Trace construction occurs only at the joint accounting boundary after final
cost allocation. The trace names the first funded exit in deterministic order
when an aggregate cash shortage affects several exits, while its aggregate
required/available fields describe the complete batch.

The codec limits are exact: `sleeve_economic_id` is non-empty and at most 256
Unicode code points; `action_kind` is one of `enter`, `exit`, or `liquidate`;
`action_reason` is non-empty and at most 128 Unicode code points; a transaction
hash is exactly `0x` followed by 64 lowercase hexadecimal characters; block and
log indexes are non-negative signed-64-bit integers; and any public diagnostic
reason remains at most 1,024 Unicode code points. Checkpoint decoding rejects
values outside these bounds rather than truncating them.

- [ ] **Step 3: Make action application pre-validatable and proposal state stageable**

Extract every validation performed by `SleeveRuntime.apply_action()` into the
side-effect-free `validate_action(action, settlement)` method; `apply_action()`
calls it before changing result counters, cooldowns, wallet, or position.

Represent the three defensive-confirmation fields with a frozen proposal-state
value and add narrow capture/restore/apply helpers. During the frozen proposal
pass, capture each resulting state and restore the runtime immediately in a
`finally` block. Carry the staged states in `EntryActionPlan`. On successful
settlement, validate every action first, apply the staged proposal states in
sleeve order, then apply every action. Exit application retains its existing
confirmation reset. A failed preparation or validation therefore changes no
runtime field.

- [ ] **Step 4: Prepare the complete funding batch without mutation**

Add frozen internal `PreparedActionSettlement` and `PreparedActionBatch` values
and public `OrdinaryExitFundingEvent`. Thread mandatory keyword-only
`cash_value` through `_prepare_action_settlements()`,
`_plan_affordable_actions()`, and `_settle_actions()`; no compatibility default
is permitted.

Preparation must:

- net the frozen batch and allocate the final joint cost first;
- keep entry affordability strict in `_adjust_variable_cost()`;
- allow only an `exit` to clamp a cost-exhausted prepared wallet to its
  canonical non-negative value;
- compute the exact post-allocation identity
  `actual = before - cost + ordinary funding`;
- classify a funding event only under the four conditions in the approved
  design and reject every other mismatch with a typed trace;
- use deterministic order and `math.fsum` for positive transfers;
- debit cash by the exact negative aggregate, never by tolerance;
- reject non-finite values, negative closing cash, non-zero-sum transfers, or a
  cash-close mismatch before mutation.

Extend `ActionBatchSettlement`, `SleeveAttribution`, and `PortfolioResult` with
separate ordinary cash fields/events. Never reuse terminal-funding fields.

- [ ] **Step 5: Preserve the exhaustive entry lattice**

Pass the same residual cash into all 256 descending candidates. Continue only
for entry `JointActionAffordabilityError` or
`InsufficientOrdinaryExitFundingError`; all other accounting errors propagate.
Because joint netting is non-monotone, do not short-circuit smaller lattice
points after a cash failure. The exits-only candidate is mandatory and its cash
failure propagates.

Add a mixed entry/exit test and a controlled preparation-seam test proving:

- no entry receives ordinary funding;
- the chosen positive scale is an exact multiple of 1/256;
- a cash-infeasible larger candidate does not prevent a feasible smaller one;
- no positive candidate allows an unaffordable entry.

- [ ] **Step 6: Verify and review Task 1**

```bash
.venv/bin/pytest -q \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_simulator.py

.venv/bin/mypy --strict \
  research/backtester/portfolio_errors.py \
  research/backtester/position_runtime.py \
  research/backtester/portfolio_simulator.py
```

Have a Terra Extra High subagent review the diff read-only for exit-only
eligibility, exact cash conservation, lattice exhaustiveness, Base/BSC token
orientation, proposal-state atomicity, and full-cost charging. The primary Sol
Ultra agent adjudicates every finding, reruns the focused tests, and commits:

```bash
git add research/backtester/portfolio_errors.py \
  research/backtester/position_runtime.py \
  research/backtester/portfolio_simulator.py \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_simulator.py
git commit -m "research: fund mandatory portfolio exits atomically"
```

---

### Task 2: Preserve Gross Attribution and Typed Checkpoint Evidence

**Files:**

- Modify: `research/backtester/portfolio_path.py`
- Modify: `research/backtester/portfolio_evaluation.py`
- Modify: `research/tests/test_portfolio_path.py`
- Modify: `research/tests/test_portfolio_evaluation.py`
- Modify: `research/tests/test_portfolio_comparators.py`
- Modify: `research/tests/test_parameter_portfolio_checkpoint.py`

- [ ] **Step 1: Add failing gross-flow and cash-attribution tests**

Add the binding receive-then-send regression: a sleeve receives internal cNGN
in one batch and sends it in a later batch. Assert that its gross receipt stays
positive, the portfolio's gross internal turnover equals the sum of positive
action receipts, and signed internal flow sums to zero at batch, sleeve, family,
and portfolio levels.

Add economic-attribution tests proving:

```text
cash closing = cash opening
             + ordinary cash funding transfer
             + terminal cash funding transfer
```

and that the sum of positive sleeve ordinary transfers equals the gross ordinary
funding summary. No funding amount may alter fixed cost, variable cost, external
notional, external input, or external output.

Run and observe the red state:

```bash
.venv/bin/pytest -q \
  research/tests/test_portfolio_path.py \
  research/tests/test_portfolio_evaluation.py \
  research/tests/test_portfolio_comparators.py \
  -k 'internal_cross or ordinary_exit_funding or signed_internal'
```

- [ ] **Step 2: Carry gross and signed values independently**

Add `internal_cross_notional_usd` to action and sleeve attribution as the gross
positive receipt accumulator while retaining
`signed_internal_cngn_value_usd`. Validate for every action batch that signed
flow sums to zero and positive receipts sum to the batch internal-cross
notional. Replace the current window-level
`max(signed_internal_cngn_value_usd, 0)` reconstruction with direct gross
accumulation.

Extend `EconomicAttribution` with signed internal flow and ordinary funding.
Extend `EconomicResult` with the ordered funding events and derived event count
and gross funded amount. Extend `EconomicSummary` and all comparison/equality
boundaries with those fields; avoid any unverified fallback or safe default.

- [ ] **Step 3: Propagate typed failures without fabricating context**

Give `FailureDiagnostic` an optional `ActionReconciliationTrace`. Construct it
only when the originating exception is the typed action-level error. Cap,
no-swap, opening-capital, terminal, and generic failures retain `trace=None`.
Blocked carried paths reuse the exact originating diagnostic and trace rather
than reconstructing a reason string.

- [ ] **Step 4: Implement exact checkpoint codecs**

Replace every failure `asdict()` path in checkpoint construction with explicit
canonical encoders. Encode datetimes using the existing canonical timestamp
boundary. Parse the exact trace key set with bounded strings, finite numbers,
integer event coordinates, and no unknown fields. Reject malformed, missing,
unbounded, non-canonical, or trace-bearing valid outcomes.

Add checkpoint tests for:

- funding-event round trip and deterministic ordering;
- two-event funding ordering by
  `(block_time, block_number, tx_hash, log_index, sleeve_economic_id)`;
- full typed-trace round trip for primary, reset, carried, and snapshot paths;
- exact blocked-path trace preservation;
- rejection of extra keys, invalid timestamps, non-finite values, wrong scalar
  types, entry funding, and a v3 protocol/schema identity.

- [ ] **Step 5: Verify and review Task 2**

```bash
.venv/bin/pytest -q \
  research/tests/test_portfolio_path.py \
  research/tests/test_portfolio_evaluation.py \
  research/tests/test_portfolio_comparators.py \
  research/tests/test_parameter_portfolio_checkpoint.py

.venv/bin/mypy --strict \
  research/backtester/portfolio_errors.py \
  research/backtester/portfolio_simulator.py \
  research/backtester/portfolio_path.py \
  research/backtester/portfolio_evaluation.py
```

Use a read-only Terra review for gross-versus-signed semantics, trace bounds,
blocked-path identity, and checkpoint canonicality. After Sol Ultra adjudication
and reruns, commit:

```bash
git add research/backtester/portfolio_path.py \
  research/backtester/portfolio_evaluation.py \
  research/tests/test_portfolio_path.py \
  research/tests/test_portfolio_evaluation.py \
  research/tests/test_portfolio_comparators.py \
  research/tests/test_parameter_portfolio_checkpoint.py
git commit -m "research: preserve v4 portfolio attribution evidence"
```

---

### Task 3: Version and Validate the Fourteen-File v4 Package

**Files:**

- Modify: `research/backtester/portfolio_publication.py`
- Modify: `research/scripts/evaluate_parameter_portfolio.py`
- Modify: `research/scripts/validate_parameter_portfolio_publication.py`
- Modify: `research/tests/test_evaluate_parameter_portfolio.py`
- Modify: `research/tests/test_validate_parameter_portfolio_publication.py`
- Modify: `research/tests/test_parameter_portfolio_checkpoint.py`

- [ ] **Step 1: Add failing v4 publication-contract tests**

Pin protocol `2026-07-24-r2`, schema `weighted-portfolio-artifacts/v4`, and the
unchanged fourteen canonical filenames. Add summary expectations for:

- `ordinary_exit_funding_event_count`;
- `gross_ordinary_exit_funding_usd`.

Add `joint_attribution.csv` expectations for:

- gross `internal_cross_notional_usd`;
- `signed_internal_cngn_value_usd`;
- `ordinary_exit_funding_transfer_usd`;
- separate `terminal_funding_transfer_usd`.

Run and observe the red state:

```bash
.venv/bin/pytest -q \
  research/tests/test_evaluate_parameter_portfolio.py \
  research/tests/test_validate_parameter_portfolio_publication.py \
  research/tests/test_parameter_portfolio_checkpoint.py \
  -k 'protocol or schema or artifact or attribution or ordinary_exit_funding'
```

- [ ] **Step 2: Produce only bounded v4 publication data**

Bump the schema and protocol everywhere, including the manifest's currently
hard-coded protocol value. Add the economic fields and joint-attribution
columns without introducing a new artifact. Replace publication PBO
`asdict(failure)` calls with an explicit public projection containing only
`exception_type` and `reason`; no trace, event identity, or funding detail may
leak into the publication package.

Add a red package-wide forbidden-key test that recursively scans every JSON
object key and every CSV header for `trace`, reconciliation event identity,
per-action funding gaps, residual-cash availability, and aggregate required
funding. Bounded public failure-reason strings remain permitted. Only the
approved aggregate summary and signed attribution columns may otherwise
describe ordinary funding publicly.

Keep runner `SOURCE_CLOSURE` and validator `FROZEN_SOURCE_CLOSURE` byte-for-byte
equivalent. The validator itself remains outside the closure so the pin-only
follow-up commit cannot invalidate its own attestation.

- [ ] **Step 3: Enforce v4 accounting in the strict validator**

Validate that:

- ordinary transfers sum to zero;
- only cash is negative and sleeves/families are non-negative;
- cash close includes both ordinary and terminal transfers;
- positive sleeve ordinary transfers sum to the summary gross amount;
- gross internal receipt sums equal the economic total;
- signed internal flow sums to zero;
- family attribution exactly aggregates sleeve rows;
- event count is an integer, non-negative, and zero exactly when gross funding
  is zero;
- funding cannot alter costs or external execution fields;
- v3 protocol/schema/checkpoints are rejected.

Per-action exit eligibility and event ordering are enforced internally and in
checkpoint tests because the approved fourteen-file package intentionally has
no fifteenth action-event artifact.

- [ ] **Step 4: Set fresh v4 operational roots**

Change defaults and examples to:

```text
research/results/parameter_portfolio_v4/
research/results/parameter_portfolio_v4_checkpoints/
research/results/parameter_portfolio_v4_run_logs/
```

Never discover, migrate, resume, overwrite, or validate the stopped v3 roots as
v4. A completed package remains immutable.

- [ ] **Step 5: Verify and review Task 3**

```bash
.venv/bin/pytest -q \
  research/tests/test_parameter_portfolio_checkpoint.py \
  research/tests/test_evaluate_parameter_portfolio.py \
  research/tests/test_validate_parameter_portfolio_publication.py

.venv/bin/mypy --strict \
  research/backtester/portfolio_publication.py \
  research/scripts/evaluate_parameter_portfolio.py \
  research/scripts/validate_parameter_portfolio_publication.py
```

Use a read-only Terra review for fourteen-file closure, public-trace exclusion,
validator equations, protocol consistency, and source-closure membership.
After Sol Ultra adjudication, leave all source, validator, and tests ready for
the freeze commit in Task 4.

---

### Task 4: Freeze the v4 Implementation and Pin Its Source Attestation

**Files:**

- Modify: `research/results/README.md`
- Verify all source and tests changed in Tasks 1 through 3

- [ ] **Step 1: Run the pre-freeze functional suite**

The validator still pins the prior source commit at this point, so exclude only
the tests whose sole purpose is checking that stale pin:

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

.venv/bin/mypy --strict \
  research/backtester/portfolio_errors.py \
  research/backtester/portfolio_simulator.py \
  research/backtester/position_runtime.py \
  research/backtester/portfolio_path.py \
  research/backtester/portfolio_evaluation.py \
  research/backtester/portfolio_publication.py \
  research/scripts/evaluate_parameter_portfolio.py \
  research/scripts/validate_parameter_portfolio_publication.py

.venv/bin/python -m compileall -q research/backtester research/scripts
```

Run Ruff on every changed Python file. Run the Base/BSC smoke fixture tests and
inspect their complete fourteen-file packages from fresh temporary roots, but do
not invoke strict source attestation yet: the validator still pins the prior
closure and must reject the changed bytes until Y2. A smoke failure is a code
defect, not permission to weaken a research rule.

- [ ] **Step 2: Perform the Sol Ultra pre-commit review**

Review the complete diff and verification evidence for exact cash conservation,
full-cost charging, cap preservation, token orientation, event ordering,
checkpoint compatibility, public/private evidence separation, comments,
documentation, typing, and unused interfaces. Request one final read-only Terra
review; adjudicate all findings and rerun affected checks.

- [ ] **Step 3: Create immutable source-closure commit X2**

Confirm the separately committed design and plan remain binding, but keep the
validator's old source pin. Stage only intended tracked files, confirm the four
protected untracked paths remain untouched, and commit the complete v4
implementation:

```bash
git status --short
git diff --check
git add -- \
  research/backtester/portfolio_errors.py \
  research/backtester/position_runtime.py \
  research/backtester/portfolio_simulator.py \
  research/backtester/portfolio_path.py \
  research/backtester/portfolio_evaluation.py \
  research/backtester/portfolio_publication.py \
  research/scripts/evaluate_parameter_portfolio.py \
  research/scripts/validate_parameter_portfolio_publication.py \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_simulator.py \
  research/tests/test_portfolio_path.py \
  research/tests/test_portfolio_evaluation.py \
  research/tests/test_portfolio_comparators.py \
  research/tests/test_parameter_portfolio_checkpoint.py \
  research/tests/test_evaluate_parameter_portfolio.py \
  research/tests/test_validate_parameter_portfolio_publication.py
git commit -m "research: freeze weighted portfolio v4 implementation"
CLOSURE_COMMIT=$(git rev-parse HEAD)
```

No source-closure member may change after `CLOSURE_COMMIT` is recorded.

- [ ] **Step 4: Record X2 in a documentation-only commit**

Update `research/results/README.md` with the exact immutable
`CLOSURE_COMMIT`, protocol, v4 roots, launch commands, status commands, and
strict validation commands. Commit only that documentation file:

```bash
git add research/results/README.md
git commit -m "docs: document weighted portfolio v4 execution"
```

This separate documentation commit avoids the impossible self-reference of
placing a commit's own hash inside that commit. It does not change a source-
closure member.

- [ ] **Step 5: Create pin-only commit Y2**

Change only `FROZEN_SOURCE_COMMIT` in
`research/scripts/validate_parameter_portfolio_publication.py` to the exact
`CLOSURE_COMMIT`. Y2 contains no source-closure-member change and no validator
logic change.

Run the full tests without exclusions:

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
  research/tests/test_validate_parameter_portfolio_publication.py
```

Then generate fresh Base and BSC smoke packages from the pinned closure and run
the normal strict validator against both. These post-pin smokes replace any
pre-freeze temporary packages and must pass source attestation as well as all
economic gates.

Commit only the pin change:

```bash
git add research/scripts/validate_parameter_portfolio_publication.py
git commit -m "research: pin weighted portfolio v4 source closure"
```

Verify the pinned tree bytes, runner/validator closure equality, clean tracked
worktree, and both commit contents before starting the real run.

---

### Task 5: Execute and Strictly Validate the Fresh Frozen Run

**Files produced under ignored result roots:**

- `research/results/parameter_portfolio_v4/`
- `research/results/parameter_portfolio_v4_checkpoints/`
- `research/results/parameter_portfolio_v4_run_logs/`

- [ ] **Step 1: Launch independent Base and BSC workers**

Start one process per pool, each bound to the same pinned v4 source closure and
its own checkpoint/log subdirectory. Record PID, command, start timestamp,
protocol, source commit, input digests, completed-primary-window count, total
primary-window count, reset progress, and last canonical checkpoint after every
durable checkpoint.

Do not share an output directory between processes. Do not reuse v3 checkpoints.
Do not publish a partially complete pool.

- [ ] **Step 2: Monitor durable progress and fail closed**

Report progress from checkpoint contents rather than process liveness. If a
process stops, classify the exact exception and last durable unit. Resume only
when the checkpoint identity exactly matches the pinned v4 run and the failure
is operational. If a runtime or accounting defect is found, stop both workers,
write a failing regression, correct it under TDD, create a new source closure
and pin, and restart fresh; never resume checkpoints generated by different
source bytes.

- [ ] **Step 3: Validate each immutable package**

After a pool completes, run the normal strict validator rather than
`--integrity-only`. Confirm all fourteen artifacts, source provenance, input
hashes, economic identities, PBO/reset matrices, cap evidence, ordinary funding,
gross/signed internal flow, and manifest hashes. Preserve the validation output
beside the run log.

- [ ] **Step 4: Authorize article evidence only after both pools pass**

Only two QA-valid packages authorize synthesis into the final article. If a
frozen rule is infeasible or a pool fails a binding gate, report that result
directly; do not relabel the run, relax the cap, or substitute the 15%
sensitivity for the 10% primary result.

## Definition of Done

The implementation is complete only when Tasks 1 through 4 pass all focused,
full, typing, lint, compilation, smoke, source-attestation, and review gates.
The research execution is complete only when Task 5 produces strict-validator
success for both fresh v4 pool packages or a documented fail-closed frozen-rule
result. Article performance claims remain blocked until that final condition.
