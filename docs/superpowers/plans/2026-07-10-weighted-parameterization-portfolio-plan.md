# Weighted Parameterization Portfolio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run one causal, pool-local backtest that treats the repository's existing LP parameterizations as jointly funded virtual positions, compares three frozen allocation rules against existing baselines, and closes the July 2026 research branch with an article-ready result.

**Architecture:** Extract the existing single-position strategy state machine into a reusable runtime, preserving `simulate_pool()` behavior. Build a stable model catalog and pure train-only allocator, then drive multiple runtimes through one event loop with a shared bankroll, portfolio-wide fee dilution, net inventory execution, and sleeve attribution. A research CLI will construct Base and BSC universes from existing config constructors, run walk-forward training and joint validation, write ignored artifacts, and update durable closeout documents after the single full run.

**Tech Stack:** Python 3, dataclasses, `Decimal` at reporting boundaries, existing CLMM math and backtester modules, CSV/JSON/Markdown artifacts, pytest, mypy.

## Global Constraints

- Keep `uni-base` and `uni-bsc` as separate portfolios and separate claims.
- Import existing EWMA, paper, frozen, static, and directional constructors; do not copy or expand their grids.
- Use only information available before each validation window to determine eligibility and weights.
- The primary rule is equal weight across eligible families, then equal weight within each family.
- Freeze the aggregate synthetic active-liquidity cap at `0.10` of total active liquidity: `L_p / (L_h + L_p) <= 0.10`.
- Freeze family weight cap at `0.35` and sleeve weight cap at `0.10`.
- Freeze eligibility at positive costed training return, at least one completed episode, and fee-to-transaction-cost ratio `>= 1.0`.
- Freeze shrinkage score as `net_return / max(abs(max_drawdown), 0.01)`, clipped to `[-2.0, 2.0]`.
- Freeze shrinkage tilt `eta=0.5` and tilt share `alpha=0.25`; do not search either value.
- Residual allocation after eligibility or caps remains cash.
- Historical swaps and sqrt-price paths remain exogenous; fail closed when the aggregate-liquidity cap invalidates the small-participant approximation.
- Net same-timestamp portfolio inventory requirements before pricing an external swap.
- Generated outputs stay under ignored `research/results/**`; commit only source, tests, and durable docs.
- No fintech, CBN, FMDQ, NAFEM, or renewed Bybit historical-data acquisition.
- No H12 capacity optimization, live sizing, production engine integration, or live-alpha claim.
- Do not remove anything from `.gitignore`.

---

## File Structure

- `research/backtester/position_runtime.py`: reusable state and transitions for one LP strategy sleeve.
- `research/backtester/simulator.py`: retain the public `simulate_pool()` API as a thin single-runtime orchestration wrapper.
- `research/backtester/portfolio_catalog.py`: stable sleeve identities and construction from existing model families.
- `research/backtester/portfolio_allocation.py`: pure eligibility and frozen allocation rules.
- `research/backtester/portfolio_simulator.py`: shared-wallet, multi-position validation simulation and attribution.
- `research/scripts/evaluate_parameter_portfolio.py`: pool-local walk-forward orchestration, comparators, artifact writing, and summaries.
- `research/tests/test_position_runtime.py`: extraction parity and transition tests.
- `research/tests/test_portfolio_catalog.py`: catalog, family, and deduplication tests.
- `research/tests/test_portfolio_allocation.py`: eligibility, caps, cash, and shrinkage tests.
- `research/tests/test_portfolio_simulator.py`: joint fees, liquidity cap, net execution, and accounting tests.
- `research/tests/test_evaluate_parameter_portfolio.py`: window causality, output separation, matrices, sensitivity, and CLI smoke tests.
- Existing closeout and Article 2 Markdown files: final research synthesis after the one full run.

### Task 1: Extract a Reusable Single-Sleeve Runtime Without Behavior Change

**Files:**
- Create: `research/backtester/position_runtime.py`
- Create: `research/tests/test_position_runtime.py`
- Modify: `research/backtester/simulator.py:1388-1740`
- Test: `research/tests/test_backtester.py`

**Interfaces:**
- Consumes: `Event`, `BacktestParams`, `PoolConfig`, `PoolState`, `SizingPolicy`, and the existing simulator math/cost helpers.
- Produces: `SleeveRuntime`, `SleeveSnapshot`, `SleeveAction`, `create_sleeve_runtime`, `advance_sleeve`, and `finalize_sleeve`; `simulate_pool` remains API-compatible.

- [ ] **Step 1: Write parity and transition tests before extraction**

```python
# research/tests/test_position_runtime.py
from research.backtester.position_runtime import create_sleeve_runtime
from research.backtester.simulator import UNISWAP_BASE_POOL, simulate_pool


def test_runtime_wrapper_matches_existing_single_position_result(flat_events, paper_params):
    expected = simulate_pool(flat_events, paper_params, UNISWAP_BASE_POOL, 500.0)
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=paper_params,
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )
    actual = runtime.run(flat_events)
    assert actual.final_value == pytest.approx(expected.final_value)
    assert actual.total_fees == pytest.approx(expected.total_fees)
    assert actual.total_transaction_cost == pytest.approx(expected.total_transaction_cost)
    assert actual.rebalance_count == expected.rebalance_count
    assert actual.value_samples == expected.value_samples


def test_runtime_exposes_action_without_applying_external_inventory_swap(
    flat_events, paper_params
):
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=paper_params,
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )
    action = runtime.propose(flat_events[1], historical_active_liquidity=10**12)
    assert action.sleeve_id == "paper"
    assert action.kind == "enter"
    assert action.required_wallet.value_usd(action.cngn_usd_price) > 0
    assert runtime.position is None
```

