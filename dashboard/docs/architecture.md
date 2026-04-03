---
title: Architecture
order: 2
---

## Layers

The engine is organised in layers. Each layer depends only on layers below it.

**`engine/venues/`** — thin adapters over on-chain contracts and CEX APIs. No strategy logic lives here. Adapters expose a uniform interface: `get_position()`, `execute_swap()`, `mint_position()`, etc.

**`engine/market/`** — shared market data: pool state cache (`pool_state.py`), price feeds and aggregation (`price_aggregation.py`, `venue_prices.py`), DEX volume tracking (`dex_volume.py`), and gas cost oracle (`gas_oracle.py`). No business logic — only data collection and normalisation.

**`engine/lp/`** — LP strategy layer. `config.py` holds `DexParams` (all tunable parameters). `strategy.py` contains pure math functions (EWMA stats, tick range calculation, price↔tick conversion, required ratio). `rebalancer.py` orchestrates the position lifecycle: check→remove→remint. Nothing here knows arb exists.

**`engine/arb/`** — arbitrage layer. Internally grouped into four subdirectories: `detection/` (opportunity finding for CEX-DEX and DEX-DEX paths), `execution/` (route execution, preflight checks, half-open recovery), `risk/` (inventory tracking, trade history), `routing/` (route registry and size selection). Nothing here knows LP exists.

**Wiring layer** — `engine/scheduler.py` drives both LP and arb on APScheduler timers. `engine/api/` exposes state over HTTP. `engine/db/` persists actions, alerts, and history. `engine/accounts.py` manages HD wallet derivation.

---

## Data flows

**Prices in:** On-chain swap event → `market/pool_state` cache updated inline (zero RPC) → arb detection reads cache → LP rebalance check reads cache.

**Arb signal out:** Detection signal → `arb/routing/router.select_route` → `arb/execution/route_execution.execute_route` → on-chain transaction → DB insert.

**LP cycle:** Scheduler timer → `lp/rebalancer.check_and_rebalance` → `lp/strategy.calculate_tick_range` → `venues/dex/lp_v4.mint_position`.

---

## Dependency rules

These invariants keep layers independently testable and extractable:

- `market/` imports from `venues/` only
- `lp/` imports from `venues/` and `market/` only
- `arb/` imports from `venues/` and `market/` only
- `lp/` and `arb/` never import from each other
- `venues/` never imports from `lp/` or `arb/`

---

## Configuration

All LP strategy parameters are per-venue env vars. Defaults and env var names:

| Parameter | Base default | BSC default | Env var (Base / BSC) |
|---|---|---|---|
| `sd_multiplier` | 2.75 | 3.0 | `UNI_BASE_SD_MULTIPLIER` / `UNI_BSC_SD_MULTIPLIER` |
| `ewma_lambda` | 0.975 | 0.975 | `UNI_BASE_EWMA_LAMBDA` / `UNI_BSC_EWMA_LAMBDA` |
| `downside_skew` | 0.45 | 0.5 | `UNI_BASE_DOWNSIDE_SKEW` / `UNI_BSC_DOWNSIDE_SKEW` |
| `min_tick_width` | 100 | 100 | `UNI_BASE_MIN_TICK_WIDTH` / `UNI_BSC_MIN_TICK_WIDTH` |
| `max_tick_width` | 1000 | 1000 | `UNI_BASE_MAX_TICK_WIDTH` / `UNI_BSC_MAX_TICK_WIDTH` |
| `lookback_points` | None (all) | None (all) | `UNI_BASE_LOOKBACK_POINTS` / `UNI_BSC_LOOKBACK_POINTS` |
| `rebalance_threshold_percent` | 10.0 | 10.0 | `UNI_BASE_REBALANCE_THRESHOLD_PERCENT` / `UNI_BSC_REBALANCE_THRESHOLD_PERCENT` |
| `max_slippage_percent` | 1.0 | 1.0 | `UNI_BASE_MAX_SLIPPAGE_PERCENT` / `UNI_BSC_MAX_SLIPPAGE_PERCENT` |

`settings.uni_base_lp_params` and `settings.uni_bsc_lp_params` are `@property` methods on `Settings` that return a fully-constructed `DexParams` object from the above fields. The adapter constructors call these properties; individual fields are never accessed from outside `config.py`.

Arbitrage thresholds use the `ARBITRAGE_*` prefix (e.g. `ARBITRAGE_MIN_PROFIT_USD`, `ARBITRAGE_MAX_SINGLE_TRADE_USD`). All defaults are in `engine/config.py` — not in `.env`.

---

## Entry points

| Goal | Start reading here |
|---|---|
| Add a venue | `engine/venues/base.py` → `engine/venues/dex/uniswap_base.py` |
| Tune LP strategy | `engine/lp/strategy.py` + `engine/config.py` LP section |
| Understand arb detection | `engine/arb/detection/cex_dex.py` and `dex_dex.py` |
| Trace an arb execution | `engine/arb/routing/route_registry.py` → `engine/arb/execution/route_execution.py` |
| Change scheduler timing | `engine/scheduler.py` + `SchedulerConfig` in the same file |
