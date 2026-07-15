# Cross-Pool Statistical Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic research-only pipeline that tests whether BSC
pool prices add out-of-sample predictive information for Base, with a
pre-specified reverse test, paired inference, event studies, and constrained
DTW diagnostics.

**Architecture:** A focused `research.cross_pool` package owns typed inputs,
causal panels, estimators, inference, diagnostics, and deterministic reporting.
The CLI only wires these components and writes ignored artifacts. The first
tracer bullet ends at inspectable one-hour BSC-to-Base prediction rows before
any bootstrap, DTW, article, or LP integration is allowed to depend on it.

**Tech Stack:** Python 3.11+, frozen dataclasses, `Decimal` at CSV boundaries,
NumPy SVD least squares, Matplotlib, CSV/JSON/Markdown artifacts, pytest, ruff,
and strict mypy for the new package.

## Global Constraints

- Canonical input price is `raw_sqrt_mid`; never reconstruct legacy
  amount-ratio prices.
- Pools are `uni-base` and `uni-bsc`; primary direction is BSC to Base.
- Horizons are exactly 15 minutes, one hour, and four hours.
- Use epoch-aligned, non-overlapping UTC decision clocks and causal as-of state.
- Initial warmup is 14 elapsed days; refits begin on the next UTC Monday and
  repeat weekly with expanding data.
- A label is trainable at refit time `r` only when `t + h <= r`.
- Bootstrap count is `2000`, seed is `20260715`, and intervals are two-sided
  95-percent percentile intervals over paired target-day UTC blocks. Sampling
  uses NumPy `Generator(PCG64(seed))` and one row-major `int64` index matrix.
  Endpoints are nearest-rank order statistics without interpolation; the
  zero-based indices are 49 and 1949 for the frozen run. Rank arithmetic uses
  `Decimal(str(confidence_level))`, never binary float subtraction. Stream
  byte-stability is scoped to matching recorded runtime provenance, not all
  NumPy 2.x environments.
- Do not tune horizons, features, thresholds, DTW bands, or classifications.
- Keep Base and BSC results separate except for the explicit transfer tests.
- Generated outputs stay under ignored
  `research/results/cross_pool_lead_lag/`.
- Do not remove or weaken any `.gitignore` rule.

---

## File Structure

- `research/cross_pool/contracts.py`: immutable shared statistical contracts.
- `research/cross_pool/io.py`: strict CSV parsing and stream validation.
- `research/cross_pool/panel.py`: causal regular-clock panel construction.
- `research/cross_pool/predictive.py`: direction projection and weekly OLS.
- `research/cross_pool/bootstrap.py`: paired metrics, inference, and classes.
- `research/cross_pool/qa.py`: influence, regime, and stop-condition audits.
- `research/cross_pool/event_study.py`: shocks, responses, and event inference.
- `research/cross_pool/dtw.py`: weekly banded DTW and rotation nulls.
- `research/cross_pool/manifest.py`: schema validation and frozen publication
  decision tables.
- `research/cross_pool/article_manifest.schema.json`: complete writer-facing
  JSON Schema.
- `research/cross_pool/market_structure.py`: venue activity and anonymized LP
  concentration diagnostics.
- `research/backtester/lp_ledger_attribution.py`: shared strict ledger parsing
  and opening-capital calculation.
- `research/cross_pool/reporting.py`: deterministic tabular and JSON outputs.
- `research/cross_pool/figures.py`: the four article-ready figures.
- `research/scripts/run_cross_pool_lead_lag.py`: thin CLI orchestration.

### Task 1: Add Research Dependencies, Typed Events, and Strict QA

**Files:**
- Create: `research/cross_pool/__init__.py`
- Create: `research/cross_pool/contracts.py`
- Create: `research/cross_pool/io.py`
- Create: `research/cross_pool/qa.py`
- Create: `research/tests/test_cross_pool_io.py`
- Modify: `pyproject.toml:15-22`

**Interfaces:**
- Consumes: the existing derived pool-feature CSV schema.
- Produces: `PoolEvent`, `StreamQuality`, `load_pool_events`, and
  `validate_stream`.

- [ ] **Step 1: Add the failing loader and validation tests**

```python
def test_load_pool_events_rejects_future_unsafe_or_ambiguous_rows(tmp_path: Path) -> None:
    path = write_feature_csv(tmp_path, pool="uni-base")
    events = load_pool_events(path, expected_pool="uni-base")
    quality = validate_stream(events, transition_block=45_848_255)
    assert quality.rows == 2
    assert quality.pre_transition_rows == 1
    assert quality.post_transition_rows == 1


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("duplicate_identity", "duplicate pool event identity"),
        ("non_monotonic_time", "timestamps must be strictly increasing"),
        ("missing_mid", "raw_sqrt_mid"),
        ("wrong_pool", "expected pool uni-base"),
        ("crossed_band", "fee-adjusted band"),
    ],
)
def test_load_pool_events_fails_closed(
    tmp_path: Path, mutation: str, match: str
) -> None:
    with pytest.raises(CrossPoolContractError, match=match):
        load_pool_events(
            write_feature_csv(tmp_path, pool="uni-base", mutation=mutation),
            expected_pool="uni-base",
        )
```

- [ ] **Step 2: Run the tests and confirm the missing-package failure**

Run: `python3 -m pytest research/tests/test_cross_pool_io.py -q`

Expected: collection fails with
`ModuleNotFoundError: No module named 'research.cross_pool'`.

- [ ] **Step 3: Declare the research dependency group**

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.4.0",
    "pytest-asyncio>=0.23.0",
    "pytest-cov>=4.1.0",
    "ruff>=0.1.0",
    "mypy>=1.8.0",
]
research = [
    "jsonschema>=4.23.0,<5.0.0",
    "matplotlib>=3.9.0,<4.0.0",
    "numpy>=2.1.0,<3.0.0",
]
```

- [ ] **Step 4: Implement immutable input contracts and strict parsing**

```python
PoolName: TypeAlias = Literal["uni-base", "uni-bsc"]


class CrossPoolContractError(ValueError):
    """Raised when research inputs violate a frozen causal contract."""


@dataclass(frozen=True)
class PoolEvent:
    pool: PoolName
    timestamp_ms: int
    block_number: int
    tx_hash: str
    log_index: int
    raw_mid: Decimal
    fee_adjusted_bid: Decimal
    fee_adjusted_ask: Decimal
    stored_cngn_usd_price: Decimal
    stored_price_model: Literal["sqrt_mid"]


@dataclass(frozen=True)
class GapQuantiles:
    p50: int
    p95: int
    p99: int


@dataclass(frozen=True)
class StreamQuality:
    pool: PoolName
    rows: int
    first_timestamp_ms: int
    last_timestamp_ms: int
    update_gap_quantiles_ms: GapQuantiles | None
    pre_transition_rows: int
    post_transition_rows: int


def load_pool_events(path: Path, *, expected_pool: PoolName) -> tuple[PoolEvent, ...]: ...


