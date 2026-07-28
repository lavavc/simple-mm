# Cross-Pool Economic and Portfolio Integration Implementation Plan

> **Execution complete 2026-07-27:** The frozen economic classification is
> `no_net_return_improvement`. Preserve this plan as implementation provenance;
> the weighted-portfolio successor is the July 24 v4 contract.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a causal BSC-forecast entry veto for the frozen Base
`upside_tight_v1` policy while preserving legacy single-sleeve numerics, joint
portfolio accounting, frozen allocations, and fail-closed liquidity caps.

**Architecture:** A cross-pool forecast timeline implements a generic
backtester entry-eligibility protocol. The runtime consults the overlay only
after intrinsic entry conditions pass and before sizing or range construction.
A separate economic runner evaluates the frozen Base counterfactual, while the
parameter-portfolio harness records cap-invalid rule/window outcomes without
aborting unrelated rules.

**Tech Stack:** Python 3.11+, frozen dataclasses, protocols, bisect-based causal
lookup, existing LP runtimes and CLMM math, CSV/JSON/Markdown artifacts, and
pytest.

## Global Constraints

- Selected policy is `directional:upside_tight_v1`.
- Evaluation windows are exactly 3-25; windows 0-2 are excluded, never
  zero-filled.
- Frozen routed-active windows are 7 `upside_capture`, 18 `fee_box`, 23
  `upside_capture`, and 25 `dip_accumulator`.
- Static comparator is `static_spot_w0025`.
- Upside requires forecast `> +5` bps, dip requires `< -5` bps, and fee-box is
  inclusive `[-5, +5]` bps.
- The overlay is veto-only. It cannot change routing, training eligibility,
  weights, sizing, exits, open positions, or market-event replay.
- Denied policy capital stays in that runtime wallet; never redistribute it.
- Preserve the 10% sleeve cap, 35% family cap, and 10% aggregate synthetic
  active-liquidity cap.
- The primary economic test uses `simulate_pool()` because the original and
  gated policies are mutually exclusive counterfactuals. The same generic
  overlay is propagated through `simulate_portfolio()` for later sensitivity.
- Generated results remain under ignored `research/results/**`.
- Do not remove or weaken any `.gitignore` rule.

---

## File Structure

- `research/backtester/entry_eligibility.py`: generic entry decision protocol.
- `research/cross_pool/forecast.py`: hourly timeline and archetype agreement.
- `research/backtester/position_runtime.py`: consult the overlay at entry only.
- `research/backtester/simulator.py`: preserve the single-sleeve wrapper.
- `research/backtester/portfolio_simulator.py`: propagate overlays by sleeve ID.
- `research/scripts/evaluate_cross_pool_economic_lp.py`: frozen economic run.
- `research/scripts/evaluate_parameter_portfolio.py`: explicit invalid outcomes.
- `research/cross_pool/manifest.py`: shared manifest validation, economic
  classification, and deterministic merge.

### Task 1: Add the Generic Eligibility Protocol and Forecast Timeline

**Files:**
- Create: `research/backtester/entry_eligibility.py`
- Create: `research/cross_pool/forecast.py`
- Create: `research/tests/test_entry_eligibility.py`

**Interfaces:**
- Consumes: `EntryContext` and statistical `PredictionRow` CSV fields.
- Produces: `EntryEligibilityOverlay`, `ForecastTimeline`, and
  `ForecastAgreementOverlay`.

- [ ] **Step 1: Write timeline and threshold truth-table tests**

```python
@pytest.mark.parametrize(
    "archetype,forecast_bps,eligible",
    [
        ("upside_capture", 5.0, False),
        ("upside_capture", 6.0, True),
        ("dip_accumulator", -5.0, False),
        ("dip_accumulator", -6.0, True),
        ("fee_box", -5.0, True),
        ("fee_box", 0.0, True),
        ("fee_box", 5.0, True),
        ("fee_box", 6.0, False),
    ],
)
def test_forecast_agreement_thresholds(
    archetype: DirectionalArchetype, forecast_bps: float, eligible: bool
) -> None:
    overlay = ForecastAgreementOverlay(archetype, one_point_timeline(forecast_bps))
    assert overlay.evaluate(entry_context_at(HOUR_1)).eligible is eligible


def test_timeline_rejects_future_missing_and_stale_forecasts() -> None:
    timeline = one_point_timeline(6.0)
    with pytest.raises(ValueError, match="no forecast at or before"):
        timeline.as_of(HOUR_0)
    with pytest.raises(ValueError, match="forecast is stale"):
        timeline.as_of(HOUR_2)
```

