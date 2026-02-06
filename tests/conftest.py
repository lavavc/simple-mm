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


@pytest.fixture(scope="session")
def anvil_base() -> Generator[str, None, None]:
    """
    Start Anvil fork of Base mainnet.

    Scoped to session to avoid restarting for every test.
    Requires Foundry to be installed.
    """
    port = 8545

    # Check if Anvil is available
    try:
        result = subprocess.run(["anvil", "--version"], capture_output=True)
        if result.returncode != 0:
            pytest.skip("Anvil not installed")
    except FileNotFoundError:
        pytest.skip("Anvil not installed")

    # Check if port is already in use (maybe Anvil already running)
    if is_port_in_use(port):
        yield f"http://localhost:{port}"
        return

    # Start Anvil with Base fork
    # Note: Not pinning block number to ensure pool contract exists
    # For deterministic tests, pin to a block AFTER pool deployment
    proc = subprocess.Popen(
        [
            "anvil",
            "--fork-url", "https://mainnet.base.org",
            "--port", str(port),
            "--silent",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for Anvil to start
    for _ in range(30):
        if is_port_in_use(port):
            break
        time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail("Anvil failed to start")

    yield f"http://localhost:{port}"

    # Cleanup
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="session")
def anvil_bsc() -> Generator[str, None, None]:
    """
    Start Anvil fork of BSC mainnet.

    Uses a different port than Base fork.
    """
    port = 8546

    # Check if Anvil is available
    try:
        result = subprocess.run(["anvil", "--version"], capture_output=True)
        if result.returncode != 0:
            pytest.skip("Anvil not installed")
    except FileNotFoundError:
        pytest.skip("Anvil not installed")

    # Check if port is already in use
    if is_port_in_use(port):
        yield f"http://localhost:{port}"
        return

    # Start Anvil with BSC fork
    proc = subprocess.Popen(
        [
            "anvil",
            "--fork-url", "https://bsc-dataseed.binance.org",
            "--fork-block-number", "45000000",  # Pinned block
            "--port", str(port),
            "--silent",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for Anvil to start
    for _ in range(30):
        if is_port_in_use(port):
            break
        time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail("Anvil failed to start")

    yield f"http://localhost:{port}"

    # Cleanup
    proc.terminate()
    proc.wait(timeout=5)


# =============================================================================
# TEST WALLET FIXTURES
# =============================================================================


@pytest.fixture
def test_private_key() -> str:
    """
    Test private key for Anvil.

    This is Anvil's default account 0 - DO NOT USE ON MAINNET.
    """
    return "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


@pytest.fixture
def test_wallet_address() -> str:
    """Test wallet address corresponding to test_private_key."""
    return "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