- [ ] **Step 2: Run the new tests and confirm the missing-module failure**

Run: `pytest research/tests/test_position_runtime.py -q`

Expected: collection fails with `ModuleNotFoundError: research.backtester.position_runtime`.

- [ ] **Step 3: Move the per-position state into explicit dataclasses**

```python
# research/backtester/position_runtime.py
@dataclass(frozen=True)
class SleeveAction:
    sleeve_id: str
    kind: Literal["enter", "exit", "rebalance", "none"]
    event: Event
    required_wallet: PortfolioComposition
    released_wallet: PortfolioComposition
    cngn_usd_price: float
    tick_lower: int | None
    tick_upper: int | None
    reason: str | None


@dataclass
class SleeveRuntime:
    sleeve_id: str
    params: BacktestParams
    pool_config: PoolConfig
    capital_budget_usd: float
    wallet: PortfolioComposition
    result: SimResult = field(default_factory=SimResult)
    position: VirtualPosition | None = None
    ewma: EWMACalculator = field(init=False)

    def __post_init__(self) -> None:
        if self.capital_budget_usd < 0:
            raise ValueError("capital_budget_usd must be non-negative")
        self.ewma = EWMACalculator(self.params.ewma_lambda)

    def propose(self, event: Event, historical_active_liquidity: int) -> SleeveAction:
        return _propose_existing_transition(self, event, historical_active_liquidity)

    def apply(self, action: SleeveAction, allocated_wallet: PortfolioComposition) -> None:
        _apply_existing_transition(self, action, allocated_wallet)
```

Move the current entry/hold/fee/exit decisions unchanged into
`_propose_existing_transition` and `_apply_existing_transition`. Keep range
construction, defensive exits, cooldowns, EWMA readiness, and episode recording
identical. `propose()` must not mutate wallet or position; `apply()` performs the
mutation after the orchestrator resolves external execution.

- [ ] **Step 4: Make `simulate_pool()` drive exactly one runtime**

```python
def simulate_pool(
    events: list[Event],
    params: BacktestParams,
    pool_config: PoolConfig,
    initial_capital_usd: float = 5000.0,
    initial_pool_state: PoolState | None = None,
    sizing_policy: SizingPolicy | None = None,
    idle_apr: float = 0.0,
) -> SimResult:
    runtime = create_sleeve_runtime(
        sleeve_id="single",
        params=params,
        pool_config=pool_config,
        capital_usd=initial_capital_usd,
        sizing_policy=sizing_policy,
        idle_apr=idle_apr,
    )
    return runtime.run(events, initial_pool_state=initial_pool_state)
```

The wrapper must preserve every existing result field and must not change
`simulate_pool` callers.

- [ ] **Step 5: Run focused and full simulator regression tests**

Run: `pytest research/tests/test_position_runtime.py research/tests/test_backtester.py -q`

Expected: all tests pass with no changes to existing expected values.

- [ ] **Step 6: Commit the behavior-preserving extraction**

```bash
git add research/backtester/position_runtime.py research/backtester/simulator.py \
  research/tests/test_position_runtime.py research/tests/test_backtester.py
git commit -m "refactor: extract LP position runtime"
```

### Task 2: Build the Stable Model Catalog

**Files:**
- Create: `research/backtester/portfolio_catalog.py`
- Create: `research/tests/test_portfolio_catalog.py`

**Interfaces:**
- Consumes: `generate_grid`, `generate_paper_grid`, `frozen_paper_configs`, `static_lp_configs`, `static_lp_closed_configs`, `directional_archetype_configs`, and `directional_policy_profiles`.
- Produces: `SleeveDefinition`, `DirectionalPolicyDefinition`, `PortfolioCatalog`, `build_portfolio_catalog`, and `parameter_fingerprint`.

- [ ] **Step 1: Write catalog identity and directional double-counting tests**

```python
def test_catalog_uses_existing_families_without_directional_component_funding():
    catalog = build_portfolio_catalog("uni-base", bankroll_usd=1200.0)
    assert set(catalog.family_names) == {
        "ewma", "paper", "static", "frozen", "directional"
    }
    assert len({s.sleeve_id for s in catalog.sleeves}) == len(catalog.sleeves)
    assert all(s.family != "directional_component" for s in catalog.sleeves)
    assert {p.profile for p in catalog.directional_policies} == {
        "balanced_v1", "upside_wide_v1", "upside_tight_v1",
        "dip_wide_v1", "fee_tight_v1",
    }


def test_exact_duplicates_share_one_underlying_sleeve():
    params = BacktestParams(strategy_mode="static", fixed_width_pct=0.01)
    fingerprint = parameter_fingerprint(params)
    assert fingerprint == parameter_fingerprint(replace(params))
```