Also reject naive or non-UTC datetimes, duplicate/non-monotonic origins,
non-hour-aligned origins, non-finite forecasts, horizons other than one hour,
and `refit_time > origin_time`.

- [ ] **Step 2: Run the tests and confirm missing modules**

Run: `python3 -m pytest research/tests/test_entry_eligibility.py -q`

Expected: import fails for the new eligibility modules.

- [ ] **Step 3: Implement the generic protocol**

```python
@dataclass(frozen=True)
class EntryEligibilityDecision:
    eligible: bool
    reason: Literal["agreement", "disagreement", "unconditional"]


class EntryEligibilityOverlay(Protocol):
    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision: ...


@dataclass(frozen=True)
class AlwaysEligibleOverlay:
    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
        return EntryEligibilityDecision(True, "unconditional")
```

- [ ] **Step 4: Implement the cross-pool forecast contract**

```python
DirectionalArchetype: TypeAlias = Literal[
    "upside_capture", "dip_accumulator", "fee_box"
]


@dataclass(frozen=True)
class ForecastPoint:
    origin_time: datetime
    refit_time: datetime
    horizon: timedelta
    forecast_bps: float


@dataclass(frozen=True)
class ForecastTimeline:
    points: tuple[ForecastPoint, ...]

    def as_of(self, decision_time: datetime) -> ForecastPoint: ...


@dataclass(frozen=True)
class ForecastAgreementOverlay:
    routed_archetype: DirectionalArchetype
    timeline: ForecastTimeline
    threshold_bps: float = 5.0

    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision: ...
```

Use `bisect_right` over origins. The selected point must satisfy
`origin_time <= decision_time < origin_time + one hour`.

- [ ] **Step 5: Run the focused tests**

Run: `python3 -m pytest research/tests/test_entry_eligibility.py -q`

Expected: all timeline, causality, and threshold tests pass.

- [ ] **Step 6: Commit the eligibility contract**

```bash
git add research/backtester/entry_eligibility.py research/cross_pool/forecast.py \
  research/tests/test_entry_eligibility.py
git commit -m "feat: add forecast entry eligibility contract"
```

### Task 2: Integrate Eligibility Without Changing Single-Sleeve Numerics

**Files:**
- Modify: `research/backtester/position_runtime.py:70-90,406-455,659-674`
- Modify: `research/backtester/simulator.py:1388-1407`
- Modify: `research/tests/test_position_runtime.py`
- Modify: `research/tests/test_backtester.py`

**Interfaces:**
- Consumes: an optional `EntryEligibilityOverlay`.
- Produces: overlay-aware `SleeveRuntime`, `create_sleeve_runtime`, and
  `simulate_pool`.

- [ ] **Step 1: Add parity and entry-only behavior tests**

```python
def test_always_eligible_overlay_matches_legacy_simulation_exactly() -> None:
    expected = simulate_pool(events(), params(), UNISWAP_BASE_POOL, 500.0)
    actual = simulate_pool(
        events(),
        params(),
        UNISWAP_BASE_POOL,
        500.0,
        entry_eligibility=AlwaysEligibleOverlay(),
    )
    assert actual == expected


def test_denial_blocks_entry_without_wallet_cost_or_cooldown_mutation() -> None:
    @dataclass(frozen=True)
    class NeverEligibleOverlay:
        def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
            return EntryEligibilityDecision(False, "disagreement")

    runtime = ready_runtime(entry_eligibility=NeverEligibleOverlay())
    before = deepcopy(runtime)
    assert runtime.propose_entry(entry_event(), ACTIVE_LIQUIDITY) is None
    assert runtime.wallet == before.wallet
    assert runtime.result == before.result
    assert runtime.cooldown_until_time is None
```

Also test that later disagreement does not exit an open position and re-entry
after a normal exit checks the latest forecast again.

- [ ] **Step 2: Run the new tests and confirm signature/behavior failures**

Run:
`python3 -m pytest research/tests/test_position_runtime.py -k "eligibility or overlay" -q`

Expected: constructor or keyword failures, followed by an entry despite denial.

- [ ] **Step 3: Add the optional runtime and wrapper parameters**

```python
@dataclass
class SleeveRuntime:
    # Existing fields remain unchanged.
    entry_eligibility: EntryEligibilityOverlay | None = None


def create_sleeve_runtime(
    *,
    sleeve_id: str,
    params: BacktestParams,
    pool_config: PoolConfig,
    capital_usd: float,
    sizing_policy: SizingPolicy | None = None,
    entry_eligibility: EntryEligibilityOverlay | None = None,
    idle_apr: float = 0.0,
) -> SleeveRuntime: ...
```

