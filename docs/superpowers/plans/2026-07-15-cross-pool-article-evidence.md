# Cross-Pool Article Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Article 2 and its evidence pack advance in parallel with research
implementation without fabricating outcomes or confusing failed policy transfer
with the new information-transfer hypothesis.

**Architecture:** The ignored `article_manifest.json` is the generated
writer-facing contract, while the committed evidence pack is the sole durable
aggregate-results ledger. Article 2 consumes only claims promoted into that
ledger after final review. Until then, committed documents carry an exact
pending-results status block and only result-independent methods and limits.

**Tech Stack:** Markdown, deterministic JSON schema `2.0.0`, pytest document
contract checks, `rg`, `json.tool`, `jq`, and `git diff --check`.

## Global Constraints

- Canonical historical finding: the Base strict-QTS 20/25 directional LP policy
  did not transfer to BSC.
- Policy transferability does not adjudicate BSC-to-Base information transfer.
- Generated code may emit only `generated_unreviewed` or `qa_blocked`.
- Only final Sol review may promote aggregate evidence to `reviewed`.
- The evidence pack is the durable claims ledger; Article 2 does not read
  generated CSVs directly.
- Do not draft result-bearing titles, ledes, conclusions, or leadership verbs
  before review.
- Never publish coefficients, per-event signals, leverage, sizing, or execution
  tactics.
- Preserve the prohibitions on causal price discovery, toxic-flow attribution,
  external-LP profitability, and deployable-alpha claims.
- Do not remove or weaken any `.gitignore` rule.

---

## File Structure

- `docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md`:
  authoritative manifest and claim contract.
- `research/articles/evidence-pack-2026-07-cngn-market-making.md`: committed
  reviewed aggregate evidence.
- `research/articles/02-backtesting-the-market-layer.md`: publication outline
  and later prose.
- `research/articles/README.md`: current article sequence and status.
- `research/autoresearch/lp.md`: durable LP closeout terminology.
- `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md`:
  dated historical handoff plus July 15 addendum.
- `research/tests/test_cross_pool_article_contract.py`: wording and status gate.

### Task 1: Pin the Manifest and Publication Branch Contract

**Files:**
- Modify: `docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md`
- Reuse: `research/cross_pool/article_manifest.schema.json`
- Reuse: `research/cross_pool/manifest.py`
- Create: `research/tests/test_cross_pool_article_contract.py`

**Interfaces:**
- Consumes: `research/results/cross_pool_lead_lag/article_manifest.json`.
- Produces: a schema/version/status contract shared by reporting and writing.

- [ ] **Step 1: Write the failing document-contract test**

```python
def test_cross_pool_design_pins_writer_manifest_and_branches() -> None:
    design = Path(
        "docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md"
    ).read_text()
    assert "article_manifest.json" in design
    assert "schema version `2.0.0`" in design
    for branch in (
        "bsc_to_base_incremental",
        "base_to_bsc_incremental",
        "bidirectional_incremental_no_unique_leader",
        "no_material_incremental_lead",
        "leadership_unresolved",
        "not_adjudicable_qa",
    ):
        assert branch in design
```

- [ ] **Step 2: Run the test and confirm missing contract text**

Run:
`python3 -m pytest research/tests/test_cross_pool_article_contract.py::test_cross_pool_design_pins_writer_manifest_and_branches -q`

Expected: failure until the design contains the approved schema and branches.

- [ ] **Step 3: Document the complete manifest groups and gates**

Pin these top-level groups:

```text
schema_version
artifact_status
provenance
qa
robustness
predictive
event_study
dtw
market_structure
economics
publication
figures
artifacts
review
```

`artifact_status` is one of `generated_unreviewed`, `reviewed`, or
`qa_blocked`. Generated code cannot create `reviewed`. A reviewed manifest
requires reviewer identity/time and a schema-valid branch. Data-valid reviewed
evidence requires complete aggregate fields, exactly one article branch, and
one independent economic branch. A human-reviewed QA failure requires
`qa.status == "blocked"`, nonempty reasons, both branches
`not_adjudicable_qa`, unavailable result groups, and no allowed performance
claims. Serialization rejects NaN/Infinity and uses sorted keys, stable arrays,
and unit-bearing field names.

The bundled Draft 2020-12 JSON Schema and `validate_article_manifest()` are the
enforcement boundary. The schema pins required nested groups, enums,
unit-bearing aggregate fields, provenance hashes, artifact hashes, review
metadata, generated-versus-reviewed state relationships, and the separate
QA-blocked shape. The validator additionally enforces the frozen statistical
and economic decision tables.

- [ ] **Step 4: Run the contract test**

Run:
`python3 -m pytest research/tests/test_cross_pool_article_contract.py::test_cross_pool_design_pins_writer_manifest_and_branches -q`

