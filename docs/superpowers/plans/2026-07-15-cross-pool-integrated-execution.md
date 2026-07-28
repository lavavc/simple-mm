# Cross-Pool Integrated Execution Plan

> **Execution complete 2026-07-27:** The reviewed parent decision is
> `leadership_unresolved`; the economic class is `no_net_return_improvement`.
> Preserve this plan as coordination provenance.

**Goal:** Execute the July 15 statistical research, portfolio/backtester
integration, and Article 2 work as three coordinated streams without letting
generated results leak into prose before review.

**Authority:** The design in
`docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md` governs
all streams. Detailed requirements live in the three sibling implementation
plans:

- `2026-07-15-cross-pool-statistical-core.md`;
- `2026-07-15-cross-pool-economic-portfolio-integration.md`;
- `2026-07-15-cross-pool-article-evidence.md`.

## Mutation And Review Boundary

The primary Sol agent performs every edit, test-first implementation, artifact
run, evidence promotion, and commit. Read-only agents inspect one stream at a
time for statistical validity, portfolio-accounting correctness, or publication
contract compliance. Agent feedback is advisory until the primary agent
reproduces and adjudicates it.

Do not run two mutating tasks concurrently in the shared checkout. Parallelism
comes from read-only review and independent plan preparation while the primary
agent advances the next test-first task.

## Dependency Graph

```text
Approved design and plans
├── Article Tasks A1-A3: contract, terminology, pending scaffold
├── Statistical Tasks S1-S3: inputs, causal panel, hourly tracer bullet
└── Economic Tasks E1-E4: eligibility seam, runtime/portfolio propagation,
    cap-invalid continuation

S3 ──> S4-S9: inference, reverse test, events, DTW, market structure, reporting
S9 ──> S10: deterministic real statistical run
S10 + E1-E4 ──> E5-E6: frozen Base economics and portfolio full run
S10 + E6 ──> A4: human evidence review
A4 ──> A5: evidence-pack promotion and result-bearing Article 2 integration
```

## Execution Order

1. Commit the approved design and all four plan files after pre-write review.
2. Complete Statistical S1-S3 test-first. This establishes the shared
   prediction-row contract and an inspectable one-hour tracer bullet.
3. While statistical reviews run, complete Article A1-A3 and Economic E1-E4 in
   small reviewed commits. These tasks do not consume realized research
   outcomes.
4. Complete Statistical S4-S9, with a read-only statistical review after each
   inference or diagnostic boundary. Run S10 only after the complete source and
   test surface passes.
5. Complete Economic E5-E6 from the hashed statistical predictions. Do not
   alter frozen windows, gas, parameters, allocations, or liquidity caps.
6. Perform Article A4 as a distinct human evidence-review action. Generated
   code must leave the manifest unreviewed or QA-blocked.
7. Complete Article A5 only from the reviewed manifest and committed evidence
   pack. Keep QA-blocked and data-valid-unresolved paths publishable but
   explicitly distinct.
8. Run a complete Sol diff review, focused regression suites, strict checks for
   the new package, artifact determinism checks, and read-only whole-branch
   review before the final commit or handoff.

## Agent Assignments

- Statistical reviewer: causal alignment, walk-forward embargo, paired block
  inference, regime/influence audits, event clustering, DTW, and classification.
- Portfolio reviewer: entry-only overlay behavior, wallet retention, joint
  accounting, cap failures, frozen fingerprints, comparator paths, and manifest
  hash linkage.
- Article reviewer: policy-versus-information wording, pending/reviewed states,
  schema/branch consistency, durable provenance, and forbidden claims.

Each reviewer receives only the relevant plan/task and current diff. Critical
or important findings are resolved and re-reviewed before the next dependent
task begins.

## Stop Conditions

Stop the affected stream without widening the search when its detailed plan
reports invalid data, causal misalignment, a frozen-contract mismatch, an
unhandled liquidity-cap path, schema inconsistency, or provenance mismatch.
Valid inconclusive or unstable evidence continues to the reviewed
`leadership_unresolved` branch; it is not silently converted into a QA failure.