Extend `simulate_pool()` with the same optional keyword. In `propose_entry()`,
construct one `EntryContext` after intrinsic filters pass. Return `None` on an
overlay denial before sizing, range construction, transaction costs, cooldown,
or mutation. Exits never consult the overlay.

- [ ] **Step 4: Run parity and regression tests**

```bash
python3 -m pytest research/tests/test_position_runtime.py \
  research/tests/test_backtester.py -q
```

Expected: all existing and new tests pass with exact legacy parity.

- [ ] **Step 5: Commit the runtime integration**

```bash
git add research/backtester/position_runtime.py research/backtester/simulator.py \
  research/tests/test_position_runtime.py research/tests/test_backtester.py
git commit -m "feat: gate sleeve entries with causal eligibility"
```

### Task 3: Propagate Overlays Through Joint Portfolio Simulation

**Files:**
- Modify: `research/backtester/portfolio_simulator.py:263-300`
- Modify: `research/tests/test_portfolio_simulator.py`
- Modify: `research/tests/test_evaluate_parameter_portfolio.py`

**Interfaces:**
- Consumes: overlays keyed by allocator-facing sleeve ID.
- Produces: overlay-aware `simulate_portfolio` without changing allocation.

- [ ] **Step 1: Add joint-accounting overlay tests**

```python
def test_denied_sleeve_keeps_budget_in_runtime_wallet() -> None:
    @dataclass(frozen=True)
    class NeverEligibleOverlay:
        def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
            return EntryEligibilityDecision(False, "disagreement")

    result = simulate_portfolio(
        events=events(),
        sleeves=sleeves(),
        allocation=allocation(),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        entry_overlays_by_sleeve={"directional:upside_tight_v1": NeverEligibleOverlay()},
    )
    assert result.attribution["directional:upside_tight_v1"].final_value == pytest.approx(50.0)
    assert result.cash_value == pytest.approx(500.0 * allocation().cash_weight)
```

Also pin exact parity for always-eligible overlays, unknown overlay IDs, normal
fee sharing and netting among allowed sleeves, unchanged weights/caps, and the
inability to promote an original `no_position` route.

- [ ] **Step 2: Run the tests and confirm the new keyword fails**

Run:
`python3 -m pytest research/tests/test_portfolio_simulator.py -k "overlay or eligibility" -q`

Expected: `simulate_portfolio()` rejects `entry_overlays_by_sleeve`.

- [ ] **Step 3: Add overlay propagation**

```python
def simulate_portfolio(
    *,
    events: Sequence[Event],
    sleeves: Sequence[SleeveDefinition],
    allocation: Allocation,
    pool_config: PoolConfig,
    bankroll_usd: float,
    initial_pool_state: PoolState | None = None,
    entry_overlays_by_sleeve: Mapping[str, EntryEligibilityOverlay] | None = None,
) -> PortfolioResult: ...
```

Reject overlay IDs absent from the routed sleeve set. Pass each overlay only to
the runtime that retains that allocator-facing policy ID.

- [ ] **Step 4: Run portfolio accounting regressions**

Run: `python3 -m pytest research/tests/test_portfolio_simulator.py -q`

Expected: overlay tests and existing fee, netting, cap, and conservation tests
all pass.

- [ ] **Step 5: Commit joint propagation**

```bash
git add research/backtester/portfolio_simulator.py \
  research/tests/test_portfolio_simulator.py \
  research/tests/test_evaluate_parameter_portfolio.py
git commit -m "feat: propagate entry overlays through joint simulation"
```

### Task 4: Record Liquidity-Cap Failures Per Rule and Window

**Files:**
- Modify: `research/backtester/portfolio_simulator.py:30-38,107-116`
- Modify: `research/scripts/evaluate_parameter_portfolio.py:65-73,116-171,300-440`
- Modify: `research/tests/test_portfolio_simulator.py`
- Modify: `research/tests/test_evaluate_parameter_portfolio.py`

**Interfaces:**
- Consumes: the existing unchanged 10% cap.
- Produces: typed valid or invalid `PortfolioRuleOutcome` rows.

- [ ] **Step 1: Add fail-closed continuation tests**

