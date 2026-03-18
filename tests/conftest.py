"""Pytest configuration and shared fixtures."""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch
from typing import Generator
import subprocess
import time
import socket

from engine.api.schemas import DexParams, CexParams, WalletParams
from engine.venues.dex.base import PoolConfig


# =============================================================================
# SHARED FIXTURES - Parameters
# =============================================================================


@pytest.fixture
def default_dex_params() -> DexParams:
    """Default DEX parameters."""
    return DexParams()


@pytest.fixture
def conservative_dex_params() -> DexParams:
    """Conservative DEX parameters with reserves."""
    return DexParams(
        max_utilization_percent=Decimal("70"),
        min_reserve_token0=Decimal("50000"),
        min_reserve_token1=Decimal("100"),
        max_position_usd=Decimal("10000"),
        sd_multiplier=Decimal("2.0"),
    )


@pytest.fixture
def default_cex_params() -> CexParams:
    """Default CEX parameters."""
    return CexParams()


@pytest.fixture
def default_wallet_params() -> WalletParams:
    """Default wallet parameters."""
    return WalletParams()


# =============================================================================
# SHARED FIXTURES - Pool Configuration
# =============================================================================


@pytest.fixture
def aerodrome_pool_config() -> PoolConfig:
    """Aerodrome cNGN/USDC pool config on Base."""
    return PoolConfig(
        chain_id=8453,
        chain_name="base",
        rpc_url="http://localhost:8545",  # Anvil
        pool_address="0x0206B696a410277eF692024C2B64CcF4EaC78589",
        nft_manager_address="0x827922686190790b37229fd06084350E74485b72",
        router_address="0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5",
        token0_address="0x46C85152bFe9f96829aA94755D9f915F9B10EF5F",  # cNGN
        token1_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
        token0_symbol="cNGN",
        token1_symbol="USDC",
        token0_decimals=6,
        token1_decimals=6,
        tick_spacing=100,
    )


# =============================================================================
# SHARED FIXTURES - Mock Web3
# =============================================================================


@pytest.fixture
def mock_web3():
    """Mock Web3 instance for unit tests."""
    w3 = MagicMock()

    # Mock eth module
    w3.eth.get_block.return_value = {
        "timestamp": 1700000000,
        "baseFeePerGas": 1000000000,
        "number": 28000000,
    }
    w3.eth.gas_price = 1000000000
    w3.eth.chain_id = 8453
    w3.eth.get_transaction_count.return_value = 0

    # Mock account
    mock_account = MagicMock()
    mock_account.address = "0x1234567890123456789012345678901234567890"
    w3.eth.account.from_key.return_value = mock_account

    # Mock to_checksum_address
    w3.to_checksum_address = lambda x: x
    w3.to_wei = lambda x, unit: int(x * 10**9) if unit == "gwei" else x

    return w3


@pytest.fixture
def mock_contract():
    """Mock contract for unit tests."""
    contract = MagicMock()

    # Mock functions
    contract.functions = MagicMock()

    return contract


# =============================================================================
# PRICE DATA FIXTURES
# =============================================================================


@pytest.fixture
def sample_prices() -> list[Decimal]:
    """Sample price history for testing."""
    # Simulates CNGN/USDC prices around 0.0006 with some volatility
    base = Decimal("0.000606")
    return [
        base + Decimal(str(i * 0.000001)) * (1 if i % 2 == 0 else -1)
        for i in range(50)
    ]


@pytest.fixture
def volatile_prices() -> list[Decimal]:
    """More volatile price history."""
    import random
    random.seed(42)  # Deterministic
    base = 0.000606
    return [
        Decimal(str(base + random.uniform(-0.00005, 0.00005)))
        for _ in range(100)
    ]


@pytest.fixture
def stable_prices() -> list[Decimal]:
    """Very stable price history."""
    return [Decimal("0.000606") for _ in range(50)]


# =============================================================================
# ANVIL FIXTURES (for fork tests)
# =============================================================================


