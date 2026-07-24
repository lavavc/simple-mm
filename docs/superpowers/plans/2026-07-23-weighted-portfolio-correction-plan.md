# Weighted LP Portfolio Correction Implementation Plan

> **Historical v2 plan:** Tasks completed here remain audit provenance. The
> operative entry-scaling, terminal-settlement, diagnostics, checkpoint, and
> source-freeze work is specified by
> [`2026-07-24-weighted-portfolio-execution-finalization.md`](2026-07-24-weighted-portfolio-execution-finalization.md).
> Never resume or overwrite the v2 result or checkpoint roots as v3.

**Status (2026-07-23):** Tasks 1–6 and Task 7's interruption/resume equivalence
are complete; the concurrent frozen Base/BSC run remains. Task 8 remains gated
on validated run evidence.

**Goal:** produce a resume-safe, pool-separated Base/BSC run that implements the
2026-07-23 amended protocol exactly, then use only its validated evidence for the
final article.

**Architecture:** correct economic identity first, then event execution and
terminal settlement, then the two-path evaluator and its artifacts, and finally
wrap stable window contracts in checkpoint/resume publication. The existing
full run finishes untouched and is preserved as an implementation audit.

## Task 1: Quarantine the implementation-audit run

**Status:** complete. The ignored audit is isolated at
`research/results/parameter_portfolio_implementation_audit_2026-07-23` and is
not research evidence.

**Files generated only:**

- `research/results/parameter_portfolio_implementation_audit_2026-07-23/**`

1. Wait for the active Base/BSC process and recover its exact exit status.
2. Require all ten legacy artifacts for both pools and validate row identities,
   blank invalid economics, caps, weights/cash, and PBO status.
3. Hash every artifact and write a safe manifest containing source/input hashes,
   start/end time, environment versions, and the four known design mismatches.
4. Rename the output directory only after validation. Never use its outcomes to
   choose amended parameters or claims.

## Task 2: Canonicalize the catalog with tests first

**Status:** complete.

**Modify:**

- `research/backtester/portfolio_catalog.py`
- `research/tests/test_portfolio_catalog.py`

1. Add failing tests for 4,895 economic units, 4,935 declarations, forty
   paper/frozen aliases, frozen ownership, paper-exclusive remainder,
   deterministic ordering, and rejection of an unknown future collision.
2. Introduce explicit declaration/alias records and pool-scoped economic IDs.
3. Make allocation units canonical-only and keep route-aware policies atomic.
4. Verify aliases cannot appear in allocator, PBO, or runtime inputs.

## Task 3: Correct event execution and terminal cash settlement

**Status:** complete; includes exact terminal semantic-zero checks and
fail-closed aggregate-liquidity overflow validation.

**Modify:**

- `research/backtester/portfolio_simulator.py`
- `research/backtester/position_runtime.py`
- `research/backtester/simulator.py`
- `research/tests/test_portfolio_simulator.py`
- `research/tests/test_position_runtime.py`
- `research/tests/test_backtester.py`

1. Add failing tests for one action per sleeve/event, mixed exit/entry netting,
   action-instance identities, exact-input/output hybrid settlement, deterministic
   pro-rata internal matching, payment-asset adjustments, atomic failure, and
   pre-action liquidity pricing.
2. Replace phase-local settlement with one immutable mixed action batch.
3. Require homogeneous variable-cost model fields and implement the frozen
   fixed-point solver with convergence and reconciliation guards.
4. Add deterministic common entry-affordability scaling and test that full-wallet
   same-direction entries remain non-negative without redistributing capital.
5. Add terminal actions that remove positions and convert every loose cNGN unit
   to USD jointly at the final valid swap.
6. Extend single-sleeve close-on-end behavior to liquidate loose inventory too.
7. Reconcile token quantities, marked value, fixed/variable costs, and
   attribution before publishing a result.
8. Test the explicit atomicity boundary: observation and fee accrual persist,
   while any failed action batch leaves wallets, positions, and action counters
   unchanged.

## Task 4: Rebuild the evaluator around reset and carried paths

**Status:** complete.

**Modify:**

- `research/backtester/portfolio_allocation.py`
- `research/scripts/evaluate_parameter_portfolio.py`
- `research/tests/test_portfolio_allocation.py`
- `research/tests/test_evaluate_parameter_portfolio.py`

1. Add failing tests for finite drawdown eligibility, canonical-only weights,
   five disjoint allocation families, deterministic rank-one selection, and
   unchanged cap/cash constants.