```python
def test_invalid_equal_config_does_not_abort_other_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = evaluate_window_with_one_cap_failure(monkeypatch)
    assert outcomes.rule_outcomes["equal_config"].status == "invalid_liquidity_cap"
    assert outcomes.rule_outcomes["equal_family"].status == "valid"
    assert outcomes.rule_outcomes["shrinkage"].status == "valid"


def test_invalid_portfolio_row_keeps_metrics_blank() -> None:
    row = invalid_portfolio_artifact_row()
    assert row["status"] == "invalid_liquidity_cap"
    assert row["net_return"] == ""
    assert row["observed_share"] == "0.118129"
    assert row["cap"] == "0.10"
```

Also verify unrelated exceptions still propagate and PBO refuses any rule with
an incomplete cap-invalid matrix rather than filtering windows. Add the same
regression for best-sleeve-removal sensitivity: one cap-invalid removal must
produce an explicit invalid sensitivity row while later removals continue.

- [ ] **Step 2: Run the tests and confirm the current whole-window abort**

Run:
`python3 -m pytest research/tests/test_evaluate_parameter_portfolio.py -k "liquidity_cap or invalid_rule" -q`

Expected: the first `LiquidityShareExceeded` aborts evaluation.

- [ ] **Step 3: Add typed cap evidence and rule outcomes**

```python
class LiquidityShareExceeded(ValueError):
    def __init__(self, observed_share: float, cap: float) -> None:
        self.observed_share = observed_share
        self.cap = cap
        super().__init__(
            f"aggregate synthetic liquidity share {observed_share:.6f} exceeds {cap:.2f}"
        )


@dataclass(frozen=True)
class PortfolioRuleOutcome:
    allocation: Allocation
    status: Literal["valid", "invalid_liquidity_cap"]
    result: PortfolioResult | None
    observed_share: float | None
    cap: float | None
```

Replace `WindowEvaluation.allocations` and `.portfolio_results` with one
`rule_outcomes` mapping. Catch only `LiquidityShareExceeded` around each rule.
Continue comparator evaluation and later rules. Best-sleeve-removal
re-simulations use the same `PortfolioRuleOutcome` contract and narrow catch;
they cannot bypass the rule/window validity surface.

- [ ] **Step 4: Serialize explicit invalid rows and PBO status**

`portfolio_validation_matrix.csv` includes every rule/window. Invalid rows
retain rule, window, attempted frozen weights, status, observed share, and cap;
performance fields are empty. `pbo_allocation_rules.json` reports
`invalid_incomplete_matrix` plus invalid window indexes for the affected rule.
The best-sleeve-removal artifact likewise retains the removed sleeve, attempted
weights, status, observed share, and cap with blank performance fields.

- [ ] **Step 5: Run focused and complete portfolio tests**

```bash
python3 -m pytest research/tests/test_portfolio_simulator.py \
  research/tests/test_evaluate_parameter_portfolio.py -q
```

Expected: invalid rules are explicit, remaining rules complete, and no cap is
relaxed.

- [ ] **Step 6: Commit fail-closed reporting**

```bash
git add research/backtester/portfolio_simulator.py \
  research/scripts/evaluate_parameter_portfolio.py \
  research/tests/test_portfolio_simulator.py \
  research/tests/test_evaluate_parameter_portfolio.py
git commit -m "fix: report portfolio cap failures per rule"
```

### Task 5: Add the Frozen Base Economic Runner

**Files:**
- Create: `research/scripts/evaluate_cross_pool_economic_lp.py`
- Create: `research/tests/test_evaluate_cross_pool_economic_lp.py`
- Verify/modify: `research/cross_pool/manifest.py`
- Reuse: `research/scripts/evaluate_directional_paper_lp.py`
- Reuse: `research/scripts/evaluate_frozen_family_lp.py`

**Interfaces:**
- Consumes: `predictive_predictions.csv`, current Base event history, existing
  entry states, and frozen directional/static constructors.
- Produces: paired original/gated/comparator window rows, aggregate metrics,
  the LP-performance figure, and a deterministic merge into the unreviewed
  article manifest.

- [ ] **Step 1: Write the frozen identity and exclusion tests**