def validate_stream(
    events: Sequence[PoolEvent], *, transition_block: int
) -> StreamQuality: ...
```

Reject missing columns, unexpected pools, invalid integers, duplicate
`(pool, tx_hash, log_index)`, non-strict timestamps, unsupported stored models,
non-finite or non-positive prices, and any row that does not satisfy
`fee_adjusted_bid < raw_mid < fee_adjusted_ask`.

Every fail-closed input, causal-alignment, estimator, event, DTW,
market-structure, and manifest validation error derives from
`CrossPoolContractError`. This gives the CLI one narrow QA-blocked boundary;
unrelated programming errors must still propagate.

- [ ] **Step 5: Run the focused tests**

Run: `python3 -m pytest research/tests/test_cross_pool_io.py -q`

Expected: all loader and QA tests pass.

- [ ] **Step 6: Commit the typed input boundary**

```bash
git add pyproject.toml research/cross_pool research/tests/test_cross_pool_io.py
git commit -m "feat: add cross-pool data contracts and QA"
```

### Task 2: Build the Causal Regular-Clock Panel

**Files:**
- Create: `research/cross_pool/panel.py`
- Create: `research/tests/test_cross_pool_panel.py`
- Modify: `research/cross_pool/contracts.py`

**Interfaces:**
- Consumes: validated Base and BSC `PoolEvent` sequences.
- Produces: `PanelConfig`, `PanelRow`, `CausalPanel`, and
  `build_causal_panel`.

- [ ] **Step 1: Write exact-boundary and leakage tests**

```python
def test_panel_uses_only_states_at_or_before_each_query_time() -> None:
    base, bsc = asynchronous_fixture()
    panel = build_causal_panel(base, bsc, PanelConfig(horizon_ms=3_600_000))
    row = panel.rows[0]
    assert row.base_state_timestamp_ms <= row.timestamp_ms
    assert row.bsc_state_timestamp_ms <= row.timestamp_ms
    assert row.base_forward_state_timestamp_ms <= row.timestamp_ms + row.horizon_ms
    assert row.bsc_forward_state_timestamp_ms <= row.timestamp_ms + row.horizon_ms


def test_future_event_cannot_change_an_earlier_panel_row() -> None:
    base, bsc = asynchronous_fixture()
    before = build_causal_panel(base, bsc, PanelConfig(horizon_ms=3_600_000))
    future = future_base_event()
    after = build_causal_panel(base + (future,), bsc, PanelConfig(3_600_000))
    assert [
        row for row in before.rows
        if row.timestamp_ms + row.horizon_ms < future.timestamp_ms
    ] == [
        row for row in after.rows
        if row.timestamp_ms + row.horizon_ms < future.timestamp_ms
    ]
```

Also pin epoch alignment, `t-h/t/t+h`, asynchronous states, state ages, long
flat intervals, the true common-input start/end, and early/mixed/late regimes.
An event inside `(t, t+h]` is allowed to change the forward label; the leakage
test protects only rows whose target endpoint precedes that event.

- [ ] **Step 2: Run the tests and confirm the missing-function failure**

Run: `python3 -m pytest research/tests/test_cross_pool_panel.py -q`

Expected: import fails for `build_causal_panel`.

- [ ] **Step 3: Implement the panel contracts and two-pointer as-of lookup**

```python
@dataclass(frozen=True)
class PanelConfig:
    horizon_ms: int
    base_transition_block: int = 45_848_255
    bsc_transition_block: int = 97_799_490


Regime: TypeAlias = Literal["early", "mixed", "late"]


@dataclass(frozen=True)
class PanelRow:
    timestamp_ms: int
    horizon_ms: int
    base_state_timestamp_ms: int
    bsc_state_timestamp_ms: int
    base_forward_state_timestamp_ms: int
    bsc_forward_state_timestamp_ms: int
    base_lag_block_number: int
    bsc_lag_block_number: int
    base_state_block_number: int
    bsc_state_block_number: int
    base_forward_block_number: int
    bsc_forward_block_number: int
    base_age_ms: int
    bsc_age_ms: int
    base_mid: float
    bsc_mid: float
    base_trailing_return_bps: float
    bsc_trailing_return_bps: float
    base_minus_bsc_gap_bps: float
    base_forward_return_bps: float
    bsc_forward_return_bps: float
    base_regime: Regime
    bsc_regime: Regime


@dataclass(frozen=True)
class CausalPanel:
    common_interval_start_ms: int
    common_interval_end_ms: int
    horizon_ms: int
    rows: tuple[PanelRow, ...]


def build_causal_panel(
    base: Sequence[PoolEvent],
    bsc: Sequence[PoolEvent],
    config: PanelConfig,
) -> CausalPanel: ...
```

Use `10_000 * log(p_t / p_t_minus_h)` for returns and
`10_000 * log(base_mid / bsc_mid)` for the gap. Keep stale states and expose
their ages; do not invent a maximum-age filter. The common interval is the
intersection of the raw stream intervals before epoch rounding. A pool regime
is `early` only when its lag/current/forward blocks are all below the boundary,
`late` only when all three are at or above it, and `mixed` otherwise.

- [ ] **Step 4: Run loader and panel tests**

Run:
`python3 -m pytest research/tests/test_cross_pool_io.py research/tests/test_cross_pool_panel.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the causal panel**

```bash
git add research/cross_pool/contracts.py research/cross_pool/panel.py \
  research/tests/test_cross_pool_panel.py
git commit -m "feat: build causal cross-pool panels"
```

### Task 3: Produce the One-Hour Walk-Forward Tracer Bullet

**Files:**
- Create: `research/cross_pool/predictive.py`
- Create: `research/tests/test_cross_pool_predictive.py`
- Modify: `research/cross_pool/contracts.py`

**Interfaces:**
- Consumes: `CausalPanel` and `WalkForwardConfig`.
- Produces: deterministic BSC-to-Base `PredictionRow` values.

- [ ] **Step 1: Write walk-forward, embargo, and train-only-scaling tests**

```python
def test_refit_excludes_labels_not_observable_at_refit_time() -> None:
    result = expanding_weekly_predictions(known_panel(), primary_config())
    assert all(
        audit.max_training_target_timestamp_ms <= audit.refit_timestamp_ms
        for audit in result.audits
    )


def test_first_refit_anchors_to_raw_common_interval_start() -> None:
    panel = known_panel(common_interval_start_ms=FIRST_WEDNESDAY_MS)
    result = expanding_weekly_predictions(panel, primary_config())
    assert result.audits[0].refit_timestamp_ms == NEXT_MONDAY_AFTER_WARMUP_MS


def test_validation_outlier_does_not_change_training_scaler() -> None:
    baseline = expanding_weekly_predictions(known_panel(), primary_config())
    contaminated = expanding_weekly_predictions(
        replace_validation_feature(known_panel(), value=1e12), primary_config()
    )
    assert baseline.audits[0].feature_means == contaminated.audits[0].feature_means
```

Also test Monday boundaries, expanding folds, known coefficients, zero-variance
features, deficient rank, and condition number above `1e12`.

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

Run: `python3 -m pytest research/tests/test_cross_pool_predictive.py -q`

Expected: import fails for `research.cross_pool.predictive`.

- [ ] **Step 3: Implement direction projection and SVD OLS**

```python
Direction: TypeAlias = Literal["bsc_to_base", "base_to_bsc"]


@dataclass(frozen=True)
class WalkForwardConfig:
    direction: Direction
    initial_train_days: int = 14
    refit_weekday: int = 0
    maximum_condition_number: float = 1e12


@dataclass(frozen=True)
class PredictionRow:
    timestamp_ms: int
    target_timestamp_ms: int
    horizon_ms: int
    refit_timestamp_ms: int
    fold_index: int
    direction: Direction
    target_regime: Regime
    source_regime: Regime
    actual_bps: float
    baseline_prediction_bps: float
    cross_prediction_bps: float


@dataclass(frozen=True)
class FoldAudit:
    fold_index: int
    refit_timestamp_ms: int
    max_training_target_timestamp_ms: int
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]


@dataclass(frozen=True)
class WalkForwardResult:
    predictions: tuple[PredictionRow, ...]
    audits: tuple[FoldAudit, ...]


def expanding_weekly_predictions(
    panel: CausalPanel, config: WalkForwardConfig
) -> WalkForwardResult: ...
```

For BSC-to-Base, baseline features are Base trailing return and Base age; cross
features add BSC trailing return, Base-minus-BSC gap, and BSC age. Reverse the
projection rather than duplicating the estimator for Base-to-BSC. Fit an
intercept and z-score only non-intercept training features. Compute the first
eligible refit from `panel.common_interval_start_ms + 14 days`, then choose the
first UTC Monday boundary at or after that instant; never infer the anchor from
the first materialized panel row.