2. Compute candidate training and reset validation metrics at the common
   reference bankroll with terminal USD settlement.
3. Emit candidate and reset-allocation CSCV/PBO separately; reject ragged or
   cap-invalid rule matrices.
4. Run one sequential carried path per allocation rule. Pass each closing cash
   balance to the next window and block permanently after a cap failure.
5. Keep reset and carried fields/schema names visibly distinct.
6. Preserve best-sleeve removal only as a post-run ex-post carried sensitivity.
7. Pin directional training to the boundary-selected-component rule, materialize
   directional `no_position` as a complete zero row, and invalidate candidate
   PBO for every other missing, failed, duplicate, or non-finite observation.
8. Permanently block a carried method after any frozen invalid status, not only
   a liquidity-cap breach.

## Task 5: Implement comparators and additive attribution

**Status:** complete.

**Modify:**

- `research/scripts/evaluate_parameter_portfolio.py`
- `research/tests/test_evaluate_parameter_portfolio.py`

1. Add failing tests requiring exactly eight comparator rows per completed
   window and the frozen selection/capital semantics.
2. Reuse the existing sqrt-mid and pool-routed hold primitives without copying
   standalone candidate rows into the comparator artifact.
3. Evaluate the fixed static and four training-selected LP comparators at the
   10% sleeve cap with residual cash.
4. Emit sleeve, disjoint-family, and cash attribution from joint carried
   results. Fail unless all value, PnL, fee, cost, and notional identities close.
5. Emit method stability and paired comparator conclusions with diagnostic-only,
   insufficient-complete-matrix, or not-evaluable-invalid-path statuses—never
   winner language.

## Task 6: Add minimal safe checkpoints before the long run

**Status:** complete. Durable progress is deterministic phase/window/count
metadata; bounded intra-window unit progress is emitted on stderr.

**Create:**

- `research/backtester/portfolio_checkpoint.py`
- `research/tests/test_parameter_portfolio_checkpoint.py`

**Modify:**

- `research/scripts/evaluate_parameter_portfolio.py`
- `research/tests/test_evaluate_parameter_portfolio.py`

1. Test canonical run identity, atomic writes, lock exclusion, contiguous-prefix
   resume, identity drift rejection, carried-cash reconciliation, and byte-equal
   resumed/uninterrupted smoke artifacts.
2. Publish one immutable window checkpoint and one safe `progress.json` update
   only after the full window is durable.
3. Stage and validate final artifacts before atomic directory publication.
4. Add explicit `--resume`, `--checkpoint-dir`, and independent-pool execution
   contracts. Full runs still forbid truncation flags.
5. Add a separately identified best-sleeve-removal phase whose checkpoints bind
   the frozen post-primary removal ID and primary-run identity.

## Task 7: Verification ladder and corrected run

**Status:** in progress.

1. Run focused unit tests after each task.
2. Run one-window, one-unit tests for Base and BSC.
3. Run full-catalog Base interruption/resume equivalence against an
   uninterrupted two-window reference and compare all 14 artifact bytes.
4. Run the full focused research suite, Ruff, and strict mypy for touched
   research modules.
5. Launch Base and BSC as separate concurrent processes with independent
   checkpoint/output directories.
6. Monitor durable progress. Never restart completed windows after a recoverable
   interruption.
7. Require exit code zero, complete artifact sets, manifest/hash validation,
   finite valid fields, blank invalid fields, exact attribution, and pool-local
   claims before closeout.

Expected compute after verified implementation is approximately 4.5 primary
CPU-hours plus modest removal/publication overhead, with approximately three to
four wall hours when Base and BSC run concurrently. Canonical deduplication
saves only about 0.8% of candidate simulations; its purpose is correctness, not
speed.

## Task 8: Research closeout and article handoff

**Status:** pending Task 7 evidence.

Only after Task 7 passes:

1. Update the July 10 research closeout and article evidence pack with the
   amended protocol version, manifest hashes, pool-separated aggregate outcomes,
   invalidity, and remaining comparator limitation.
2. Draft the third article from reviewed aggregate evidence only.
3. Keep implementation-audit outcomes, selected IDs, weights, coefficients,
   operational thresholds, and execution tactics out of public prose.
4. Run article-contract tests and a final source-to-claim audit.

## Commit gates

The main Sol Ultra agent performs the complete-diff and verification review.
Terra review is advisory and read-only. Commit in coherent layers only after the
corresponding tests pass; do not push unless the user explicitly requests it.
