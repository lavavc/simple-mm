# Refactored Data Methodology Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the refactored DEX LP and Fair Value data methodology with additive derived artifacts first, then introduce paper-faithful LP reconstruction after the active pool exports finish.

**Architecture:** Keep the existing pool CSVs as append-only raw data and layer derived scripts/tables beside them. Avoid touching `backtester/v4_export.py` or `scripts/update_v4_pool_history.py` while the current Base/BSC update processes are running, because retry attempts spawn fresh exporter subprocesses from the current working tree. Add read-only quality reports, pool-to-`price_snapshots` bridging, causal cone features, report diagnostics, then move to event-time replay and owner/token-id LP ledger work.

**Tech Stack:** Python 3.12, stdlib `csv`/`sqlite3`, `aiosqlite`, Web3.py, pytest, existing `backtester` and `engine.db` helpers.

## Global Constraints

- Raw pool CSVs remain append-only and auditable.
- `uni-base` and `uni-bsc` raw histories stay separate unless a test explicitly evaluates cross-pool transfer.
- Derived features must use only information available at or before the feature timestamp.
- DEX pool prices are Fair Value context features only; they are not labels.
- Fair Value labels remain future CEX executable values.
- LP lifecycle reconstruction uses a sidecar ledger, not overloaded pool CSV columns.
- Do not edit `backtester/v4_export.py` or `scripts/update_v4_pool_history.py` while the current pool update processes are active.
- Do not run backtests on partially updating CSVs; copy or wait for a completed checkpoint first.
- Every as-of join reports source age and missingness.
- Backtest reporting separates opportunity screens from full costed backtests.

---

## Current Operational Constraint

At plan time, two updater/exporter pairs are active:

- `uni-base`: `scripts/update_v4_pool_history.py --pool uni-base ...`
- `uni-bsc`: `scripts/update_v4_pool_history.py --pool uni-bsc ...`

The active exporters append to:

- `data/uni_base_pool_history.csv`
- `data/uni_bsc_pool_history.csv`

Safe while these run:

- Add new scripts that read temporary fixtures.
- Add modules that are not imported by the running exporter/updater.
- Add docs and tests that do not mutate live CSVs.
- Run tests that do not invoke the live exporter or updater.

Unsafe while these run:

- Editing `backtester/v4_export.py`.
- Editing `scripts/update_v4_pool_history.py`.
- Running formatters or imports that rewrite the live pool CSVs.
- Running derived imports directly against a CSV while it is still being appended.

## File Structure

- Create `backtester/pool_features.py`: canonical signed swap-flow and causal feature helpers for pool-history rows.
- Create `backtester/cone_features.py`: causal rolling percentile/cone calculations and as-of join helpers.
- Create `scripts/report_pool_history_quality.py`: read-only quality and coverage report for pool CSVs.
- Create `scripts/import_pool_snapshots.py`: idempotent CSV-to-`price_snapshots` bridge for `uni-base_pool` and `uni-bsc_pool`.
- Create `scripts/build_pool_feature_table.py`: derived pool feature and cone table builder.
- Create `scripts/report_backtest_regime_stability.py`: opportunity/full-cost/stability diagnostics from existing backtest outputs.
- After active collectors finish, create `backtester/v4_event_replay.py`: event-time pool price replay.
- After active collectors finish, create `backtester/v4_lp_ledger.py` and `scripts/export_v4_lp_ledger.py`: owner/token-id lifecycle ledger.
- After the LP ledger exists, create `backtester/lp_paper_episodes.py`: burn+collect matching, FIFO episode reconstruction, paper taxonomy, exact realized-PnL win-score.
- After the LP ledger exists, create `scripts/export_tx_receipts.py`: receipt/gas sidecar keyed by `chain,tx_hash`.

---

### Task 1: Read-Only Pool Quality Baseline

**Files:**
- Create: `scripts/report_pool_history_quality.py`
- Create: `tests/test_pool_history_quality.py`
- Read: `data/uni_base_pool_history.csv`
- Read: `data/uni_bsc_pool_history.csv`

**Interfaces:**
- Produces: `PoolHistoryQualityReport` dataclass with `pool`, `row_count`, `event_counts`, `first_block`, `last_block`, `first_time`, `last_time`, `duplicate_events`, `monotonic_blocks`, `token_order_valid`, `sqrt_price_mismatch_count`, `missing_active_liquidity_swaps`, `coverage_days`.
- Produces: `analyze_pool_history(csv_path: Path, pool: str) -> PoolHistoryQualityReport`.
- Produces CLI: `python scripts/report_pool_history_quality.py --csv data/uni_base_pool_history.csv --pool uni-base --out data/quality/uni_base_pool_history.md`.

- [ ] **Step 1: Write failing tests for quality report basics**