Validation folds are half-open `[refit, next_refit)`, including the final
partial fold, with one audit per nonempty fold. `FoldAudit.feature_means` and
`feature_scales` contain the five direction-relative cross-model features in
declared order; the nested baseline reuses the first two. Scaling uses the
population standard deviation (`ddof=0`). Rank and condition number are checked
on each standardized design including its intercept, with relative cutoff
`numpy.finfo(float64).eps * max(n_rows, n_columns)` passed unchanged to the SVD
least-squares solve. Reject only when the condition number is strictly greater
than `maximum_condition_number`; equality is valid.

- [ ] **Step 4: Run the primary one-hour tests**

Run:
`python3 -m pytest research/tests/test_cross_pool_io.py research/tests/test_cross_pool_panel.py research/tests/test_cross_pool_predictive.py -q`

Expected: tests pass and the synthetic known-lead fixture produces lower
cross-model loss than the baseline.

- [ ] **Step 5: Commit the inspectable tracer bullet**

```bash
git add research/cross_pool/contracts.py research/cross_pool/predictive.py \
  research/tests/test_cross_pool_predictive.py
git commit -m "feat: add expanding cross-pool OLS"
```

### Task 4: Add Paired Inference, Classification, and Influence Audits

**Files:**
- Create: `research/cross_pool/bootstrap.py`
- Modify: `research/cross_pool/qa.py`
- Modify: `research/cross_pool/contracts.py`
- Create: `research/tests/test_cross_pool_bootstrap.py`

**Interfaces:**
- Consumes: complete paired `PredictionRow` sequences.
- Produces: `DirectionalPredictiveAudit`, composed from `PredictiveInference`,
  `InfluenceReport`, and `RegimeSensitivity`, with typed adequacy and
  unavailable-subset states.

- [ ] **Step 1: Write deterministic paired-bootstrap tests**

```python
def test_bootstrap_is_paired_by_target_utc_day_and_seeded() -> None:
    first = bootstrap_predictive(prediction_fixture(), resamples=2_000, seed=20_260_715)
    second = bootstrap_predictive(prediction_fixture(), resamples=2_000, seed=20_260_715)
    assert first == second
    assert first.resamples == 2_000


def test_positive_class_requires_both_loss_bounds_and_direction_bound() -> None:
    result = classify_predictive(positive_metrics(), positive_intervals())
    assert result == "positive_evidence"
```

Pin the nearest-rank helper directly at indices 49 and 1949 for 2,000 draws and
95-percent confidence; prove binary float drift cannot move either endpoint.
Pin the exact `Generator(PCG64(20260715))` `int64` draw matrix with
`high=4` and shape `(2, 4)` as `[[2, 0, 2, 0], [1, 2, 3, 1]]` in the recorded
NumPy environment.
Also pin a hand-calculated interval fixture, duplicated day weighting, exact
positive and negative 10-bps inclusion, a just-below-threshold exclusion, zero
prediction as a miss, baseline directional gain,
classification precedence, affirmative-null boundaries, leave-one-day and
leave-one-fold reversals, all nine target/source regime combinations, empty
regime subsets, and early/late regime reversal. Use direct `PredictionRow`
fixtures with at least 20 target days and 10 conditional target days rather
than relying on the sub-10-bps predictive fixture. Do not assert that a
percentile interval must contain the original point estimate; that is not a
bootstrap invariant. Reject zero or negative resample counts, negative seeds,
and non-finite or out-of-open-interval confidence levels. Reject derived-error,
squared-error, or aggregate overflow even when every input field is finite.
Require at least two target days and two folds for leave-one influence and the
complete directional audit; keep ordinary one-day inference reportable as
underpowered.

- [ ] **Step 2: Run the tests and confirm missing inference functions**

Run: `python3 -m pytest research/tests/test_cross_pool_bootstrap.py -q`

Expected: imports fail for bootstrap and classification functions.

- [ ] **Step 3: Implement metrics and paired target-day resampling**

```python
EvidenceClass: TypeAlias = Literal[
    "positive_evidence", "suggestive", "affirmative_null", "inconclusive"
]


@dataclass(frozen=True)
class PredictiveMetrics:
    direction: Direction
    horizon_ms: int
    rows: int
    target_day_count: int
    conditional_rows: int
    conditional_target_day_count: int
    baseline_mae_bps: float
    cross_mae_bps: float
    mae_improvement_bps: float
    baseline_mse_bps2: float
    cross_mse_bps2: float
    mse_improvement_bps2: float
    baseline_rmse_bps: float
    cross_rmse_bps: float
    relative_oos_r2: float | None
    baseline_conditional_directional_accuracy: float | None
    cross_conditional_directional_accuracy: float | None
    conditional_directional_accuracy_gain: float | None


@dataclass(frozen=True)
class ConfidenceInterval:
    point: float
    lower: float
    upper: float


@dataclass(frozen=True)
class PredictiveBootstrap:
    direction: Direction
    horizon_ms: int
    resamples: int
    seed: int
    confidence_level: float
    mae_improvement_bps: ConfidenceInterval
    mse_improvement_bps2: ConfidenceInterval
    cross_conditional_directional_accuracy: ConfidenceInterval | None
    conditional_directional_accuracy_gain: ConfidenceInterval | None
    valid_conditional_resamples: int


@dataclass(frozen=True)
class AdequacyAudit:
    target_day_count: int
    conditional_target_day_count: int

    @property
    def underpowered(self) -> bool: ...


@dataclass(frozen=True)
class PredictiveInference:
    metrics: PredictiveMetrics
    bootstrap: PredictiveBootstrap
    adequacy: AdequacyAudit
    evidence_class: EvidenceClass


@dataclass(frozen=True)
class OmissionResult:
    unit: str
    mae_improvement_sign: Literal[-1, 0, 1]
    evidence_class: EvidenceClass


@dataclass(frozen=True)
class InfluenceReport:
    direction: Direction
    horizon_ms: int
    full_mae_improvement_sign: Literal[-1, 0, 1]
    full_evidence_class: EvidenceClass
    leave_one_day: tuple[OmissionResult, ...]
    leave_one_fold: tuple[OmissionResult, ...]
    unit_dependent: bool


@dataclass(frozen=True)
class RegimeSubsetInference:
    regime: Regime
    row_count: int
    inference: PredictiveInference | None
    unavailable_reason: Literal["no_rows"] | None


@dataclass(frozen=True)
class RegimeSensitivity:
    direction: Direction
    horizon_ms: int
    early: RegimeSubsetInference
    mixed: RegimeSubsetInference
    late: RegimeSubsetInference
    regime_unstable: bool
    regime_not_adjudicable: bool


@dataclass(frozen=True)
class DirectionalPredictiveAudit:
    direction: Direction
    horizon_ms: int
    inference: PredictiveInference
    influence: InfluenceReport
    regime_sensitivity: RegimeSensitivity


def summarize_predictions(rows: Sequence[PredictionRow]) -> PredictiveMetrics: ...


def bootstrap_predictive(
    rows: Sequence[PredictionRow],
    *,
    resamples: int = 2_000,
    seed: int = 20_260_715,
    confidence_level: float = 0.95,
) -> PredictiveBootstrap: ...


def assess_adequacy(metrics: PredictiveMetrics) -> AdequacyAudit: ...


def classify_predictive(
    metrics: PredictiveMetrics, bootstrap: PredictiveBootstrap
) -> EvidenceClass: ...


def infer_predictions(
    rows: Sequence[PredictionRow],
) -> PredictiveInference: ...


def assess_prediction_influence(rows: Sequence[PredictionRow]) -> InfluenceReport: ...


def assess_regime_sensitivity(rows: Sequence[PredictionRow]) -> RegimeSensitivity: ...


def audit_predictions(rows: Sequence[PredictionRow]) -> DirectionalPredictiveAudit: ...
```

