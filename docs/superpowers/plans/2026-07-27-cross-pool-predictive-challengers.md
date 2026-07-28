# Cross-pool predictive challengers implementation plan

> **Execution rule:** Sol Ultra owns every mutation, test adjudication, evidence
> review, commit, and push. Terra agents may inspect and review only.

**Goal:** Produce a reviewed, deterministic post-hoc evidence package for the
four approved challenger models, then add its exact results and figures to the
CTO brief without changing the reviewed parent conclusion.

**Design:**
`docs/superpowers/specs/2026-07-27-cross-pool-predictive-challengers-design.md`
at commit `9e6c173`.

**Architecture:** Keep the frozen parent pipeline unchanged. A separate loader
validates the reviewed parent bytes; separate estimator and inference modules
run the challengers; separate reporting, manifest, publication, and CLI modules
seal the new package.

**Dependencies:** Python standard library, NumPy, Matplotlib, jsonschema, pytest.
No new dependency or lockfile change.

## Protected worktree boundaries

Do not edit or stage the existing portfolio files, the July 24 portfolio plan,
`quidax_cngn_usdt.json`, checkpoint archives, or `uv.lock`. Do not remove or
weaken `.gitignore`. The frozen files below are read-only:

- `research/cross_pool/contracts.py`
- `research/cross_pool/predictive.py`
- `research/cross_pool/bootstrap.py`
- `research/cross_pool/pipeline.py`
- `research/cross_pool/reporting.py`
- `research/cross_pool/manifest.py`
- `research/cross_pool/publication.py`
- `research/scripts/run_cross_pool_lead_lag.py`

## Task 1: Parent anchor and local contracts

**Files**

- Create `research/cross_pool/challenger_contracts.py`
- Create `research/cross_pool/challenger_parent.py`
- Create `research/tests/test_cross_pool_challenger_parent.py`

**Red tests**

1. Load the current reviewed parent and assert all five frozen SHA-256 values,
   exact headers, horizons, directions, folds, row counts, and the shared
   88-day OOS target calendar.
2. Reject a wrong review state, wrong parent hash, CRLF input, duplicate CSV
   key, noncanonical scalar, missing prediction, duplicated prediction,
   prediction/panel target mismatch, irregular panel time, or missing fold.
3. Assert update labels use forward-state timestamps rather than `actual != 0`.
4. Assert direction-relative target/source features, ages, gaps, regimes, and
   predecessor links are exact.

Run:

```bash
python3 -m pytest research/tests/test_cross_pool_challenger_parent.py -q
```

**Implementation**

- Add immutable local contracts for model family, feature variant, support,
  endpoint, parent anchor, projected row, challenger prediction, fold audit,
  fit failure, metric, contrast, and study.
- Call the existing parent evidence-directory validator, then enforce the five
  challenger-specific hashes and reviewed `leadership_unresolved` projection.
- Parse canonical UTF-8/LF CSV bytes with exact headers and strict scalar
  round-tripping.
- Join every parent prediction to exactly one panel row and its parent fold.
- Build predecessor links before any slice and derive the single ordered
  88-day target calendar.

**Green verification**

Run the focused test, then:

```bash
python3 -m pytest research/tests/test_cross_pool_reporting.py -q
```

## Task 2: Deterministic estimator primitives

**Files**

- Create `research/cross_pool/challenger_estimators.py`
- Create `research/tests/test_cross_pool_challenger_estimators.py`

**Red tests**

1. Checked OLS recovers a known linear process and rejects zero scale, rank
   failure, excessive conditioning, and non-finite data.
2. The ARX(2) design recovers a known second-lag process and never fabricates
   the first predecessor.
3. The fixed additive basis recovers a known hinge process; knots use training
   rows only; duplicate knots and rank failure stop the fit; extrapolation is
   counted.
4. Huber is materially less affected by a fixed outlier than OLS; its exact
   scale, weight, convergence, zero-MAD, weighted-rank, and iteration-limit
   behavior are pinned.
5. Ridge logistic probabilities recover a known update process and remain in
   `[0,1]`; class absence, failed line search, and non-convergence stop the fit.
6. A two-part fixture returns the exact probability, conditional signed-return
   forecast, and product. Log-loss clipping does not alter Brier probabilities.

Run:

```bash
python3 -m pytest research/tests/test_cross_pool_challenger_estimators.py -q
```

**Implementation**

- Implement training-only population scaling and checked linear algebra.
- Implement the exact OLS, additive basis, Huber IRLS, and ridge-logistic
  algorithms from the design. Intercepts are explicit and never standardized
  or penalized.
- Return narrow fit-result objects containing only predictions, coefficients,
  and required audit values.

**Green verification**

Run the focused test twice and require identical output.

## Task 3: Causal walk-forward challenger runner

**Files**

- Create `research/cross_pool/challengers.py`
- Create `research/tests/test_cross_pool_challengers.py`

**Red tests**

