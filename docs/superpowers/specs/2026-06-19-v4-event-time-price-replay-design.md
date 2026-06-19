# V4 Event-Time Price Replay Design

## Goal

Fix V4 pool-history liquidity rows so `sqrt_price_x96`, `tick`, and derived `cngn_usd_price` reflect the pool state at each event's log position, not the end-of-block state.

## Current Problem

Swap and initialize rows already decode event-native price from their logs. Liquidity rows currently call `getSlot0(..., block_identifier=block_number)` through `_state_at_block`, which returns block-end state. If a liquidity action and swap share a block, this can assign a future same-block swap price to a liquidity row that occurred earlier in log order.

## Design

Add a pure replay module, `backtester/v4_event_replay.py`, with no Web3, CSV, or exporter dependencies.

The module exposes:

- `PoolStateSnapshot(sqrt_price_x96: int, tick: int, source: str)`
- `ReplayEvent(block_number: int, log_index: int, event_order: int, event_type: str, sqrt_price_x96: int | None, tick: int | None)`
- `ReplayedEvent`, carrying original event metadata plus `event_time_sqrt_price_x96`, `event_time_tick`, and `event_time_state_source`
- `attach_event_time_state(events, initial_state)` sorted by `(block_number, log_index, event_order)`. `initial_state` may be `None` only when initialize or swap events seed state before any liquidity or collect event consumes it.

Replay rules:

- Initialize and swap events with non-null sqrt/tick consume the current carried state for their row, then update the carried state to their own sqrt/tick.
- Liquidity and collect events consume the current carried state and do not mutate price state.
- A liquidity or collect event before any seed state raises `ValueError`.
- State sources are explicit: initial seed source, `same_block_prior_event`, `prior_event`, or `self_event`.

Exporter integration stays narrow:

- Preserve existing CSV schema.
- Keep swap and initialize row decoding unchanged.
- Change pool-manager modify-liquidity rows and position-manager collect rows to use replayed event-time state.
- Use prior-block `_state_at_block(..., block_number - 1)` as seed when a chunk/block needs a seed state.
- Order replay inputs with real `log_index`; for decoded periphery actions, use receipt log indexes where available and deterministic `event_order` within the transaction.

## Testing

Add focused tests before implementation:

- Liquidity before same-block swap uses prior-block seed.
- Liquidity after same-block swap uses same-block prior swap.
- Initialize and swap update carried state.
- Missing seed for liquidity/collect raises.
- Exporter liquidity row tests prove no block-end lookahead for same-block ordering.

Then run:

```bash
PYTHONPATH=. python3 -m pytest -q tests/test_v4_event_replay.py tests/test_v4_export.py
PYTHONPATH=. python3 -m py_compile backtester/v4_event_replay.py backtester/v4_export.py
```

## Scope Boundaries

This task does not build token-id ownership ledgers, LP episode reconstruction, receipt sidecars, or new derived datasets. Those remain Tasks 9-12. Task 8 only corrects event-time pool price semantics used by exported history rows.
