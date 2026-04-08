---
title: Architecture
order: 2
---

## Layers

The engine is organised in layers. Each layer depends only on layers below it.

**`engine/types.py`** — zero-dependency home for all shared domain types. Any layer may import from here freely. Covers cross-cutting types (`Position`, `LPPosition`, `PriceQuote`, `TxResult`), order book types (`OrderBookLevel`, `OrderBookDepth`), config-adjacent params (`CexParams`, `WalletParams`), account types (`AccountInfo`, `AccountBalanceResponse`, `Alert`, etc.), and arb-domain types (`ArbitrageOpportunity`, `ArbitrageTrade`, `ArbitrageStatus`, etc.). `engine/api/schemas.py` contains only HTTP-specific response types (`VenueStatus`, `SystemStatus`, `GlobalPosition`, etc.) and does not re-export from `engine/types.py`. No module outside `engine/api/` should import from `engine/api/`.

**`engine/venues/`** — thin adapters over on-chain contracts and CEX APIs. No strategy logic lives here. Adapters expose a uniform interface: `get_position()`, `swap()`, `get_current_price()`, etc. LP position management is **not** part of the adapter; it lives in `engine/lp/uniswap_v4.py`.

**`engine/market/`** — shared market data and layer-safe shared services: pool state cache (`pool_state.py`), price feeds and aggregation (`price_aggregation.py`, `venue_prices.py`), DEX volume tracking (`dex_volume.py`), gas cost oracle (`gas_oracle.py`), and the global portfolio snapshot service (`portfolio_exposure.py` + `portfolio_registry.py`). These modules must stay importable in isolation: no HTTP-specific schemas, no concrete venue adapters, and no eager package exports that pull in higher layers. Imports from `engine/types.py` are allowed.

**`engine/lp/`** — LP strategy and position management. `strategy.py` contains pure math (EWMA stats, tick range calculation). `rebalancer.py` orchestrates the position lifecycle: check→remove→remint. `uniswap_v4.py` owns `V4PositionManager`, which manages all LP position operations for a single V4 pool (position queries, portfolio balances, mint/remove/ratio-swap). Nothing here knows arb exists. `DexParams` lives in `engine/config.py`; tick/ratio protocol math lives in `venues/dex/shared.py`.

**`engine/arb/`** — arbitrage layer. Internally grouped into four subdirectories: `detection/` (opportunity finding for CEX-DEX and DEX-DEX paths), `execution/` (route execution, preflight checks, half-open recovery), `risk/` (inventory tracking, trade history), `routing/` (route registry and size selection). Nothing here knows LP exists.

**`engine/db/`** — persistence layer. `repository.py` is a thin lifecycle container over SQLite connection management and schema bootstrap. It exposes focused domain stores (`system_state`, `prices`, `positions`, `actions`, `alerts`, `venue_config`, `arbitrage`, `history`, `pool_metrics`) and narrow protocols in `backend.py` so consumers depend only on the store surface they actually use.

**Wiring layer** — `engine/main.py` builds the long-lived runtime, stores it on `app.state.runtime`, and wires together venues, LP managers, market data, arb, scheduler, Telegram, and the DB stores. `engine/api/` exposes runtime state over HTTP. `engine/scheduler/` runs timed jobs and websocket listeners. `engine/accounts.py` manages HD wallet derivation.

---

## Runtime composition

At startup the app constructs a single `EngineRuntime` in `engine/runtime.py`. It contains the shared long-lived services:

- `db`
- `scheduler`
- `venues` — `dict[str, VenueAdapter]`, keyed by venue name; owns swap execution and price queries
- `lp_managers` — `dict[str, V4PositionManager]`, keyed by venue name; owns LP position management for DEX venues. Routes that need LP data go here; routes that need swap/price data go to `venues`.
- `price_aggregator`
- `arbitrage_engine`
- `account_manager`
- `blended_calculator`
- `normalizer`
- `token_contracts`
- `portfolio_exposure_calculator`
- `portfolio_source_registry`
- `quidax_lp`

FastAPI routes resolve everything from `app.state.runtime` via shared dependencies in `engine/api/deps.py`. There is no parallel `app.state.db` or module-global route state.

The API package is intentionally split by concern:

