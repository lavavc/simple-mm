# Weighted Portfolio v4 Shared Exit Funding Design

**Date:** 2026-07-24

**Protocol:** `2026-07-24-r2`

**Artifact schema:** `weighted-portfolio-artifacts/v4`

**Status:** binding; implementation approved

This amendment replaces only the ordinary-action funding, internal-cross
attribution, failure-trace, artifact-version, checkpoint-compatibility, and
source-freeze clauses of the July 24 weighted portfolio execution finalization
protocol. Every other July 10, July 23, and July 24 requirement remains binding.

## Research Methodology Preserved

- The primary experiment retains the 10% aggregate synthetic-liquidity-share
  cap. A 15% cap remains a separately labelled sensitivity and cannot rescue a
  failed primary rule.
- The 10% sleeve cap, 35% family cap, causal windows, frozen catalog, allocation
  rules, comparators, residual cash, removal tests, and both reset-matrix PBO
  analyses remain unchanged.
- Base and BSC remain separate experiments. Base cNGN is token0; BSC cNGN is
  token1. All execution paths use the common stablecoin-per-cNGN economic
  orientation rather than branching on token index.
- Historical pool price and liquidity paths remain exogenous. Synthetic
  positions affect portfolio accounting and the aggregate-liquidity cap only.
- A legitimate cap breach remains an invalid frozen-rule observation. Shared
  cash cannot waive, reclassify, or repair it.

## Ordinary Action Settlement

An ordinary action batch is the set of entry and mandatory strategy-exit
actions proposed at one observed event before terminal liquidation. A mandatory
exit is an exit already selected by the frozen strategy state, including a
profit target, stop loss, defensive exit, or rebalance. It may not be dropped,
delayed, or converted into a no-op because its sleeve is too small to pay the
full exit cost.

The residual cash account is the undeployed portfolio cash established by the
frozen allocation. It is distinct from every sleeve wallet. Ordinary funding
may debit this cash account only; it may not debit another sleeve or create
synthetic capital.

For each candidate batch, the simulator must first complete joint inventory
netting and allocate the full fixed and variable execution cost to each action.
For action `i`, define:

- `before_i`: marked sleeve value before the action;
- `cost_i`: the complete allocated transaction cost after joint netting;
- `expected_i = before_i - cost_i`;
- `actual_i`: the non-negative marked value of the prepared action wallet.

The action reconciliation tolerance is
`1e-8 * max(1, abs(before_i), abs(actual_i), cost_i)`, matching the existing
marked-value boundary.

The ordinary funding gap is recognized only when all of the following hold:

1. the action is a mandatory `exit`;
2. `expected_i` is negative beyond the reconciliation tolerance;
3. `actual_i` is zero within the same tolerance; and
4. `actual_i - expected_i` is positive beyond that tolerance.

The transfer to that exit is exactly `actual_i - expected_i`. It closes the
identity

`actual_i = before_i - cost_i + ordinary_exit_funding_i`.

The recipient does not gain spendable wealth: the transfer is immediately
consumed by the mandatory exit cost, the action wallet remains at its canonical
non-negative value, and the full cost remains charged. Any other reconciliation
gap is an execution-accounting error and must not be relabelled as funding.
No entry-time reserve or terminal-style pro-rata haircut substitutes for this
event-time cash transfer.

All transfers are prepared before mutation. Positive transfers belong only to
funded exits; the cash account receives the single equal-and-opposite negative
transfer. Their signed sum must be zero within
`1e-10 * max(1, opening portfolio capital)`. The current residual cash must
cover the aggregate positive transfer. Insufficient cash fails the batch before
any runtime or cash-account mutation.

## Interaction With the Entry Lattice

Entry intents remain frozen and independently funded exactly as in v3. No entry
may receive an ordinary funding transfer, consume residual cash, or bypass the
1/256 scale lattice.

A mixed entry/exit batch can change allocated variable cost through joint
inventory netting. Therefore, each descending lattice trial is feasible only
when:

- every entry remains affordable from its own frozen sleeve wallet; and
- residual cash can cover only the candidate's mandatory-exit funding gaps.

Entry affordability and cash insufficiency are allowed to reject a positive
lattice trial. The search still evaluates every declared scale in descending
order without assuming monotonicity. If no positive scale is feasible, the
exits-only batch is mandatory. An exits-only cash shortfall is a typed,
fail-closed invalid execution result; it cannot suppress the exits or turn the
portfolio into cash.

The selected action batch, ordinary transfers, and residual-cash debit are
validated and applied atomically. Mapping order cannot affect the chosen scale,
funding vector, or final state.

## Ordinary Funding Evidence

Each funded exit produces a deterministic `OrdinaryExitFundingEvent` containing:

- event time, block number, transaction hash, and log index;
- sleeve economic ID, action kind, and frozen exit reason;
- `before_i`, `expected_i`, `actual_i`, `cost_i`, and reconciliation tolerance;
- positive funding transfer; and
- residual cash immediately before and after the transfer batch.

Events are ordered by event identity and sleeve economic ID and are persisted in
checkpoints. Economic results expose the event count and gross positive funded
amount. Joint attribution adds a signed
`ordinary_exit_funding_transfer_usd` field for every sleeve and the cash row.
Terminal funding remains in the separate
`terminal_funding_transfer_usd` field; the two mechanisms may not be combined.