```python
from pathlib import Path
from scripts.report_pool_history_quality import analyze_pool_history

def test_quality_report_counts_events_and_blocks(tmp_path: Path):
    csv_path = tmp_path / "pool.csv"
    csv_path.write_text(
        "block_time,chain,pool_id,event_type,tx_hash,log_index,block_number,sqrt_price_x96,tick,active_liquidity,fee_rate,amount0,amount1,amount_usd,cngn_usd_price,token0_symbol,token1_symbol\n"
        "2026-01-01T00:00:00+00:00,base,0xpool,swap,0x1,1,10,79228162514264337593543950336,0,100,0.0015,-1,1,1,1,cNGN,USDC\n"
        "2026-01-01T00:01:00+00:00,base,0xpool,mint,0x2,2,11,79228162514264337593543950336,0,100,0.0015,0,0,0,1,cNGN,USDC\n"
    )
    report = analyze_pool_history(csv_path, "uni-base")
    assert report.row_count == 2
    assert report.event_counts == {"swap": 1, "mint": 1}
    assert report.first_block == 10
    assert report.last_block == 11
    assert report.duplicate_events == 0
    assert report.monotonic_blocks is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_pool_history_quality.py`

Expected: FAIL with `ModuleNotFoundError` for `scripts.report_pool_history_quality`.

- [ ] **Step 3: Implement the report dataclass and analyzer**

Use `csv.DictReader`, sort-free streaming, and `backtester.pool_price_semantics.classify_pool_price_row` behavior as the reference for price semantics. `sqrt_price_x96` is canonical for marginal pool price. Raw CSV `cngn_usd_price` must be classified as `sqrt_mid`, `swap_amount_ratio`, or `unexplained`; `unexplained` is a hard data-quality alert. The analyzer must raise `ValueError` when `pool` is not `uni-base` or `uni-bsc`.

- [ ] **Step 4: Add duplicate, monotonicity, token-order, and price-mismatch tests**

Add tests that assert:

```python
assert report.duplicate_events == 1
assert report.monotonic_blocks is False
assert report.sqrt_price_mismatch_count == 1
assert report.token_order_valid is False
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_pool_history_quality.py`

Expected: PASS.

---

### Task 2: Canonical Swap-Flow Helpers

**Files:**
- Create: `backtester/pool_features.py`
- Create: `tests/test_pool_features.py`

**Interfaces:**
- Produces: `SwapFlow(cngn_flow_direction: int, signed_cngn_amount: Decimal, signed_usd_notional: Decimal)`.
- Produces: `derive_swap_flow(row: Mapping[str, str]) -> SwapFlow`.
- Direction convention: `+1` means cNGN leaves the pool and buy pressure for cNGN; `-1` means cNGN enters the pool and sell pressure for cNGN.

- [ ] **Step 1: Write failing Base and BSC tests**

```python
from backtester.pool_features import derive_swap_flow

def test_base_cngn_token0_negative_amount_is_buy_pressure():
    flow = derive_swap_flow({
        "event_type": "swap",
        "chain": "base",
        "token0_symbol": "cNGN",
        "token1_symbol": "USDC",
        "amount0": "-1000",
        "amount1": "0.72",
        "amount_usd": "0.72",
    })
    assert flow.cngn_flow_direction == 1
    assert str(flow.signed_cngn_amount) == "1000"
    assert str(flow.signed_usd_notional) == "0.72"

def test_bsc_cngn_token1_negative_amount_is_buy_pressure():
    flow = derive_swap_flow({
        "event_type": "swap",
        "chain": "bsc",
        "token0_symbol": "USDT",
        "token1_symbol": "cNGN",
        "amount0": "0.72",
        "amount1": "-1000",
        "amount_usd": "0.72",
    })
    assert flow.cngn_flow_direction == 1
    assert str(flow.signed_cngn_amount) == "1000"
    assert str(flow.signed_usd_notional) == "0.72"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_pool_features.py`

Expected: FAIL with `ModuleNotFoundError` for `backtester.pool_features`.

- [ ] **Step 3: Implement `derive_swap_flow`**

Use `Decimal(str(value))`. Return zero direction and zero amounts for non-swap rows. Raise `ValueError` if neither token symbol is `cNGN`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_pool_features.py`

Expected: PASS.

---

### Task 3: Idempotent Pool Snapshot Bridge

**Files:**
- Create: `scripts/import_pool_snapshots.py`
- Create: `tests/test_import_pool_snapshots.py`
- Uses: `backtester.pool_features.derive_swap_flow`
- Uses: `engine.db.queries.prices.insert_price_snapshot`
- Uses: `engine.types.PriceQuote`

**Interfaces:**
- Produces: `iter_pool_price_quotes(csv_path: Path, pool: str) -> Iterator[tuple[PriceQuote, dict[str, object]]]`.
- Produces: `ImportSummary(pool: str, inserted_or_updated: int, first_timestamp_ms: int | None, last_timestamp_ms: int | None)`.
- Produces: `import_pool_snapshots(db_path: Path, csv_path: Path, pool: str) -> ImportSummary`.
- Produces CLI: `python scripts/import_pool_snapshots.py --db data/cngn.db --csv data/uni_base_pool_history.csv --pool uni-base`.
- Source names: `uni-base_pool`, `uni-bsc_pool`.
- Timestamp rule: `timestamp_ms = block_time_ms + sequence_within_source_second`; raise `ValueError` if more than 1000 rows share the same source second.
- DEX quote convention: `mid` is the raw sqrt-derived cNGN/USD marginal price; `bid` and `ask` are fee-adjusted infinitesimal executable prices.
- Fee-adjusted convention: for pool fee fraction `f` and raw midpoint `p`, set `bid = p * (1 - f)` and `ask = p / (1 - f)`.
- Metadata must include `raw_sqrt_mid`, `fee_rate`, `quote_model`, `fee_adjusted_bid`, `fee_adjusted_ask`, `stored_cngn_usd_price`, `stored_price_model`, and `price_impact_included: false`.
- The importer must reject rows whose stored `cngn_usd_price` matches neither sqrt-derived marginal price nor swap amount ratio.

- [ ] **Step 1: Write failing idempotent import test**

```python
import sqlite3
from pathlib import Path
from scripts.import_pool_snapshots import import_pool_snapshots
from engine.db.migrations.schema import SCHEMA_SQL