def is_port_in_use(port: int) -> bool:
    """Check if a port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def _spawn_anvil(fork_url: str, port: int) -> Generator[str, None, None]:
    """Shared helper: spawn an Anvil fork on the given port, yield the RPC URL, teardown."""
    try:
        result = subprocess.run(["anvil", "--version"], capture_output=True)
        if result.returncode != 0:
            pytest.skip("Anvil not installed")
    except FileNotFoundError:
        pytest.skip("Anvil not installed")

    if is_port_in_use(port):
        yield f"http://localhost:{port}"
        return

    proc = subprocess.Popen(
        ["anvil", "--fork-url", fork_url, "--port", str(port), "--silent"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    for _ in range(30):
        if is_port_in_use(port):
            break
        time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail(f"Anvil failed to start on port {port} (fork: {fork_url})")

    yield f"http://localhost:{port}"

    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="session")
def anvil_base() -> Generator[str, None, None]:
    """Anvil fork of Base mainnet. RPC URL read from settings (uses Alchemy if key set)."""
    from engine.config import settings
    yield from _spawn_anvil(settings.base_rpc_url, port=8545)


@pytest.fixture(scope="session")
def anvil_bsc() -> Generator[str, None, None]:
    """Anvil fork of BSC mainnet. RPC URL read from settings (uses Alchemy if key set)."""
    from engine.config import settings
    yield from _spawn_anvil(settings.bsc_rpc_url, port=8546)


# =============================================================================
# TEST WALLET FIXTURES
# =============================================================================


@pytest.fixture(scope="session")
def test_private_key() -> str:
    """Test private key for Anvil (account 0). DO NOT USE ON MAINNET."""
    return "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


@pytest.fixture(scope="session")
def test_wallet_address() -> str:
    """Test wallet address corresponding to test_private_key."""
    return "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


# =============================================================================
# POOL CACHE FIXTURE
# =============================================================================

import math as _math

_Q96 = 2 ** 96

# Realistic sqrtPriceX96 for Base pool (cNGN/USDC, 6/6 dec) at price ≈ 0.000606
_BASE_SQRT_X96 = Decimal(int(_math.sqrt(0.000606) * _Q96))
# Realistic sqrtPriceX96 for BSC pool (USDT 18dec / cNGN 6dec) at cNGN price ≈ 0.000606
# Formula: 1/((sqrt/Q96)^2 * 10^12) = 0.000606  →  sqrt/Q96 = sqrt(1/(0.000606*1e12))
_BSC_SQRT_X96 = Decimal(int(_math.sqrt(1 / (0.000606 * 1e12)) * _Q96))
_POOL_LIQUIDITY = Decimal(10 ** 18)
_POOL_FEE = Decimal("0.0005")  # 0.05%


@pytest.fixture
def seeded_pool_cache(monkeypatch):
    """Inject known pool state into _POOL_CACHE for unit tests.

    Provides realistic sqrtPriceX96, liquidity, and fee for BSC and Base pools.
    Returns a dict mapping venue name to pool_address for convenience.
    """
    from engine.core.arbitrage import pool_state as _ps
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG

    base_key = UNISWAP_BASE_POOL_READ_CONFIG.pool_address
    bsc_key = UNISWAP_BSC_POOL_READ_CONFIG.pool_address

    fake_cache = {
        base_key: {
            "tick": -276324,
            "liquidity": _POOL_LIQUIDITY,
            "fee": _POOL_FEE,
            "sqrt_p": _BASE_SQRT_X96,
            "balance0": Decimal("500000"),  # cNGN
            "balance1": Decimal("600"),     # USDC
            "timestamp": time.time(),
        },
        bsc_key: {
            "tick": -276324,
            "liquidity": _POOL_LIQUIDITY,
            "fee": _POOL_FEE,
            "sqrt_p": _BSC_SQRT_X96,
            "balance0": Decimal("9200"),      # USDT
            "balance1": Decimal("26090000"),  # cNGN
            "timestamp": time.time(),
        },
    }
    monkeypatch.setattr(_ps, "_POOL_CACHE", fake_cache)

    # Seed gas oracle so arb functions don't block on missing prices.
    from engine.core import gas_oracle as _go
    monkeypatch.setitem(_go._state, "gas_usd_base", Decimal("0.003"))
    monkeypatch.setitem(_go._state, "gas_usd_bsc", Decimal("0.005"))

    return {"uni-base": base_key, "uni-bsc": bsc_key, "cache": fake_cache}


# =============================================================================
# FAKE ADAPTERS (in-process doubles for scheduler/executor tests)
# =============================================================================

from tests.fakes import FakeDexAdapter, FakeCexAdapter  # noqa: E402


@pytest.fixture
def fake_dex_adapter():
    return FakeDexAdapter()


@pytest.fixture
def fake_cex_adapter():
    return FakeCexAdapter()
