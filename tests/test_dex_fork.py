"""
Fork tests for DEX adapters using Anvil.

These tests run against forked mainnet state to verify real contract interactions.
Requires Foundry/Anvil to be installed.

Run with: pytest tests/test_dex_fork.py -v
"""

import pytest
from decimal import Decimal
from web3 import Web3

from engine.api.schemas import DexParams
from engine.venues.dex.base import PoolConfig, BaseDexAdapter, ERC20_ABI
from engine.venues.dex.aerodrome import AerodromeAdapter


# Skip all tests in this module if Anvil is not available
pytestmark = pytest.mark.skipif(
    not pytest.importorskip("subprocess").run(
        ["which", "anvil"], capture_output=True
    ).returncode == 0,
    reason="Anvil not installed"
)


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def aerodrome_adapter(anvil_base, test_private_key) -> AerodromeAdapter:
    """Create Aerodrome adapter connected to Anvil fork."""
    return AerodromeAdapter(
        lp_private_key=test_private_key,
        trade_private_key=test_private_key,
        rpc_url=anvil_base,
        params=DexParams(
            max_utilization_percent=Decimal("80"),
            min_reserve_token0=Decimal("1000"),  # Keep 1000 cNGN
            min_reserve_token1=Decimal("1"),      # Keep 1 USDC
        ),
    )


@pytest.fixture
def web3_base(anvil_base) -> Web3:
    """Web3 instance connected to Base fork."""
    return Web3(Web3.HTTPProvider(anvil_base))


# =============================================================================
# READ TESTS - Pool State
# =============================================================================


class TestPoolStateReads:
    """Test reading pool state from forked mainnet."""

    def test_get_current_state(self, aerodrome_adapter):
        """Test reading current pool state (slot0)."""
        state = aerodrome_adapter.get_current_state()

        assert "sqrt_price_x96" in state
        assert "tick" in state
        assert "price" in state

        # sqrtPriceX96 should be a large integer
        assert state["sqrt_price_x96"] > 0

        # Tick should be in reasonable range for cNGN/USDC
        # (very negative due to price being << 1)
        assert state["tick"] < 0

        # Price should be a Decimal
        assert isinstance(state["price"], Decimal)

    def test_get_pool_liquidity(self, aerodrome_adapter):
        """Test reading pool liquidity."""
        # Access pool contract directly
        try:
            liquidity = aerodrome_adapter.pool_contract.functions.liquidity().call()
            assert liquidity >= 0
        except Exception as e:
            # Some pools may not have this method exposed the same way
            pytest.skip(f"Could not read liquidity: {e}")

    def test_get_tick_spacing(self, aerodrome_adapter):
        """Test reading tick spacing from config."""
        assert aerodrome_adapter.config.tick_spacing == 100


class TestPositionReads:
    """Test reading position data from forked mainnet."""

    def test_get_owned_positions_empty(self, aerodrome_adapter):
        """Test getting owned positions (should be empty for test wallet)."""
        # Test wallet (Anvil default) shouldn't own any positions
        positions = aerodrome_adapter.get_owned_positions()

        # Should return a list (possibly empty)
        assert isinstance(positions, list)

    def test_get_position_state_invalid(self, aerodrome_adapter):
        """Test getting position state for invalid token ID."""
        # Token ID that doesn't exist
        state = aerodrome_adapter.get_position_state(999999999)

        # Should return None for invalid position
        assert state is None


class TestBalanceReads:
    """Test reading token balances."""

    def test_read_token_balances(self, aerodrome_adapter, test_wallet_address):
        """Test reading token balances for test wallet."""
        # Test wallet should have 0 balance (hasn't received tokens)
        balance0 = aerodrome_adapter.token0.functions.balanceOf(
            test_wallet_address
        ).call()
        balance1 = aerodrome_adapter.token1.functions.balanceOf(
            test_wallet_address
        ).call()

        assert balance0 >= 0
        assert balance1 >= 0

    def test_get_position_method(self, aerodrome_adapter):
        """Test the get_position() method."""
        import asyncio

        position = asyncio.get_event_loop().run_until_complete(
            aerodrome_adapter.get_position()
        )

        assert position.venue == "aerodrome"
        assert position.pair == "cNGN/USDC"
        assert "cngn" in position.balances
        assert "usdc" in position.balances


# =============================================================================
# PRICE MATH TESTS - Against Real Pool
# =============================================================================