Expected: pass.

- [ ] **Step 5: Commit the publication contract**

```bash
git add docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md \
  research/tests/test_cross_pool_article_contract.py
git commit -m "docs: pin cross-pool evidence contract"
```

### Task 2: Normalize Policy-Transfer Wording

**Files:**
- Modify: `research/articles/02-backtesting-the-market-layer.md:50-57,120-131`
- Modify: `research/articles/evidence-pack-2026-07-cngn-market-making.md:94-103`
- Modify: `research/autoresearch/lp.md:131-137`
- Modify: `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md:76-88,190-196`
- Modify: `research/tests/test_cross_pool_article_contract.py`

**Interfaces:**
- Consumes: the existing Base/BSC strict-QTS table.
- Produces: one precise policy-transfer statement everywhere.

- [ ] **Step 1: Add a stale-language regression test**

```python
@pytest.mark.parametrize(
    "path",
    [
        Path("research/articles/02-backtesting-the-market-layer.md"),
        Path("research/articles/evidence-pack-2026-07-cngn-market-making.md"),
        Path("research/autoresearch/lp.md"),
        Path("research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md"),
    ],
)
def test_policy_transfer_language_does_not_prejudge_information_transfer(
    path: Path,
) -> None:
    text = path.read_text()
    for stale_phrase in (
        "BSC rejects cross-pool generalization",
        "BSC rejects transferability",
        "BSC rejection",
        "BSC rejects the same strict QTS gates",
        "strict QTS gates reject BSC",
    ):
        assert stale_phrase not in text
    assert "did not transfer to BSC" in text
```

- [ ] **Step 2: Run the test and confirm stale phrases fail**

Run:
`python3 -m pytest research/tests/test_cross_pool_article_contract.py::test_policy_transfer_language_does_not_prejudge_information_transfer -q`

Expected: failure in each file that still contains the broad claim.

- [ ] **Step 3: Apply the canonical two-sentence distinction**

```text
The Base strict-QTS 20/25 directional LP policy did not transfer to BSC: it
returned -1.272% across seven BSC windows, versus +1.039% across four Base
windows.

That result concerns policy transferability. It does not test whether lagged
BSC pool prices contain incremental information about future Base price
changes.
```

Preserve the existing tables, source paths, and no-live-promotion conclusion.

- [ ] **Step 4: Run the wording tests**

Run: `python3 -m pytest research/tests/test_cross_pool_article_contract.py -q`

Expected: all current document-contract tests pass.

- [ ] **Step 5: Commit the terminology correction**

```bash
git add research/articles research/autoresearch/lp.md \
  research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md \
  research/tests/test_cross_pool_article_contract.py
git commit -m "docs: separate policy and information transfer claims"
```

### Task 3: Add Pending-Results Scaffolding and Safe Methods Prose

**Files:**
- Modify: `research/articles/README.md`
- Modify: `research/articles/evidence-pack-2026-07-cngn-market-making.md`
- Modify: `research/articles/02-backtesting-the-market-layer.md`
- Modify: `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md`
- Modify: `research/tests/test_cross_pool_article_contract.py`

**Interfaces:**
- Consumes: the approved July 15 design.
- Produces: parallel-safe article structure with no result claims.

- [ ] **Step 1: Add exact pending-status tests**

```python
PENDING_BLOCK = """CPL_EDITORIAL_STATUS: DESIGN_APPROVED_RESULTS_PENDING
CPL_PRIMARY_CLASS: UNAVAILABLE
CPL_REVERSE_CLASS: UNAVAILABLE
CPL_ARTICLE_BRANCH: UNAVAILABLE
CPL_ECONOMIC_CLASS: UNAVAILABLE
CPL_ROBUSTNESS_STATUS: UNAVAILABLE
CPL_SOURCE_MANIFEST: research/results/cross_pool_lead_lag/article_manifest.json"""


@pytest.mark.parametrize(
    "path",
    [
        Path("research/articles/README.md"),
        Path("research/articles/02-backtesting-the-market-layer.md"),
        Path("research/articles/evidence-pack-2026-07-cngn-market-making.md"),
    ],
)
def test_pending_article_files_share_one_status_contract(path: Path) -> None:
    assert PENDING_BLOCK in path.read_text()
```

- [ ] **Step 2: Run the tests and confirm the block is absent**

Run:
`python3 -m pytest research/tests/test_cross_pool_article_contract.py::test_pending_article_files_share_one_status_contract -q`

Expected: failure for all three article files.

- [ ] **Step 3: Add the exact status block and source links**

Insert the block verbatim in the README, evidence pack, and Article 2. Link the
July 15 design from README. Add a dated July 15 addendum to the July 10 handoff
that identifies it as the closeout of the earlier policy-transfer work.