The following identities are binding:

- ordinary transfers sum to zero;
- only actual ordinary exits have positive ordinary transfers;
- only the residual cash row has a negative ordinary transfer;
- cash closing value equals cash opening value plus ordinary and terminal cash
  transfers;
- funding changes neither fixed costs, variable costs, external notional,
  external input, nor external output; and
- an entry, comparator without joint sleeves, or terminal-only action has no
  ordinary transfer.

The fourteen-file artifact package remains intact. Schema v4 adds the ordinary
funding summary fields and signed attribution column without adding a fifteenth
file.

## Gross Internal-Cross Attribution

Signed internal cNGN flow remains the per-action conservation quantity. In each
batch its signed sum across actions must be zero. Gross internal-cross notional
is accumulated separately at action time:

`gross_internal_cross_i += max(signed_internal_flow_i, 0)`.

For every batch, the sum of positive action receipts must equal the batch's
internal-cross notional. Across a window, the sum of sleeve gross receipts must
equal the portfolio internal-cross notional, while the sum of sleeve signed
flows must remain zero.

Window-level gross attribution must never be reconstructed as
`max(window_signed_flow, 0)`. A sleeve that receives internal inventory in one
batch and sends inventory in a later batch retains the earlier gross receipt in
turnover attribution. Joint attribution publishes both gross internal-cross
notional and signed internal flow so the turnover and conservation identities
are independently auditable.

## Structured Failure Traces

`FailureDiagnostic` retains its bounded exception type and reason and gains an
optional typed action-reconciliation trace. The trace is constructed at the
root accounting boundary before mutation and includes the same event, sleeve,
action, marked-value, cost, and tolerance fields used by ordinary funding.

Only failures with an action-level reconciliation context carry this trace.
Generic cap, no-swap, opening-capital, and terminal errors retain no fabricated
action context. Valid outcomes never contain a failure diagnostic or failure
trace. Blocked carried paths repeat the originating diagnostic and trace
exactly. Checkpoint serialization is canonical, deterministic, bounded, and
round-trips the complete typed payload. Published CSV failure type and reason
remain unchanged; the detailed trace is checkpoint evidence.

## Fail-Closed Conditions

The corrected simulator fails before mutation for any of the following:

- insufficient residual cash for the exits-only batch;
- any ordinary transfer assigned to an entry, terminal action, or non-cash
  donor;
- a non-zero-sum transfer vector or cash-close mismatch;
- a non-finite or negative cost, transfer, cash balance, or marked value where
  the contract requires non-negativity;
- an action mismatch that is not the exact underfunded-exit clamp described
  above;
- incompatible variable-cost models or failed external-execution
  reconciliation;
- malformed, unbounded, or non-canonical failure-trace data; or
- any existing liquidity-cap, terminal, source-provenance, or publication-gate
  violation.

Numerical tolerance may classify floating residue only. It may not create cash,
reduce a charged cost, or convert a genuinely negative expected balance into a
valid unfunded action.

## Checkpoints, Artifacts, and Source Freeze

Protocol `2026-07-24-r2` uses artifact schema
`weighted-portfolio-artifacts/v4`. The checkpoint container may remain
`weighted-portfolio-checkpoint/v1`, but its run identity must bind the new
protocol, schema, source closure, and input hashes.

The stopped v3 run, bound to source closure
`07d5dfe1a2ae7a03eac068fe341655421ff6b984`, is diagnostic evidence only. Its
7 Base and 6 BSC completed window checkpoints and logs are preserved under their
existing v3 roots and must never be resumed, upgraded, overwritten, or validated
as v4. Raw histories, derived features, QTS inputs, and frozen catalogs may be
reused only when their bytes are included in the new run identity.

The fresh run uses distinct v4 output, checkpoint, and log roots. Source
provenance again uses two commits:

1. commit the complete v4 source closure as immutable commit `X2`;
2. in a pin-only follow-up commit `Y2`, bind the validator to `X2` and attest
   that the worktree closure bytes equal `X2`.

No article performance claim is authorized until the v4 validator accepts both
pool packages from the fresh frozen run.

## Required Verification

Implementation is incomplete until all of the following pass:

- Base and BSC regressions where a sub-cost mandatory exit is funded from cash,
  the sleeve ends non-negative, and the portfolio loses the full cost;
- an insufficient-cash exit with a deterministic trace and no runtime or cash
  mutation;
- a mixed entry/exit batch proving cash can fund only exits and the entry still
  obeys the selected lattice scale;
- multiple simultaneous exits with deterministic ordering and exactly
  zero-sum transfers;
- exact-cash and zero-cash boundary cases;
- a receive-then-send internal-flow regression that preserves sleeve and
  portfolio gross turnover while signed flow remains conserved;
- checkpoint round trips and validator rejection of non-zero-sum transfers,
  cash drift, entry funding, malformed traces, and v3 resume attempts;
- unchanged cap-failure tests, including the observed Base 10% cap breaches;
- focused tests, the complete research suite, strict typing for the source
  closure, changed-file lint, and compilation;
- fresh two-pool smoke packages; and
- the complete fresh frozen v4 run followed by strict publication validation.