- `engine/api/router.py` composes the top-level router
- `engine/api/deps.py` owns runtime/service dependency resolution and auth checks
- `engine/api/helpers/` holds shared non-route helpers
- `engine/api/protocols.py` holds small route-facing protocols
- `engine/api/routes/` contains domain routers such as `system`, `prices`, `positions`, `venues`, `arbitrage`, and `accounts`

The scheduler follows the same pattern:

- `engine/scheduler/core.py` owns APScheduler lifecycle, registration, and websocket wiring
- `engine/scheduler/context.py` holds shared scheduler dependencies
- `engine/scheduler/jobs/` contains cohesive job coordinators for market, positions, LP, arbitrage, and accounts
- `engine/scheduler/config.py` holds `SchedulerConfig`
- `engine/scheduler/types.py` holds shared scheduler types and small protocols

---

## Data flows

**Prices in:** On-chain swap event → `market/pool_state` cache updated inline (zero RPC) → arb detection reads cache → LP rebalance check reads cache.

**Arb signal out:** Detection signal → `arb/routing/router.select_route` → `arb/execution/route_execution.execute_route` → on-chain transaction → DB insert.

**LP cycle:** Scheduler timer → `lp/rebalancer.check_and_rebalance` → `lp/strategy.calculate_tick_range` → `lp/uniswap_v4.V4PositionManager.mint_position`.

**API read path:** HTTP request → `api/deps.get_runtime` → domain router in `engine/api/routes/` → runtime service or DB store → response model.

**Global portfolio path:** HTTP request or scheduler tick → `market/portfolio_exposure.py` → all account-manager balances + registered additive sources from `market/portfolio_registry.py` → `/positions/global`, `/portfolio/exposure`, or `portfolio_delta` broadcast.

**Scheduler job path:** `TradingScheduler.start()` registers timers → delegated job coordinator in `engine/scheduler/jobs/` executes the business logic → events are broadcast over websocket/Telegram and persisted via domain stores.

---

## Dependency rules

These invariants keep layers independently testable and extractable:

- `market/` imports from `venues/` only
- `lp/` imports from `venues/` and `market/` only
- `arb/` imports from `venues/` and `market/` only
- `lp/` and `arb/` never import from each other
- `venues/` never imports from `lp/` or `arb/`
- All layers may import from `engine/config.py` (shared configuration types)
- All layers may import from `engine/types.py` (shared domain types: `Position`, `LPPosition`, `PriceQuote`, `TxResult`)
- `engine/market/portfolio_exposure.py` must not import from `engine/api/`, concrete venue adapters, or eager package exports from `engine.db`
- `engine/api/schemas.py` contains only HTTP-specific response types. All other shared types belong in `engine/types.py`

This is also the packageability rule:

- prices/dashboard should be easy to ship independently
- LP / market-maker-in-a-box should be easy to ship independently
- arbitrage should be easy to ship independently

Cross-subsystem dependencies should therefore stay rare and deliberate. Shared infrastructure is fine; shared business logic is not.

At the wiring edge:

- route modules should depend on `EngineRuntime` or narrow DB stores via `engine/api/deps.py`
- scheduler jobs should depend on `SchedulerContext` plus narrow store protocols, not the whole DB container
- domain logic should not import from FastAPI route modules

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
| Add a venue | `engine/venues/base.py` → venue adapter → route registry; if it contributes to global totals, also add one explicit entry in `engine/market/portfolio_registry.py` |
| Add a shared domain type | `engine/types.py` — add it here if it is used outside `engine/api/` |
| Tune LP strategy | `engine/lp/strategy.py` + `engine/config.py` LP section |
| Inspect LP position state | `engine/lp/uniswap_v4.py` (`V4PositionManager`) |
| Understand arb detection | `engine/arb/detection/cex_dex.py` and `dex_dex.py` |
| Trace an arb execution | `engine/arb/routing/route_registry.py` → `engine/arb/execution/route_execution.py` |
| Change scheduler timing | `engine/scheduler/core.py` + `engine/scheduler/config.py` |
| Add or move an API endpoint | `engine/api/router.py` → the matching module under `engine/api/routes/` |
| Change persistence behavior | `engine/db/repository.py` → matching query module under `engine/db/queries/` |
