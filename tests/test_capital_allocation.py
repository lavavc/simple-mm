"""Unit tests for DEX capital allocation logic."""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from engine.api.schemas import DexParams


class TestCapitalAllocationLogic:
    """Test the capital allocation calculation logic."""

    def calculate_allocation(
        self,
        balance0: Decimal,
        balance1: Decimal,
        params: DexParams,
        reference_price_usd: Decimal | None = None,
        token0_decimals: int = 18,
        token1_decimals: int = 6,
    ) -> tuple[int, int]:
        """
        Simulate BaseDexAdapter.calculate_mint_amounts() logic.

        This mirrors the actual implementation for testing without web3.
        """
        # 1. Apply max utilization percent
        max_util = params.max_utilization_percent / Decimal("100")
        available0 = balance0 * max_util
        available1 = balance1 * max_util

        # 2. Subtract minimum reserves
        available0 = max(Decimal("0"), available0 - params.min_reserve_token0)
        available1 = max(Decimal("0"), available1 - params.min_reserve_token1)

        # Also ensure we don't go below reserves
        max_from_reserve0 = max(Decimal("0"), balance0 - params.min_reserve_token0)
        max_from_reserve1 = max(Decimal("0"), balance1 - params.min_reserve_token1)
        available0 = min(available0, max_from_reserve0)
        available1 = min(available1, max_from_reserve1)

        # 3. Apply max position USD cap if set
        if params.max_position_usd and reference_price_usd:
            token0_usd_value = available0 * reference_price_usd
            token1_usd_value = available1  # Stablecoin ≈ $1

            total_usd = token0_usd_value + token1_usd_value

            if total_usd > params.max_position_usd:
                scale_factor = params.max_position_usd / total_usd
                available0 = available0 * scale_factor
                available1 = available1 * scale_factor

        # Convert to raw units
        amount0 = int(available0 * Decimal(10**token0_decimals))
        amount1 = int(available1 * Decimal(10**token1_decimals))

        return amount0, amount1


class TestMaxUtilization(TestCapitalAllocationLogic):
    """Test max_utilization_percent parameter."""

    def test_default_80_percent(self):
        """Default 80% utilization."""
        params = DexParams()  # Default 80%

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
        )

        # Should use 80% of each
        expected0 = int(Decimal("800000") * Decimal(10**18))
        expected1 = int(Decimal("800") * Decimal(10**6))

        assert amount0 == expected0
        assert amount1 == expected1

    def test_100_percent_utilization(self):
        """100% utilization uses full balance."""
        params = DexParams(max_utilization_percent=Decimal("100"))

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
        )

        expected0 = int(Decimal("1000000") * Decimal(10**18))
        expected1 = int(Decimal("1000") * Decimal(10**6))

        assert amount0 == expected0
        assert amount1 == expected1

    def test_50_percent_utilization(self):
        """50% utilization uses half balance."""
        params = DexParams(max_utilization_percent=Decimal("50"))

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
        )

        expected0 = int(Decimal("500000") * Decimal(10**18))
        expected1 = int(Decimal("500") * Decimal(10**6))

        assert amount0 == expected0
        assert amount1 == expected1

    def test_zero_utilization(self):
        """0% utilization returns zero."""
        params = DexParams(max_utilization_percent=Decimal("0"))

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
        )

        assert amount0 == 0
        assert amount1 == 0


class TestMinimumReserves(TestCapitalAllocationLogic):
    """Test min_reserve_token0 and min_reserve_token1 parameters."""

    def test_reserves_subtracted(self):
        """Reserves are subtracted from available balance."""
        params = DexParams(
            max_utilization_percent=Decimal("100"),
            min_reserve_token0=Decimal("100000"),
            min_reserve_token1=Decimal("200"),
        )

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
        )

        # Should be balance - reserve
        expected0 = int(Decimal("900000") * Decimal(10**18))
        expected1 = int(Decimal("800") * Decimal(10**6))

        assert amount0 == expected0
        assert amount1 == expected1

    def test_reserves_with_utilization(self):
        """Reserves and utilization work together."""
        params = DexParams(
            max_utilization_percent=Decimal("90"),
            min_reserve_token0=Decimal("100000"),
            min_reserve_token1=Decimal("200"),
        )

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
        )

        # 90% of 1M = 900k, minus 100k reserve = 800k
        # But also can't exceed (balance - reserve) = 900k
        # So final = min(800k, 900k) = 800k
        expected0 = int(Decimal("800000") * Decimal(10**18))

        # 90% of 1000 = 900, minus 200 reserve = 700
        # Can't exceed (1000 - 200) = 800
        # So final = min(700, 800) = 700
        expected1 = int(Decimal("700") * Decimal(10**6))

        assert amount0 == expected0
        assert amount1 == expected1

    def test_reserve_larger_than_balance(self):
        """Reserve larger than balance returns zero."""
        params = DexParams(
            max_utilization_percent=Decimal("100"),
            min_reserve_token0=Decimal("2000000"),  # More than balance
            min_reserve_token1=Decimal("100"),
        )

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
        )

        assert amount0 == 0
        # Token1 should still work
        expected1 = int(Decimal("900") * Decimal(10**6))
        assert amount1 == expected1

    def test_reserve_equals_balance(self):
        """Reserve equal to balance returns zero."""
        params = DexParams(
            max_utilization_percent=Decimal("100"),
            min_reserve_token0=Decimal("1000000"),
            min_reserve_token1=Decimal("1000"),
        )

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
        )

        assert amount0 == 0
        assert amount1 == 0