def test_import_pool_snapshots_upserts_dex_rows(tmp_path: Path):
    db = tmp_path / "cngn.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_SQL)
    csv_path = tmp_path / "pool.csv"
    csv_path.write_text(
        "block_time,chain,pool_id,event_type,tx_hash,log_index,block_number,sqrt_price_x96,tick,active_liquidity,fee_rate,amount0,amount1,amount_usd,cngn_usd_price,token0_symbol,token1_symbol\n"
        "2026-01-01T00:00:00+00:00,base,0xpool,swap,0x1,7,10,79228162514264337593543950336,0,100,0.0015,-1000,1,1,1,cNGN,USDC\n"
    )
    first = import_pool_snapshots(db, csv_path, "uni-base")
    second = import_pool_snapshots(db, csv_path, "uni-base")
    assert first.inserted_or_updated == 1
    assert second.inserted_or_updated == 1
    with sqlite3.connect(db) as conn:
        count = conn.execute("select count(*) from price_snapshots where source='uni-base_pool'").fetchone()[0]
        bid, ask, mid, metadata = conn.execute("select bid, ask, mid, metadata_json from price_snapshots").fetchone()
    assert count == 1
    assert bid < mid < ask
    assert '"tx_hash": "0x1"' in metadata
    assert '"quote_model": "dex_fee_adjusted_sqrt"' in metadata
    assert '"price_impact_included": false' in metadata
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_import_pool_snapshots.py`

Expected: FAIL with `ModuleNotFoundError` for `scripts.import_pool_snapshots`.

- [ ] **Step 3: Implement importer**

Use a synchronous `sqlite3` connection for CLI speed and schema compatibility. Upsert with:

```sql
INSERT INTO price_snapshots (source, timestamp_ms, bid, ask, mid, metadata_json)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(source, timestamp_ms) DO UPDATE SET
    bid = excluded.bid,
    ask = excluded.ask,
    mid = excluded.mid,
    metadata_json = excluded.metadata_json
```

Set `mid = sqrt-derived cNGN/USD`, `bid = mid * (1 - fee_rate)`, and `ask = mid / (1 - fee_rate)`. This represents the one-swap, infinitesimal, fee-only executable envelope. It deliberately excludes price impact, gas, hooks, routing, and tick crossing; metadata must make that explicit.

Treat the raw CSV `cngn_usd_price` column as legacy diagnostic metadata, not as the canonical pool-state input. Preserve it as `stored_cngn_usd_price`, classify it as `stored_price_model`, and import only the sqrt-derived `mid`.

- [ ] **Step 4: Add timestamp-collision test**

Create two swap rows with the same `block_time`; assert timestamps differ by 1 ms and metadata contains the true block time for both rows.

- [ ] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_import_pool_snapshots.py tests/test_pool_features.py`

Expected: PASS.

---

### Task 4: Causal Cone Feature Builder

**Files:**
- Create: `backtester/cone_features.py`
- Create: `scripts/build_pool_feature_table.py`
- Create: `tests/test_cone_features.py`
- Create: `tests/test_build_pool_feature_table.py`

**Interfaces:**
- Produces: `previous_or_equal_with_age(rows: Sequence[TimestampedValue], timestamp_ms: int, max_age_ms: int | None) -> AsOfValue | None`.
- Produces: `TimestampedValue(timestamp_ms: int, value: object)`.
- Produces: `AsOfValue(timestamp_ms: int, value: object, age_ms: int)`.
- Produces: `causal_percentile(prior_values: Sequence[Decimal], current: Decimal) -> Decimal | None`.
- Produces CLI: `python scripts/build_pool_feature_table.py --pool uni-base --csv data/uni_base_pool_history.csv --db data/cngn.db --out data/derived/uni_base_pool_features.csv`.
- Output fields include `timestamp_ms`, `pool`, `block_number`, `tx_hash`, `log_index`, `raw_sqrt_mid`, `fee_adjusted_bid`, `fee_adjusted_ask`, `stored_cngn_usd_price`, `stored_price_model`, `realized_volatility`, `realized_volatility_cone_pct`, `dex_premium_bps`, `dex_premium_cone_pct`, `active_liquidity_cone_pct`, `active_liquidity_running_max_share`, `active_liquidity_running_max_share_cone_pct`, `active_liquidity_running_max_denominator`, `swap_flow_imbalance`, `swap_flow_imbalance_cone_pct`, `fee_intensity_proxy`, `fee_intensity_proxy_cone_pct`, `fee_intensity_proxy_model`, `volume_cone_pct`, `source_age_ms`.