- [ ] **Step 2: Run tests and confirm the missing-module failure**

Run: `pytest research/tests/test_portfolio_catalog.py -q`

Expected: collection fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement immutable catalog contracts**

```python
FamilyName = Literal["ewma", "paper", "static", "frozen", "directional"]


@dataclass(frozen=True)
class SleeveDefinition:
    sleeve_id: str
    family: FamilyName
    config_name: str
    params: BacktestParams
    parameter_fingerprint: str
    source_constructor: str


@dataclass(frozen=True)
class DirectionalPolicyDefinition:
    sleeve_id: str
    family: Literal["directional"]
    profile: str
    archetypes: Mapping[str, SleeveDefinition]


@dataclass(frozen=True)
class PortfolioCatalog:
    pool: str
    sleeves: tuple[SleeveDefinition, ...]
    directional_policies: tuple[DirectionalPolicyDefinition, ...]

    @property
    def family_names(self) -> tuple[str, ...]:
        return tuple(sorted({s.family for s in self.sleeves}))
```

Serialize `BacktestParams`, nested `EntryFilters`, and `TransactionCostModel`
with sorted canonical JSON and SHA-256. Reject duplicate `sleeve_id` values.
Reuse a fingerprint within the same economic family; do not merge route-aware
directional policy identities with their component params.

- [ ] **Step 4: Verify catalog counts and deterministic output**

Run: `pytest research/tests/test_portfolio_catalog.py -q`

Expected: tests pass and two consecutive catalogs serialize identically.

- [ ] **Step 5: Commit the catalog**

```bash
git add research/backtester/portfolio_catalog.py research/tests/test_portfolio_catalog.py
git commit -m "feat: catalog LP parameter sleeves"
```

### Task 3: Implement Frozen Eligibility and Allocation Rules

**Files:**
- Create: `research/backtester/portfolio_allocation.py`
- Create: `research/tests/test_portfolio_allocation.py`

**Interfaces:**
- Consumes: `SleeveDefinition` and training metric rows keyed by `sleeve_id`.
- Produces: `TrainingMetrics`, `Allocation`, `eligible_sleeves`, `equal_config_weights`, `equal_family_weights`, and `shrinkage_weights`.

- [ ] **Step 1: Write tests for eligibility, caps, residual cash, and grid-size neutrality**

```python
def test_equal_family_weights_ignore_family_grid_size():
    sleeves = [sleeve("a", "ewma"), sleeve("b", "ewma"), sleeve("c", "paper")]
    metrics = {s.sleeve_id: passing_metrics() for s in sleeves}
    allocation = equal_family_weights(sleeves, metrics)
    assert allocation.weights["a"] == pytest.approx(0.175)
    assert allocation.weights["b"] == pytest.approx(0.175)
    assert allocation.weights["c"] == pytest.approx(0.35)
    assert allocation.cash_weight == pytest.approx(0.30)


def test_no_eligible_sleeves_holds_cash():
    metrics = {"a": TrainingMetrics(-0.01, 1, 2.0, -0.02)}
    allocation = equal_family_weights([sleeve("a", "ewma")], metrics)
    assert allocation.weights == {}
    assert allocation.cash_weight == 1.0


def test_shrinkage_constants_are_frozen():
    assert SHRINKAGE_ETA == 0.5
    assert SHRINKAGE_ALPHA == 0.25
    assert SCORE_CLIP == (-2.0, 2.0)
```

- [ ] **Step 2: Run tests and confirm the missing-module failure**

Run: `pytest research/tests/test_portfolio_allocation.py -q`

Expected: collection fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement fail-closed allocation contracts**

```python
FAMILY_CAP = 0.35
SLEEVE_CAP = 0.10
MIN_FEE_COST_RATIO = 1.0
SHRINKAGE_ETA = 0.5
SHRINKAGE_ALPHA = 0.25
SCORE_CLIP = (-2.0, 2.0)


@dataclass(frozen=True)
class TrainingMetrics:
    net_return: float
    episode_count: int
    fee_to_transaction_cost_ratio: float
    max_drawdown: float


@dataclass(frozen=True)
class Allocation:
    rule: Literal["equal_config", "equal_family", "shrinkage"]
    weights: Mapping[str, float]
    cash_weight: float

    def __post_init__(self) -> None:
        if any(not math.isfinite(w) or w < 0 for w in self.weights.values()):
            raise ValueError("weights must be finite and non-negative")
        if sum(self.weights.values()) + self.cash_weight > 1.0 + 1e-12:
            raise ValueError("allocation exceeds bankroll")


def is_eligible(metrics: TrainingMetrics) -> bool:
    return (
        math.isfinite(metrics.net_return)
        and metrics.net_return > 0
        and metrics.episode_count >= 1
        and metrics.fee_to_transaction_cost_ratio >= MIN_FEE_COST_RATIO
    )
```

For equal-family weights, assign `min(1 / eligible_family_count, 0.35)` to each
family and divide by eligible unique sleeves, capping each sleeve at `0.10`.
Do not redistribute cap residuals. For shrinkage, compute the clipped risk score,
normalize `base * exp(0.5 * score)`, mix `75%` base with `25%` tilt, reapply caps,
and leave residual cash.