Positive improvements are baseline loss minus cross loss. Relative OOS
R-squared is `1 - SSE_cross / SSE_baseline`; it is `None` when baseline SSE is
zero so serialization never receives NaN or Infinity. Validate nonempty,
finite, strictly timestamp-ordered rows with one direction, one positive
horizon, exact `target_timestamp_ms == timestamp_ms + horizon_ms`, unique
target timestamps, nonnegative nondecreasing fold indices, one refit timestamp
per observed fold, and every row inside its half-open seven-day refit window.
Do not require observed fold indices to be contiguous: leave-one-fold and
regime subsets legitimately contain gaps. Check the finiteness of derived
errors, absolute errors, squared errors, per-day sums, sampled sums, and final
metrics with `math.fsum`; finite inputs do not excuse overflow. Preserve mixed
transition rows as their own diagnostic group rather than silently assigning
them pre or post.
Use the target UTC date—not the prediction origin date—for day blocks.
On rows with `abs(actual_bps) >= 10`, define a directional hit with equivalent
same-nonzero-sign comparisons rather than multiplication; exact positive and
negative 10-bps moves are included and a zero prediction is a miss. Directional
gain is cross accuracy minus baseline accuracy on the identical conditional
rows.

Draw paired blocks with `numpy.random.Generator(numpy.random.PCG64(seed))` and
one call to `integers(0, target_day_count, size=(resamples,
target_day_count), dtype=numpy.int64, endpoint=False)`. Keep integer counts and
hits in `int64` arrays and loss sums in `float64` arrays rather than coercing one
mixed matrix. Repeated days repeat all their rows; retain row weighting rather
than averaging daily statistics first. Sort each empirical
bootstrap distribution and select zero-based nearest-rank indices
`ceil(alpha / 2 * B) - 1` and `ceil((1 - alpha / 2) * B) - 1`, clamped to the
observed range, where `alpha = 1 - confidence_level`. Construct
`confidence_level` as `Decimal(str(confidence_level))` before subtraction and
rank arithmetic. This yields 49 and 1949 for `B=2000` and 95-percent confidence;
performing the subtraction as binary float is forbidden because it moves the
lower endpoint to index 50.
The interval point is always the original paired-sample statistic, not the
bootstrap mean.

`bootstrap_predictive()` accepts smaller configurations only for focused
statistical tests and rejects non-positive `resamples`, negative `seed`, or a
non-finite `confidence_level` outside the open interval `(0, 1)`.
`infer_predictions()`, `assess_prediction_influence()`,
`assess_regime_sensitivity()`, and `audit_predictions()` are production paths:
they always use exactly 2,000 resamples, seed 20260715, and confidence 0.95 and
offer no configuration parameters. Task 9 independently rejects any typed
inference object whose stored bootstrap settings differ from that frozen tuple.

The fixed adequacy floors are 20 target days overall and 10 target days
containing at least one `abs(actual_bps) >= 10` row. Below either applicable
floor, retain loss metrics but classify the result as `inconclusive`;
`AdequacyAudit.underpowered` is the typed source of the later robustness flag.
If a directional bootstrap draw has no eligible rows, omit only that draw's
directional statistic, record the valid count, and leave both directional
intervals unavailable unless every requested draw is valid; never emit a
numeric sentinel.

`PredictiveMetrics` and `PredictiveBootstrap` both carry direction and horizon.
`classify_predictive()` rejects mismatched provenance or confidence-interval
points that do not equal the corresponding full-sample metrics. Apply
classification after the adequacy gate in this exact order: positive evidence,
affirmative null, suggestive, then inconclusive. Positive evidence requires
positive MAE and MSE point improvements, both loss-interval lower bounds above
zero, and a cross directional-accuracy lower bound above 0.50. Affirmative null
requires an MAE-improvement upper bound below 1.0 bps and a directional-gain
upper bound below 0.05. Missing directional intervals make the positive and
affirmative-null predicates false; an adequate result may still be suggestive
when both loss point improvements are positive. Suggestive does not require a
directional point improvement. Every equality boundary fails its strict branch.

`unit_dependent` is true when any omitted day or fold changes the full evidence
class or changes the exact sign of MAE improvement. Day units use ISO
`YYYY-MM-DD`; fold units use their decimal index. Each omission is reevaluated
independently with the frozen resampling count and seed. Crossing an adequacy
floor counts as a class change. `assess_prediction_influence()` and
`audit_predictions()` fail closed unless the input contains at least two target
UTC days and at least two observed folds, because omitting the sole unit would
leave no inference. `infer_predictions()` itself retains valid one-unit loss
metrics and classifies them as underpowered.

`InfluenceReport`, `RegimeSensitivity`, and `DirectionalPredictiveAudit` carry
direction and horizon explicitly. `audit_predictions()` validates that those
fields, both metric/bootstrap provenance fields, and every nonempty regime
subset agree. This complete audit is the only predictive input accepted by the
manifest builder.

Regime sensitivity forms `early` only from rows whose target and source regimes
are both early, `late` only when both are late, and `mixed` otherwise. Report all
three through `RegimeSubsetInference`; an empty subset has `inference=None` and
`unavailable_reason="no_rows"` rather than a sentinel metric. Nonempty subsets
are independently inferred. Compare early with late only when each contains at
least 20 target UTC days and at least 10 target UTC days with conditionally
eligible moves. `regime_unstable` is true when those two adequate subsets have
opposite MAE-improvement signs or different evidence classes. If either subset
misses either support floor, report `regime_not_adjudicable` without treating
it as a sign reversal or QA failure, and set `regime_unstable=False`. Zero
versus a positive or negative sign is not an opposite-sign result. Task 9
derives `underpowered`, `unit_dependent`, `regime_unstable`, and
`regime_not_adjudicable` only from these typed outputs.

- [ ] **Step 4: Run Tasks 1-4 tests**

Run: `python3 -m pytest research/tests/test_cross_pool_*.py -q`

Expected: all current cross-pool tests pass.

- [ ] **Step 5: Commit paired inference**

```bash
git add research/cross_pool research/tests/test_cross_pool_bootstrap.py
git commit -m "feat: add paired inference for cross-pool forecasts"
```

### Task 5: Add the Reverse-Direction Falsification

**Files:**
- Modify: `research/cross_pool/predictive.py`
- Modify: `research/tests/test_cross_pool_predictive.py`
- Modify: `research/tests/test_cross_pool_bootstrap.py`

**Interfaces:**
- Consumes: the same panel and estimator with `direction="base_to_bsc"`.
- Produces: independently classified Base-to-BSC results.

- [ ] **Step 1: Add a synthetic Base-leading-BSC test**

```python
def test_reverse_projection_recovers_only_base_to_bsc_lead() -> None:
    panel = synthetic_base_leads_bsc_panel()
    reverse = expanding_weekly_predictions(
        panel, WalkForwardConfig(direction="base_to_bsc")
    )
    primary = expanding_weekly_predictions(
        panel, WalkForwardConfig(direction="bsc_to_base")
    )
    reverse_metrics = summarize_predictions(reverse.predictions)
    primary_metrics = summarize_predictions(primary.predictions)
    assert reverse_metrics.mae_improvement > 0
    assert primary_metrics.mae_improvement <= 0
```

- [ ] **Step 2: Run the test and confirm the incomplete projection failure**

Run:
`python3 -m pytest research/tests/test_cross_pool_predictive.py::test_reverse_projection_recovers_only_base_to_bsc_lead -q`

Expected: test fails until target/source features are projected symmetrically.