- [ ] **Step 1: Write failing causal percentile test**

```python
from decimal import Decimal
from backtester.cone_features import causal_percentile

def test_causal_percentile_uses_only_prior_values():
    assert causal_percentile([Decimal("1"), Decimal("3"), Decimal("5")], Decimal("4")) == Decimal("0.6666666667")
    assert causal_percentile([], Decimal("4")) is None
```

- [ ] **Step 2: Write failing as-of join age test**

```python
from backtester.cone_features import TimestampedValue, previous_or_equal_with_age

def test_previous_or_equal_reports_age_and_respects_max_age():
    rows = [TimestampedValue(1000, "a"), TimestampedValue(2000, "b")]
    assert previous_or_equal_with_age(rows, 2500, 600).age_ms == 500
    assert previous_or_equal_with_age(rows, 2500, 400) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_cone_features.py`

Expected: FAIL with `ModuleNotFoundError` for `backtester.cone_features`.

- [ ] **Step 4: Implement cone helpers**

Use `bisect_right` for as-of joins and count `prior <= current` for percentile. Round percentile to 10 decimal places using `Decimal("0.0000000001")`.

- [ ] **Step 5: Implement feature table builder**

Read pool CSV rows, derive swap flow, compute returns from prior swap price, maintain causal histories per feature, and write one row per swap. Use only prior values when computing cone percentiles.

- [ ] **Step 6: Run tests**

Run: `python -m pytest -q tests/test_cone_features.py tests/test_build_pool_feature_table.py`

Expected: PASS.

---

### Task 5: Fair Value Markout As-Of Feature Integration

**Files:**
- Modify: `scripts/export_fair_price_markouts.py`
- Modify: `tests/test_export_fair_price_markouts.py`
- Modify: `tests/test_analyze_fair_price_markouts.py`

**Interfaces:**
- Adds CLI arg: `--pool-feature-csv POOL=PATH`, repeatable.
- Adds CLI arg: `--feature-max-age-seconds`, default `900`.
- Adds CLI arg: `--quality-out PATH`.
- Adds markout fields: `uni_base_feature_age_ms`, `uni_base_dex_premium_cone_pct`, `uni_base_swap_flow_imbalance_cone_pct`, `uni_base_active_liquidity_cone_pct`, and BSC equivalents.
- Produces quality JSON with `missing_feature_counts`, `median_feature_age_ms`, `max_feature_age_ms`.
- Extends: `build_markout_rows(..., pool_feature_rows: Mapping[str, list[dict[str, str]]] | None = None, feature_max_age_ms: int | None = None) -> list[dict[str, str]]`.

- [ ] **Step 1: Write failing feature join test**

```python
from decimal import Decimal
from scripts.export_fair_price_markouts import PriceSnapshot, build_markout_rows

def test_markout_export_joins_pool_features_by_previous_or_equal_timestamp():
    snapshots = [
        PriceSnapshot("quidax", 10_000, Decimal("1"), Decimal("1"), Decimal("1"), {"capture_type": "ticker_depth"}),
        PriceSnapshot("quidax", 20_000, Decimal("1.1"), Decimal("1.1"), Decimal("1.1"), {"capture_type": "ticker_depth"}),
    ]
    feature_rows = {
        "uni-base": [
            {"timestamp_ms": "9000", "dex_premium_cone_pct": "0.90", "swap_flow_imbalance_cone_pct": "0.80", "active_liquidity_cone_pct": "0.10"}
        ]
    }
    rows = build_markout_rows(snapshots, horizons_seconds=[10], target_usd=Decimal("100"), pool_feature_rows=feature_rows, feature_max_age_ms=2000)
    assert rows[0]["uni_base_feature_age_ms"] == "1000"
    assert rows[0]["uni_base_dex_premium_cone_pct"] == "0.90"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_export_fair_price_markouts.py`

Expected: FAIL because `build_markout_rows` does not accept pool feature rows.

- [ ] **Step 3: Extend markout builder**

Keep existing behavior when no feature CSV is provided. When feature CSVs are provided, perform previous-or-equal joins and blank fields when source age exceeds max age.

- [ ] **Step 4: Add quality output test**

Assert the CLI writes JSON containing:

```json
{
  "feature_max_age_seconds": 900,
  "pool_features": {
    "uni-base": {
      "missing_rows": 0,
      "max_age_ms": 1000
    }
  }
}
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_export_fair_price_markouts.py tests/test_analyze_fair_price_markouts.py`

Expected: PASS.

---

### Task 6: Opportunity, Costed Backtest, and Stability Reports

**Files:**
- Create: `scripts/report_backtest_regime_stability.py`
- Create: `tests/test_report_backtest_regime_stability.py`
- Optionally modify: `backtester/run.py` only if a metric is unavailable from existing window CSVs.

