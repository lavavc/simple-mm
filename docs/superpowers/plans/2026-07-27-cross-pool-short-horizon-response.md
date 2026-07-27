# Cross-Pool Short-Horizon Response Implementation Plan

> **Execution rule:** Implement with GPT-5.6 Sol Ultra. Use red-green-refactor
> for each task, perform the required Sol pre-write and pre-commit reviews, and
> leave all weighted-portfolio worktree changes untouched.

**Goal:** Produce, review, and seal a parent-bound seven-horizon Base/BSC
response package; integrate only reviewed evidence into the final empirical
article; prepare a concise CTO brief; then clean task-created repository debris.

**Design:**
`docs/superpowers/specs/2026-07-27-cross-pool-short-horizon-response-design.md`

**Critical path:** Tasks 1-5 are sequential. Article prose may be outlined after
Task 3, but exact claims and figures wait for Task 5. The CTO brief waits for the
sealed article evidence package. Cleanup is last.

## Protected State

Do not edit, stage, delete, or reformat:

```text
docs/superpowers/plans/2026-07-24-weighted-portfolio-v4-shared-exit-funding.md
research/backtester/portfolio_errors.py
research/backtester/portfolio_simulator.py
research/backtester/position_runtime.py
research/tests/test_portfolio_simulator.py
research/tests/test_position_runtime.py
.firecrawl/
quidax_cngn_usdt.json
research/results/parameter_portfolio_checkpoint_archives/
uv.lock
research/results/cross_pool_lead_lag/
```

Never change `.gitignore`.

## Task 1: Measurement Contracts And Common-Support Rows

**Create:**

- `research/cross_pool/short_horizon.py`
- `research/tests/test_cross_pool_short_horizon.py`

**Reuse unchanged:**

- `research/cross_pool/contracts.py`
- `research/cross_pool/event_study.py`
- `research/cross_pool/io.py`

### Steps

1. Write failing tests for:
   - exact horizon order `(30s, 1m, 2m, 3m, 5m, 10m, 15m)`;
   - seven rows per eligible shock and common 15-minute support;
   - unconditional as-of zero when the target does not update;
   - first update strictly after the shock and inclusive at the endpoint;
   - right censoring without conditional zero imputation;
   - unchanged-price updates, multiple updates, long-stale starts, and exact
     timestamp ages/delays;
   - same-timestamp flagging and fixed exclusion sensitivity;
   - correct Base/BSC stablecoin-per-cNGN orientation; and
   - positive- and negative-shock fee-gap formulas.
2. Run the narrow test and confirm it fails because the module is absent:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_short_horizon.py
   ```

3. Perform the Sol pre-write review. Confirm typed row/status invariants,
   endpoint rules, fee directions, structural nulls, and documentation impact.
4. Implement immutable typed rows and measurement functions. Call the existing
   `detect_shocks()` and `measure_event_responses()` rather than duplicating the
   frozen parent estimator.
5. Add construction-time validation so malformed rows fail before reporting.
6. Run the narrow test to green, then:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_event_study.py research/tests/test_cross_pool_short_horizon.py
   python -m ruff check research/cross_pool/short_horizon.py research/tests/test_cross_pool_short_horizon.py
   ```

7. Review the diff and commit only Task 1 files:

   ```bash
   git commit -m "research: measure short-horizon pool responses"
   ```

## Task 2: Paired Day Bootstrap And Multiplicity

**Create:**

- `research/cross_pool/short_horizon_inference.py`
- `research/tests/test_cross_pool_short_horizon_inference.py`

### Steps

1. Write failing tests for deterministic PCG64 day draws, shared draws across
   horizons, pointwise nearest-rank intervals, max-z bands, sparse conditional
   support, zero variance, and valid-resample thresholds.
2. Confirm red:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_short_horizon_inference.py
   ```

3. Implement per-direction paired UTC-day resampling with 2,000 draws and seed
   `20260715`. Keep pointwise and simultaneous intervals as different types.
4. Make unavailable simultaneous inference carry a stable reason code; never
   replace it with pointwise inference, NaN, infinity, or zero.
5. Validate count identities, nested-horizon keys, finite statistics, and
   deterministic ordering at construction.
6. Run:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_short_horizon.py research/tests/test_cross_pool_short_horizon_inference.py
   python -m ruff check research/cross_pool/short_horizon_inference.py research/tests/test_cross_pool_short_horizon_inference.py
   ```

7. Review and commit:

   ```bash
   git commit -m "research: infer short-horizon response profiles"
   ```

## Task 3: Independent Manifest And Atomic Publication

**Create:**

- `research/cross_pool/short_horizon_manifest.schema.json`
- `research/cross_pool/short_horizon_manifest.py`
- `research/cross_pool/short_horizon_publication.py`
- `research/tests/test_cross_pool_short_horizon_manifest.py`
- `research/tests/test_cross_pool_short_horizon_publication.py`

**Do not modify:**

- `research/cross_pool/article_manifest.schema.json`
- the reviewed parent output directory

### Steps

