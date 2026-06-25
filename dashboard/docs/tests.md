---
title: Testing Architecture
order: 3
---

## Test Tiers

| Tier | When | Pattern |
|------|------|---------|
| **Pure unit** | No external deps | Call the function directly |
| **Seeded cache** | Needs `_POOL_CACHE` state | `monkeypatch` the global dict via `seeded_pool_cache` fixture |
| **Fake adapter** | Scheduler/executor needs a venue | `FakeDexAdapter` / `FakeCexAdapter` in-process doubles |
| **Anvil fork** | Real contracts, real EVM math | Spawned Anvil process (`anvil_base`, `anvil_bsc` fixtures) |

Mocks (`AsyncMock`) are reserved for the DB layer in scheduler tests only.

## Static Checking

```bash
source .venv/bin/activate && python -m mypy engine --no-error-summary
```

`mypy` runs in strict mode on `engine/`, the production Python package. It intentionally excludes `tests/` and `dashboard/` because test doubles are looser by design and frontend code is checked by its own toolchain.

## Running the Suite

```bash
source .venv/bin/activate && python -m pytest -x -q --ignore=tests/test_dex_fork.py
```

Fork tests (requires `anvil` CLI from Foundry):

```bash
source .venv/bin/activate && python -m pytest -q tests/test_dex_fork.py -v
```

CI runs the strict `mypy` check above plus the default pytest command inside Docker.
It intentionally skips `tests/test_dex_fork.py` because those tests require `anvil` and fork-capable RPC endpoints.

## Coverage Map

This map is grouped by behavior rather than by every test file, so it stays
useful as filenames change.

| Area | Primary modules | Representative tests |
|------|-----------------|----------------------|
| Shared contracts and config | `engine/types.py`, `engine/config.py`, `engine/api/schemas.py` | `test_params_validation.py`, `test_schemas.py`, `test_config.py` |
| Market data and fair price | `engine/market/price_aggregation.py`, `engine/market/fair_price.py`, `engine/market/venue_prices.py`, `research/scripts/` | `test_price_aggregation.py`, `test_market_fair_price.py`, `test_executable_fair_price.py`, `research/tests/test_fair_price_feed_quality.py`, `research/tests/test_capture_fair_price_feeds.py`, `research/tests/test_export_fair_price_markouts.py`, `research/tests/test_analyze_fair_price_markouts.py` |
| DEX math and pool state | `engine/math/v3.py`, `engine/venues/dex/shared.py`, `engine/market/pool_state.py`, `engine/market/dex_volume.py` | `test_price_math.py`, `test_pool_state.py`, `test_pool_history_update.py`, `test_pool_history_launchd.py`, `test_dex_volume.py`, `research/tests/test_v4_export.py`, `test_dex_fork.py` |
| LP lifecycle and research | `engine/lp/strategy.py`, `engine/lp/policy.py`, `engine/lp/rebalancer.py`, `engine/lp/uniswap_v4.py`, `engine/lp/research.py`, `research/backtester/` | `test_lp_strategy.py`, `test_lp_ratio.py`, `test_lp_e2e.py`, `test_lp_research.py`, `test_export_lp_position_history.py`, `research/tests/test_backtester.py` |
| Arbitrage detection, routing, and risk | `engine/arb/detection/`, `engine/arb/routing/`, `engine/arb/risk/`, `engine/arb/valuation.py` | `test_orderbook.py`, `test_cex_dex.py`, `test_dex_dex.py`, `test_router.py`, `test_inventory.py`, `test_arb_guards.py`, `test_arb_persistence.py`, `test_valuation.py` |
| Arbitrage execution and recovery | `engine/arb/execution/`, `engine/arb/engine.py`, `engine/arb/listener.py` | `test_executor.py`, `test_cex_dex_execution.py`, `test_dex_dex_execution.py`, `test_listener.py` |
| API, scheduler, accounts, and persistence | `engine/api/`, `engine/scheduler/`, `engine/accounts.py`, `engine/db/`, `engine/ws.py`, `engine/web3_utils.py` | `test_api_routes.py`, `test_scheduler.py`, `test_accounts.py`, `test_database.py`, `test_ws.py`, `test_web3_boundaries.py`, `test_telegram.py`, `test_portfolio_exposure.py` |

## Adding Tests for a New Venue

1. **DEX venue** — add a `FakeDexAdapter` with the venue's token decimals configured, seed `_POOL_CACHE` with realistic sqrtPriceX96 values, add to the relevant fixture in `conftest.py`.
2. **CEX venue** — add a `FakeCexAdapter` variant; test `place_market_order` success/failure paths.
3. **Fork tests** — add a new `anvil_<chain>` fixture in `conftest.py`, write Section A reads using `update_single_v4_pool_state` or `update_single_pool_state`.
