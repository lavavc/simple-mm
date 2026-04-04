## Voice

Never use first-person singular pronouns. When there is a need to refer to the model, use "Claude" or "Codex". If that is grammatically awkward, use collaborative pronouns such as "we", "us", and "our".

Address directly. Never refer to "the user".

## Source Of Truth

Source-of-truth order matters:

1. Code and typed contracts.
2. Tests that pin non-obvious behavior.
3. Operator-facing docs in `dashboard/docs/` and setup docs like `README.md`.

Important consequences:

- `dashboard/docs/` explains intended behavior, but it is not allowed to overrule working code when the code, types, and tests already define the live contract.
- If docs and code disagree, fix the docs in the same change unless the code is clearly wrong and is being changed deliberately.
- Prefer concrete typed sources over prose when behavior is subtle:
  - `engine/arb/routing/route_registry.py`
  - `engine/api/schemas.py`
  - `engine/db/backend.py`
  - `engine/venues/base.py`
  - `engine/web3_utils.py`

Documentation should stay succinct and concept-first. Avoid adding large code blocks unless a command or interface would otherwise be ambiguous.

## Architecture Rules

These boundaries are intentional and should stay sharp:

- `engine/venues/` contains thin adapters over on-chain contracts and external APIs. No strategy logic belongs there.
- `engine/market/` owns shared market data, normalization, pool cache state, volume tracking, and gas data. No execution policy belongs there.
- `engine/lp/` owns LP strategy and rebalancing only. LP code should not grow arb-specific knowledge.
- `engine/arb/` owns arbitrage detection, routing, execution, and risk. Arb code should not depend on LP internals.
- `engine/db/` is the persistence layer. High-level modules should depend on narrow store protocols from `engine/db/backend.py`, not concrete query modules or repository internals.
- `engine/scheduler/core.py` is a wiring shell. Timers, APScheduler lifecycle, and websocket wiring belong there; job behavior belongs in `engine/scheduler/jobs/` or the domain modules they call.
- `engine/main.py` is the wiring edge. It builds runtime state and connects long-lived services. Do not move business logic there.

When adding a new dependency edge, preserve the current layer model unless there is a deliberate architectural change.

## Code Style

In order of priority:

- No safe defaults for gas or other fee estimations. Fail quickly and alert instead of silently assuming profitability.
- Add the least code possible to solve the problem.
- Avoid complexity, dead abstractions, and compatibility shims.
- Do not keep unused fields, parameters, or dataclass attributes after a refactor.
- Add comments or docstrings only when the logic would otherwise be hard to reconstruct.
- Prefer precise types, protocols, and narrowing helpers over broad `Any` or cast-heavy silencing.
- When working at Web3 boundaries, normalize once and keep the representation consistent all the way through.

## Execution Flow Invariants

These are the critical invariants for arb changes. Review all of them when touching detection, routing, execution, recovery, or arb persistence.

### Route And Sizing Invariants

- `engine/arb/routing/route_registry.py` is the only source of truth for route direction, pipeline, leg type, venue names, and `cngn_effect`. Do not create duplicate direction lists or ad hoc route classifiers elsewhere.
- `select_route()` produces a route decision, not a bag of execution-time token amounts. Routing owns:
  - candidate ranking
  - adjusted USD size
  - expected profit rescoring after caps
  - inventory alignment tiebreaking
- `SelectedRoute` should stay limited to routed size and profit metadata. Do not add swap-output amounts or other live execution state there.

### Inventory And Venue Invariants

- Inventory is venue-local. `quidax`, `uni-base`, and `uni-bsc` balances are independent and must be capped independently.
- A CEX fill does not make tokens available in a DEX trade wallet, and a DEX fill does not make tokens available on Quidax.
- Router caps must continue to protect both legs:
  - buy-side stablecoin balance
  - sell-side cNGN balance

### Serialization Invariants

- `_arb_executing` serializes all live execution and all recovery across both pipelines. There is no arb queue. If a signal arrives while another trade or recovery is in flight, it is skipped.

### Preflight Invariants

