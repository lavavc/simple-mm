# CNGN Trading Engine

Automated market-making engine for CNGN stablecoin across DEXs, CEXs, and wallet systems.

## Getting Started

### Prerequisites

- Python 3.11+
- Access to Base/BSC RPC endpoints
- API keys for venues (Quidax, Blockradar, etc.)

### Quick Start

```bash
git clone https://github.com/lavavc/simple-mm.git
cd simple-mm

# Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env — at minimum set QUIDAX_API_KEY for live CEX prices

# Run the engine
python -m engine.main
```

## Local Checks

```bash
source .venv/bin/activate
python -m mypy engine --no-error-summary
python -m pytest -x -q --ignore=tests/test_dex_fork.py
python -m pytest -q tests/test_dex_fork.py -v
```

- CI runs the same strict `mypy` check and the default pytest suite on pull requests and pushes to `main`.
- `mypy` covers `engine/`, the production Python package, and intentionally ignores `tests/` and `dashboard/` because test doubles are looser by design and frontend code can be checked by its own toolchain.
- The default pytest run covers the fast local suite and intentionally skips `tests/test_dex_fork.py`, because those tests need Foundry's `anvil` plus RPC-backed fork access.

---

## Dashboard

A Next.js dashboard for real-time monitoring.

### Running the Dashboard

```bash
cd dashboard

# Install dependencies
npm install

# Development mode (with hot reload)
npm run dev

# Production build
npm run build
npm start
```

The dashboard will be available at `http://localhost:3000`.

### Configuration

Create `dashboard/.env.local`:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000/api
NEXT_PUBLIC_WS_URL=ws://localhost:8000/ws
```

---

## Running with Docker

A `Dockerfile` and `docker-compose.yml` are provided. Before deploying, edit
`docker-compose.yml` and set `VIRTUAL_HOST` / `LETSENCRYPT_HOST` / `LETSENCRYPT_EMAIL`
to your own domain and email (they ship as placeholders). Then:

```bash
cp .env.example .env      # fill in your keys
docker compose up -d --build
```

`deploy/setup.sh` documents a full server deployment behind nginx-proxy + Let's Encrypt.

GitHub Actions runs checks on pull requests and pushes to `main`. Production deployment runs only when a `v*` release tag is pushed and its checks pass; merging to `main` does not deploy. See the [deployment runbook](dashboard/docs/runbook.md#cicd-pipeline) for the release process and production environment approval settings.

## Documentation

Design and operational docs live in [`dashboard/docs/`](dashboard/docs/):

- [Architecture](dashboard/docs/architecture.md)
- [Arbitrage](dashboard/docs/arbitrage/overview.md) — signal, execution, risk, post-trade
- [Liquidity provision](dashboard/docs/lp/overview.md) — price range, inventory
- [Runbook](dashboard/docs/runbook.md) — deployment and operations

## License

Released under the [MIT License](LICENSE).

## Disclaimer

This software is provided for educational and research purposes only. It executes
automated trades with real funds on live venues; running it can result in the loss
of some or all of your capital. It is not financial advice. Use at your own risk
the authors accept no liability for any losses. Test thoroughly on testnets/paper
before deploying with real money.