```python
def test_frozen_economic_windows_and_routes_are_unchanged() -> None:
    plan = build_frozen_economic_plan(current_base_entry_states())
    assert plan.excluded_windows == (0, 1, 2)
    assert plan.evaluation_windows == tuple(range(3, 26))
    assert plan.active_routes == {
        7: "upside_capture",
        18: "fee_box",
        23: "upside_capture",
        25: "dip_accumulator",
    }
    assert plan.entry_state_contract_sha256 == FROZEN_ENTRY_STATE_SHA256
    assert plan.parameter_contract_sha256 == FROZEN_PARAMETER_SHA256
    assert plan.mint_gas_usd == 0.073
    assert plan.remove_gas_usd == 0.022


def test_prediction_loader_selects_only_primary_hourly_cross_forecast(tmp_path: Path) -> None:
    timeline = load_primary_forecast_timeline(write_predictions(tmp_path))
    assert all(point.horizon == timedelta(hours=1) for point in timeline.points)
```

Also require every window 3-25 exactly once per comparator, original/gated
parameter equality, disagreement-as-cash, missing forecast failure, and exact
aggregate reconciliation. Add tests that reject a prediction CSV whose SHA-256
differs from the statistical manifest, reject a frozen entry/parameter hash
mismatch, preserve every parsed statistical manifest group during the economic
merge, keep `generated_unreviewed`, and cover every economic classification
branch.

- [ ] **Step 2: Run the tests and confirm the missing-runner failure**

Run:
`python3 -m pytest research/tests/test_evaluate_cross_pool_economic_lp.py -q`

Expected: import fails for the new runner.

- [ ] **Step 3: Implement frozen constants and input validation**

```python
FROZEN_POLICY_PROFILE = "upside_tight_v1"
FROZEN_STATIC_CONFIG = "static_spot_w0025"
FROZEN_EVALUATION_WINDOWS = tuple(range(3, 26))
FROZEN_ENTRY_STATE_SHA256 = "61d9f7977c9806bf25c01a3f2db9bc5ecb56bb7612fcc6c72bf68a94a31f118b"
FROZEN_PARAMETER_SHA256 = "c8504486cc7573e634dd9f90afe35d488e48a1580a0ed4f07e5a22c3e66fa4a1"
FROZEN_MINT_GAS_USD = 0.073
FROZEN_REMOVE_GAS_USD = 0.022
FROZEN_ACTIVE_ROUTES: Mapping[int, DirectionalArchetype] = {
    7: "upside_capture",
    18: "fee_box",
    23: "upside_capture",
    25: "dip_accumulator",
}
```

Compute the entry-state fingerprint from windows 3-25 and exactly these ordered
fields: `window_index`, `entry_timestamp_ms`,
`validation_start_timestamp_ms`, `validation_end_timestamp_ms`,
`directional_archetype`, `directional_route_reason`, and
`gate_directional_active`. Sort rows by integer `window_index`, but retain every
field value as the exact nonempty CSV string. Each object contains only those
seven keys. Serialize the list with
`json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")`, with no trailing newline, before SHA-256.
This freezes exact boundaries, routes, and causal entry identities, not only
indexes.

Compute the parameter fingerprint from sorted-key compact JSON containing the
exact outer object
`{"profile": "upside_tight_v1", "directional": {archetype: asdict(params)}, "static": {"static_spot_w0025": asdict(params)}, "mint_gas_usd": 0.073, "remove_gas_usd": 0.022}`.
The directional mapping has exactly the three archetype keys, and JSON sorting
determines their encoded order. Use the identical `json.dumps` options above.
The two literal digests are complete test vectors for these payload definitions.
Any hash, gas, initial-capital, or selected-config mismatch fails before
simulation.

Read prediction rows with fields
`timestamp_ms,target_timestamp_ms,horizon_ms,refit_timestamp_ms,fold_index,direction,actual_bps,baseline_prediction_bps,cross_prediction_bps`.
Select only `direction=bsc_to_base` and `horizon_ms=3600000`.
Require the prediction file SHA-256 to match
`article_manifest.json.artifacts["predictive_predictions.csv"]` and
require the statistical manifest to validate with `economics.status="pending"`.

- [ ] **Step 4: Implement paired counterfactual simulation**

For each complete post-warmup window:

1. Resolve the existing route using current causal entry state.
2. If `no_position`, emit zero-return rows for original and gated policies.
3. Otherwise simulate the routed `upside_tight_v1` archetype once without an
   overlay and once with `ForecastAgreementOverlay`.
4. Simulate `static_spot_w0025`, pool-mark hold-cNGN, and cash on the identical
   validation slice.

Compute pool-mark hold-cNGN values at every validation swap as
`raw_sqrt_mid / first_validation_raw_sqrt_mid`. Its net return is the final
normalized value minus one and its maximum drawdown is the largest decline from
the running peak of that path. Do not reuse `hold_cngn_rows()`'s hard-coded zero
drawdown field and do not add entry/exit costs to this mark-only comparator.

