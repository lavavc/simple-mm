---
title: Post-Trade
order: 6
---

## Database records

Every detected opportunity is written to `dex_arbitrage_opportunities` (DEX-DEX) or `arbitrage_opportunities` (CEX-DEX) with a unique ID, direction, sizes, expected profit, and status. Status transitions:

```
detected → executing → completed
                    ↘ abandoned (if expired without execution)
```

Old records are expired after 60 seconds if still in `detected` state — the market has moved and the opportunity is no longer valid.

Each trade leg (buy and sell) is written to `arbitrage_trades` with the venue, side, amount, price, tx hash, and status. Actual profit is recorded on completion using actual fill prices, not estimates.

## Per-account architecture and audit trail

All execution uses dedicated HD-derived hot wallets. The BIP44 derivation tree is:

```
Master Seed
│
├── m/44'/60'/0'/1/0  → uni-base-lp      Base (8453)  cNGN, USDC
├── m/44'/60'/0'/1/1  → uni-base-trade   Base (8453)  cNGN, USDC
├── m/44'/60'/0'/2/0  → blockradar       Base (8453)  cNGN, USDT, USDC
├── m/44'/60'/0'/3/0  → quidax-trade-fund BSC (56)    cNGN, USDT
├── m/44'/60'/0'/3/1  → quidax-lp         BSC (56)    cNGN, USDT
├── m/44'/60'/0'/4/0  → uni-bsc-lp        BSC (56)    cNGN, USDT
└── m/44'/60'/0'/4/1  → uni-bsc-trade     BSC (56)    cNGN, USDT
```

LP accounts hold liquidity positions and are not used for arb swaps. Trade accounts hold the stablecoin and cNGN that arb trades actually move. This separation means:
- On-chain history for each account type is clean and attributable by role
- LP positions are never accidentally swept during arb execution
- Balances can be monitored independently; refill alerts fire per role

The `uni-bsc-trade` account is the buy-side for BSC arb legs and the sell-side's inventory source for DEX-DEX BSC→Base trades. Its stablecoin balance is what the router reads via `per_account_stable["uni-bsc"]`.

## WebSocket broadcast

Every signal, opportunity, execution, and alert is broadcast to connected dashboard clients in real time:

| Event type | Trigger |
|------------|---------|
| `quidax_dex_optimal_arb` | Every Quidax depth update — fast path result + portfolio valuation |
| `quidax_dex_arb_curve` | Background slow path — full 1000-point curve |
| `dex_arb_opportunity` | DEX-DEX fast path result |
| `dex_arb_curve` | DEX-DEX slow path curve |
| `arb_executed` | CEX-DEX trade completed |
| `dex_arb_executed` | DEX-DEX trade completed |
| `alert` (severity: critical) | Half-open trade detected |

## Monitoring

The `/api/arbitrage/status` endpoint returns the current state of all risk parameters and circuit breakers:

```json
{
  "enabled": true,
  "execute_cex_dex": false,
  "execute_dex_dex": false,
  "daily_volume_usd": 1240.50,
  "inventory_imbalance_usd": 80.00,
  "circuit_breaker_active": false,
  "consecutive_failures": 0,
  "low_inventory_venues": [],
  "opportunities_detected_24h": 47,
  "opportunities_executed_24h": 0,
  "total_profit_24h_usd": 0.0
}
```

## Configuration reference

All thresholds are set in `engine/config.py` and can be overridden in `.env`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ARBITRAGE_EXECUTE_CEX_DEX_ENABLED` | `false` | Enable live CEX-DEX execution |
| `ARBITRAGE_EXECUTE_DEX_DEX_ENABLED` | `false` | Enable live DEX-DEX execution |
| `ARBITRAGE_MAX_DAILY_VOLUME_USD` | 10,000 | Rolling 24h volume cap |
| `ARBITRAGE_MAX_INVENTORY_IMBALANCE_USD` | 5,000 | Max net directional exposure |
| `ARBITRAGE_MAX_DAILY_LOSS_USD` | 500 | Circuit breaker loss threshold |
| `ARBITRAGE_MAX_CONSECUTIVE_FAILURES` | 3 | Circuit breaker failure count |
| `ARBITRAGE_MAX_DELTA_RATIO` | 0.60 | Portfolio cNGN% ceiling |
| `ARBITRAGE_MIN_ACCOUNT_STABLECOIN_USD` | 10 | Min stablecoin per venue before pausing |
| `ARBITRAGE_CROSS_CHAIN_REBALANCE_BPS` | 10 | Max rebalance penalty in route scoring |
| `ARBITRAGE_CEX_TAKER_FEE_BPS` | 10 | Quidax taker fee (0.1%) |
| `ARBITRAGE_DEX_SWAP_FEE_BPS` | 30 | Fallback DEX fee if pool read fails |
