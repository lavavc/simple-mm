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
