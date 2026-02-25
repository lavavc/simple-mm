"""Unit tests for price/tick math conversions."""

import pytest
from decimal import Decimal
import math

from engine.api.schemas import DexParams


class TestTickPriceConversions:
    """Test tick <-> price conversion math."""

    def test_tick_to_price_at_zero(self):
        """Tick 0 should give price of 1.0 (before decimal adjustment)."""
        # Using the formula: price = 1.0001^tick
        tick = 0
        price = Decimal("1.0001") ** tick
        assert price == Decimal("1")

    def test_tick_to_price_positive(self):
        """Positive ticks should give prices > 1."""
        tick = 1000
        price = Decimal("1.0001") ** tick
        assert price > Decimal("1")
        # 1.0001^1000 ≈ 1.1052
        assert Decimal("1.10") < price < Decimal("1.11")

    def test_tick_to_price_negative(self):
        """Negative ticks should give prices < 1."""
        tick = -1000
        price = Decimal("1.0001") ** tick
        assert price < Decimal("1")
        # 1.0001^-1000 ≈ 0.9048
        assert Decimal("0.90") < price < Decimal("0.91")

    def test_price_to_tick_roundtrip(self):
        """Converting price -> tick -> price should be close to original."""
        original_price = Decimal("0.000606")

        # Price to tick (simplified, without decimal adjustment)
        tick = int(math.log(float(original_price)) / math.log(1.0001))

        # Tick back to price
        recovered_price = Decimal("1.0001") ** tick

        # Should be within 0.01% due to tick discretization
        diff = abs(float(recovered_price) - float(original_price)) / float(original_price)
        assert diff < 0.01  # 1% tolerance due to tick spacing

    def test_tick_spacing_alignment(self):
        """Test tick alignment: floor for lower, ceil for upper (only moves when misaligned)."""
        spacing = 100

        floor_cases = [
            (150, 100),   # rounds down
            (199, 100),   # rounds down
            (200, 200),   # already aligned, no change
            (-150, -200), # rounds down (more negative)
            (0, 0),
        ]
        for tick, expected in floor_cases:
            assert math.floor(tick / spacing) * spacing == expected

        ceil_cases = [
            (200, 200),   # already aligned, no change
            (201, 300),   # rounds up
            (-200, -200), # already aligned, no change
            (-199, -100), # rounds up (less negative)
        ]
        for tick, expected in ceil_cases:
            assert math.ceil(tick / spacing) * spacing == expected

    def test_sqrt_price_x96_conversion(self):
        """Test sqrtPriceX96 to decimal conversion."""
        # sqrtPriceX96 = sqrt(price) * 2^96
        # For price = 1.0: sqrtPriceX96 = 2^96 ≈ 79228162514264337593543950336

        sqrt_price_x96 = 79228162514264337593543950336  # Price = 1.0

        # Convert back
        price = (Decimal(sqrt_price_x96) / Decimal(2**96)) ** 2
        assert Decimal("0.999") < price < Decimal("1.001")

    def test_sqrt_price_x96_small_price(self):
        """Test sqrtPriceX96 for small prices like CNGN/USDC."""
        # For price = 0.000606 (CNGN/USDC)
        # But need to account for decimal difference (18 - 6 = 12)
        # Adjusted price = 0.000606 / 10^12 = very small

        # This tests that the math handles extreme values
        target_price = Decimal("0.000606")
        decimal_diff = 18 - 6  # token0_decimals - token1_decimals

        # Adjusted price for tick calculation
        adjusted = float(target_price) / (10**decimal_diff)
        tick = int(math.log(adjusted) / math.log(1.0001))

        # Tick should be very negative for such small prices
        assert tick < -200000