`CPL_EDITORIAL_STATUS` is intentionally separate from the generated manifest's
lowercase `artifact_status`. Its committed-document values are
`DESIGN_APPROVED_RESULTS_PENDING` and `EVIDENCE_REVIEWED`; it never pretends an
ignored generated artifact is already durable evidence.

- [ ] **Step 4: Add only result-independent Article 2 structure**

Safe sections cover:

- policy transfer versus information transfer;
- canonical `raw_sqrt_mid` and the early/late boundary sensitivity;
- causal as-of construction and preregistered horizons;
- nested baseline/cross models, weekly walk-forward validation, and reverse
  falsification;
- the unconditional frozen-policy test;
- USDC/USDT parity and the sub-10-basis-point caveat;
- limitations against causal discovery, toxic flow, other-LP profitability,
  and deployable alpha.

Do not add result-bearing title, lede, conclusion, metrics, event counts, DTW
lags, economic comparisons, robustness conclusions, or figures.

- [ ] **Step 5: Run article contract and formatting checks**

```bash
python3 -m pytest research/tests/test_cross_pool_article_contract.py -q
rg -n "DESIGN_APPROVED_RESULTS_PENDING|article_manifest.json" \
  research/articles/README.md \
  research/articles/02-backtesting-the-market-layer.md \
  research/articles/evidence-pack-2026-07-cngn-market-making.md
git diff --check -- research/articles research/autoresearch \
  docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md
```

Expected: tests and diff check pass; `rg` finds the same block in all three
article files.

- [ ] **Step 6: Commit the parallel-safe article scaffold**

```bash
git add research/articles research/autoresearch \
  research/tests/test_cross_pool_article_contract.py
git commit -m "docs: scaffold pending cross-pool article evidence"
```

### Task 4: Validate and Review the Generated Manifest

**Files:**
- Read only: `research/results/cross_pool_lead_lag/article_manifest.json`
- Generate only: `research/results/cross_pool_lead_lag/**`

**Interfaces:**
- Consumes: complete statistical and economic artifacts.
- Produces: an evidence-review decision; no article mutation yet.

- [ ] **Step 1: Validate the complete schema and unreviewed gate**

```bash
python3 - <<'PY'
from pathlib import Path
from research.cross_pool.manifest import load_and_validate_article_manifest

manifest = load_and_validate_article_manifest(
    Path("research/results/cross_pool_lead_lag/article_manifest.json")
)
assert manifest["artifact_status"] in {"generated_unreviewed", "qa_blocked"}
PY
jq -e '
  .schema_version == "2.0.0" and
  (.artifact_status == "generated_unreviewed" or .artifact_status == "qa_blocked") and
  (.provenance.input_sha256 | type == "object") and
  (.publication.forbidden_claims | type == "array")
' research/results/cross_pool_lead_lag/article_manifest.json
```

Expected: both commands exit zero.

- [ ] **Step 2: Perform the Sol evidence review**

Check input hashes, causal audits, paired counts, pre/post stability,
leave-one-day/fold influence, DTW band sensitivity, exact economic windows,
comparators, figures, and every classification boundary.

Classify invalid inputs or causal alignment as `not_adjudicable_qa`. A reviewer
may promote only the QA reason, provenance, and forbidden claims from that
shape. Treat valid suggestive, inconclusive, or underpowered results as
`leadership_unresolved`, not QA failures. Treat day/fold, regime, or DTW
instability as reviewed robustness failures that constrain directional claims
and select `leadership_unresolved`.

- [ ] **Step 3: Create review metadata outside generated code**

Only after the manifest's own applicable review checks pass, run
`date -u +%Y-%m-%dT%H:%M:%SZ`, change
`artifact_status` to `reviewed`, set `review.reviewed_by` to `sol_ultra`, and
set `review.reviewed_at_utc` to the command's literal output. Also set
`review.status` to `reviewed`. This is a
deliberate review action, never an executable default; no timestamp placeholder
is prewritten into source. Re-run the full schema and decision-table validator
after this mutation.

### Task 5: Integrate Reviewed Aggregate Evidence

**Files:**
- Modify: `research/cross_pool/manifest.py`
- Modify: `research/articles/evidence-pack-2026-07-cngn-market-making.md`
- Modify: `research/articles/02-backtesting-the-market-layer.md`
- Modify: `research/articles/README.md`
- Modify: `research/tests/test_cross_pool_article_contract.py`

**Interfaces:**
- Consumes: only a validated `reviewed` manifest.
- Produces: one predictive branch, one independent economic branch, and
  source-linked Article 2 claims.
- Adds:
  `validate_evidence_provenance_block(evidence_text: str, manifest_path: Path) -> None`.