**Interfaces:**
- Produces CLI: `python scripts/report_backtest_regime_stability.py --windows backtester/results/uni_base_paper_swapwf_windows.csv --features data/derived/uni_base_pool_features.csv --out backtester/results/uni_base_stability_report.md`.
- Produces: `parameter_jump_rows(window_rows: list[dict[str, str]], feature_by_window: dict[int, dict[str, float]], parameter_fields: list[str], regime_fields: list[str], min_regime_delta: float) -> list[dict[str, object]]`.
- Outputs:
  - opportunity screen summary
  - full costed backtest summary
  - return on capital
  - drawdown
  - churn/rebalance count
  - fee/cost ratio
  - capacity/share validity
  - sGHO APY benchmark comparison
  - selected parameter jumps by neighboring window
  - regime-feature deltas across neighboring windows
  - unexplained jump count

- [ ] **Step 1: Write failing parameter jump test**

```python
from scripts.report_backtest_regime_stability import parameter_jump_rows

def test_parameter_jumps_require_regime_explanation():
    rows = [
        {"window_index": "1", "fixed_width_pct": "0.01", "validation_net_return": "0.01"},
        {"window_index": "2", "fixed_width_pct": "0.05", "validation_net_return": "0.01"},
    ]
    features = {
        1: {"realized_volatility_cone_pct": 0.50},
        2: {"realized_volatility_cone_pct": 0.52},
    }
    jumps = parameter_jump_rows(rows, features, parameter_fields=["fixed_width_pct"], regime_fields=["realized_volatility_cone_pct"], min_regime_delta=0.20)
    assert jumps[0]["unexplained_jump"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_report_backtest_regime_stability.py`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement report helpers and Markdown output**

Treat `validation_total_transaction_cost == 0` as an opportunity-screen row and nonzero cost as full-costed row. If the window CSV does not contain both, report the missing section explicitly as `not_available`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_report_backtest_regime_stability.py`

Expected: PASS.

---

### Task 7: Gate Before Exporter Refactor

**Files:**
- No code files.
- Read: active processes and checkpoints.

**Interfaces:**
- Gate command: `ps -axo pid,ppid,stat,etime,command | rg 'update_v4_pool_history|export_v4_pool_history'`.
- Gate condition: no active `scripts/export_v4_pool_history.py` process for `uni-base` or `uni-bsc`.

- [ ] **Step 1: Check active exporters**

Run: `ps -axo pid,ppid,stat,etime,command | rg 'update_v4_pool_history|export_v4_pool_history'`

Expected before continuing to Task 8: no matching running exporter/updater lines.

- [ ] **Step 2: Snapshot completed CSVs**

Run:

```bash
mkdir -p data/snapshots
cp data/uni_base_pool_history.csv data/snapshots/uni_base_pool_history_pre_replay_refactor.csv
cp data/uni_bsc_pool_history.csv data/snapshots/uni_bsc_pool_history_pre_replay_refactor.csv
```

Expected: both snapshot files exist and have the same line counts as the source files at copy time.

- [ ] **Step 3: Run read-only quality reports**

Run:

```bash
python scripts/report_pool_history_quality.py --csv data/uni_base_pool_history.csv --pool uni-base --out data/quality/uni_base_pool_history.md
python scripts/report_pool_history_quality.py --csv data/uni_bsc_pool_history.csv --pool uni-bsc --out data/quality/uni_bsc_pool_history.md
```

Expected: both reports are written and show `monotonic_blocks: true`.

---

### Task 8: Event-Time Pool Price Replay

**Files:**
- Create: `backtester/v4_event_replay.py`
- Create: `tests/test_v4_event_replay.py`
- Later modify: `backtester/v4_export.py`
- Later modify: `tests/test_v4_export.py`

**Interfaces:**
- Produces: `ReplayEvent(block_number: int, log_index: int, event_order: int, event_type: str, sqrt_price_x96: int | None, tick: int | None)`.
- Produces: `attach_event_time_state(events: Sequence[ReplayEvent], initial_state: PoolStateSnapshot | None) -> list[ReplayedEvent]`.
- `PoolStateSnapshot` has `sqrt_price_x96`, `tick`, `source`.
- `ReplayedEvent` carries `event_time_sqrt_price_x96`, `event_time_tick`, `event_time_state_source`.
- `initial_state=None` is allowed only when the sorted event stream first seeds price from an initialize or swap row.
- State source labels are `self_event`, `same_block_prior_event`, `prior_event`, or the provided initial seed source.

- [x] **Step 1: Write failing same-block lookahead test**

```python
from backtester.v4_event_replay import PoolStateSnapshot, ReplayEvent, attach_event_time_state

def test_liquidity_event_before_swap_gets_prior_price_not_swap_price():
    events = [
        ReplayEvent(100, 5, 0, "mint", None, None),
        ReplayEvent(100, 6, 0, "swap", 222, 2),
        ReplayEvent(100, 7, 0, "burn", None, None),
    ]
    replayed = attach_event_time_state(events, PoolStateSnapshot(111, 1, "prior_block"))
    assert replayed[0].event_time_sqrt_price_x96 == 111
    assert replayed[0].event_time_state_source == "prior_block"
    assert replayed[2].event_time_sqrt_price_x96 == 222
    assert replayed[2].event_time_state_source == "same_block_prior_event"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_v4_event_replay.py`

Expected: FAIL with `ModuleNotFoundError`.

- [x] **Step 3: Implement replay helper**

Sort by `(block_number, log_index, event_order)`. Update carried state only on initialize/swap events with non-null sqrt/tick. Raise `ValueError` if a liquidity event appears before any seed state.

- [x] **Step 4: Integrate into exporter after active collectors finish**

Modify liquidity row construction so `sqrt_price_x96`, `tick`, and `cngn_usd_price` use replayed event-time state rather than `state_view.getSlot0(..., block_identifier=block_number)` for all events in the block.

- [x] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_v4_event_replay.py tests/test_v4_export.py`