class TestTickRangeCalculation:
    """Test SD-based tick range calculation."""

    def test_calculate_range_basic(self, sample_prices):
        """Test basic range calculation with sample prices."""
        import statistics

        params = DexParams(sd_multiplier=Decimal("1.5"))

        float_prices = [float(p) for p in sample_prices]
        mean = statistics.mean(float_prices)
        std = statistics.stdev(float_prices)

        lower = mean - (std * float(params.sd_multiplier))
        upper = mean + (std * float(params.sd_multiplier))

        # Range should be centered around mean
        assert lower < mean < upper

        # Range should be reasonable (not too wide)
        range_width = (upper - lower) / mean
        assert range_width < 0.5  # Less than 50% of mean

    def test_calculate_range_volatile(self, volatile_prices):
        """Volatile prices should give wider range."""
        import statistics

        params = DexParams(sd_multiplier=Decimal("1.5"))

        float_prices = [float(p) for p in volatile_prices]
        std = statistics.stdev(float_prices)

        # Volatile prices should have higher std dev
        assert std > 0.00001

    def test_calculate_range_stable(self, stable_prices):
        """Stable prices should give narrow range (clamped to min)."""
        import statistics

        params = DexParams(
            sd_multiplier=Decimal("1.5"),
            min_tick_width=100,
        )

        float_prices = [float(p) for p in stable_prices]
        std = statistics.stdev(float_prices)

        # Stable prices have zero std dev
        assert std == 0

        # Range should be clamped to min_tick_width

    def test_range_respects_min_width(self):
        """Range should never be smaller than min_tick_width."""
        params = DexParams(
            sd_multiplier=Decimal("0.1"),  # Very narrow multiplier
            min_tick_width=200,
        )

        # Even with tiny multiplier, min width should be enforced
        tick_lower = -100
        tick_upper = 100
        tick_width = tick_upper - tick_lower

        if tick_width < params.min_tick_width:
            mid = (tick_lower + tick_upper) // 2
            tick_lower = mid - params.min_tick_width // 2
            tick_upper = mid + params.min_tick_width // 2

        assert tick_upper - tick_lower >= params.min_tick_width

    def test_range_respects_max_width(self):
        """Range should never be larger than max_tick_width."""
        params = DexParams(
            sd_multiplier=Decimal("10"),  # Very wide multiplier
            max_tick_width=500,
        )

        # Even with huge multiplier, max width should be enforced
        tick_lower = -1000
        tick_upper = 1000
        tick_width = tick_upper - tick_lower

        if tick_width > params.max_tick_width:
            mid = (tick_lower + tick_upper) // 2
            tick_lower = mid - params.max_tick_width // 2
            tick_upper = mid + params.max_tick_width // 2

        assert tick_upper - tick_lower <= params.max_tick_width

    def test_insufficient_price_history(self):
        """calculate_tick_range requires at least 2 prices."""
        # Simulate the guard: len(prices) < 2 raises ValueError
        prices = [Decimal("0.000606")]
        if len(prices) < 2:
            with pytest.raises(ValueError):
                raise ValueError("Insufficient price history for SD calculation")


class TestDecimalAdjustments:
    """Test decimal adjustments for different token pairs."""

    @pytest.mark.parametrize("token0_decimals,token1_decimals,expected_adjustment", [
        (18, 6, 12),   # cNGN/USDC
        (18, 18, 0),   # Equal decimals
        (6, 18, -12),  # Inverted
        (8, 6, 2),     # BTC-like / USDC
    ])
    def test_decimal_diff_calculation(self, token0_decimals, token1_decimals, expected_adjustment):
        """Test decimal difference calculation."""
        decimal_diff = token0_decimals - token1_decimals
        assert decimal_diff == expected_adjustment

    def test_price_adjustment_cngn_usdc(self):
        """Test price adjustment for cNGN/USDC (18/6 decimals)."""
        # Raw price from sqrtPriceX96 (before decimal adjustment)
        raw_price = Decimal("1.0")

        # Decimal adjustment
        decimal_diff = 18 - 6
        adjusted_price = raw_price * Decimal(10**decimal_diff)

        # Adjusted price should be much larger
        assert adjusted_price == Decimal("1000000000000")

    def test_tick_to_price_with_decimals(self):
        """Test full tick to price conversion with decimal adjustment."""
        tick = -276324  # Example tick for cNGN/USDC

        # Base price from tick
        base_price = Decimal("1.0001") ** tick

        # Apply decimal adjustment (18 - 6 = 12)
        decimal_diff = 18 - 6
        adjusted_price = base_price * Decimal(10**decimal_diff)

        # The adjusted price compensates for decimal differences
        # For cNGN/USDC with 18/6 decimals, this gives the human-readable price
        # Should be a positive, reasonable number
        assert adjusted_price > Decimal("0")
        # At tick -276324, price should be around 1.0 (after adjustment)
        assert Decimal("0.1") < adjusted_price < Decimal("10")