- [ ] **Step 1: Update the contract test for reviewed evidence**

Replace the pending-block assertion with a parameterized test requiring the
same reviewed editorial block in the evidence pack, Article 2, and README. The
block begins with
`CPL_EDITORIAL_STATUS: EVIDENCE_REVIEWED`. For data-valid evidence, record the
reviewed primary and reverse classes, one approved article branch, one approved
economic branch, the reviewed robustness status, and its sorted flags. Record
the latter separately as `CPL_ROBUSTNESS_STATUS` and `CPL_ROBUSTNESS_FLAGS`
(`NONE` when the flag list is empty). For reviewed QA-blocked
evidence, record exactly `CPL_PRIMARY_CLASS: UNAVAILABLE`,
`CPL_REVERSE_CLASS: UNAVAILABLE`,
`CPL_ARTICLE_BRANCH: NOT_ADJUDICABLE_QA`,
`CPL_ECONOMIC_CLASS: NOT_ADJUDICABLE_QA`,
`CPL_ROBUSTNESS_STATUS: unavailable`, and
`CPL_ROBUSTNESS_FLAGS: NONE`.
Both shapes retain
`CPL_SOURCE_MANIFEST: research/results/cross_pool_lead_lag/article_manifest.json`.

- [ ] **Step 2: Promote reviewed aggregates into the evidence pack first**

Transcribe only aggregate metrics, uncertainty, robustness results, provenance,
allowed/forbidden claims, and approved figure captions. Do not copy fitted
coefficients or per-event signals.

Because the reviewed manifest remains ignored, also copy its SHA-256, reviewer
identity and UTC time, code commit, schema version, source path, and complete
input-provenance map into the committed evidence pack. For data-valid evidence,
this is all six input SHA-256 values. For reviewed QA-blocked evidence, copy
every available hash and record every missing input with the manifest's exact
reason code. These values make the durable ledger independently auditable even
when the local generated artifact is absent.

Use exact provenance keys `CPL_MANIFEST_SHA256`, `CPL_REVIEWED_BY`,
`CPL_REVIEWED_AT_UTC`, `CPL_CODE_COMMIT`, `CPL_SCHEMA_VERSION`,
`CPL_SOURCE_DIFF_SHA256`, one
`CPL_INPUT_SHA256_<UPPER_SNAKE_INPUT_NAME>` line for each available input, and
one `CPL_INPUT_MISSING_<UPPER_SNAKE_INPUT_NAME>` line for each unavailable
input. Add fixture-backed valid and QA-blocked tests for
`validate_evidence_provenance_block()` and run that validator against the real
evidence pack and reviewed manifest before commit. It compares every copied
value, available/missing key set, and reason code, and computes the manifest
SHA-256 from the exact local bytes.

- [ ] **Step 3: Fill Article 2 from the evidence pack**

Select exactly one article branch:

```text
bsc_to_base_incremental
base_to_bsc_incremental
bidirectional_incremental_no_unique_leader
no_material_incremental_lead
leadership_unresolved
not_adjudicable_qa
```

Select the economic branch independently. Preserve a “where this breaks”
section and every causal/alpha limitation.

- [ ] **Step 4: Update README last and verify reviewed status**

```bash
jq -e '
  .schema_version == "2.0.0" and
  .artifact_status == "reviewed" and
  .review.status == "reviewed" and
  (.review.reviewed_by | type == "string") and
  (.review.reviewed_at_utc | type == "string") and
  (
    (.qa.status == "pass" and .publication.article_branch != "unavailable") or
    (
      .qa.status == "blocked" and
      .publication.article_branch == "not_adjudicable_qa" and
      .publication.economic_class == "not_adjudicable_qa"
    )
  )
' research/results/cross_pool_lead_lag/article_manifest.json
python3 -m pytest research/tests/test_cross_pool_article_contract.py -q
rg -n "CPL_EDITORIAL_STATUS: EVIDENCE_REVIEWED" \
  research/articles/README.md \
  research/articles/02-backtesting-the-market-layer.md \
  research/articles/evidence-pack-2026-07-cngn-market-making.md
python3 - <<'PY'
from pathlib import Path
from research.cross_pool.manifest import validate_evidence_provenance_block

validate_evidence_provenance_block(
    Path("research/articles/evidence-pack-2026-07-cngn-market-making.md").read_text(),
    Path("research/results/cross_pool_lead_lag/article_manifest.json"),
)
PY
git diff --check -- research/articles research/autoresearch
```

Expected: every command exits zero.

- [ ] **Step 5: Commit reviewed article evidence**

```bash
git add research/articles research/autoresearch \
  research/cross_pool/manifest.py \
  research/tests/test_cross_pool_article_contract.py
git commit -m "docs: integrate reviewed cross-pool evidence"
```