- [ ] **Step 3: Complete the direction-neutral projection**

Use one feature-builder and one estimator; do not add a second model function.
The gap is always target-minus-source in the projected model.

- [ ] **Step 4: Run predictive and inference tests**

Run:
`python3 -m pytest research/tests/test_cross_pool_predictive.py research/tests/test_cross_pool_bootstrap.py -q`

Expected: both directions pass their synthetic recovery and no-lead tests.

- [ ] **Step 5: Commit the falsification direction**

```bash
git add research/cross_pool/predictive.py \
  research/tests/test_cross_pool_predictive.py \
  research/tests/test_cross_pool_bootstrap.py
git commit -m "feat: add reverse cross-pool falsification"
```

### Task 6: Add the Clustered Event Study

**Files:**
- Create: `research/cross_pool/event_study.py`
- Create: `research/tests/test_cross_pool_event_study.py`
- Modify: `research/cross_pool/contracts.py`

**Interfaces:**
- Consumes: source and target event streams.
- Produces: clustered `ShockEvent`, explicit exclusions, `EventResponse`, and
  bootstrapped `EventSummary` rows.

- [ ] **Step 1: Write first-crossing and response tests**

```python
def test_shock_cluster_retains_first_crossing_only() -> None:
    shocks = detect_shocks(burst_source_events(), ShockConfig())
    assert [shock.timestamp_ms for shock in shocks] == [FIRST_CROSSING_MS]


def test_event_response_uses_target_as_of_not_next_swap() -> None:
    result = measure_event_responses(one_shock(), sparse_target_events(), ShockConfig())
    row = result.responses[0]
    assert row.target_start_timestamp_ms <= row.shock_timestamp_ms
    assert row.target_end_timestamp_ms <= row.shock_timestamp_ms + row.horizon_ms
```

Also pin the exact refractory boundary, shock sign, cross-midnight day grouping,
stale target states, both directions, and paired event-day bootstrap.

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

Run: `python3 -m pytest research/tests/test_cross_pool_event_study.py -q`

Expected: import fails for `research.cross_pool.event_study`.

- [ ] **Step 3: Implement the frozen event-study contract**

```python
@dataclass(frozen=True)
class ShockConfig:
    lookback_ms: int = 900_000
    threshold_bps: float = 5.0
    cluster_ms: int = 900_000
    response_horizons_ms: tuple[int, ...] = (900_000, 3_600_000, 14_400_000)


@dataclass(frozen=True)
class ShockEvent:
    direction: Direction
    source_pool: PoolName
    target_pool: PoolName
    source_start_timestamp_ms: int
    shock_timestamp_ms: int
    source_move_bps: float
    source_move_sign: Literal[-1, 1]
    shock_day_utc: date


@dataclass(frozen=True)
class EventResponse:
    direction: Direction
    source_pool: PoolName
    target_pool: PoolName
    shock_timestamp_ms: int
    shock_day_utc: date
    horizon_ms: int
    source_move_bps: float
    target_start_timestamp_ms: int
    target_end_timestamp_ms: int
    target_response_bps: float
    direction_agrees: bool


@dataclass(frozen=True)
class EventExclusion:
    direction: Direction
    shock_timestamp_ms: int
    shock_day_utc: date
    horizon_ms: int
    reason: Literal["missing_target_start_state", "missing_target_end_state"]


@dataclass(frozen=True)
class EventStudyResult:
    responses: tuple[EventResponse, ...]
    exclusions: tuple[EventExclusion, ...]


@dataclass(frozen=True)
class EventSummary:
    direction: Direction
    horizon_ms: int
    event_count: int
    event_day_count: int
    mean_response_bps: ConfidenceInterval
    median_response_bps: ConfidenceInterval
    direction_agreement: ConfidenceInterval


def detect_shocks(
    source: Sequence[PoolEvent], config: ShockConfig
) -> tuple[ShockEvent, ...]: ...


def measure_event_responses(
    shocks: Sequence[ShockEvent],
    target: Sequence[PoolEvent],
    config: ShockConfig,
) -> EventStudyResult: ...


def bootstrap_event_responses(
    responses: Sequence[EventResponse],
    *,
    resamples: int = 2_000,
    seed: int = 20_260_715,
    confidence_level: float = 0.95,
) -> tuple[EventSummary, ...]: ...
```

Each response requires an as-of target state at the shock and at the requested
endpoint. A start is missing when the shock precedes the target stream; an end
is missing when `shock + horizon` exceeds the target stream's observed end.
Missing endpoints become deterministic exclusion rows rather than zero
responses, extrapolation past the sample, or next-event lookups. Bootstrap
source-shock UTC days within each direction/horizon, using the same sampled day
multiset for the mean, median, and direction-agreement statistics.

- [ ] **Step 4: Run the event-study tests**

Run: `python3 -m pytest research/tests/test_cross_pool_event_study.py -q`

Expected: all event and bootstrap tests pass.

- [ ] **Step 5: Commit the event study**

```bash
git add research/cross_pool/contracts.py research/cross_pool/event_study.py \
  research/tests/test_cross_pool_event_study.py
git commit -m "feat: add cross-pool event study"
```

### Task 7: Add Weekly Band-Constrained DTW and Rotation Nulls

**Files:**
- Create: `research/cross_pool/dtw.py`
- Create: `research/tests/test_cross_pool_dtw.py`
- Modify: `research/cross_pool/contracts.py`

**Interfaces:**
- Consumes: the 15-minute causal panel.
- Produces: `DtwPath`, `DtwWeekResult`, and `DtwNullResult`.

- [ ] **Step 1: Write exact-path and null tests**

```python
def test_banded_dtw_never_escapes_requested_band() -> None:
    path = banded_dtw(shifted_source(), shifted_target(), band_steps=4)
    assert all(abs(source_i - target_i) <= 4 for source_i, target_i in path.matches)


def test_weekly_nulls_use_every_nonzero_day_rotation() -> None:
    nulls = build_day_rotation_nulls(one_complete_week_panel(), DtwConfig())
    assert {row.rotation_days for row in nulls} == {1, 2, 3, 4, 5, 6}
```

Also test known shifted innovations, no-lead data, legal steps, fixed endpoints,
diagonal-first ties, path normalization, zero variance, and partial-week
exclusion.

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

Run: `python3 -m pytest research/tests/test_cross_pool_dtw.py -q`

Expected: import fails for `research.cross_pool.dtw`.

- [ ] **Step 3: Implement banded weekly dynamic programming**

```python
@dataclass(frozen=True)
class DtwConfig:
    grid_ms: int = 900_000
    band_steps: tuple[int, ...] = (1, 4, 16)
    primary_band_steps: int = 4


@dataclass(frozen=True)
class DtwPath:
    matches: tuple[tuple[int, int], ...]
    total_cost: float
    normalized_cost: float
    median_signed_lag_steps: float


@dataclass(frozen=True)
class DtwWeekResult:
    week_start_timestamp_ms: int
    direction: Direction
    band_steps: int
    path_length: int
    normalized_cost: float
    median_signed_lag_steps: float
    matches: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class DtwNullResult:
    week_start_timestamp_ms: int
    direction: Direction
    band_steps: int
    rotation_days: int
    normalized_cost: float
    observed_cost_improvement: float
    median_signed_lag_steps: float
    observed_signed_lag_difference_steps: float


@dataclass(frozen=True)
class DtwStability:
    direction: Direction
    aggregate_median_lag_by_band: Mapping[int, float]
    primary_band_same_sign_week_share: float
    band_unstable: bool


def banded_dtw(
    source: Sequence[float], target: Sequence[float], *, band_steps: int
) -> DtwPath: ...


def evaluate_weekly_dtw(
    panel_15m: Sequence[PanelRow], config: DtwConfig
) -> tuple[DtwWeekResult, ...]: ...


def build_day_rotation_nulls(
    panel_15m: Sequence[PanelRow], config: DtwConfig
) -> tuple[DtwNullResult, ...]: ...


def assess_dtw_stability(rows: Sequence[DtwWeekResult], config: DtwConfig) -> DtwStability: ...
```