1. Reconstruct the parent's target-only and full OLS predictions within
   `1e-10` bps while preserving the parent's exact column order.
2. Produce every registered family/variant/direction/horizon/fold combination
   on the exact parent OOS keys.
3. Contaminating any current validation label or transform input cannot change
   its fold fit.
4. Every training target timestamp is no later than the refit timestamp.
5. ARX(2) excludes only the first training row and fails if an OOS predecessor
   is absent.
6. Two-part update labels preserve price-neutral state updates.
7. A model-specific rank, support, or convergence failure records a cell-level
   failure and discards that combination's partial predictions; it does not
   replace the model or block unrelated combinations.

Run:

```bash
python3 -m pytest research/tests/test_cross_pool_challengers.py -q
```

**Implementation**

- Reconstruct training sets from parent refit identities and observable labels.
- Build the three matched feature variants for OLS, ARX(2), GAM-style, Huber,
  and both two-part components.
- Preserve parent OLS predictions as the reference after independent endpoint
  reproduction succeeds.
- Collect complete long-form predictions, fold audits, and explicit failures
  in canonical order.

**Green verification**

Run Tasks 1-3 tests together and inspect one one-hour fold audit manually.

## Task 4: Metrics, freshness, block bootstrap, and max-t

**Files**

- Create `research/cross_pool/challenger_inference.py`
- Create `research/tests/test_cross_pool_challenger_inference.py`

**Red tests**

1. Freshness supports use both decision-time ages and never refit or redefine
   lags.
2. Metrics pin MAE, MSE, RMSE, 10-bps direction, update incidence, Brier, log
   loss, calibration, conditional-update MAE, and support counts.
3. Reliability groups use stable probability/timestamp ordering and five
   equal-count partitions only at 25 or more rows.
4. One `10000 x 88` PCG64 draw fixture is reused globally. Every sampled block
   is seven circular calendar days and every original row stays with its day.
5. Empty slice-days contribute zero counts; a zero-total draw makes the cell
   unavailable without redraw.
6. Nearest-rank raw intervals, `ddof=1` standard errors, centered max-t critical
   value, simultaneous interval, and plus-one adjusted p-value match hand-built
   fixtures.
7. The registered source-price family contains exactly 126 cells before
   support failures. Missing cells are package errors; inadequate cells remain
   explicit `not_adjudicable` results.
8. Day-level ACF, fold loss, joint regime slices, and secondary metrics are
   deterministic and descriptive.

Run:

```bash
python3 -m pytest research/tests/test_cross_pool_challenger_inference.py -q
```

**Implementation**

- Compute metrics from matched variants only.
- Aggregate loss sums and counts by the global target-day calendar.
- Produce all three feature-block contrasts and the 126-cell adjusted family.
- Apply the 42-day, six-block, 20-update-day, fit, and variance gates exactly.
- Keep secondary diagnostics separate from adjusted claims.

**Green verification**

Run Tasks 1-4 tests and a small deterministic synthetic end-to-end study.

## Task 5: Deterministic artifacts and figures

**Files**

- Create `research/cross_pool/challenger_artifacts.py`
- Create `research/cross_pool/challenger_reporting.py`
- Create `research/tests/test_cross_pool_challenger_reporting.py`

**Red tests**

1. Pin exact CSV headers, canonical ordering, LF newlines, float rendering,
   canonical JSON, Markdown sections, artifact names, and hashes.
2. Parse every rendered table back and reconcile it with the typed study.
3. Render figures twice under Matplotlib `Agg` and require identical PNG bytes.
4. Assert figure labels distinguish absolute error, incremental source-price
   value, freshness, incidence calibration, and dependence.
5. Reject missing, extra, empty, noncanonical, or semantically inconsistent
   artifacts.

Run:

```bash
python3 -m pytest research/tests/test_cross_pool_challenger_reporting.py -q
```

**Implementation**

- Render predictions, fold audits, metrics, contrasts, diagnostics, and a plain
  language report from typed evidence only.
- Render the five approved figures with exact data labels and explanatory
  captions in the report.
- Keep graphical output diagnostic; no plot may imply causal leadership or
  trading economics.

## Task 6: Manifest, atomic publication, review, and CLI

**Files**

- Create `research/cross_pool/challenger_manifest.schema.json`
- Create `research/cross_pool/challenger_manifest.py`
- Create `research/cross_pool/challenger_publication.py`
- Create `research/scripts/run_cross_pool_challengers.py`
- Create `research/scripts/review_cross_pool_challengers.py`
- Create `research/tests/test_cross_pool_challenger_manifest.py`
- Create `research/tests/test_run_cross_pool_challengers.py`

**Red tests**

1. Schema and semantic validation require the research role, unchanged parent
   decision, five parent hashes, frozen estimator/bootstrap configuration,
   complete cell registry, exact artifacts, QA state, and review state.