- [ ] **Step 4: Run allocator tests**

Run: `pytest research/tests/test_portfolio_allocation.py -q`

Expected: all eligibility, cap, cash, invalid-weight, and deterministic-shrinkage tests pass.

- [ ] **Step 5: Commit the allocator**

```bash
git add research/backtester/portfolio_allocation.py research/tests/test_portfolio_allocation.py
git commit -m "feat: add frozen LP portfolio allocation rules"
```

### Task 4: Implement Joint Multi-Position Portfolio Simulation

**Files:**
- Create: `research/backtester/portfolio_simulator.py`
- Create: `research/tests/test_portfolio_simulator.py`
- Modify: `research/backtester/position_runtime.py`

**Interfaces:**
- Consumes: `Allocation`, `SleeveDefinition`, `SleeveRuntime`, validation events, pool config, initial pool state, and bankroll.
- Produces: `SleeveAttribution`, `PortfolioResult`, `LiquidityShareExceeded`, and `simulate_portfolio`.

- [ ] **Step 1: Write the joint-fee, netting, and liquidity-cap tests**

```python
def test_overlapping_positions_split_one_fee_pool(portfolio_case):
    result = simulate_portfolio(
        events=portfolio_case.events,
        sleeves=portfolio_case.sleeves,
        allocation=portfolio_case.allocation,
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        initial_pool_state=portfolio_case.initial_pool_state,
    )
    one_swap = portfolio_case.fee_swap
    l_h = one_swap.active_liquidity
    l_a = result.attribution["a"].liquidity_at(one_swap.block_time)
    l_b = result.attribution["b"].liquidity_at(one_swap.block_time)
    total_fee = one_swap.amount_usd * one_swap.fee_rate
    assert result.attribution["a"].fees_usd == pytest.approx(total_fee * l_a / (l_h + l_a + l_b))
    assert result.attribution["b"].fees_usd == pytest.approx(total_fee * l_b / (l_h + l_a + l_b))
    assert result.total_fees <= total_fee


def test_opposing_same_event_inventory_actions_are_netted(opposing_action_case):
    result = simulate_portfolio(
        events=opposing_action_case.events,
        sleeves=opposing_action_case.sleeves,
        allocation=opposing_action_case.allocation,
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        initial_pool_state=opposing_action_case.initial_pool_state,
    )
    assert result.external_swap_notional_usd == pytest.approx(40.0)
    assert result.internal_netting_notional_usd == pytest.approx(60.0)


def test_aggregate_share_fails_even_when_each_sleeve_is_below_cap(over_cap_case):
    with pytest.raises(LiquidityShareExceeded, match="aggregate synthetic"):
        simulate_portfolio(
            events=over_cap_case.events,
            sleeves=over_cap_case.sleeves,
            allocation=over_cap_case.allocation,
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=500.0,
            initial_pool_state=over_cap_case.initial_pool_state,
        )
```

- [ ] **Step 2: Run tests and confirm the missing-module failure**

Run: `pytest research/tests/test_portfolio_simulator.py -q`

Expected: collection fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement portfolio result contracts and liquidity denominator**

```python
AGGREGATE_LIQUIDITY_SHARE_CAP = 0.10


@dataclass
class SleeveAttribution:
    sleeve_id: str
    capital_budget_usd: float
    fees_usd: float = 0.0
    transaction_cost_usd: float = 0.0
    value_samples: list[tuple[datetime, float]] = field(default_factory=list)


@dataclass
class PortfolioResult:
    bankroll_usd: float
    final_value: float
    cash_value: float
    total_fees: float
    total_transaction_cost: float
    total_price_impact_cost: float
    external_swap_notional_usd: float
    internal_netting_notional_usd: float
    max_aggregate_liquidity_share: float
    value_samples: list[tuple[datetime, float]]
    attribution: Mapping[str, SleeveAttribution]


def _fee_shares(active: Sequence[SleeveRuntime], historical_liquidity: int) -> dict[str, float]:
    synthetic = sum(runtime.position.liquidity_L for runtime in active if runtime.position)
    denominator = historical_liquidity + synthetic
    if denominator <= 0:
        return {runtime.sleeve_id: 0.0 for runtime in active}
    share = synthetic / denominator
    if share > AGGREGATE_LIQUIDITY_SHARE_CAP + 1e-12:
        raise LiquidityShareExceeded(f"aggregate synthetic liquidity share {share:.6f} exceeds 0.10")
    return {
        runtime.sleeve_id: runtime.position.liquidity_L / denominator
        for runtime in active
        if runtime.position
    }
```

- [ ] **Step 4: Implement deterministic action netting and joint execution**

Collect all sleeve proposals for an event before mutating any runtime. Convert
required/released wallets to signed stable and cNGN deltas, net opposite cNGN
requirements, and call the existing `_swap_cost_breakdown` once for the residual
external notional. Allocate external variable cost pro rata to sleeves causing
the residual; allocate mint/remove gas to each triggering sleeve. Apply actions
in sorted `sleeve_id` order only after the portfolio execution result is fixed.

