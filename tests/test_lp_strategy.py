"""Unit tests for compute_ewma_stats and calculate_tick_range in engine/lp/strategy.py.

Covers log-return variance computation, the σ floor, scale invariance, and the
invert_price path. All tests are pure-math — no mocks, no DB, no Web3.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from engine.lp import strategy
from tests.conftest_params import make_dex_params


# ---------------------------------------------------------------------------
# compute_ewma_stats
# ---------------------------------------------------------------------------


class TestComputeEwmaStats:
    def test_flat_series_returns_floor_std_dev(self):
        """A perfectly flat price series has no log-returns, so std_dev hits the floor."""
        prices = [Decimal("0.001")] * 20
        params = make_dex_params(ewma_lambda=Decimal("0.975"))

        mean, std_dev = strategy.compute_ewma_stats(prices, params)

        # Mean converges to the constant price (all weights sum to 1)
        assert abs(mean - 0.001) < 1e-9, f"expected mean≈0.001, got {mean}"
        # No volatility → variance is 0 → floored to 3 bps
        assert std_dev == pytest.approx(3e-4, rel=1e-9), f"expected floor 3e-4, got {std_dev}"

    def test_volatile_series_exceeds_floor(self):
        """A price series with real moves produces std_dev above the floor."""
        # ±1 % moves at each step
        prices = [Decimal("1.0"), Decimal("1.01"), Decimal("1.0"), Decimal("1.01"), Decimal("1.0")]
        params = make_dex_params(ewma_lambda=Decimal("0.5"))

        _, std_dev = strategy.compute_ewma_stats(prices, params)

        assert std_dev > 3e-4, f"expected std_dev > floor, got {std_dev}"
        # Log-return for a 1 % move ≈ 0.00995; with λ=0.5 the EWMA var should be in that ballpark
        assert std_dev < 0.02, f"std_dev unexpectedly large: {std_dev}"

    def test_log_returns_are_scale_invariant(self):
        """Proportionally identical moves at different price levels produce the same std_dev.

        A 1 % ratio move on prices around 1.0 must give the same std_dev as the same
        ratio move on prices around 0.001. Raw-price variance would differ by ~10^6.
        """
        ratio = Decimal("1.01")  # each step is a 1 % up/down cycle
        base_high = Decimal("1.0")
        base_low = Decimal("0.001")

        # 5-element series: up, down, up, down from each base
        prices_high = [base_high, base_high * ratio, base_high, base_high * ratio, base_high]
        prices_low = [base_low, base_low * ratio, base_low, base_low * ratio, base_low]

        params = make_dex_params(ewma_lambda=Decimal("0.5"))

        _, std_high = strategy.compute_ewma_stats(prices_high, params)
        _, std_low = strategy.compute_ewma_stats(prices_low, params)

        # Log-returns are dimensionless; variance must be identical regardless of scale
        assert std_high == pytest.approx(std_low, rel=1e-9), (
            f"scale invariance violated: std_high={std_high}, std_low={std_low}"
        )


# ---------------------------------------------------------------------------
# calculate_tick_range
# ---------------------------------------------------------------------------


def _stable_params(**overrides):
    """DexParams suitable for tick-range tests: moderate multiplier, symmetric skew."""
    return make_dex_params(
        sd_multiplier=Decimal("3.0"),
        downside_skew=Decimal("0.5"),
        ewma_lambda=Decimal("0.975"),
        min_tick_width=100,
        max_tick_width=2000,
        **overrides,
    )


class TestCalculateTickRange:
    def test_basic_returns_valid_ordered_tick_pair(self):
        """A stable price series produces tick_lower < tick_upper, both multiples of tick_spacing."""
        tick_spacing = 10
        prices = [Decimal("0.000606")] * 15  # cNGN/USDC-style stable price

        params = _stable_params()
        tick_lower, tick_upper = strategy.calculate_tick_range(
            prices,
            params,
            tick_spacing=tick_spacing,
            token0_decimals=18,
            token1_decimals=6,
        )

        assert tick_lower < tick_upper, f"expected lower < upper, got {tick_lower}, {tick_upper}"
        assert tick_lower % tick_spacing == 0, f"tick_lower {tick_lower} not aligned to spacing {tick_spacing}"
        assert tick_upper % tick_spacing == 0, f"tick_upper {tick_upper} not aligned to spacing {tick_spacing}"

    def test_invert_price_mirrors_direct_range(self):
        """With invert_price=True the inverted series should produce the same tick range
        as the direct series fed directly (up to tick-spacing rounding).

        The log-return variance is direction-agnostic; only the mean is inverted.
        After inverting the mean, lower/upper boundaries in log-price space are symmetric,
        so the same tick range is recovered.
        """
        direct_prices = [Decimal("1400"), Decimal("1420"), Decimal("1410")]
        inverted_prices = [Decimal(1) / p for p in direct_prices]

        params_direct = _stable_params()
        params_inverted = _stable_params()

        tick_lower_d, tick_upper_d = strategy.calculate_tick_range(
            direct_prices,
            params_direct,
            tick_spacing=24,
            token0_decimals=18,
            token1_decimals=6,
            invert_price=False,
            venue_name="direct",
        )
        tick_lower_i, tick_upper_i = strategy.calculate_tick_range(
            inverted_prices,
            params_inverted,
            tick_spacing=24,
            token0_decimals=18,
            token1_decimals=6,
            invert_price=True,
            venue_name="inverted",
        )

        # Allow one tick-spacing of slop from EWMA(1/x) ≠ 1/EWMA(x) mean rounding
        assert tick_lower_d == tick_lower_i, (
            f"tick_lower mismatch: direct={tick_lower_d}, inverted={tick_lower_i}"
        )
        assert tick_upper_d == tick_upper_i, (
            f"tick_upper mismatch: direct={tick_upper_d}, inverted={tick_upper_i}"
        )

    def test_invert_price_raises_on_non_positive_mean(self):
        """invert_price=True with an all-zero price series produces a non-positive mean
        and must raise ValueError when the mean is checked before inversion."""
        # All-zero prices → EWMA mean stays 0; invert_price check must catch mean <= 0
        prices = [Decimal("0"), Decimal("0"), Decimal("0")]

        params = _stable_params()
        with pytest.raises((ValueError, ZeroDivisionError)):
            strategy.calculate_tick_range(
                prices,
                params,
                tick_spacing=10,
                token0_decimals=18,
                token1_decimals=6,
                invert_price=True,
            )

    def test_insufficient_prices_raises(self):
        """A single-element price list must raise ValueError."""
        params = _stable_params()
        with pytest.raises(ValueError, match="Insufficient price history"):
            strategy.calculate_tick_range(
                [Decimal("1.0")],
                params,
                tick_spacing=10,
                token0_decimals=18,
                token1_decimals=6,
            )
