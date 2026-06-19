# Quidax Execution Latency Evaluation Plan

## Goal

Measure Quidax execution latency well enough to choose defensible fair-price
markout horizons and execution-risk assumptions.

This plan is separate from fair-price feed capture. Feed markouts answer "what
did the executable book become later?" Execution latency answers "how long does
our order/cancel path take, and when can we trust a fill or cancel state?"

## Questions To Answer

- What is the REST round-trip latency for market order submission?
- What is the time from market order submission to confirmed fill state?
- What is the time from limit order submission to accepted/open state?
- What is the time from cancel request to cancel acknowledgement?
- What is the time from cancel request to final cancel state or webhook?
- How often do order endpoints return transient 5xx, timeout, or ambiguous
  state?
- Does latency differ by order side, order type, size, market regime, or time of
  day?

## Instrumentation

Record one structured event row per lifecycle transition:

- `venue`, `market`, `order_id`, `client_order_id` if available
- `operation`: `market_submit`, `limit_submit`, `cancel`, `status_poll`,
  `webhook_fill`, `webhook_cancel`
- `side`, `ord_type`, `requested_volume`, `limit_price`
- `started_monotonic_ns`, `ended_monotonic_ns`, `wall_timestamp_ms`
- HTTP status, Quidax status/state, error class, retry attempt
- best bid/ask/depth snapshot timestamp before submit
- fill volume, average fill price, fee if returned
- final state: `done`, `wait`, `confirm`, `cancel`, `unknown`

Use monotonic time for latency. Use wall-clock timestamps only for joining to
market data.

## Experiment Design

1. Passive baseline:
   - Measure authenticated `GET open orders`, `GET order details`, and public
     depth/ticker latency without placing orders.
   - Run across at least one trading day to estimate p50/p90/p99 and timeout
     rates.

2. Cancel benchmark:
   - Place tiny non-marketable limit orders far from touch.
   - Cancel immediately after accepted/open state.
   - Measure submit ack, visible/open confirmation, cancel ack, cancel finality,
     and webhook delay.

3. Taker benchmark:
   - Place very small market orders only when inventory/risk limits allow.
   - Measure submit ack, immediate response fill fields, order-detail finality,
     webhook delay, and realized slippage versus pre-submit depth.

4. Stress-free cadence:
   - Keep tests low-frequency enough to avoid rate-limit or endpoint stress.
   - Do not run sub-second order tests until passive and cancel benchmarks show
     p99 latencies safely below one second.

## Metrics

Report by operation and side:

- p50, p90, p95, p99 latency
- timeout and 5xx rate
- ambiguous-state rate
- retry count distribution
- webhook delay distribution
- slippage versus pre-submit depth for market orders
- cancel risk window: submit-to-final-cancel latency

## Decision Rules

- Use a markout horizon only if the feed label lag and execution latency are
  materially below that horizon.
- Treat `5s` as diagnostic unless authenticated order p99 and raw feed p99 are
  both comfortably below `5s`.
- Do not use `1s` or sub-`1s` markouts for fair-price modeling unless Quidax
  provides a reliable streaming market-data/order-state feed and measured p99
  execution lifecycle latency supports it.
- If cancel finality p99 is above the quote-refresh horizon, widen spreads or
  reduce quote size rather than shortening markouts.

## Safety

- Use a dedicated test subaccount where possible.
- Use tiny order sizes and explicit inventory caps.
- Never place market-order benchmarks during abnormal spread/depth conditions.
- Stop immediately on repeated 5xx, timeout, or unknown order states.
- Persist every ambiguous order id and reconcile before continuing.

## Implementation Tasks

1. Add an execution-latency event table or append-only JSONL capture file.
2. Wrap Quidax adapter order submit, cancel, status poll, and webhook handlers
   with monotonic latency instrumentation.
3. Add a passive latency script for authenticated reads and public depth/ticker.
4. Add a cancel benchmark using tiny non-marketable limit orders.
5. Add a gated taker benchmark with explicit max notional and inventory checks.
6. Add a report script that outputs latency percentiles, error rates, and
   recommended minimum markout horizons.

## Initial Recommendation

Until this benchmark is complete, keep fair-price markouts at `10s+`, with
`30s`, `60s`, and `120s` as the primary Quidax modeling horizons. Treat `5s`,
`1s`, and sub-`1s` as execution-latency research, not fair-price model targets.