1. Write failing contract tests for exact parent SHA/decision binding, feature
   and Girum-note hashes, schema `1.0.0`, canonical JSON, duplicate/non-finite
   rejection, exact artifact set, generated/reviewed states, and QA-blocked
   states.
2. Write failing publication tests for new publication, byte-identical
   compare-only rerun, candidate mismatch, output lock, symlink rejection,
   atomic replacement, credential redaction, and reviewed immutability.
3. Confirm red:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_short_horizon_manifest.py research/tests/test_cross_pool_short_horizon_publication.py
   ```

4. Implement a narrow independent schema and manifest builder. Reuse canonical
   JSON and provenance helpers where their contracts are generic; do not widen
   the v2 article-manifest schema.
5. Implement output-scoped locking, private staging, fsync, no-replace publish,
   compare-only verification, and fail-closed evidence validation for
   `short_horizon_manifest.json`.
6. Ensure generated code cannot stamp `reviewed`.
7. Run the new tests plus parent publication/reporting regressions:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_short_horizon_manifest.py research/tests/test_cross_pool_short_horizon_publication.py research/tests/test_cross_pool_reporting.py
   python -m ruff check research/cross_pool/short_horizon_manifest.py research/cross_pool/short_horizon_publication.py research/tests/test_cross_pool_short_horizon_manifest.py research/tests/test_cross_pool_short_horizon_publication.py
   ```

8. Review and commit:

   ```bash
   git commit -m "research: seal short-horizon evidence"
   ```

## Task 4: Reports, Figures, CLI, And Parent Anchor

**Create:**

- `research/cross_pool/short_horizon_reporting.py`
- `research/scripts/run_cross_pool_short_horizon.py`
- `research/scripts/review_cross_pool_short_horizon.py`
- `research/tests/test_cross_pool_short_horizon_reporting.py`
- `research/tests/test_cross_pool_short_horizon_cli.py`

### Steps

1. Write failing tests for exact CSV headers/order, JSON QA counts, all seven
   horizons in both directions, the three deterministic figures, report claim
   wording, Girum method reconciliation, and credential-safe CLI failures.
2. Add a fixed parent-anchor fixture requiring exact 15-minute shock keys,
   response rows, counts, and point estimates from the reviewed manifest.
3. Write review-command tests: only QA-pass `generated_unreviewed` evidence may
   be stamped; review identity/time must be explicit; artifact bytes stay
   unchanged; reviewed evidence becomes immutable.
4. Confirm red:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_short_horizon_reporting.py research/tests/test_cross_pool_short_horizon_cli.py
   ```

5. Implement deterministic CSV/JSON/Markdown rendering and figures:
   - unconditional response profile with pointwise and simultaneous intervals;
   - update incidence, conditional response, latency, and staleness; and
   - fee-only shock/horizon gap and closure.
6. Implement the runner with required explicit input, parent, context-note, and
   output paths. Snapshot every input before analysis and publish only a full
   candidate or a stable QA-blocked manifest.
7. Implement the separate review command. It must validate the complete
   package before atomically changing only the manifest review state.
8. Run:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_short_horizon_reporting.py research/tests/test_cross_pool_short_horizon_cli.py
   python -m pytest -q research/tests/test_cross_pool_event_study.py research/tests/test_cross_pool_reporting.py research/tests/test_cross_pool_cli.py
   python -m ruff check research/cross_pool/short_horizon_reporting.py research/scripts/run_cross_pool_short_horizon.py research/scripts/review_cross_pool_short_horizon.py research/tests/test_cross_pool_short_horizon_reporting.py research/tests/test_cross_pool_short_horizon_cli.py
   ```

9. Perform the full Sol pre-commit review across Tasks 1-4 and commit:

   ```bash
   git commit -m "research: publish short-horizon response study"
   ```

## Task 5: Full Run, Reproduction, Review, And Evidence Seal

**Output:** `research/results/cross_pool_short_horizon_v1/` (ignored)

### Steps

1. Confirm the worktree still contains only expected implementation changes
   plus the protected pre-existing paths.
2. Run all focused tests:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_short_horizon.py research/tests/test_cross_pool_short_horizon_inference.py research/tests/test_cross_pool_short_horizon_manifest.py research/tests/test_cross_pool_short_horizon_publication.py research/tests/test_cross_pool_short_horizon_reporting.py research/tests/test_cross_pool_short_horizon_cli.py
   ```

3. Run the complete parent regression surface:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_bootstrap.py research/tests/test_cross_pool_event_study.py research/tests/test_cross_pool_io.py research/tests/test_cross_pool_reporting.py research/tests/test_cross_pool_cli.py research/tests/test_cross_pool_article_contract.py
   ```

4. Run the extension once:

   ```bash
   python3 research/scripts/run_cross_pool_short_horizon.py --base-features research/data/derived/uni_base_pool_features.csv --bsc-features research/data/derived/uni_bsc_pool_features.csv --parent-manifest research/results/cross_pool_lead_lag/article_manifest.json --girum-note research/autoresearch/cross-venue-lead-lag-2026-07-15.md --out-dir research/results/cross_pool_short_horizon_v1
   ```

5. Run the identical command again before review. It must verify byte-identical
   candidate artifacts rather than replace them.