2. Parent/input/serialization failures produce only a canonical `qa_blocked`
   manifest; model-cell failures remain QA-pass evidence with explicit cells.
3. Candidate writing is private, fsynced, exact-file, and atomic. Existing
   generated evidence is compare-only and byte-identical; reviewed evidence is
   immutable except through explicit review.
4. Review changes only the manifest review fields and revalidates the directory.
5. The CLI redacts credentials, never publishes a partial directory, and
   returns nonzero on blocked evidence.

Run:

```bash
python3 -m pytest \
  research/tests/test_cross_pool_challenger_manifest.py \
  research/tests/test_run_cross_pool_challengers.py -q
```

**Implementation**

- Capture the statically tested full transitive local source closure, current
  commit/diff identity, runtime, parent identity, configuration, artifact
  hashes, QA reasons, and review state.
- Reuse established atomic-publication mechanics without importing the frozen
  parent's closed artifact contracts.
- Add narrow run and review CLIs.

## Task 7: Full verification and evidence run

1. Run formatting/static checks used by the research surface.
2. Run all challenger tests, then the complete cross-pool test suite.
3. Complete the Sol Ultra source review, stage only the challenger implementation,
   and create a provenance checkpoint commit. The evidence CLI rejects an
   untracked source closure; the canonical manifest must point to a commit that
   contains the executable study rather than a pre-implementation `HEAD` plus an
   unrecoverable content hash.
4. Preserve the immutable v1 and v1.1 candidates as unreviewed. The v1.1 audit
   identified one numerical-null Huber cell but missed the same phenomenon on
   the four-hour freshness support. Generate the review-capable replacement in
   `research/results/cross_pool_challengers_v1_2` and require all 11 statistical
   artifact bytes to match v1.1.
5. Validate the generated package independently, inspect all support/failure
   cells, and compare headline tables to direct calculations.
6. Ask a read-only Terra reviewer to audit the complete code diff and generated
   results. Sol Ultra adjudicates every finding and makes any correction.
7. Rerun the CLI against the existing generated package in compare-only mode
   and require byte-identical artifacts. Do not delete or clean the target.
8. Explicitly review the final QA-pass package as `sol_ultra` with a UTC review
   timestamp and both required Huber numerical-null acknowledgements, then
   validate the manifest-bound adjudications, evidence-count summary, and
   package again.
9. Copy the five reviewed PNG artifacts byte-for-byte into
   `research/results/reports/cross_pool_challengers_v1_2/` and verify each copied
   hash against the reviewed manifest before editing the brief.

Run at minimum:

```bash
python3 -m pytest research/tests/test_cross_pool_challenger_*.py -q
python3 -m pytest research/tests/test_cross_pool_*.py -q
python3 research/scripts/run_cross_pool_challengers.py \
  --parent-dir research/results/cross_pool_lead_lag \
  --supersedes-dir research/results/cross_pool_challengers_v1_1 \
  --out-dir research/results/cross_pool_challengers_v1_2

python3 research/scripts/review_cross_pool_challengers.py \
  --evidence-dir research/results/cross_pool_challengers_v1_2 \
  --supersedes-dir research/results/cross_pool_challengers_v1_1 \
  --reviewed-by sol_ultra \
  --reviewed-at-utc <UTC-Z> \
  --acknowledge-numerical-null \
  huber/full_source/base_to_bsc/3600000/all/mae/source_price \
  --acknowledge-numerical-null \
  huber/full_source/base_to_bsc/3600000/both_age_le_4h/mae/source_price
```

## Task 8: CTO brief and publication figures

**Files**

- Modify `research/articles/cto-final-research-brief-2026-07.md`
- Generate `research/results/reports/cross_pool_challengers_v1_2/*.png`

1. Preserve unrelated existing brief edits and repair only the visible broken
   word split in the overlapping section.
2. Add exact model definitions, assumptions, which observed data property each
   addresses, fit/convergence evidence, OOS results, adjusted uncertainty,
   freshness results, and the two-part incidence explanation.
3. Explain why ARMA/ARIMA, broad tree ensembles, and neural models were not
   promoted on this sample.
4. Embed and explain the approved figures. State what each axis, interval, and
   comparison means.
5. Keep `leadership_unresolved` unchanged and list the fresh-data replication
   requirement.
6. Apply the no-AI-slop review and verify every number against the reviewed
   package.

## Task 9: Sol Ultra pre-commit gate

1. Inspect the complete scoped diff and staged file list.
2. Confirm frozen parent modules, `.gitignore`, dependencies, lockfiles, and
   protected user files are untouched.
3. Re-run focused and full verification from the final tree.
4. Check statistical formulas, edge cases, comments, docs, deterministic bytes,
   and figure references.
5. Confirm the provenance checkpoint contains only challenger code/tests/docs.
   Stage the intended CTO brief change and the five reviewed, manifest-matched
   CTO figure copies as a separate publication commit.
6. Push `research` only after every gate passes.
