---
title: Post-Trade
order: 6
---

## Monitoring

Use `/arb` in the Telegram bot for a live summary of arb state: detection mode, execution flags, consecutive failures, circuit breaker, and 24h P&L. The dashboard arbitrage tab shows the same data in real time via WebSocket.

Before discussing the actual DB though, let's look at our account structure:

## Per-account architecture and audit trail

All execution uses dedicated HD-derived hot wallets. The BIP44 derivation tree is:

```
Master Seed
│
├── m/44'/60'/0'/1/0  → uni-base-lp    Base (8453)  cNGN, USDC
├── m/44'/60'/0'/1/1  → uni-base-trade Base (8453)  cNGN, USDC
├── m/44'/60'/0'/2/0  → blockradar     Base (8453)  cNGN, USDT, USDC
├── m/44'/60'/0'/4/0  → uni-bsc-lp    BSC (56)     cNGN, USDT
└── m/44'/60'/0'/4/1  → uni-bsc-trade BSC (56)     cNGN, USDT
```

LP accounts hold liquidity positions and are not used for arb swaps. Trade accounts hold the stablecoin and cNGN that arb trades actually move. This separation means:
- On-chain history for each account type is clean and attributable by role
- LP positions are never accidentally swept during arb execution
- Balances can be monitored independently; refill alerts fire per role

Quidax CEX accounts (`quidax` and `quidax-lp`) are funded manually from the treasury multisig. The engine monitors CEX balances each balance-check cycle and fires a `refill_alert` when `cNGN < quidax_min_cngn` or `USDT < quidax_min_usdt`.

## Execution model: pre-funded inventory

Both legs of every arb trade run against pre-existing balances in trade accounts — the cNGN acquired in the buy leg is **not** the cNGN sold in the sell leg (they are on different chains or venues). The router therefore caps trade size against both:

- **Buy-side stablecoin** (`per_account_stable[buy_venue]`) — how much the buy leg can spend
- **Sell-side cNGN** (`per_account_cngn[sell_venue]`) — how much the sell leg can deliver

Both caps are seeded from on-chain balances at startup and refreshed every balance-check cycle. A trade will not be routed if either side lacks sufficient inventory to cover the adjusted size.

The `uni-bsc-trade` account is the buy-side for BSC arb legs and the sell-side's inventory source for DEX-DEX BSC→Base trades. Its stablecoin balance is what the router reads via `per_account_stable["uni-bsc"]`.

## Database records

Every detected opportunity is written to `dex_arbitrage_opportunities` (DEX-DEX) or `arbitrage_opportunities` (CEX-DEX) with a unique ID, direction, sizes, expected profit, and status. Status transitions:

```
detected → executing → completed
                    ↘ abandoned (if expired without execution)
```

Old records are expired after 60 seconds if still in `detected` state — the market has moved and the opportunity is no longer valid.

Each trade leg (buy and sell) is written to `arbitrage_trades` with the venue, side, amount, price, tx hash, and status. Actual profit is recorded on completion using actual fill prices, not estimates.

## Arbitrage lifecycle history

Every routed opportunity is tracked in `arbitrage_history_events` as a sequence of lifecycle events. Each event records the pipeline, direction, venue pair, trade sizes, expected and actual profit, and (on the routed event) a wallet snapshot of both sides' stablecoin and cNGN balances at the moment the route was selected.

Three event types are recorded per opportunity:

- **routed** — emitted when a route is selected. Captures the wallet snapshot and the net profit estimate. Always present.
- **executed** — emitted on successful completion of both legs. Adds actual profit and tx hashes.
- **failed** — emitted on any error or abandoned state. Adds the status code and reason string.

The table has a unique index on `(opportunity_id, event_type)`. Each lifecycle event is upserted, so replaying a history write is safe.

The `/arbitrage/history` endpoint returns grouped items — one per opportunity — sorted by most recent activity. Optional query parameters: `pipeline` (`cex_dex` or `dex_dex`), `from_ts` / `to_ts` (millisecond timestamps), `limit` (default 50). Time filters select which opportunities to include based on their latest event; all events for a selected opportunity are always returned regardless of the time window, so wallet snapshots from the routed event are never lost.

Every history write also fires an `arb_history_updated` WebSocket event with the `opportunity_id`, allowing the dashboard to refresh just the affected row.