Expected: PASS.

Implementation note: the exporter applies replay as a post-decode, pre-write pass over each chunk, preserving the raw CSV schema. It also normalizes the ERC-20 `Transfer` topic constant through `coerce_hex_str` so Web3.py v7 `HexBytes.hex()` output and normalized log topics compare consistently.

---

### Task 9: Paper-Faithful LP Lifecycle Ledger

**Files:**
- Create: `backtester/v4_lp_ledger.py`
- Create: `scripts/export_v4_lp_ledger.py`
- Create: `tests/test_v4_lp_ledger.py`

**Interfaces:**
- Produces: `DecodedLiquidityAction(action_type: str, block_number: int, log_index: int, event_order: int, token_id: int, lp_owner: str | None, tick_lower: int | None, tick_upper: int | None, liquidity_delta: int, amount0: Decimal, amount1: Decimal, collect_amount0: Decimal)`.
- Produces: `OwnershipEvent(block_number: int, log_index: int, token_id: int, previous_owner: str | None, new_owner: str | None)`.
- Produces: `LPLedgerRow` with fields from `autoresearch/data-methodology-refactor.md`.
- Produces: `build_lp_ledger_rows(decoded_actions: Sequence[DecodedLiquidityAction], ownership_events: Sequence[OwnershipEvent], price_events: Sequence[ReplayedEvent]) -> list[LPLedgerRow]`.
- Produces CLI: `python scripts/export_v4_lp_ledger.py --pool uni-base --start-block 42926879 --end-block 47130126 --out data/derived/uni_base_lp_ledger.csv`.

- [x] **Step 1: Write failing owner/token-id ledger test**

```python
from backtester.v4_event_replay import ReplayedEvent
from backtester.v4_lp_ledger import DecodedLiquidityAction, OwnershipEvent, build_lp_ledger_rows

def test_collect_row_carries_token_id_owner_range_and_event_price():
    rows = build_lp_ledger_rows(
        decoded_actions=[
            DecodedLiquidityAction("mint", 100, 10, 0, 42, "0xowner", -120, 120, 1000, 0, 1, 2),
            DecodedLiquidityAction("collect", 100, 20, 1, 42, "0xowner", -120, 120, 0, 3, 4, 0),
        ],
        ownership_events=[OwnershipEvent(100, 9, 42, None, "0xowner")],
        price_events=[ReplayedEvent(100, 10, 0, "mint", 111, 1, "prior_block"), ReplayedEvent(100, 20, 1, "collect", 222, 2, "same_block_prior_event")],
    )
    assert rows[1].token_id == 42
    assert rows[1].lp_owner == "0xowner"
    assert rows[1].tick_lower == -120
    assert rows[1].collect_amount0 == 3
    assert rows[1].sqrt_price_x96_at_event == 222
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_v4_lp_ledger.py`

Expected: FAIL with `ModuleNotFoundError`.

- [x] **Step 3: Implement ledger types and pure row builder**

Keep network/RPC code in `scripts/export_v4_lp_ledger.py`; keep deterministic matching and ownership logic in `backtester/v4_lp_ledger.py`.

- [x] **Step 4: Add CLI smoke test with fixture data**

Use monkeypatched RPC readers or local fixture inputs. Do not hit network in unit tests.

- [x] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_v4_lp_ledger.py`

Expected: PASS.

Implementation note: Task 9 currently provides the pure ledger dataclasses,
deterministic owner/range/liquidity matching, event-time price attachment, and
a fixture-backed CSV export smoke path. Full RPC decoding of PositionManager
actions into fixture-equivalent `DecodedLiquidityAction` rows remains the next
Task 9 continuation before population-level ledger exports.

---

### Task 10: Paper Episode Reconstruction and Taxonomy

**Files:**
- Create: `backtester/lp_paper_episodes.py`
- Create: `tests/test_lp_paper_episodes.py`

**Interfaces:**
- Produces: `reconstruct_paper_episodes(rows: Sequence[LPLedgerRow]) -> list[PaperLPEpisode]`.
- Produces: `classify_position_type(start_price: Decimal, end_price: Decimal, lower: Decimal, upper: Decimal, pnl: Decimal) -> int`.
- Produces: `paper_win_score(episodes: Sequence[PaperLPEpisode], start_ms: int, end_ms: int) -> Decimal`.
- Produces: `PaperLPEpisode(pool: str, lp_owner: str, tick_lower: int, tick_upper: int, open_ms: int, close_ms: int, opening_capital: Decimal, closing_capital: Decimal, pnl: Decimal, start_price: Decimal, end_price: Decimal, lower_price: Decimal, upper_price: Decimal, closed_liquidity: Decimal, position_type: int | None, delta_traversed: Decimal | None)`.

- [x] **Step 1: Write failing FIFO partial-burn test**

```python
from decimal import Decimal
from backtester.v4_lp_ledger import LPLedgerRow
from backtester.lp_paper_episodes import reconstruct_paper_episodes