```python
def _net_actions(actions: Sequence[SleeveAction], price: float) -> NettedExecution:
    signed = {a.sleeve_id: _signed_cngn_notional(a, price) for a in actions}
    buys = sum(max(v, 0.0) for v in signed.values())
    sells = sum(max(-v, 0.0) for v in signed.values())
    internal = min(buys, sells)
    residual = buys - sells
    return NettedExecution(
        direction="stable_to_cngn" if residual > 0 else "cngn_to_stable" if residual < 0 else None,
        external_notional_usd=abs(residual),
        internal_notional_usd=internal,
        signed_notional_by_sleeve=signed,
    )
```

- [ ] **Step 5: Preserve the exogenous historical path and sleeve attribution**

For each swap event, use its recorded `sqrt_price_x96`, tick, volume, fee, and
historical active liquidity. Do not apply synthetic mint/burns to `PoolState` and
do not mutate the recorded price after portfolio inventory execution. Append one
portfolio value sample and one value sample per sleeve after all actions and fee
allocation for that event.

- [ ] **Step 6: Run joint simulator and single-runtime regression tests**

Run: `pytest research/tests/test_portfolio_simulator.py research/tests/test_position_runtime.py research/tests/test_backtester.py -q`

Expected: all tests pass; existing single-position results remain unchanged.

- [ ] **Step 7: Commit the joint simulator**

```bash
git add research/backtester/portfolio_simulator.py research/backtester/position_runtime.py \
  research/tests/test_portfolio_simulator.py
git commit -m "feat: simulate joint LP parameter portfolios"
```

### Task 5: Add Causal Walk-Forward Orchestration and Directional Routing

**Files:**
- Create: `research/scripts/evaluate_parameter_portfolio.py`
- Create: `research/tests/test_evaluate_parameter_portfolio.py`

**Interfaces:**
- Consumes: `POOL_EXPERIMENTS`, feature/QTS entry states, catalog, allocator, `_iter_window_slices`, `simulate_pool`, and `simulate_portfolio`.
- Produces: `evaluate_pool`, `evaluate_window`, `route_directional_catalog`, and CSV/JSON writers.

- [ ] **Step 1: Write a causality test that changes validation results without changing weights**

```python
def test_validation_metrics_cannot_change_frozen_weights(window_fixture):
    first = evaluate_window(window_fixture, validation_return_override={"ewma:a": 0.90})
    second = evaluate_window(window_fixture, validation_return_override={"ewma:a": -0.90})
    assert first.allocations == second.allocations
    assert first.portfolio_results != second.portfolio_results


def test_directional_policy_funds_only_the_causally_routed_archetype(entry_state, catalog):
    routed = route_directional_catalog(catalog, entry_state)
    assert routed["directional:upside_tight_v1"].config_name.startswith("upside_capture_")
    assert all("dip_accumulator" not in item.config_name for item in routed.values())
```

- [ ] **Step 2: Run tests and confirm the missing-script failure**

Run: `pytest research/tests/test_evaluate_parameter_portfolio.py -q`

Expected: collection fails because `evaluate_parameter_portfolio` does not exist.

- [ ] **Step 3: Implement one training pass and three frozen validation allocations**

```python
@dataclass(frozen=True)
class WindowEvaluation:
    pool: str
    window_index: int
    training_metrics: Mapping[str, TrainingMetrics]
    allocations: Mapping[str, Allocation]
    portfolio_results: Mapping[str, PortfolioResult]
    comparator_metrics: Mapping[str, Mapping[str, float]]


def evaluate_window(
    *,
    pool: str,
    catalog: PortfolioCatalog,
    window_slice: WindowSlice,
    entry_state: Mapping[str, str],
    pool_config: PoolConfig,
    bankroll_usd: float,
) -> WindowEvaluation:
    routed_catalog = route_directional_catalog(catalog, entry_state)
    training_metrics = _simulate_training_sleeves(
        routed_catalog,
        window_slice.train_events,
        pool_config,
        bankroll_usd,
    )
    allocations = {
        "equal_config": equal_config_weights(catalog.sleeves, training_metrics),
        "equal_family": equal_family_weights(catalog.sleeves, training_metrics),
        "shrinkage": shrinkage_weights(catalog.sleeves, training_metrics),
    }
    portfolio_results = {
        name: simulate_portfolio(
            events=window_slice.val_events,
            sleeves=routed_catalog.sleeves,
            allocation=allocation,
            pool_config=pool_config,
            bankroll_usd=bankroll_usd,
            initial_pool_state=_build_pool_state(window_slice.train_events),
        )
        for name, allocation in allocations.items()
    }
    comparator_metrics = _evaluate_comparators(
        routed_catalog,
        window_slice,
        pool_config,
        bankroll_usd,
        training_metrics,
    )
    return WindowEvaluation(
        pool=pool,
        window_index=window_slice.window.index,
        training_metrics=training_metrics,
        allocations=allocations,
        portfolio_results=portfolio_results,
        comparator_metrics=comparator_metrics,
    )
```

Training runs may simulate sleeves independently because they estimate each
candidate before capital is allocated. Validation portfolio claims must come
only from the joint simulator. Existing standalone validation runs remain
comparators and PBO matrix inputs, never portfolio arithmetic.

- [ ] **Step 4: Integrate directional entry-state routing without component double funding**