Within a path, signed lag is `target_index - source_index`; summarize it by the
median path match. Give each complete UTC week equal weight and define each
band's aggregate lag as the median of weekly medians. The primary band is four
steps because the confirmatory horizon is one hour. Its same-sign share counts
weeks whose nonzero median has the aggregate primary-band sign divided by all
complete weeks. Mark instability when the primary aggregate sign is zero, any
two nonzero band aggregates have opposite signs, or the same-sign share is
below two-thirds.

For each observed week/band, every nonzero whole-day rotation produces one null
row. `observed_cost_improvement` is rotation normalized cost minus observed
normalized cost; `observed_signed_lag_difference_steps` is rotation median lag
minus observed median lag. `dtw_weekly_paths.csv` expands every stored
`DtwWeekResult.matches` pair with its week, direction, and band identifiers.
Reporting also derives the observed ranks for normalized cost and absolute
signed lag among each week's six
rotations plus itself as `1 + count(rotation_cost < observed_cost)`, with
strict comparison and no randomized tie breaking; the lag rank applies the
same rule to absolute median lag.

- [ ] **Step 4: Run the DTW tests**

Run: `python3 -m pytest research/tests/test_cross_pool_dtw.py -q`

Expected: all paths obey their band and all weekly/null tests pass.

- [ ] **Step 5: Commit constrained DTW**

```bash
git add research/cross_pool/contracts.py research/cross_pool/dtw.py \
  research/tests/test_cross_pool_dtw.py
git commit -m "feat: add constrained weekly DTW analysis"
```

### Task 8: Add Market-Structure Diagnostics Without Attribution Claims

**Files:**
- Create: `research/backtester/lp_ledger_attribution.py`
- Create: `research/cross_pool/market_structure.py`
- Create: `research/tests/test_cross_pool_market_structure.py`
- Modify: `research/scripts/report_lp_ledger_attribution.py`
- Modify: `research/tests/test_lp_ledger_attribution_report.py`

**Interfaces:**
- Consumes: canonical Base/BSC replay CSVs and LP ledgers.
- Produces: one `VenueStructureSummary` per pool without serializing owner
  addresses.

- [ ] **Step 1: Write activity, concentration, and privacy tests**

```python
def test_market_structure_reports_activity_and_exact_capital_concentration(
    tmp_path: Path,
) -> None:
    summary = summarize_venue_structure(
        pool="uni-base",
        replay_path=write_replay_fixture(tmp_path),
        ledger_path=write_ledger_fixture(tmp_path),
    )
    assert summary.swap_count == 4
    assert summary.meaningful_move_count == 2
    assert summary.known_owner_count == 2
    assert summary.exact_opening_capital_top_owner_share == Decimal("0.75")
    assert summary.exact_opening_capital_hhi == Decimal("0.625")


def test_serialized_market_structure_never_contains_owner_addresses(
    tmp_path: Path,
) -> None:
    payload = serialize_market_structure(fixture_summaries(tmp_path))
    assert "0x1111111111111111111111111111111111111111" not in payload
```

Also test the inclusive 10-bp meaningful-move threshold, update-gap quantiles,
fee-tier consistency, swap-only volume and active-liquidity summaries, unknown
owners, ambiguous opening attribution, token orientation, missing columns, and
non-finite values.

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

Run:
`python3 -m pytest research/tests/test_cross_pool_market_structure.py -q`

Expected: import fails for `research.cross_pool.market_structure`.

- [ ] **Step 3: Implement the shared row-level ledger boundary**

Move strict ledger column validation, token orientation, normalized-owner
validation, and per-opening USD capital calculation into
`research.backtester.lp_ledger_attribution`. Its public `LedgerAttributionRow`
values retain owners only in memory. Update the existing attribution report to consume
that module so the capital formula has one source of truth. A known owner is a
nonzero 20-byte hexadecimal address; normalize it to lowercase. Empty, malformed,
or zero addresses are unknown rather than silently grouped together.

```python
AttributionClass: TypeAlias = Literal["exact", "ambiguous", "other"]


@dataclass(frozen=True)
class LedgerAttributionRow:
    pool: str
    block_number: int
    tx_hash: str
    token_id: int
    event_type: str
    owner: str | None
    liquidity_delta: Decimal
    amount0_actual: Decimal
    amount1_actual: Decimal
    cngn_usd_price: Decimal
    raw_attribution_status: str
    attribution_class: AttributionClass
    opening_capital_usd: Decimal | None


def opening_capital_usd(
    pool: str,
    *,
    amount0_actual: Decimal,
    amount1_actual: Decimal,
    cngn_usd_price: Decimal,
) -> Decimal: ...


def load_ledger_attribution_rows(
    pool: str, path: Path
) -> tuple[LedgerAttributionRow, ...]: ...
```

Map every `amount_attribution_status` beginning with `ambiguous` to the typed
`ambiguous` class, exact to `exact`, and all other closing/zero-delta statuses to
`other`, while retaining the raw status for the existing aggregate report. Set
`opening_capital_usd` only for positive-liquidity exact rows. Reject negative
actual amounts, a non-positive price on an exact opening, a positive-liquidity
row with an unsupported attribution status, or unknown pool orientation.

- [ ] **Step 4: Implement strict replay and venue summaries**

```python
@dataclass(frozen=True)
class DistributionSummary:
    count: int
    minimum: Decimal
    median: Decimal
    p95: Decimal
    p99: Decimal
    maximum: Decimal


@dataclass(frozen=True)
class VenueStructureSummary:
    pool: PoolName
    swap_count: int
    meaningful_move_count: int
    fee_rate: Decimal
    update_gaps_ms: DistributionSummary
    active_liquidity: DistributionSummary
    volume_usd: DistributionSummary
    total_volume_usd: Decimal
    ledger_rows: int
    opening_count: int
    exact_opening_count: int
    ambiguous_opening_count: int
    known_owner_count: int
    exact_unknown_owner_opening_count: int
    exact_opening_capital_usd: Decimal
    exact_known_owner_capital_usd: Decimal
    exact_known_owner_capital_coverage: Decimal
    ambiguous_opening_liquidity_share: Decimal
    exact_opening_capital_top_owner_share: Decimal
    exact_opening_capital_top_three_share: Decimal
    exact_opening_capital_hhi: Decimal


def summarize_venue_structure(
    *, pool: PoolName, replay_path: Path, ledger_path: Path
) -> VenueStructureSummary: ...


def serialize_market_structure(rows: Sequence[VenueStructureSummary]) -> str: ...
```

Meaningful moves are consecutive canonical `sqrt_price_x96` mids whose
absolute log change is at least 10 bps. Update gaps are computed across swap
timestamps. Active-liquidity and `amount_usd` distributions use swap rows only;
fee rates must be constant within each pool or fail closed.

For owner diagnostics, count normalized known owners but never emit their
addresses or stable address-derived labels. Concentration uses only
positive-liquidity rows with `amount_attribution_status == "exact"` and known
owners, aggregating repeated openings by normalized owner before computing top
shares and HHI. `exact_known_owner_capital_coverage` divides exact capital with
a known owner by all exact opening capital. `ambiguous_opening_liquidity_share`
divides ambiguous opening liquidity by exact plus ambiguous opening liquidity;
ambiguous capital is never imputed. Quantiles use deterministic type-7 linear
interpolation on sorted values; a distribution with no observations fails
closed.

- [ ] **Step 5: Run the focused tests**

