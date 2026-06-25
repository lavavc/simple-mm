# CNGN Trading Engine

Automated CNGN market-data, concentrated-liquidity, and arbitrage engine across Quidax, Uniswap Base, Uniswap BSC, Blockradar, and reference price feeds.

## Repository Map

| Path | Purpose |
|---|---|
| `engine/` | Production FastAPI service, scheduler, market data, LP management, arbitrage, venues, accounts, and persistence. |
| `dashboard/` | Next.js dashboard and active implementation docs under `dashboard/docs/`. |
| `research/autoresearch/` | Active research workflows for fair price, DEX LP policy, and Quidax latency. Historical notes live in `research/autoresearch/archive/`. |
| `research/literature/` | Consolidated finance, order-book, Kelly sizing, CLMM, and fair-price PDF references. |
| `research/backtester/` | LP backtesting framework and historical result artifacts. |
| `research/scripts/` | Research capture, export, analysis, and backtest helper scripts. |
| `scripts/` | Operational helpers for manual execution, accounts, and scheduled pool-history updates. |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

At minimum, configure RPC access, Quidax credentials for live CEX data, and wallet settings appropriate for the environment. `ALCHEMY_KEY` is recommended so Base, BSC, and Ethereum RPC/WSS endpoints are generated consistently.

## Run Locally

Engine:

```bash
source .venv/bin/activate
python -m engine.main
```

Dashboard:

```bash
cd dashboard
npm install
npm run dev
```

The dashboard expects:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000/api
NEXT_PUBLIC_WS_URL=ws://localhost:8000/ws
```

## Current Design Docs

Active engine design and implementation docs live in `dashboard/docs/`:

- `dashboard/docs/architecture.md` — layer ownership, dependency rules, runtime composition, and entry points.
- `dashboard/docs/arbitrage/` — market data, signal, risk, execution, and post-trade behavior.
- `dashboard/docs/lp/` — LP overview, range policy, inventory, operations, and pool-history jobs.
- `dashboard/docs/data-persistence.md` — SQLite repository/store model and research metadata.
- `dashboard/docs/runbook.md` — deployment, funding, controls, and operational risks.
- `dashboard/docs/tests.md` — test tiers, commands, and coverage map.

Research docs are intentionally outside the dashboard docs:

- `research/autoresearch/fair-price.md`
- `research/autoresearch/lp.md`
- `research/autoresearch/quidax-latency.md`
- `research/autoresearch/archive/`
- `research/literature/README.md`

## Verification

Backend:

```bash
source .venv/bin/activate
python -m mypy engine --no-error-summary
python -m pytest -x -q --ignore=tests/test_dex_fork.py
```

Fork tests require Foundry `anvil` and fork-capable RPC endpoints:

```bash
source .venv/bin/activate
python -m pytest -q tests/test_dex_fork.py -v
```

Dashboard:

```bash
cd dashboard
npm run build
```

## Core Invariants

- `engine/venues/` are thin adapters only.
- `engine/market/` owns market data, fair price, pool cache, gas, and portfolio aggregation.
- `engine/lp/` owns LP policy and Uniswap V4 position management; LP volatility and rerange triggers stay venue-local, with an optional market-layer fair-price center.
- `engine/arb/` owns detection, routing, execution, recovery, and risk.
- Route direction metadata comes from `engine/arb/routing/route_registry.py`.
- Global portfolio totals are explicit through `engine/market/portfolio_registry.py`.
- Docs should follow code and typed contracts; when behavior changes, update the closest doc in the same change.