Build entry states once using the existing causal feature/QTS functions. Replace
each directional profile's policy sleeve with only the archetype routed for that
window. When the route is `no_position`, omit that directional sleeve and leave
its prospective weight in cash.

- [ ] **Step 5: Write complete artifacts per pool**

Write these files under the pool-specific directory beneath `--out-dir`:

```text
configuration_catalog.csv
training_eligibility.csv
window_weights.csv
sleeve_validation_matrix.csv
family_validation_matrix.csv
portfolio_validation_matrix.csv
comparators.csv
concentration_and_contribution.csv
pbo_allocation_rules.json
summary.md
```

Every row includes `pool`, `window_index`, `window_start`, and `window_end`.
Reject ragged portfolio matrices before PBO. Compute best-sleeve removal by
identifying the best sleeve across completed validation windows, removing it from
each frozen allocation without redistributing its weight, and rerunning joint
validation with the residual held as cash.

- [ ] **Step 6: Add CLI arguments and deterministic smoke mode**

```python
parser.add_argument("--pool", choices=["uni-base", "uni-bsc", "all"], default="all")
parser.add_argument("--out-dir", type=Path, default=Path("research/results/parameter_portfolio"))
parser.add_argument("--max-windows", type=int)
parser.add_argument("--catalog-limit-per-family", type=int,
                    help="Smoke-test only; forbidden when --full-run is set")
parser.add_argument("--full-run", action="store_true")
```

The full run rejects `--max-windows` and `--catalog-limit-per-family` so the
published result cannot silently use a truncated universe.

- [ ] **Step 7: Run orchestration tests and a two-window smoke run**

Run: `pytest research/tests/test_evaluate_parameter_portfolio.py -q`

Expected: all tests pass.

Run:

```bash
python3 research/scripts/evaluate_parameter_portfolio.py \
  --pool all \
  --max-windows 2 \
  --catalog-limit-per-family 2 \
  --out-dir research/results/parameter_portfolio_smoke
```

Expected: both pool directories contain all ten output files; summaries label the run `SMOKE / NOT EVIDENCE`.

- [ ] **Step 8: Commit orchestration**

```bash
git add research/scripts/evaluate_parameter_portfolio.py \
  research/tests/test_evaluate_parameter_portfolio.py
git commit -m "feat: add LP parameter portfolio experiment"
```

### Task 6: Verify the Harness Before the Full Run

**Files:**
- Modify only if tests expose a defect in Tasks 1-5.

**Interfaces:**
- Consumes: completed source and tests.
- Produces: verified harness ready for the single full experiment.

- [ ] **Step 1: Run focused research tests**

```bash
pytest \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_catalog.py \
  research/tests/test_portfolio_allocation.py \
  research/tests/test_portfolio_simulator.py \
  research/tests/test_evaluate_parameter_portfolio.py \
  research/tests/test_backtester.py \
  research/tests/test_evaluate_frozen_family_lp.py \
  research/tests/test_evaluate_directional_paper_lp.py -q
```

Expected: all pass.

- [ ] **Step 2: Run strict typing and compilation**

```bash
mypy research/backtester/position_runtime.py \
  research/backtester/portfolio_catalog.py \
  research/backtester/portfolio_allocation.py \
  research/backtester/portfolio_simulator.py \
  research/scripts/evaluate_parameter_portfolio.py
python3 -m py_compile \
  research/backtester/position_runtime.py \
  research/backtester/portfolio_catalog.py \
  research/backtester/portfolio_allocation.py \
  research/backtester/portfolio_simulator.py \
  research/scripts/evaluate_parameter_portfolio.py
```

Expected: both commands exit zero.

- [ ] **Step 3: Inspect smoke artifacts for accounting identities**

Regenerate the two-window smoke output with the exact Step 7 command after any
harness correction. Do not validate smoke files produced by an earlier schema;
the status branches below are part of the evidence contract.

Then run:

```bash
python3 - <<'PY'
import csv
import math
from pathlib import Path

from research.scripts.evaluate_parameter_portfolio import ARTIFACT_NAMES

root = Path("research/results/parameter_portfolio_smoke")
performance_fields = (
    "net_return",
    "max_drawdown",
    "final_value",
    "cash_value",
    "total_fees",
    "total_transaction_cost",
    "max_aggregate_liquidity_share",
)
for directory in ("uni_base", "uni_bsc"):
    pool_root = root / directory
    assert {path.name for path in pool_root.iterdir()} == set(ARTIFACT_NAMES)
    path = pool_root / "portfolio_validation_matrix.csv"
    rows = list(csv.DictReader(path.open()))
    assert rows
    for row in rows:
        assert math.isclose(
            float(row["deployed_weight"]) + float(row["cash_weight"]),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        if row["status"] == "valid":
            assert row["observed_share"] == row["cap"] == ""
            assert all(
                row[name] and math.isfinite(float(row[name]))
                for name in performance_fields
            )
            assert float(row["max_aggregate_liquidity_share"]) <= 0.10 + 1e-12
        else:
            assert row["status"] == "invalid_liquidity_cap"
            assert 0 < float(row["cap"]) < float(row["observed_share"]) <= 1
            assert all(row[name] == "" for name in performance_fields)
print("portfolio smoke accounting: ok")
PY
```

