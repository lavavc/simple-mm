---
title: Testing Architecture
order: 3
---

## Three-Tier Architecture

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

## Per-File Coverage

| File | Module | Tests |
|------|--------|-------|
| `test_params_validation.py` | `api/schemas.py` | DexParams/CexParams/WalletParams defaults, custom values |
| `test_schemas.py` | `api/schemas.py` | All Pydantic models |
| `test_config.py` | `config.py` | Settings defaults |
| `test_price_math.py` | `venues/dex/lp_v4.py` | Tick/price math, `compute_ewma_stats`, `calculate_tick_range` recovery skew |
| `test_capital_allocation.py` | `venues/dex/lp_v4.py` | `calculate_mint_amounts` caps |
| `test_price_aggregation.py` | `core/price_aggregation.py` | VWAP, confidence, blended price |
| `test_accounts.py` | `core/accounts.py` | HD derivation, role access |
| `test_inventory.py` | `core/arbitrage/inventory.py` | Limits, circuit breakers, `reconcile_stables` |
| `test_database.py` | `db/database.py` | SQLite CRUD |
| `test_orderbook.py` | `core/arbitrage/cex_dex.py` | `walk_orderbook_*`, `_ternary_search` |
| `test_pool_state.py` | `core/arbitrage/pool_state.py` | Swap math, `update_pool_state_from_event`, cache reads |
| `test_cex_dex.py` | `core/arbitrage/cex_dex.py` | `find_optimal_arb`, `compute_arb_curve` |
| `test_dex_dex.py` | `core/arbitrage/dex_dex.py` | `find_optimal_dex_arb` null cases and result structure |
| `test_router.py` | `core/arbitrage/router.py` | `select_route` sizing, filtering, tiebreak |
| `test_valuation.py` | `core/arbitrage/valuation.py` | `portfolio_value`, CEX/DEX holdings valuation |
| `test_scheduler.py` | `scheduler/core.py` + `scheduler/jobs/*` | Shell wiring plus `_check_dex_rebalance`, `_rebalance_dex_position`, `_create_dex_position`, DEX bootstrap and wallet activity delegation |
| `test_executor.py` | `core/arbitrage/executor.py` | Detection mode, CEX-CEX, half-open, unknown venue |
| `test_dex_fork.py` | V4 pool state + lifecycle | Section A: reads; Section B: funded wallet; Section C: rebalance |

## Adding Tests for a New Venue

1. **DEX venue** — add a `FakeDexAdapter` with the venue's token decimals configured, seed `_POOL_CACHE` with realistic sqrtPriceX96 values, add to `seeded_pool_cache` fixture in `conftest.py`.
2. **CEX venue** — add a `FakeCexAdapter` variant; test `place_market_order` success/failure paths.
3. **Fork tests** — add a new `anvil_<chain>` fixture in `conftest.py`, write Section A reads using `update_single_v4_pool_state` or `update_single_pool_state`.