6. Inspect every QA count, exact 15-minute parent anchor, confidence band,
   figure, manifest hash, and Girum comparison. Recompute a sample of raw and
   fee-gap rows independently.
7. If any gate fails, leave the package unreviewed or QA-blocked, fix through a
   new TDD cycle, and repeat Steps 2-6.
8. Stamp review only after the Sol evidence review:

   ```bash
   python3 research/scripts/review_cross_pool_short_horizon.py --evidence-dir research/results/cross_pool_short_horizon_v1 --reviewed-by sol_ultra
   ```

9. Validate the reviewed directory and record its manifest SHA-256. Do not
   modify it again.

## Task 6: Article Evidence Package And Final Article

**Modify:**

- `research/articles/02-backtesting-the-market-layer.md`
- `research/articles/evidence-pack-2026-07-cngn-market-making.md`
- `research/articles/README.md`
- `research/tests/test_cross_pool_article_contract.py`

**Create only if separation keeps the existing contract clearer:**

- `research/tests/test_cross_pool_short_horizon_article_contract.py`

### Steps

1. Write failing article-contract tests requiring the reviewed extension path,
   manifest hash, schema/status, figure hashes, complete horizon family,
   `post_hoc_exploratory` label, and unchanged parent conclusions.
2. Confirm the tests reject unreviewed evidence and directional, causal, alpha,
   or net-executable claims.
3. Update the evidence pack first with exact reviewed figures, methods,
   uncertainty, limitations, and the Girum reconciliation table.
4. Update the article with only the minimum narrative and figures supported by
   both manifests. Keep thresholds, signal coefficients, and execution tactics
   out of publication prose.
5. Update the article README provenance markers without replacing the frozen
   parent source marker.
6. Run:

   ```bash
   python -m pytest -q research/tests/test_cross_pool_article_contract.py research/tests/test_cross_pool_short_horizon_article_contract.py
   rg -n 'TODO|TBD|placeholder|causal price discovery|deployable alpha|Base leads|BSC leads' research/articles/02-backtesting-the-market-layer.md research/articles/evidence-pack-2026-07-cngn-market-making.md research/articles/README.md
   git diff --check -- research/articles research/tests/test_cross_pool_article_contract.py research/tests/test_cross_pool_short_horizon_article_contract.py
   ```

   If the optional test file is not created, omit it from commands.
7. Perform a claim-by-claim Sol review against both reviewed manifests, then
   commit the sealed article evidence package:

   ```bash
   git commit -m "docs: integrate reviewed short-horizon evidence"
   ```

## Task 7: CTO Final Research Brief

**Create:** `research/articles/cto-final-research-brief-2026-07.md`

### Steps

1. Draft no more than two pages covering:
   - the decisions the research was intended to inform;
   - frozen versus exploratory methods;
   - exact reviewed lead/lag and portfolio outcomes;
   - which gates passed or failed;
   - practical implications for LP, arbitrage, data collection, and further
     research;
   - limitations and prohibited inferences; and
   - a short recommended decision list.
2. Bind every exact figure to a reviewed manifest or evidence-pack location.
3. Check that the brief contains no operational signals, coefficients,
   leverage, sizing, credentials, or unreviewed claims.
4. Run prose and placeholder checks, then review against the article evidence
   package:

   ```bash
   rg -n 'TODO|TBD|placeholder|API[_ -]?key|ALCHEMY' research/articles/cto-final-research-brief-2026-07.md
   git diff --check -- research/articles/cto-final-research-brief-2026-07.md
   ```

5. Commit the prepared brief. Do not send it externally without a separate
   user instruction:

   ```bash
   git commit -m "docs: prepare final research brief"
   ```

## Task 8: Controlled Repository Cleanup And Handoff

### Steps

1. Inventory tracked modifications, untracked files, ignored research outputs,
   caches, staging directories, obsolete plans, and completed checkpoint state.
2. Classify each item as:
   - task-created disposable;
   - durable research evidence;
   - protected pre-existing user work; or
   - ambiguous and requiring approval.
3. Remove only task-created caches, failed candidate stages, temporary locks,
   and byte-identical disposable scratch. Do not delete the reviewed evidence
   package, raw inputs, portfolio checkpoints, or protected paths.
4. Present the ambiguous cleanup list before any destructive action.
5. Run the final verification suite appropriate to all changed code and docs,
   then inspect:

   ```bash
   git status --short --branch
   git diff --check
   git log --oneline --decorate -12
   ```

6. Provide a final handoff containing:
   - reviewed manifest hashes and exact output locations;
   - test and reproduction commands with results;
   - article and CTO-brief paths;
   - preserved dirty paths;
   - cleanup performed and cleanup still awaiting approval; and
   - commit and push status.

## Final Stop Conditions

Stop and report rather than improvise if:

- the parent manifest or feature hashes differ;
- the 15-minute anchor fails exact reconciliation;
- common-support or update/censor identities fail;
- simultaneous inference is silently unavailable;
- deterministic rerun bytes differ;
- evidence is QA-blocked or unreviewed;
- the article requests a claim forbidden by either manifest; or
- cleanup would touch pre-existing or ambiguous user work.