Expected: `portfolio smoke accounting: ok`.

- [ ] **Step 4: Commit any verified corrections as a separate commit**

If no correction is needed, do not create an empty commit. If needed:

```bash
git add research/backtester/position_runtime.py \
  research/backtester/portfolio_catalog.py \
  research/backtester/portfolio_allocation.py \
  research/backtester/portfolio_simulator.py \
  research/scripts/evaluate_parameter_portfolio.py \
  research/tests/test_position_runtime.py \
  research/tests/test_portfolio_catalog.py \
  research/tests/test_portfolio_allocation.py \
  research/tests/test_portfolio_simulator.py \
  research/tests/test_evaluate_parameter_portfolio.py
git commit -m "fix: correct LP portfolio accounting"
```

### Task 7: Execute the Single Full Experiment and Interpret It

**Files:**
- Generate only: `research/results/parameter_portfolio/**`
- Do not stage generated results.

**Interfaces:**
- Consumes: full declared catalog, Base/BSC derived histories and causal feature files.
- Produces: the complete ignored evidence set used for closeout writing.

- [ ] **Step 1: Confirm required local inputs and clean command shape**

```bash
test -f research/data/derived/uni_base_pool_history_replay.csv
test -f research/data/derived/uni_bsc_pool_history_replay.csv
test -f research/data/derived/uni_base_pool_features.csv
test -f research/data/derived/uni_bsc_pool_features.csv
test -f research/data/derived/uni_base_flow_markout_features.csv
test -f research/data/derived/uni_bsc_flow_markout_features.csv
```

Expected: all commands exit zero.

- [ ] **Step 2: Run the experiment once without truncation**

```bash
python3 research/scripts/evaluate_parameter_portfolio.py \
  --pool all \
  --full-run \
  --out-dir research/results/parameter_portfolio
```

Expected: one complete Base run and one complete BSC run; no `max_windows` or catalog limit in run metadata.

- [ ] **Step 3: Verify matrices, caps, and Base/BSC separation**

```bash
python3 - <<'PY'
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from research.scripts.evaluate_parameter_portfolio import (
    ALLOCATION_RULE_NAMES,
    ARTIFACT_NAMES,
)

root = Path("research/results/parameter_portfolio")
performance_fields = (
    "net_return",
    "max_drawdown",
    "final_value",
    "cash_value",
    "total_fees",
    "total_transaction_cost",
    "max_aggregate_liquidity_share",
)
for pool, directory in (("uni-base", "uni_base"), ("uni-bsc", "uni_bsc")):
    pool_root = root / directory
    assert {path.name for path in pool_root.iterdir()} == set(ARTIFACT_NAMES)
    assert "run label: **FULL RUN**" in (pool_root / "summary.md").read_text()
    portfolio = list(
        csv.DictReader((pool_root / "portfolio_validation_matrix.csv").open())
    )
    assert portfolio
    assert {row["pool"] for row in portfolio} == {pool}
    assert {row["rule"] for row in portfolio} == set(ALLOCATION_RULE_NAMES)
    window_indexes = {int(row["window_index"]) for row in portfolio}
    assert len(portfolio) == len(window_indexes) * len(ALLOCATION_RULE_NAMES)
    assert len({(row["window_index"], row["rule"]) for row in portfolio}) == len(
        portfolio
    )
    invalid_by_rule = defaultdict(list)
    for row in portfolio:
        assert math.isclose(
            float(row["deployed_weight"]) + float(row["cash_weight"]),
            1.0,
            abs_tol=1e-12,
        )
        if row["status"] == "valid":
            assert row["observed_share"] == row["cap"] == ""
            assert all(
                field and math.isfinite(float(field))
                for field in (row[name] for name in performance_fields)
            )
            assert float(row["max_aggregate_liquidity_share"]) <= 0.10 + 1e-12
        else:
            assert row["status"] == "invalid_liquidity_cap"
            assert 0 < float(row["cap"]) < float(row["observed_share"]) <= 1
            assert all(row[name] == "" for name in performance_fields)
            invalid_by_rule[row["rule"]].append(int(row["window_index"]))

    pbo = json.loads((pool_root / "pbo_allocation_rules.json").read_text())
    if invalid_by_rule:
        allocation_pbo = pbo["allocation_rules"]
        assert allocation_pbo["status"] == "invalid_incomplete_matrix"
        assert allocation_pbo["invalid_window_indexes_by_rule"] == {
            rule: sorted(indexes) for rule, indexes in invalid_by_rule.items()
        }
    else:
        assert pbo["allocation_rules"]["config_count"] == len(
            ALLOCATION_RULE_NAMES
        )
print("full portfolio artifacts: ok")
PY
```

Expected: `full portfolio artifacts: ok`.

Also audit `window_weights.csv` against the per-window catalog: every weight is
nonnegative; weights plus cash sum to one; and HHI/largest-weight values exactly
recompute `concentration_and_contribution.csv`. Apply the same valid/invalid
field contract to every best-sleeve-removal row. Sleeve and family PBO are
separate complete-matrix diagnostics and must not be described as allocation-rule
PBO.

