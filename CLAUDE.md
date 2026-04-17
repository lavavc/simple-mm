## Voice
No first-person singular wfor self-reference. Use Claude/Codex when referring to the model, or we/us/our.

## Source Of Truth
Code and typed contracts → tests pinning non-obvious behavior → docs in `dashboard/docs/`.

Docs must not overrule working code. If docs and code disagree, fix the docs in the same change unless the code is deliberately being corrected.

Authoritative typed sources:
- `engine/types.py` — all shared domain types
- `engine/arb/routing/route_registry.py`
- `engine/api/schemas.py` — HTTP-specific response wrappers only
- `engine/db/backend.py`, `engine/venues/base.py`, `engine/web3_utils.py`

## Architecture Rules

- `engine/types.py` is zero-dependency; any layer may import from here.
- No module outside `engine/api/` imports from `engine/api/`. `engine/api/schemas.py` holds HTTP-specific wrappers only and does not re-export from `engine/types.py`.
- `engine/venues/` — thin adapters only. No strategy, LP, or arb logic.
- `engine/market/` — market data, pool cache, volume, gas. No execution policy. Must stay importable in isolation: no `engine/api/` or concrete adapter imports.
- `engine/lp/` — LP strategy and V4 position management. No arb-specific knowledge.
- `engine/arb/` — detection, routing, execution, risk. No LP internals.
- `engine/db/` — depend on store protocols in `engine/db/backend.py`, not concrete query modules.
- `engine/scheduler/core.py` and `engine/main.py` are wiring shells. Business logic belongs in domain modules or `engine/scheduler/jobs/`.
- In `EngineRuntime`, `venues` and `lp_managers` are parallel dicts keyed by venue name. LP position state → `lp_managers`; swap execution or price queries → `venues`.
- Portfolio totals are governed by `engine/market/portfolio_registry.py`. Unregistered positions must not silently affect global totals.
- The engine must stay splittable into three packages (Prices/dashboard, LP, Arbitrage). Cross-subsystem dependencies need explicit justification. 
- LP core decisions (range setting, rerange, lifecycle) must not depend on arb or blended fair value. 

## Code Style

- No safe defaults for anything. Fail quickly and alert.
- No defensive coding, dead abstractions, or compatibility shims.
- No unused fields, parameters, or dataclass attributes after a refactor.
- Comments only where logic cannot be reconstructed without them.
- Prefer precise types, protocols, and narrowing helpers over `Any`.
- Normalize once at Web3 boundaries; keep the representation consistent throughout.

## Execution Flow Invariants

Review all when touching detection, routing, execution, recovery, or arb persistence.

**Route and sizing**
- `engine/arb/routing/route_registry.py` is the sole source of truth for direction, pipeline, leg type, venue names, and `cngn_effect`. No duplicate direction lists or ad hoc classifiers elsewhere.
- `select_route()` produces a decision (ranking, adjusted size, rescored profit, inventory tiebreaking). `SelectedRoute` holds routed size and profit metadata only — no execution-time token amounts or live state.

**Inventory**
- Inventory is venue-local. `quidax`, `uni-base`, `uni-bsc` balances are independent and capped independently. A fill on one venue makes nothing available on another.
- Router caps protect both legs: buy-side stablecoin and sell-side cNGN.

**Serialization**
- `_arb_executing` serializes all execution and recovery across both pipelines. No queue — signals during a live trade or recovery are skipped.

**Preflight**
- All on-chain legs must be preflighted with `simulate_swap()` before the live transaction. CEX REST legs are the explicit exception.
- Error handling in `engine/arb/execution/preflight.py` is intentionally asymmetric: `balance` errors zero inventory for the venue; `rpc`, `permit2`, and `unknown` errors do not mutate inventory; `pool_paused` trips the circuit breaker. Do not widen inventory mutation without a concrete incident-driven reason.

**Live amounts**
- Execution derives amounts locally at execution time. CEX sell legs use the actual buy fill. DEX sell legs use the preflight cNGN estimate — not a routing field or live wallet balance. Recovery uses persisted `buy_amount_cngn`, never a fresh wallet query.

**Half-open and recovery**
- Half-open = buy succeeded, sell failed. Immediately persist `buy_tx_hash`, `buy_amount_cngn`, executed size/status. Immediately trip the circuit breaker.
- CEX-DEX and DEX-DEX recovery are separate flows. DEX-DEX: retry sell if preflight passes, else reverse the buy. CEX-DEX: reverse the buy using the stored buy amount.

## Persistence And DB Invariants

- CEX-DEX and DEX-DEX use separate persistence paths: `update_arbitrage_opportunity` / `update_dex_arbitrage_execution_state`. Preserve that split.
- `buy_amount_cngn` and other recovery-critical fields must never be dropped by later updates.
- For Optional numeric fields from DB or schemas: use `is not None`, never truthiness when zero is meaningful.
- `engine/arb/risk/history.py` is append-only. Pipeline and direction come from the route registry.

## Scheduler Invariants

- `TradingScheduler` owns lifecycle and job registration only. Dependencies flow through `SchedulerContext` and narrow protocols. Do not add parallel ad hoc listeners for WS-driven arb updates or wallet activity.
- DEX arb bootstrap is gated: seed pool state → seed gas → first DEX-DEX update.

## Web3 And Typing Invariants

- `engine/web3_utils.py` normalizes hashes, topics, log data. Reuse it — no open-coded bytes/hex conversions.
- Transaction hashes at application and DB level stay plain `str`. Request payloads use typed shapes (`HexStr`, `TxParams`, `Wei`).
- `engine/` is the strict-mypy surface. Narrow from `VenueAdapter` through `is_dex_execution_venue(...)` — not `hasattr` checks or unchecked casts.

## Docs And Test Hygiene

When changing behavior, update the closest relevant docs in the same commit: `dashboard/docs/architecture.md`, `dashboard/docs/arbitrage/*.md`, `dashboard/docs/lp/*.md`, `README.md`. When adding a venue that contributes to global totals, update `engine/market/portfolio_registry.py` and the relevant docs.

Non-obvious invariants must be test-backed: router sizing/tiebreaking, half-open persistence and recovery, bytes/hex normalization, Optional-zero persistence.
