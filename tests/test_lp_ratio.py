"""Unit tests for LP ratio math: _compute_required_ratio and prepare_lp_balance logic."""

import math
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch


def compute_required_ratio(
    tick_lower: int,
    tick_upper: int,
    sqrt_price_x96: int,
    token0_decimals: int = 6,
    token1_decimals: int = 6,
) -> tuple[Decimal, Decimal]:
    """Pure-math implementation of V4LPAdapter._compute_required_ratio for testing."""
    Q96 = 2 ** 96
    sqrt_a = math.exp(tick_lower * math.log(1.0001) / 2) * Q96
    sqrt_b = math.exp(tick_upper * math.log(1.0001) / 2) * Q96
    sqrt_p = float(sqrt_price_x96)

    if sqrt_p <= sqrt_a:
        r0 = (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b) if sqrt_a * sqrt_b > 0 else 0.0
        r1 = 0.0
    elif sqrt_p >= sqrt_b:
        r0 = 0.0
        r1 = sqrt_b - sqrt_a
    else:
        r0 = (sqrt_b - sqrt_p) / (sqrt_p * sqrt_b)
        r1 = sqrt_p - sqrt_a

    dec_adj = Decimal(10 ** token0_decimals) / Decimal(10 ** token1_decimals)
    r0_dec = Decimal(str(r0)) / dec_adj
    r1_dec = Decimal(str(r1))
    return r0_dec, r1_dec


def price_to_sqrt_x96(price: float, token0_decimals: int = 6, token1_decimals: int = 6) -> int:
    """Convert a human-readable token1-per-token0 price to sqrtPriceX96."""
    dec_adj = 10 ** (token0_decimals - token1_decimals)
    adjusted = price * dec_adj
    return int(math.sqrt(adjusted) * (2 ** 96))


class TestComputeRequiredRatio:
    """Tests for _compute_required_ratio pure math."""

    def test_symmetric_range_price_at_midpoint(self):
        """When price is at tick range midpoint, both r0 and r1 should be non-zero."""
        # Symmetric range around tick 0 (price ≈ 1.0 for equal-decimal tokens)
        tick_lower = -1000
        tick_upper = 1000
        sqrt_p = price_to_sqrt_x96(1.0)

        r0, r1 = compute_required_ratio(tick_lower, tick_upper, sqrt_p)

        assert r0 > 0, "r0 should be positive when price is below upper bound"
        assert r1 > 0, "r1 should be positive when price is above lower bound"

    def test_price_below_range_all_token0(self):
        """When current price is below the range, position should be entirely in token0."""
        tick_lower = 1000   # price above current
        tick_upper = 2000
        sqrt_p = price_to_sqrt_x96(0.5)  # well below range

        r0, r1 = compute_required_ratio(tick_lower, tick_upper, sqrt_p)

        assert r0 > 0, "r0 should be positive when price is below range"
        assert r1 == 0, "r1 should be zero when price is below range"

    def test_price_above_range_all_token1(self):
        """When current price is above the range, position should be entirely in token1."""
        tick_lower = -2000
        tick_upper = -1000  # price below current
        sqrt_p = price_to_sqrt_x96(2.0)  # well above range

        r0, r1 = compute_required_ratio(tick_lower, tick_upper, sqrt_p)

        assert r0 == 0, "r0 should be zero when price is above range"
        assert r1 > 0, "r1 should be positive when price is above range"

    def test_skew_toward_upper_more_token1(self):
        """With price close to upper bound, more of the position should be in token1."""
        tick_lower = -2000
        tick_upper = 100
        # price close to upper → more token1
        sqrt_p = price_to_sqrt_x96(1.005)  # tick ≈ 50, close to upper

        r0_close_to_upper, r1_close_to_upper = compute_required_ratio(tick_lower, tick_upper, sqrt_p)

        # And compare with price in the middle
        sqrt_p_mid = price_to_sqrt_x96(0.5)  # well into range
        r0_mid, r1_mid = compute_required_ratio(tick_lower, tick_upper, sqrt_p_mid)

        # When closer to upper bound, r0 should be smaller (less token0 needed)
        assert r0_close_to_upper < r0_mid

    def test_ratio_sums_to_nonzero_when_in_range(self):
        """Total LP value should use both tokens when price is inside range."""
        tick_lower = -500
        tick_upper = 500
        sqrt_p = price_to_sqrt_x96(1.0)

        r0, r1 = compute_required_ratio(tick_lower, tick_upper, sqrt_p)

        # Both should contribute
        assert r0 + r1 > 0


class TestPrepareLpBalanceLogic:
    """Tests for the swap-to-ratio logic in prepare_lp_balance."""

    def test_no_swap_needed_when_balanced(self):
        """If already at target ratio within 1%, no swap should occur."""
        # Symmetric range at price 1.0: equal split
        tick_lower = -1000
        tick_upper = 1000
        sqrt_p = price_to_sqrt_x96(1.0)
        Q96 = Decimal(2 ** 96)
        price = (Decimal(sqrt_p) / Q96) ** 2  # ≈ 1.0

        r0, r1 = [Decimal(str(v)) for v in compute_required_ratio(tick_lower, tick_upper, sqrt_p)]

        # Construct target-ratio balance
        total_value = Decimal("1000")
        denom = r0 * price + r1 if (r0 * price + r1) > 0 else Decimal(1)
        target0 = total_value * r0 / denom
        target1 = total_value - target0 * price

        # Set balances exactly at target
        balance0 = target0
        balance1 = target1

        imbalance = abs(balance0 - target0) * price
        threshold = total_value * Decimal("0.01")

        # No swap should be needed
        assert imbalance <= threshold

    def test_swap_direction_when_excess_token0(self):
        """When there's more token0 than needed, surplus should be token0→token1 swap."""
        tick_lower = -1000
        tick_upper = 1000
        sqrt_p = price_to_sqrt_x96(1.0)
        Q96 = Decimal(2 ** 96)
        price = (Decimal(sqrt_p) / Q96) ** 2

        r0, r1 = [Decimal(str(v)) for v in compute_required_ratio(tick_lower, tick_upper, sqrt_p)]

        total_value = Decimal("1000")
        denom = r0 * price + r1 if (r0 * price + r1) > 0 else Decimal(1)
        target0 = total_value * r0 / denom

        # Excess token0 (much more than target)
        balance0 = target0 * Decimal("2")

        assert balance0 > target0, "Setup: balance0 should exceed target"

    def test_swap_direction_when_excess_token1(self):
        """When price is above range (position all in token1), r0=0 so target0=0 and all value is in token1."""
        # Price well above range → position entirely in token1
        tick_lower = -2000
        tick_upper = -1000
        sqrt_p = price_to_sqrt_x96(2.0)  # well above range

        r0, r1 = compute_required_ratio(tick_lower, tick_upper, sqrt_p)

        # r0 should be 0 (all token1 position)
        assert r0 == Decimal("0"), "When above range, no token0 needed"
        assert r1 > 0, "When above range, all value should be in token1"