def ledger_row(action_type, owner, tick_lower, tick_upper, liquidity_delta, amount0=Decimal("0"), amount1=Decimal("0"), collect_amount0=Decimal("0"), collect_amount1=Decimal("0"), price=Decimal("1"), timestamp_ms=1000):
    return LPLedgerRow(
        chain="base",
        pool_id="0xpool",
        block_number=timestamp_ms,
        block_time="2026-01-01T00:00:00+00:00",
        tx_hash=f"0x{timestamp_ms}",
        log_index=0,
        event_order=0,
        event_type=action_type,
        position_manager="0xpm",
        token_id=1,
        lp_owner=owner,
        owner_source="transfer",
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_delta=liquidity_delta,
        liquidity_after=max(liquidity_delta, 0),
        amount0=amount0,
        amount1=amount1,
        amount0_raw=str(amount0),
        amount1_raw=str(amount1),
        collect_amount0=collect_amount0,
        collect_amount1=collect_amount1,
        sqrt_price_x96_at_event=79228162514264337593543950336,
        tick_at_event=0,
        cngn_usd_price_at_event=price,
        timestamp_ms=timestamp_ms,
    )

def test_partial_burn_closes_first_position_and_splits_payout():
    rows = [
        ledger_row("mint", owner="0xlp", tick_lower=-100, tick_upper=100, liquidity_delta=100, amount0=Decimal("10"), amount1=Decimal("10"), price=Decimal("1"), timestamp_ms=1000),
        ledger_row("mint", owner="0xlp", tick_lower=-100, tick_upper=100, liquidity_delta=100, amount0=Decimal("20"), amount1=Decimal("20"), price=Decimal("1"), timestamp_ms=2000),
        ledger_row("burn_collect", owner="0xlp", tick_lower=-100, tick_upper=100, liquidity_delta=-150, collect_amount0=Decimal("45"), collect_amount1=Decimal("45"), price=Decimal("1"), timestamp_ms=3000),
    ]
    episodes = reconstruct_paper_episodes(rows)
    assert len(episodes) == 1
    assert episodes[0].closed_liquidity == Decimal("100")
    assert episodes[0].closing_capital == Decimal("60")
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_lp_paper_episodes.py`

Expected: FAIL with `ModuleNotFoundError`.

- [x] **Step 3: Implement reconstruction**

Group by `(pool, lp_owner, tick_lower, tick_upper)`, drop over-burns that exceed observed liquidity, close FIFO, and split collect payout by consumed liquidity fraction.

- [x] **Step 4: Add taxonomy and win-score tests**

Assert:

```python
from decimal import Decimal
from backtester.lp_paper_episodes import PaperLPEpisode, classify_position_type, paper_win_score

assert classify_position_type(Decimal("0.9"), Decimal("1.1"), Decimal("1.0"), Decimal("1.2"), Decimal("1")) == 3
winning_episode = PaperLPEpisode("uni-base", "0xlp", -100, 100, 0, 5_000, Decimal("100"), Decimal("110"), Decimal("10"), Decimal("1"), Decimal("1.1"), Decimal("0.9"), Decimal("1.2"), Decimal("100"), 5, Decimal("0.1"))
losing_episode = PaperLPEpisode("uni-base", "0xlp", -100, 100, 5_000, 10_000, Decimal("100"), Decimal("90"), Decimal("-10"), Decimal("1"), Decimal("0.9"), Decimal("0.9"), Decimal("1.2"), Decimal("100"), 6, Decimal("-0.1"))
assert paper_win_score([winning_episode, losing_episode], 0, 10_000) == Decimal("0.5")
```

- [x] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_lp_paper_episodes.py`

Expected: PASS.

Implementation note: Task 10 now provides pure paper episode reconstruction
from `LPLedgerRow` inputs. The implementation closes mint lots FIFO only when a
lot is fully consumed, carries partial-burn proceeds forward, caps over-burns to
observed in-sample liquidity, values capital according to each pool's cNGN/stable
token orientation, derives range bounds from event-time tick math, and exposes
the first taxonomy/win-score helpers. Full 15-type taxonomy expansion remains a
later derived-feature task.

---

### Task 11: Receipt and Gas Sidecar

**Files:**
- Create: `scripts/export_tx_receipts.py`
- Create: `tests/test_export_tx_receipts.py`

**Interfaces:**
- Produces CLI: `python scripts/export_tx_receipts.py --chain base --tx-csv data/derived/uni_base_lp_ledger.csv --out data/derived/uni_base_tx_receipts.csv`.
- Output fields: `chain`, `tx_hash`, `block_number`, `gas_used`, `effective_gas_price_wei`, `native_fee_wei`, `tx_from`, `tx_to`.

- [x] **Step 1: Write failing receipt normalization test**

