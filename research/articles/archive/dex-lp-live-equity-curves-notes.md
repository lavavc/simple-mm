# DEX LP Live Equity Curve Notes

Archived side note. The active July 2026 article outlines should use
`research/articles/evidence-pack-2026-07-cngn-market-making.md` as the primary
evidence source. Plot and CSV outputs from this side run were local generated
scratch artifacts, not committed publication assets.

Purpose: article-ready notes for the realized live-traded Uniswap V4 LP equity
and PnL curves. These are not simulated strategy backtests. They are
transaction-ledger reconstructions from the exported live V4 LP lifecycle
ledgers.

## Source Artifacts

- Base ledger: `research/data/derived/uni_base_lp_ledger.csv`
- BSC ledger: `research/data/derived/uni_bsc_lp_ledger.csv`
- Base mark series: `research/data/derived/uni_base_pool_history_replay.csv`
- BSC mark series: `research/data/derived/uni_bsc_pool_history_replay.csv`
- Ledger QA: `research/data/quality/lp_ledger_attribution.md`

The ledger QA reports exact opening attribution for all opening rows in these
exports:

| Pool | Ledger rows | Openings | Exact openings | Exact opening capital |
|---|---:|---:|---:|---:|
| `uni-base` | 36 | 25 | 25 | $105,260.90 |
| `uni-bsc` | 13 | 8 | 8 | $86,211.97 |

## Accounting Definition

At every pool replay event, equity is marked using the pool event-time
`sqrt_price_x96` converted to cNGN/USD. Net PnL is:

```
marked open LP token inventory
+ realized collect/removal token flows
- contributed token deposits
```

All token legs are converted with the current event-time pool mark. This is a
live LP ledger curve, not a wallet-wide statement. It does not fully reconcile
external wallet transfers or subtract gas unless those costs are already
embedded in the exported ledger inputs.

## Aggregate Live-Ledger Curves

These aggregate all LP owners present in each exported live ledger.

| Pool | Window | Final gross LP equity | Final net PnL | Trough net PnL | Peak net PnL | Final return on final-marked contributed capital |
|---|---|---:|---:|---:|---:|---:|
| `uni-base` | 2026-03-04 16:51 UTC to 2026-06-18 21:44 UTC | $103,764.84 | -$38.68 | -$264.28 | $0.00 | -0.037% |
| `uni-bsc` | 2026-03-04 16:15 UTC to 2026-06-19 11:42 UTC | $36,371.38 | +$82.35 | -$195.06 | +$87.74 | +0.095% |

Base curve shape: capital steps up materially after inception, but net PnL
stays below zero for the full exported window. The drawdown happens early, on
2026-03-10, and the curve later recovers most of that loss before ending
slightly negative.

BSC curve shape: early drawdown into 2026-03-10, followed by recovery by late
April or early May. After recovery, the curve is mostly flat and positive in a
roughly $40 to $88 net PnL band.

## Confirmed In-House LP Addresses

The in-house LP addresses for the two V4 pools are:

| Pool | Address |
|---|---|
| `uni-base` | `0xB25dB46588634D1153c058407D08361AbC6323fE` |
| `uni-bsc` | `0x71A39D4663d52FFEb2EC78CAa3FC73d4Cc7E9302` |

These addresses are present in the exported live V4 LP ledgers. The previous
cross-pool `0x2db7...` grouping is still useful as a pool participant in the
aggregate ledger, but it is not the in-house LP address pair for this article.

## Owner-Filtered Curves For Our LP Addresses

The local side run also produced owner-filtered plot and CSV artifacts. Those
outputs were generated scratch, not part of the active article sequence.

| Pool | Address | Window | Final gross LP equity | Final net PnL | Trough net PnL | Peak net PnL | Final contributed capital mark | Final collections mark |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `uni-base` | `0xB25d...3fE` | 2026-04-08 13:57 UTC to 2026-06-18 21:44 UTC | $1,200.74 | -$1.10 | -$8.29 | +$0.02 | $1,597.89 | $396.05 |
| `uni-bsc` | `0x71A3...302` | 2026-04-08 13:09 UTC to 2026-06-19 11:42 UTC | $441.83 | -$1.04 | -$3.49 | +$0.01 | $442.87 | $0.01 |

Interpretation for the article:

- Our Base LP address had a short active live-ledger window, several
  mint/burn-collect cycles, and ended slightly negative.
- Our BSC LP address had a smaller live-ledger footprint, ended slightly
  negative, and stayed close to flat after the final mint sequence.
- These owner-filtered live results are much smaller than the aggregate ledger
  because the aggregate includes other owners and larger positions.

## Quidax LP Address

Quidax LP deposit address:

`0x02DfDc514A3E0E72BBFfEDb79202CCe389cb6AAC`

The repo currently has no `position_snapshots` rows for `quidax` or
`quidax-lp`, so there is no local off-chain Quidax LP equity/PnL curve. The
only local Quidax market history is `price_snapshots` for the public order book,
which gives prices but not the LP subaccount balances.

An attempted on-chain cNGN transfer-history fetch for the Quidax address was
blocked by RPC/log-scan limits in this side run: the default configured RPC did
not connect, public Base/BSC RPCs connected, but the historical Base log scan
was too slow for the full window. Even if fetched, that would only show on-chain
deposit-address cNGN balances and would not capture off-chain Quidax account
crediting, USDT balances, orders, or withdrawals.

Article-safe conclusion: do not plot or publish Quidax LP PnL until we have
either Quidax `quidax-lp` balance snapshots from `position_snapshots` or a
separate Quidax account export with timestamped cNGN and USDT balances.

## Owner-Level Final Snapshot

Final marks use each pool's last replay event in the local export.

### Base

| Owner | Final-marked deposits | Final open LP equity | Final net PnL | Return on final-marked deposits |
|---|---:|---:|---:|---:|
| `0xb52c73CB71428a2a727044BCC82ed48E545AE468` | $20,559.82 | $19,540.09 | -$37.48 | -0.1823% |
| `0xB25dB46588634D1153c058407D08361AbC6323fE` | $1,597.89 | $1,200.74 | -$1.10 | -0.0690% |
| `0x21426D68a9E5Df153FE75cE0fEd20173EBcb80eF` | $535.38 | $535.36 | -$0.02 | -0.0034% |

### BSC

| Owner | Final-marked deposits | Final open LP equity | Final net PnL | Return on final-marked deposits |
|---|---:|---:|---:|---:|
| `0x71A39D4663d52FFEb2EC78CAa3FC73d4Cc7E9302` | $442.87 | $441.83 | -$1.04 | -0.2337% |

## Article Framing

This result is useful because it is grounded in actual live LP lifecycle events,
not just simulated policies. The cautious framing is:

- The aggregate live LP books were not materially profitable over the exported
  windows.
- Our specific Base and BSC LP addresses both ended slightly negative, with
  small absolute PnL relative to deployed notional.
- The aggregate ledger includes other live/test LP owners, so article charts
  should keep aggregate and owner-filtered views separate.
- The evidence supports an operational story about building the measurement
  layer before claiming LP alpha. It does not support claiming a robust live LP
  edge.