Run:
`python3 -m pytest research/tests/test_cross_pool_market_structure.py research/tests/test_lp_ledger_attribution_report.py -q`

Expected: every activity, capital-accounting, and privacy test passes.

- [ ] **Step 6: Commit market-structure diagnostics**

```bash
git add research/backtester/lp_ledger_attribution.py \
  research/cross_pool/market_structure.py \
  research/scripts/report_lp_ledger_attribution.py \
  research/tests/test_cross_pool_market_structure.py \
  research/tests/test_lp_ledger_attribution_report.py
git commit -m "feat: add cross-pool market structure diagnostics"
```

### Task 9: Add Deterministic Reporting, Figures, and the CLI

**Files:**
- Create: `research/cross_pool/article_manifest.schema.json`
- Create: `research/cross_pool/manifest.py`
- Create: `research/cross_pool/reporting.py`
- Create: `research/cross_pool/figures.py`
- Create: `research/scripts/run_cross_pool_lead_lag.py`
- Create: `research/tests/test_cross_pool_reporting.py`

**Interfaces:**
- Consumes: all statistical results and the approved manifest schema.
- Produces: the complete ignored statistical artifact set and
  `article_manifest.json` with status `generated_unreviewed` or `qa_blocked`.

- [ ] **Step 1: Write stable serialization and manifest tests**

```python
def test_reporting_is_byte_stable_for_identical_inputs(tmp_path: Path) -> None:
    first = run_fixture_pipeline(tmp_path / "first")
    second = run_fixture_pipeline(tmp_path / "second")
    assert artifact_hashes(first) == artifact_hashes(second)


def test_generated_code_cannot_mark_manifest_reviewed() -> None:
    with pytest.raises(
        CrossPoolContractError, match="generated code cannot mark evidence reviewed"
    ):
        build_article_manifest(fixture_results(), artifact_status="reviewed")


@pytest.mark.parametrize(
    ("qa_status", "primary", "reverse", "robustness", "expected"),
    publication_decision_cases(),
)
def test_publication_branch_uses_frozen_decision_table(
    qa_status: QaStatus,
    primary: EvidenceClass,
    reverse: EvidenceClass,
    robustness: RobustnessStatus,
    expected: ArticleBranch,
) -> None:
    assert select_article_branch(qa_status, primary, reverse, robustness) == expected
```

Also test stable CSV fields, sorted JSON keys, no NaN/Infinity, manifest artifact
coverage, nonempty figures, QA-blocked failure behavior, every publication
branch, every economic branch and equality boundary, and JSON-Schema rejection
after deleting or mistyping every required group. Prove that an underpowered,
unit-dependent, or regime-unstable reverse audit blocks a positive primary from
selecting the one-way BSC-to-Base branch.

- [ ] **Step 2: Implement the typed manifest and decision boundary**

```python
QaStatus: TypeAlias = Literal["pass", "blocked"]
GeneratedArtifactStatus: TypeAlias = Literal["generated_unreviewed", "qa_blocked"]
RobustnessFlag: TypeAlias = Literal[
    "underpowered",
    "unit_dependent",
    "regime_unstable",
    "regime_not_adjudicable",
    "dtw_band_unstable",
]
ArticleBranch: TypeAlias = Literal[
    "bsc_to_base_incremental",
    "base_to_bsc_incremental",
    "bidirectional_incremental_no_unique_leader",
    "no_material_incremental_lead",
    "leadership_unresolved",
    "not_adjudicable_qa",
]
EconomicClass: TypeAlias = Literal[
    "unavailable",
    "pareto_improvement",
    "return_risk_tradeoff",
    "no_net_return_improvement",
    "not_adjudicable_qa",
]


@dataclass(frozen=True)
class RobustnessStatus:
    flags: tuple[RobustnessFlag, ...]

    @property
    def blocks_directional_claim(self) -> bool: ...


@dataclass(frozen=True)
class StatisticalManifestInput:
    qa_status: QaStatus
    primary_predictive_audit: DirectionalPredictiveAudit
    reverse_predictive_audit: DirectionalPredictiveAudit
    primary_dtw_stability: DtwStability
    reverse_dtw_stability: DtwStability
    provenance: Mapping[str, JsonValue]
    qa: Mapping[str, JsonValue]
    robustness: Mapping[str, JsonValue]
    predictive: Mapping[str, JsonValue]
    event_study: Mapping[str, JsonValue]
    dtw: Mapping[str, JsonValue]
    market_structure: Mapping[str, JsonValue]
    figures: Mapping[str, JsonValue]
    artifacts: Mapping[str, str]


@dataclass(frozen=True)
class EconomicDecisionMetrics:
    inputs_valid: bool
    original_net_return: Decimal
    gated_net_return: Decimal
    original_worst_window_return: Decimal
    gated_worst_window_return: Decimal
    original_worst_drawdown: Decimal
    gated_worst_drawdown: Decimal


@dataclass(frozen=True)
class EconomicManifestInput:
    economics: Mapping[str, JsonValue]
    decision_metrics: EconomicDecisionMetrics
    lp_performance_figure: Mapping[str, JsonValue]
    artifacts: Mapping[str, str]


def select_article_branch(
    qa_status: QaStatus,
    primary: EvidenceClass,
    reverse: EvidenceClass,
    robustness: RobustnessStatus,
) -> ArticleBranch: ...


def derive_robustness_status(
    primary: DirectionalPredictiveAudit,
    reverse: DirectionalPredictiveAudit,
    *,
    primary_dtw: DtwStability,
    reverse_dtw: DtwStability,
) -> RobustnessStatus: ...


def claims_for_article_branch(
    branch: ArticleBranch,
) -> tuple[tuple[str, ...], tuple[str, ...]]: ...


def select_economic_class(
    metrics: EconomicDecisionMetrics,
) -> EconomicClass: ...


def build_article_manifest(
    inputs: StatisticalManifestInput,
    *,
    artifact_status: GeneratedArtifactStatus = "generated_unreviewed",
) -> dict[str, JsonValue]: ...


def merge_economic_manifest(
    manifest: Mapping[str, JsonValue], inputs: EconomicManifestInput
) -> dict[str, JsonValue]: ...


def validate_article_manifest(payload: Mapping[str, JsonValue]) -> None: ...


def load_and_validate_article_manifest(path: Path) -> dict[str, JsonValue]: ...
```

`JsonValue` is the recursive JSON union of null, bool, int, finite float, str,
lists, and string-keyed mappings; validation rejects non-finite floats before
schema evaluation. `RobustnessStatus.flags` is sorted and unique. Directional
claims are blocked by every flag except `regime_not_adjudicable` alone.
`select_economic_class` applies the spec's ordered, mutually exclusive
predicates and has table-driven tests for equality on each metric before the
economic runner depends on it.

`derive_robustness_status()` forms the union of both directional audits:
`underpowered`, `unit_dependent`, `regime_unstable`, and
`regime_not_adjudicable` are present when either the primary or reverse audit
reports them, while `dtw_band_unstable` is present when either typed DTW
stability result reports it. It
validates BSC-to-Base primary and Base-to-BSC reverse direction, a one-hour
`3_600_000`-millisecond horizon throughout every nested provenance field, and
the frozen bootstrap tuple `(2000, 20260715, 0.95)`. It also validates matching
primary/reverse directions on the DTW results. A reverse robustness failure
therefore blocks a one-way primary claim even though the reverse evidence class
is merely `inconclusive`.

`build_article_manifest` derives both evidence classes, the robustness status,
`publication.article_branch`, and both claim arrays from the two typed audits;
callers cannot supply publication outcomes or pre-aggregated predictive flags.
It cross-checks those typed values against the corresponding QA, robustness,
and predictive mapping fields. `merge_economic_manifest` derives the economic
class from `decision_metrics` and cross-checks those metrics against the
serialized economics group; callers cannot supply an economic class.
`validate_article_manifest` recomputes both decision tables and the claim arrays
after JSON-Schema validation and rejects any mismatch. Tests mutate each branch,
class, and claim array in an otherwise schema-valid payload and require a
contract error.