```python
from scripts.export_tx_receipts import receipt_row

def test_receipt_row_computes_native_fee():
    row = receipt_row("base", {"transactionHash": "0x1", "blockNumber": 10, "gasUsed": 21_000, "effectiveGasPrice": 2_000_000_000, "from": "0xfrom", "to": "0xto"})
    assert row["native_fee_wei"] == "42000000000000"
    assert row["tx_hash"] == "0x1"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_export_tx_receipts.py`

Expected: FAIL with `ModuleNotFoundError`.

- [x] **Step 3: Implement receipt exporter**

Deduplicate tx hashes before RPC calls. Write rows sorted by `block_number,tx_hash`. Fail if a receipt is missing; do not silently skip.

- [x] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_export_tx_receipts.py`

Expected: PASS.

Implementation note: Task 11 now provides `scripts/export_tx_receipts.py` with
receipt normalization, input-CSV tx hash deduplication, fail-fast missing receipt
checks, deterministic `block_number,tx_hash` output sorting, and a CLI that
builds a Web3 client from the configured Base/BSC pool RPCs. Unit tests use a
fake Web3 client and do not hit network.

---

### Task 12: Integration Runbook and First Non-Destructive Run

**Files:**
- Modify: `autoresearch/data-methodology-refactor.md`
- Modify: `dashboard/docs/lp/pool-history-operations.md`
- Create: `data/derived/README.md`

**Interfaces:**
- Documents run order:
  1. Wait for pool exporters to finish.
  2. Run quality reports.
  3. Snapshot CSVs.
  4. Import pool snapshots into `price_snapshots`.
  5. Build pool feature tables.
  6. Export Fair Value markouts with feature CSVs.
  7. Run stability diagnostics.
  8. Run LP ledger and paper episode reconstruction.

- [x] **Step 1: Add derived artifact README**

Create `data/derived/README.md` with exact filenames:

```markdown
# Derived Research Artifacts

- `uni_base_pool_features.csv`: causal swap-level pool features for Base.
- `uni_bsc_pool_features.csv`: causal swap-level pool features for BSC.
- `uni_base_lp_ledger.csv`: paper-faithful LP lifecycle ledger for Base.
- `uni_bsc_lp_ledger.csv`: paper-faithful LP lifecycle ledger for BSC.
- `uni_base_tx_receipts.csv`: gas sidecar for Base LP lifecycle transactions.
- `uni_bsc_tx_receipts.csv`: gas sidecar for BSC LP lifecycle transactions.
```

- [x] **Step 2: Update docs**

Add the exact run commands to `dashboard/docs/lp/pool-history-operations.md` under `Research Methodology Guardrails`.

- [ ] **Step 3: Run non-network tests**

Run:

```bash
python -m pytest -q \
  tests/test_pool_history_quality.py \
  tests/test_pool_features.py \
  tests/test_import_pool_snapshots.py \
  tests/test_cone_features.py \
  tests/test_build_pool_feature_table.py \
  tests/test_export_fair_price_markouts.py \
  tests/test_report_backtest_regime_stability.py \
  tests/test_v4_event_replay.py \
  tests/test_v4_lp_ledger.py \
  tests/test_lp_paper_episodes.py \
  tests/test_export_tx_receipts.py
```

Expected: PASS.

Verification note: The exact command currently runs 54 tests and then fails only
on `tests/test_export_fair_price_markouts.py::test_load_price_snapshots_reads_metadata_from_sqlite`
because this Python 3.12 environment does not have an active async pytest
plugin (`pytest-asyncio` mark is unknown). The same non-network suite with that
one plugin-gated test deselected passed: 57 passed, 1 deselected.

- [x] **Step 4: Run first read-only reports on a CSV snapshot**

Run against `data/snapshots/*_pre_replay_refactor.csv`, not active CSVs.

Expected: reports are written under `data/quality/` and derived CSVs are written under `data/derived/`.

Implementation note: Snapshot quality reports, replay-corrected CSVs, replay
quality reports, and causal pool feature tables were regenerated from the
snapshot inputs. Base replay produced 1,596 rows and 1,508 feature rows; BSC
replay produced 3,119 rows and 3,105 feature rows. Replay quality reports show
`unexplained_price_mismatch_count=0`, `duplicate_events=0`, and
`monotonic_blocks=True` for both pools.

---

## Least-Invasive Execution Order

1. Implement Tasks 1-6 while the current pool exporters run. These tasks are additive and do not touch running exporter/updater code.
2. Wait for both current pool exporters to finish.
3. Snapshot the completed CSVs.
4. Run Tasks 7-8 to fix event-time price methodology.
5. Run Tasks 9-11 to build paper-faithful LP reconstruction and receipt sidecars.
6. Run Task 12 to document and execute the first non-destructive derived-data pass.

## Verification Matrix

- Normal path: fixture CSV -> quality report -> pool snapshot bridge -> feature table -> markout export -> stability report.
- Failure path: malformed token order, duplicate tx/log, stale as-of feature, timestamp collision above 1000 rows per source second, missing receipt.
- Integration edge: same-block liquidity event before swap must use prior price; liquidity event after swap must use same-block prior swap price.
- Replay rebuild edge: use `scripts/replay_pool_history_prices.py` for local corrected research histories from completed CSVs. A full from-genesis RPC export is not the default rebuild path until the PositionManager candidate scan is optimized.