class TestMaxPositionUsd(TestCapitalAllocationLogic):
    """Test max_position_usd parameter."""

    def test_cap_applied_when_exceeded(self):
        """Position is scaled down when USD cap is exceeded."""
        params = DexParams(
            max_utilization_percent=Decimal("100"),
            max_position_usd=Decimal("5000"),
        )

        # 10M CNGN at $0.0006 = $6000
        # 5000 USDC = $5000
        # Total = $11000, cap is $5000

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("10000000"),
            balance1=Decimal("5000"),
            params=params,
            reference_price_usd=Decimal("0.0006"),
        )

        # Scale factor = 5000 / 11000 ≈ 0.4545
        # Should be roughly 4.5M CNGN and 2272 USDC
        amount0_decimal = Decimal(amount0) / Decimal(10**18)
        amount1_decimal = Decimal(amount1) / Decimal(10**6)

        # Verify total USD value is at cap
        total_usd = (amount0_decimal * Decimal("0.0006")) + amount1_decimal
        assert Decimal("4999") < total_usd < Decimal("5001")

    def test_cap_not_applied_when_under(self):
        """No scaling when under USD cap."""
        params = DexParams(
            max_utilization_percent=Decimal("100"),
            max_position_usd=Decimal("50000"),  # High cap
        )

        # Total value well under cap
        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
            reference_price_usd=Decimal("0.0006"),
        )

        # Should use full balance
        expected0 = int(Decimal("1000000") * Decimal(10**18))
        expected1 = int(Decimal("1000") * Decimal(10**6))

        assert amount0 == expected0
        assert amount1 == expected1

    def test_cap_without_reference_price(self):
        """USD cap is ignored without reference price."""
        params = DexParams(
            max_utilization_percent=Decimal("100"),
            max_position_usd=Decimal("100"),  # Very low cap
        )

        # Without reference_price_usd, cap is not applied
        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("10000000"),
            balance1=Decimal("5000"),
            params=params,
            reference_price_usd=None,  # No reference price
        )

        # Should use full balance despite cap
        expected0 = int(Decimal("10000000") * Decimal(10**18))
        expected1 = int(Decimal("5000") * Decimal(10**6))

        assert amount0 == expected0
        assert amount1 == expected1

    def test_cap_with_no_max_set(self):
        """No scaling when max_position_usd is None."""
        params = DexParams(
            max_utilization_percent=Decimal("100"),
            max_position_usd=None,
        )

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("10000000"),
            balance1=Decimal("5000"),
            params=params,
            reference_price_usd=Decimal("0.0006"),
        )

        # Should use full balance
        expected0 = int(Decimal("10000000") * Decimal(10**18))
        expected1 = int(Decimal("5000") * Decimal(10**6))

        assert amount0 == expected0
        assert amount1 == expected1


class TestCombinedConstraints(TestCapitalAllocationLogic):
    """Test multiple constraints working together."""

    def test_all_constraints(self, conservative_dex_params):
        """Test with utilization, reserves, and USD cap."""
        params = conservative_dex_params

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("5000000"),
            balance1=Decimal("3000"),
            params=params,
            reference_price_usd=Decimal("0.0006"),
        )

        amount0_decimal = Decimal(amount0) / Decimal(10**18)
        amount1_decimal = Decimal(amount1) / Decimal(10**6)

        # Verify constraints are respected
        # 1. Less than 70% of balance
        assert amount0_decimal < Decimal("5000000") * Decimal("0.7")
        assert amount1_decimal < Decimal("3000") * Decimal("0.7")

        # 2. Reserves maintained
        used0 = Decimal("5000000") - amount0_decimal
        used1 = Decimal("3000") - amount1_decimal
        assert used0 >= params.min_reserve_token0 or amount0_decimal == 0
        assert used1 >= params.min_reserve_token1 or amount1_decimal == 0

    def test_zero_balance(self):
        """Zero balance returns zero."""
        params = DexParams()

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("0"),
            balance1=Decimal("0"),
            params=params,
        )

        assert amount0 == 0
        assert amount1 == 0

    def test_asymmetric_balances(self):
        """Handles asymmetric token balances."""
        params = DexParams(max_utilization_percent=Decimal("100"))

        # Only token1 has balance
        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("0"),
            balance1=Decimal("1000"),
            params=params,
        )

        assert amount0 == 0
        expected1 = int(Decimal("1000") * Decimal(10**6))
        assert amount1 == expected1


class TestEdgeCases(TestCapitalAllocationLogic):
    """Test edge cases and boundary conditions."""

    def test_very_small_balance(self):
        """Handle very small balances (dust)."""
        params = DexParams()

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("0.000001"),
            balance1=Decimal("0.000001"),
            params=params,
        )

        # Should produce valid integers (may be 0 due to rounding)
        assert isinstance(amount0, int)
        assert isinstance(amount1, int)

    def test_very_large_balance(self):
        """Handle very large balances."""
        params = DexParams()

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000000000"),  # 1 trillion
            balance1=Decimal("1000000000"),     # 1 billion
            params=params,
        )

        # Should not overflow
        assert amount0 > 0
        assert amount1 > 0

    def test_precision_maintenance(self):
        """Decimal precision is maintained throughout calculation."""
        params = DexParams(max_utilization_percent=Decimal("33.333333"))

        amount0, amount1 = self.calculate_allocation(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            params=params,
        )

        # Should handle fractional percentages
        expected0_approx = Decimal("333333.33") * Decimal(10**18)
        assert abs(amount0 - int(expected0_approx)) < int(Decimal(10**18))
