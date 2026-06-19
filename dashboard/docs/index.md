---
title: Engine Docs
order: 1
---

# CNGN Trading Engine

The engine manages CNGN market operations across:

| Area | Current implementation |
|---|---|
| DEX LP | Uniswap V4 LP management on Base and BSC |
| CEX | Quidax order book price/depth, balance checks, and executable arb legs |
| Wallet systems | Blockradar fixed-rate sync and monitored liquidity |
| Arbitrage | CEX-DEX and DEX-DEX detection, routing, execution, recovery, and audit history |
| Portfolio | Account-role inventory, LP positions, Quidax balances, and global delta |

## Read This First

- [Architecture](architecture) — package boundaries, runtime composition, and dependency rules.
- [Market Data](arbitrage/market-data) — venue feeds, fair-value roles, and research metadata.
- [Arbitrage Overview](arbitrage/overview) — signal, risk, execution, and post-trade flow.
- [LP Overview](lp/overview) — V4 position management and wallet separation.
- [Deployment Runbook](runbook) — funding, environment variables, and operations.
- [Testing Architecture](tests) — local checks, fork tests, and test ownership.

## What Belongs Elsewhere

- Active research workflows live in `autoresearch/`.
- Historical research notes live in `autoresearch/archive/`.
- PDFs and papers live in `literature/`.

Docs in `dashboard/docs/` should describe the current engine design and operator workflow. Research ideas should be promoted here only after they become live design.