- Any on-chain leg that can be simulated must be preflighted with `simulate_swap()` before sending the live transaction.
- CEX REST legs are the explicit exception because there is no on-chain preflight surface.
- Preflight error handling in `engine/arb/execution/preflight.py` is intentionally asymmetric:
  - `balance` errors zero inventory for the affected venue
  - `rpc`, `permit2`, and `unknown` errors do not mutate inventory
  - `pool_paused` trips the circuit breaker
- Do not broaden inventory mutation beyond the balance case without a concrete incident-driven reason.

### Live Amount Invariants

- Execution derives live amounts locally. Detection and routing provide the route and routed USD size, but execution recomputes the amounts it needs at execution time.
- CEX sell legs use the actual buy fill.
- DEX sell legs in the main execution path use the preflight cNGN estimate computed at execution time, not a carried routing field and not the live wallet balance.
- Recovery uses persisted `buy_amount_cngn`, never a fresh wallet balance query, so reversal logic cannot accidentally consume unrelated inventory.

### Half-Open And Recovery Invariants

- A half-open trade is any path where the buy succeeded and the sell failed.
- Half-open handling must persist recovery-critical state immediately, especially:
  - `buy_tx_hash`
  - `buy_amount_cngn`
  - executed size / status
- Half-open flows trip the circuit breaker immediately.
- CEX-DEX and DEX-DEX recovery are different flows and must stay separate.
- DEX-DEX recovery first retries the sell if preflight now passes; otherwise it reverses the buy.
- CEX-DEX recovery reverses the buy leg on the venue that can unwind the position, using the stored buy amount.

## Persistence And DB Invariants

- High-level modules should depend on store protocols from `engine/db/backend.py`, not concrete repository/query implementations.
- CEX-DEX and DEX-DEX use different persistence paths. Preserve that split:
  - `update_arbitrage_opportunity(...)`
  - `update_dex_arbitrage_execution_state(...)`
- Recovery-critical fields such as `buy_amount_cngn` must never be dropped by later updates.
- When working with Optional numeric fields loaded from DB or schemas, never use truthiness if zero is meaningful. Use `is not None`.
- `engine/arb/risk/history.py` is the append-only lifecycle surface. When route metadata is available, pipeline and direction should come from the route registry, not ad hoc strings.

## Scheduler Invariants

- `TradingScheduler` owns lifecycle and job registration, not business logic.
- Shared dependencies should flow through `SchedulerContext` and narrow protocols, not through direct imports of the entire runtime container.
- Websocket-driven arb updates and wallet activity are part of scheduler orchestration. Do not add parallel ad hoc listeners for the same responsibilities.
- DEX arb bootstrap is intentionally gated: seed pool state, seed gas, then allow the first DEX-DEX update.

## Web3 And Typing Invariants

- `engine/web3_utils.py` is the shared normalization boundary for hashes, topics, and log data. Reuse it instead of open-coding bytes/hex conversions.
- Application-level and DB-level transaction hashes should remain plain `str`.
- Web3 request payloads should use the actual typed shapes the library expects, such as `HexStr`, `TxParams`, and `Wei`, instead of untyped dicts where practical.
- `engine/` is the strict-mypy surface. Do not weaken typing there casually.
- When code starts from `VenueAdapter` and needs DEX-only behavior, narrow through `is_dex_execution_venue(...)` instead of scattered `hasattr(...)` checks or unchecked casts.

## Docs And Test Hygiene

- When changing behavior, update the closest relevant docs in the same change:
  - `dashboard/docs/architecture.md` for structure
  - `dashboard/docs/arbitrage/*.md` for arb behavior
  - `dashboard/docs/lp/*.md` for LP behavior
  - `README.md` for local setup, checks, and CI expectations
- Non-obvious invariants should be test-backed. The most important ones currently are:
  - router sizing and tiebreak behavior
  - half-open persistence and recovery
  - bytes/hex normalization at Web3 boundaries
  - Optional-zero persistence behavior
- Do not leave docs describing a behavior that tests or code no longer implement.

## Domain Appendix

These are still intentionally hard constraints:

- Price confidence is capped at `0.9`.
- Confidence drops by `0.2` per missing venue, flooring at `0.0`.

If changing those rules, update both the code and the arbitrage/market-data docs together.
