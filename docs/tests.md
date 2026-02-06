## Testing Architecture

### Quick Start

```bash
# Run all unit tests (no network required)
pytest tests/ --ignore=tests/test_dex_fork.py -v

# Run with coverage report
pytest tests/ --ignore=tests/test_dex_fork.py --cov=engine --cov-report=term-missing

# Run fork tests (requires Anvil)
pytest tests/test_dex_fork.py -v
```

### Layer 1: Unit Tests

Fast tests that run without network access or external dependencies.

| Test file | Module under test | What's tested |
|-----------|-------------------|---------------|
| `test_params_validation.py` | `api/schemas.py` | `DexParams`, `CexParams`, `WalletParams` defaults, custom values, serialization |
| `test_schemas.py` | `api/schemas.py` | All Pydantic models: `PriceQuote`, `ArbitrageOpportunity`, `BlendedPriceResponse`, `Alert`, etc. |
| `test_config.py` | `config.py` | `Settings` defaults, removed legacy fields (`keys_file`, `quidax_api_secret`) |
| `test_price_math.py` | DEX math | Tick/price conversions, sqrtPriceX96, tick alignment, decimal adjustments |
| `test_capital_allocation.py` | DEX allocation | Max utilization, reserves, USD caps, edge cases |
| `test_price_aggregation.py` | `core/price_aggregation.py` | `PriceNormalizer` (all venue types, cross-rates), VWAP, confidence scores, `BlendedPrice` |
| `test_accounts.py` | `core/accounts.py` | HD derivation, deterministic addresses, role access, threshold updates |
| `test_arbitrage.py` | `core/arbitrage/detector.py` | Fee estimation, opportunity detection, spread filtering, async detection flow |
| `test_inventory.py` | `core/arbitrage/inventory.py` | Daily limits, circuit breakers, trade recording, status dict |
| `test_executor.py` | `core/arbitrage/executor.py` | Detection-only mode, Phase 2/3 stubs |
| `test_database.py` | `db/database.py` | SQLite CRUD: system state, price snapshots, alerts, actions |

### Layer 2: Fork Tests with Anvil

Tests against real contract state by forking mainnet using [Anvil](https://book.getfoundry.sh/reference/anvil/).

**What's tested:**
- Reading pool state (slot0, liquidity, tick spacing)
- Reading position data from NFT manager
- Token balance queries
- Price math against real pool prices
- Transaction building (approvals, mints)

**Prerequisites:**
```bash
curl -L https://foundry.paradigm.xyz | bash
foundryup
```

| Fixture | Chain | Port | RPC |
|---------|-------|------|-----|
| `anvil_base` | Base | 8545 | https://mainnet.base.org |
| `anvil_bsc` | BSC | 8546 | https://bsc-dataseed.binance.org |

Fork tests use Anvil's default test account (index 0) which has 10,000 ETH but no tokens. Tests requiring funded wallets use impersonation or skip gracefully.

### Coverage Summary

Modules with **100%** coverage: `schemas.py`, `config.py`, `executor.py`, `pancakeswap.py`, `abis.py`.

Modules at **67–94%**: `accounts.py`, `detector.py`, `inventory.py`, `price_aggregation.py`, `database.py`.

Modules at **0%** (require live infrastructure): `routes.py`, `main.py`, `scheduler.py`, `ws.py`, `quidax.py`, `blockradar.py`, `base.py` (DEX adapter). These depend on network, WebSockets, or full app lifecycle and are covered by fork tests and manual integration testing.