The bundled Draft 2020-12 schema is authoritative for every nested field. Set
`additionalProperties: false` on every object. Require explicit status and
unit-bearing aggregate properties in each result group, six named input hashes
and intervals plus a required runtime-environment object in provenance, the
full artifact map, all five figure slots,
publication claim arrays, and review identity/time nullability conditioned on
artifact status. Use schema `if`/`then` branches for valid statistical pending,
complete economic, generated QA-blocked, reviewed data-valid, and reviewed
QA-blocked shapes. `merge_economic_manifest` may change only `economics`,
`publication.economic_class`, `figures.lp_performance`, and the economic entries
in `artifacts`; it validates the full result before returning.

- [ ] **Step 3: Run the tests and confirm missing reporting modules**

Run: `python3 -m pytest research/tests/test_cross_pool_reporting.py -q`

Expected: imports fail for reporting, figures, and CLI orchestration.

- [ ] **Step 4: Implement deterministic outputs**

Write under `research/results/cross_pool_lead_lag/`:

```text
data_quality.json
market_structure.json
panel_15m.csv
panel_1h.csv
panel_4h.csv
predictive_predictions.csv
predictive_metrics.json
event_study.csv
dtw_weekly_paths.csv
dtw_nulls.csv
statistical_report.md
price_gap.png
predictive_performance.png
event_response.png
dtw_lag.png
article_manifest.json
```

Do not serialize wall-clock generation time. Provenance uses schema version,
code commit, complete configuration, input SHA-256 values, input intervals,
artifact hashes, and runtime fields: Python and NumPy versions, SHA-256 of the
`numpy.show_config(mode="dicts")` value serialized by
`json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
allow_nan=False).encode("utf-8")` with no trailing newline, machine
architecture, byte order, bit generator `PCG64`, and draw dtype `int64`.
Reporting tests claim byte stability only when these runtime fields match.
Review time is added only by the later evidence-review gate.

`manifest.py` is the single writer-facing contract used by statistical
reporting and the later economic merge. Validate with a bundled JSON Schema
using `jsonschema.Draft202012Validator` before every write. The complete valid
statistical manifest contains:

```text
schema_version: "1.0.0"
artifact_status: generated_unreviewed
provenance: code_commit, six input hashes, six input intervals, full config,
            Python/NumPy/build/machine/byte-order/PCG64/int64 runtime fields
qa: status="pass", reasons=[], causal audit counts
robustness: status, flags, support counts, influence and regime audits
predictive: status="complete", primary and reverse metrics/intervals/classes
event_study: status="complete", aggregate summaries and exclusion counts
dtw: status="complete", aggregate bands, null ranks, stability
market_structure: status="complete", aggregate venue summaries
economics: status="pending"
publication: article branch, economic_class="unavailable", allowed/forbidden claims
figures: price_gap, event_response, dtw_lag, diagnostic_predictive_performance,
         lp_performance=null
artifacts: every emitted relative filename and SHA-256 except the manifest itself
review: status="pending", reviewed_by=null, reviewed_at_utc=null
```

A generated `qa_blocked` manifest retains schema version, available provenance,
`qa.status="blocked"` with nonempty reasons, unavailable result-group statuses,
both publication classes `not_adjudicable_qa`, the complete forbidden-claim
set, no allowed performance claims, and pending review metadata. It emits no
partial performance artifacts.

`select_article_branch` first returns `not_adjudicable_qa` for blocked QA and
`leadership_unresolved` for underpowered, unit-dependent, regime-unstable, or
DTW-band-unstable evidence. Otherwise it maps primary/reverse positive evidence
to the corresponding one-way or bidirectional branch, maps two affirmative
nulls to `no_material_incremental_lead`, and maps every remaining combination
to `leadership_unresolved`.

The three statistical article figures are `price_gap`, `event_response`, and
`dtw_lag`. `predictive_performance.png` is a diagnostic figure, not part of the
declared four-figure article set. The statistical manifest remains explicitly
economics-pending until the economic runner adds `lp_performance` and the
economic class.

- [ ] **Step 5: Implement the thin CLI and exit behavior**

```python
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_analysis(args)
    except CrossPoolContractError as exc:
        write_qa_blocked_manifest(args.out_dir, exc)
        return 2
    return 0
```

Contract or causal-QA errors emit only a `qa_blocked` manifest and exit 2.
Valid inconclusive or robustness-stopped analyses write diagnostics, use
`generated_unreviewed`, and exit 0.

- [ ] **Step 6: Run all statistical tests and static checks**

```bash
python3 -m pytest research/tests/test_cross_pool_*.py -q
python3 -m ruff check research/cross_pool \
  research/scripts/run_cross_pool_lead_lag.py research/tests/test_cross_pool_*.py
python3 -m mypy --strict research/cross_pool \
  research/scripts/run_cross_pool_lead_lag.py
python3 -m py_compile research/cross_pool/*.py \
  research/scripts/run_cross_pool_lead_lag.py
git diff --check
```

Expected: every command exits zero.

- [ ] **Step 7: Commit the statistical reporting surface**

```bash
git add research/cross_pool research/scripts/run_cross_pool_lead_lag.py \
  research/tests/test_cross_pool_reporting.py
git commit -m "feat: report cross-pool leadership analysis"
```

The CLI requires `--base-replay`, `--bsc-replay`, `--base-ledger`, and
`--bsc-ledger` in addition to the two feature tables. The report presents
market-structure diagnostics after performance results and labels owner
concentration as post hoc and non-causal.

### Task 10: Run the Real Statistical Experiment Once

**Files:**
- Generate only: `research/results/cross_pool_lead_lag/**`

**Interfaces:**
- Consumes: the two canonical feature CSVs, two canonical replay CSVs, and two
  LP ledgers.
- Produces: deterministic unreviewed statistical evidence for the economic and
  article plans.

- [ ] **Step 1: Confirm the frozen inputs exist**

```bash
test -f research/data/derived/uni_base_pool_features.csv
test -f research/data/derived/uni_bsc_pool_features.csv
test -f research/data/derived/uni_base_pool_history_replay.csv
test -f research/data/derived/uni_bsc_pool_history_replay.csv
test -f research/data/derived/uni_base_lp_ledger.csv
test -f research/data/derived/uni_bsc_lp_ledger.csv
```

Expected: all six commands exit zero.

- [ ] **Step 2: Run the full statistical pipeline**

```bash
python3 research/scripts/run_cross_pool_lead_lag.py \
  --base-features research/data/derived/uni_base_pool_features.csv \
  --bsc-features research/data/derived/uni_bsc_pool_features.csv \
  --base-replay research/data/derived/uni_base_pool_history_replay.csv \
  --bsc-replay research/data/derived/uni_bsc_pool_history_replay.csv \
  --base-ledger research/data/derived/uni_base_lp_ledger.csv \
  --bsc-ledger research/data/derived/uni_bsc_lp_ledger.csv \
  --out-dir research/results/cross_pool_lead_lag
```

Expected: exit 0 for valid evidence, including an inconclusive outcome; exit 2
only for a contract or causal-QA failure.

- [ ] **Step 3: Re-run and compare artifact hashes**

Run the same command again and compare the artifact hashes recorded in
`article_manifest.json`.

Expected: every non-manifest artifact hash is unchanged.

- [ ] **Step 4: Stop before article claims**

Confirm `article_manifest.json` still contains
`"artifact_status": "generated_unreviewed"`. Do not change it to `reviewed`
inside the executable or as part of this task.
