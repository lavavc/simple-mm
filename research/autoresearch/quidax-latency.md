# Quidax Latency Autoresearch

## Goal

Measure Quidax execution latency well enough to choose fair-price markout horizons and execution-risk assumptions.

Feed markouts answer: what did the executable book become later?

Execution latency answers: how long does order submission, acknowledgement, fill, cancel, and finality take?

## Questions

- REST round-trip latency for authenticated reads, order submit, cancel, and status poll.
- Time from market order submission to confirmed fill state.
- Time from limit order submission to accepted/open state.
- Time from cancel request to final cancel state.
- Timeout, 5xx, retry, and ambiguous-state rates.
- Side, order type, size, and time-of-day effects.

## Instrumentation Target

Record one lifecycle row per transition:

- venue, market, order id, client order id
- operation: submit, cancel, poll, webhook fill, webhook cancel
- side, order type, requested volume, limit price
- monotonic start/end times and wall timestamp
- HTTP status, Quidax status, error class, retry attempt
- pre-submit depth timestamp
- fill volume, average fill price, fee
- final state

Use monotonic time for latency. Use wall time only to join with market data.

## Experiment Order

1. Passive authenticated reads and public depth/ticker.
2. Tiny non-marketable limit order submit/cancel benchmark.
3. Tiny gated taker benchmark only when inventory/risk limits allow.
4. Higher-cadence tests only after p99 latency supports them.

## Decision Rules

- Treat 10s+ as the current fair-price research floor.
- Treat 5s as diagnostic until feed and authenticated-order p99 are comfortably below 5s.
- Do not use 1s or sub-1s markouts without a reliable streaming feed and measured order lifecycle support.
- If cancel finality is slower than the quote-refresh horizon, widen spreads or reduce quote size rather than shortening markouts.

Archive detail: `research/autoresearch/archive/quidax_execution_latency_plan.md`.

