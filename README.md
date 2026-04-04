# CNGN Trading Engine

Automated market-making engine for CNGN stablecoin across DEXs, CEXs, and wallet systems.

## Getting Started

### Prerequisites

- Python 3.11+
- Access to Base/BSC RPC endpoints
- API keys for venues (Quidax, Blockradar, etc.)

### Quick Start

```bash
cd cngn

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
- `mypy` covers `engine/`, the production Python package, and intentionally ignores `tests/` and `dashboard/` because test doubles are looser by design and frontend code is checked by its own toolchain.
T- he default pytest run covers the fast local suite and intentionally skips `tests/test_dex_fork.py`, because those tests need Foundry's `anvil` plus RPC-backed fork access.

---

## Dashboard

A Next.js dashboard for real-time monitoring and control.

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