Never stitch reset windows into a synthetic drawdown. Report within-window
maximum drawdown, worst window, all-window and active-window positive rates,
total fees, transaction costs, fee/cost ratio, and rebalance count.

- [ ] **Step 5: Write deterministic economic artifacts**

```text
frozen_policy_windows.csv
frozen_policy_summary.csv
frozen_policy_exclusions.csv
frozen_policy_report.md
lp_performance.png
```

Merge the aggregate economic group, economic publication class, figure
identifier/hash, and artifact hashes into the existing `article_manifest.json`.
The merge must preserve the statistical groups byte-for-byte at the parsed JSON
value level, reject missing or `qa_blocked` statistical manifests, and leave
`artifact_status` as `generated_unreviewed`. Generated code must reject any
attempt to create or preserve `reviewed` status.

Use `research.cross_pool.manifest.select_economic_class` as the single decision
table and verify it in this task's integration tests. Relative to the original
policy, evaluate `pareto_improvement` first; it requires strictly
higher aggregate net return, a non-lower worst-window return, and a non-higher
worst within-window drawdown magnitude. `return_risk_tradeoff` requires at least
one improvement and at least one worsening or unchanged metric, but is evaluated
only if the Pareto predicate failed. `no_net_return_improvement` requires
non-higher aggregate return and no improvement in either risk metric. Invalid
forecasts, hashes, frozen contracts, or economic inputs select
`not_adjudicable_qa`. Comparisons are exact on serialized `Decimal` values; no
tolerance is introduced.

- [ ] **Step 6: Run the economic tests**

Run: `python3 -m pytest research/tests/test_evaluate_cross_pool_economic_lp.py -q`

Expected: all frozen identities, causal forecasts, comparator, and aggregation
tests pass.

- [ ] **Step 7: Commit the economic runner**

```bash
git add research/scripts/evaluate_cross_pool_economic_lp.py \
  research/cross_pool/manifest.py \
  research/tests/test_evaluate_cross_pool_economic_lp.py
git commit -m "feat: evaluate forecast-gated Base directional policy"
```

### Task 6: Verify and Run the Economic and Portfolio Experiments

**Files:**
- Generate only: `research/results/cross_pool_lead_lag/**`
- Generate only: `research/results/parameter_portfolio/**`

**Interfaces:**
- Consumes: the completed statistical predictions and verified harnesses.
- Produces: unreviewed economic evidence and explicit portfolio validity rows.

- [ ] **Step 1: Run focused verification**

```bash
python3 -m pytest \
  research/tests/test_entry_eligibility.py \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_simulator.py \
  research/tests/test_evaluate_directional_paper_lp.py \
  research/tests/test_evaluate_parameter_portfolio.py \
  research/tests/test_evaluate_cross_pool_economic_lp.py -q
python3 -m ruff check \
  research/backtester/entry_eligibility.py \
  research/cross_pool/forecast.py \
  research/cross_pool/manifest.py \
  research/backtester/position_runtime.py \
  research/backtester/portfolio_simulator.py \
  research/scripts/evaluate_parameter_portfolio.py \
  research/scripts/evaluate_cross_pool_economic_lp.py
python3 -m py_compile \
  research/backtester/entry_eligibility.py \
  research/cross_pool/forecast.py \
  research/cross_pool/manifest.py \
  research/scripts/evaluate_parameter_portfolio.py \
  research/scripts/evaluate_cross_pool_economic_lp.py
```

Expected: every command exits zero.

- [ ] **Step 2: Run the frozen economic comparison**

```bash
python3 research/scripts/evaluate_cross_pool_economic_lp.py \
  --predictions research/results/cross_pool_lead_lag/predictive_predictions.csv \
  --out-dir research/results/cross_pool_lead_lag
```

Expected: all five economic artifacts exist, windows 3-25 are preserved, and
the manifest remains `generated_unreviewed` with complete statistical and
economic groups.

- [ ] **Step 3: Run the portfolio experiment without relaxing caps**

```bash
python3 research/scripts/evaluate_parameter_portfolio.py \
  --pool all \
  --full-run \
  --out-dir research/results/parameter_portfolio
```

Expected: the command exits successfully even when individual rule/windows
breach the unchanged 10% cap. Those rows are explicitly invalid and absent
from performance or PBO claims.

- [ ] **Step 4: Stop before publication claims**

Leave `article_manifest.json` at `generated_unreviewed` until the final evidence
and diff review is complete.