- [ ] **Step 4: Record the evidence table before writing prose**

For every pool and all three allocation rules, first record valid-window count,
invalid-window count, reason, and allocation-PBO eligibility. Produce aggregate
portfolio metrics only when the rule is valid in every completed window. The
allowed aggregate is the sum and arithmetic mean of reset-capital window net
returns, explicitly labeled as such; do not compound returns or stitch a
synthetic equity path. Also record worst window, worst within-window drawdown,
positive-window rate, total fees, transaction cost, and maximum liquidity share.

Report best-sleeve removal from `concentration_and_contribution.csv` only when
that diagnostic is valid in every window, and label its sleeve selection as
ex-post. Do not invent a fourth allocation rule or a removal PBO. Report the
emitted raw sleeve-weight HHI and largest weight directly. Because HHI excludes
residual cash while sleeve weights may sum to less than one, raw `1 / HHI` can
exceed the catalog size and must not be labeled an effective sleeve count. Any
cash-inclusive or deployed-weight-normalized concentration statistic is post hoc
and must be identified as a separate diagnostic. Turnover, maximum family P&L
contribution, static-LP delta, and hold-cNGN delta are not direct outputs of the
frozen artifact contract; mark them unavailable unless a separate validated
derivation is added. Do not choose only the favorable pool or rule.

### Task 8: Close the Research Branch and Update Article Evidence

**Files:**
- Modify: `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md`
- Modify: `research/autoresearch/lp.md`
- Modify: `research/autoresearch/README.md`
- Modify: `research/autoresearch/fair-price.md`
- Modify: `research/articles/evidence-pack-2026-07-cngn-market-making.md`
- Modify: `research/articles/02-backtesting-the-market-layer.md`

**Interfaces:**
- Consumes: verified full-run summaries and evidence table.
- Produces: one consistent closeout verdict and Article 2 research-improvements section.

- [ ] **Step 1: Write the closeout result from the complete evidence table**

Add a dated `Weighted Parameterization Portfolio Result` section to the handoff
and LP guide. Include Base and BSC rows for `equal_config`, `equal_family`, and
`shrinkage`, with full matrix coverage and frozen-cap feasibility before any
performance field. If a rule has even one invalid row, report its invalid window
count and `invalid_incomplete_matrix` allocation-PBO status, and leave aggregate
performance not adjudicable; blank cells are not zero returns. For a complete
valid rule, report only the reset-window aggregates allowed by Task 7.

Add best-sleeve-removal as a separate ex-post diagnostic from
`concentration_and_contribution.csv`, not as
`equal_family_without_best_sleeve`. Distinguish sleeve/family PBO from
allocation-rule PBO. Mark non-emitted comparator and turnover metrics
unavailable rather than substituting standalone sleeve results for joint
portfolio accounting. End with a diagnostic verdict that states the pool-marked
inventory and exogenous-price/small-participant limitations. Every number must
be traceable to one named generated column or documented formula.

- [ ] **Step 2: Close external-rate acquisition explicitly**

Update `fair-price.md`, the autoresearch README, and handoff to state:

```markdown
The July 2026 branch no longer includes fintech quote APIs, CBN/FMDQ/NAFEM
rates, or renewed Bybit historical searches. External-reference hooks remain
available only for genuinely new timestamped overlapping data supplied later;
acquiring that data is not an open task.
```

- [ ] **Step 3: Add the Article 2 research-improvements section**

Explain:

- parameterizations as a portfolio of hypotheses rather than a unique optimum;
- why equal-family allocation was the predeclared benchmark, while frozen-cap
  infeasibility prevents treating it as a performance benchmark;
- why optimized weights can create a second overfitting layer;
- joint fee dilution and net execution across simultaneous positions;
- the exogenous-price/small-participant limitation;
- the exact Base and BSC outcomes;
- why no shadow-allocation or live-consideration path follows unless the frozen
  feasibility and evidence gates first pass.

Retain the claims-to-avoid list and do not call a positive pool-marked result live alpha.

- [ ] **Step 4: Verify prose consistency and absence of abandoned tasks**

```bash
rg -n "fintech|CBN|FMDQ|NAFEM|Bybit|weighted parameter|equal family|joint portfolio|live alpha" \
  research/autoresearch research/articles
rg -n "TBD|TODO" \
  research/autoresearch research/articles && exit 1 || true
git diff --check
```

Expected: external sources appear only as closed/rejected context; no placeholders or whitespace errors.

- [ ] **Step 5: Run final verification**

```bash
pytest research/tests -q
mypy engine research/backtester research/scripts/evaluate_parameter_portfolio.py
git diff --check
git status --short
```

Expected: pytest and mypy pass; generated `research/results/**` remains ignored; only intended source, tests, and docs are staged later.

- [ ] **Step 6: Commit the closeout**

```bash
git add research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md \
  research/autoresearch/lp.md \
  research/autoresearch/README.md \
  research/autoresearch/fair-price.md \
  research/articles/evidence-pack-2026-07-cngn-market-making.md \
  research/articles/02-backtesting-the-market-layer.md
git commit -m "research: close weighted LP portfolio experiment"
```

Do not stage `.firecrawl/`, `quidax_cngn_usdt.json`, or `research/results/**`.