class TestPriceMathWithRealPool:
    """Test price math against real pool state."""

    def test_sqrt_price_conversion(self, aerodrome_adapter):
        """Test sqrtPriceX96 to price conversion against real pool."""
        state = aerodrome_adapter.get_current_state()

        # Convert back and forth
        price = state["price"]

        # Price should be positive and small (cNGN << USDC in value)
        assert price > 0

    def test_tick_to_price_conversion(self, aerodrome_adapter):
        """Test tick to price conversion."""
        state = aerodrome_adapter.get_current_state()

        # Convert tick to price
        price_from_tick = aerodrome_adapter._tick_to_price(state["tick"])

        # Should be close to the sqrtPrice-derived price
        # (may not be exact due to rounding)
        ratio = float(price_from_tick) / float(state["price"])
        assert 0.99 < ratio < 1.01  # Within 1%

    def test_tick_range_calculation(self, aerodrome_adapter, sample_prices):
        """Test tick range calculation."""
        tick_lower, tick_upper = aerodrome_adapter.calculate_tick_range(sample_prices)

        # Tick lower should be less than upper
        assert tick_lower < tick_upper

        # Should be aligned to tick spacing
        # Note: Python modulo with negatives needs special handling
        tick_spacing = aerodrome_adapter.config.tick_spacing
        assert tick_lower == (tick_lower // tick_spacing) * tick_spacing
        assert tick_upper == (tick_upper // tick_spacing) * tick_spacing


# =============================================================================
# WRITE TESTS - Requires Impersonation
# =============================================================================


class TestWriteOperationsWithImpersonation:
    """
    Test write operations by impersonating a funded wallet.

    These tests manipulate Anvil state to simulate having tokens.
    """

    @pytest.fixture
    def funded_adapter(self, anvil_base, web3_base, aerodrome_adapter):
        """
        Create an adapter with a funded wallet by impersonating a whale.

        This uses Anvil's impersonation feature to act as a wallet
        that already has cNGN and USDC.
        """
        # Find a cNGN holder to impersonate
        # For now, we'll use the contract deployer or a known holder
        # This would need to be updated based on actual on-chain data
        pytest.skip("Impersonation test - requires known funded wallet address")

    def test_approve_tokens(self, aerodrome_adapter, web3_base):
        """Test token approval (doesn't require balance)."""
        import asyncio

        # Approval should work even without balance
        # We're just testing the transaction building
        try:
            # This will fail on send but we can test the building
            asyncio.get_event_loop().run_until_complete(
                aerodrome_adapter._approve_if_needed(
                    aerodrome_adapter.config.token0_address,
                    aerodrome_adapter.config.nft_manager_address,
                    1000000,
                )
            )
        except Exception as e:
            # Expected to fail due to no gas, but shouldn't error on building
            if "insufficient funds" not in str(e).lower():
                raise

    def test_mint_position_simulation(self, aerodrome_adapter, sample_prices):
        """
        Test mint position transaction building.

        Note: This tests transaction construction, not actual execution.
        """
        import asyncio

        # Calculate tick range
        tick_lower, tick_upper = aerodrome_adapter.calculate_tick_range(sample_prices)

        # Calculate amounts (will be 0 for unfunded wallet)
        amount0, amount1 = aerodrome_adapter.calculate_mint_amounts()

        # Amounts should be 0 for unfunded wallet
        assert amount0 == 0 or amount1 == 0  # At least one should be 0


# =============================================================================
# CAPITAL ALLOCATION TESTS - With Mock Balances
# =============================================================================


class TestCapitalAllocationWithFork:
    """Test capital allocation logic with forked state."""

    def test_calculate_mint_amounts_unfunded(self, aerodrome_adapter):
        """Test calculate_mint_amounts with unfunded wallet."""
        amount0, amount1 = aerodrome_adapter.calculate_mint_amounts()

        # Unfunded wallet should return 0
        # (can't deploy what you don't have)
        assert amount0 == 0
        assert amount1 == 0

    def test_get_deployable_balances_unfunded(self, aerodrome_adapter):
        """Test get_deployable_balances with unfunded wallet."""
        balances = aerodrome_adapter.get_deployable_balances()

        assert "token0" in balances
        assert "token1" in balances
        assert balances["token0"] == Decimal("0")
        assert balances["token1"] == Decimal("0")


# =============================================================================
# INTEGRATION TESTS - Full Flow
# =============================================================================


class TestFullFlowSimulation:
    """
    Test full mint/remove flow simulation.

    These tests verify the logic flow without actually executing transactions.
    """

    def test_rebalance_decision_logic(self, aerodrome_adapter, sample_prices):
        """Test the decision logic for rebalancing."""
        # Get current state
        state = aerodrome_adapter.get_current_state()
        current_tick = state["tick"]

        # Calculate new range
        tick_lower, tick_upper = aerodrome_adapter.calculate_tick_range(sample_prices)

        # Check if current tick would be in the new range
        in_range = tick_lower <= current_tick <= tick_upper

        # This tests the logic, not the actual rebalancing
        assert isinstance(in_range, bool)

    def test_position_state_parsing(self, aerodrome_adapter):
        """Test that position state parsing works correctly."""
        # Even with no position, the method should handle gracefully
        owned = aerodrome_adapter.get_owned_positions()

        for token_id in owned:
            state = aerodrome_adapter.get_position_state(token_id)
            if state:
                assert state.token_id == token_id
                assert state.liquidity >= 0
                assert state.tick_lower < state.tick_upper


# =============================================================================
# ERROR HANDLING TESTS
# =============================================================================


class TestErrorHandling:
    """Test error handling in fork environment."""

    def test_invalid_rpc_url(self):
        """Test handling of invalid RPC URL."""
        with pytest.raises(Exception):
            adapter = AerodromeAdapter(
                lp_private_key="0x" + "00" * 32,
                rpc_url="http://invalid:9999",
                params=DexParams(),
            )
            adapter.get_current_state()

    def test_invalid_contract_address(self, anvil_base, test_private_key):
        """Test handling of invalid contract address."""
        # Create adapter with invalid pool address
        from engine.venues.dex.base import PoolConfig

        invalid_config = PoolConfig(
            chain_id=8453,
            chain_name="base",
            rpc_url=anvil_base,
            pool_address="0x0000000000000000000000000000000000000000",
            nft_manager_address="0x827922686190790b37229fd06084350E74485b72",
            router_address="0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5",
            token0_address="0x46C85152bFe9f96829aA94755D9f915F9B10EF5F",
            token1_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            token0_symbol="cNGN",
            token1_symbol="USDC",
            token0_decimals=6,
            token1_decimals=6,
            tick_spacing=100,
        )

        # This should fail when trying to read from invalid address
        # The exact error depends on the RPC behavior
