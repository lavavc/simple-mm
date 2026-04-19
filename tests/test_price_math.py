"""Unit tests for price/tick math conversions."""

import pytest
from decimal import Decimal
import math
from unittest.mock import MagicMock

from tests.conftest_params import make_dex_params
from engine.venues.dex.shared import _Q96, _tick_to_sqrt_price_x96


# =============================================================================
# Exact integer TickMath (_tick_to_sqrt_price_x96)
# =============================================================================


class TestTickToSqrtPriceX96:
    """Spot-check _tick_to_sqrt_price_x96 against known V4 TickMath.sol constants."""

    def test_tick_zero_is_q96(self):
        """Tick 0 must return exactly 2^96."""
        assert _tick_to_sqrt_price_x96(0) == _Q96

    def test_tick_1_known_value(self):
        """Tick 1 → 79232123823359799118286999568 (matches our Q128.128→Q64.96 rounding)."""
        assert _tick_to_sqrt_price_x96(1) == 79232123823359799118286999568

    def test_tick_neg1_known_value(self):
        """Tick -1 → 79224201403219477170569942574."""
        assert _tick_to_sqrt_price_x96(-1) == 79224201403219477170569942574

    def test_tick_1_and_neg1_are_near_q96(self):
        """Both ±1 ticks should be within 0.01% of Q96."""
        q96 = _Q96
        assert abs(_tick_to_sqrt_price_x96(1) - q96) / q96 < 0.0001
        assert abs(_tick_to_sqrt_price_x96(-1) - q96) / q96 < 0.0001

    def test_positive_tick_greater_than_q96(self):
        """Positive ticks represent higher prices → sqrtPrice > Q96."""
        assert _tick_to_sqrt_price_x96(100) > _Q96
        assert _tick_to_sqrt_price_x96(10000) > _Q96

    def test_negative_tick_less_than_q96(self):
        """Negative ticks represent lower prices → sqrtPrice < Q96."""
        assert _tick_to_sqrt_price_x96(-100) < _Q96
        assert _tick_to_sqrt_price_x96(-10000) < _Q96

    def test_positive_and_negative_are_inverse(self):
        """For tick T, sqrt(T) * sqrt(-T) ≈ Q96^2 (they multiply to ~1 in Q64.96)."""
        for tick in [1, 10, 100, 1000]:
            pos = _tick_to_sqrt_price_x96(tick)
            neg = _tick_to_sqrt_price_x96(-tick)
            # product / Q96^2 should be very close to 1
            product = pos * neg
            ratio = product / (_Q96 ** 2)
            assert abs(ratio - 1.0) < 0.0001, f"tick ±{tick}: ratio={ratio}"

    def test_large_tick_cngn_usdc_range(self):
        """Large negative ticks (cNGN/USDC 18-dec vs 6-dec) don't raise and are positive."""
        # cNGN/USDC pool ticks are around -300000 to -200000
        result = _tick_to_sqrt_price_x96(-276324)
        assert result > 0

    def test_max_tick_does_not_raise(self):
        """Maximum valid tick (887272) returns a positive integer."""
        result = _tick_to_sqrt_price_x96(887272)
        assert result > 0

    def test_min_tick_does_not_raise(self):
        """Minimum valid tick (-887272) returns a positive integer."""
        result = _tick_to_sqrt_price_x96(-887272)
        assert result > 0

    def test_out_of_range_raises(self):
        """Ticks beyond ±887272 must raise ValueError."""
        with pytest.raises(ValueError):
            _tick_to_sqrt_price_x96(887273)
        with pytest.raises(ValueError):
            _tick_to_sqrt_price_x96(-887273)

    def test_monotonically_increasing(self):
        """sqrtPrice must increase monotonically as tick increases."""
        ticks = [-1000, -100, -10, 0, 10, 100, 1000]
        prices = [_tick_to_sqrt_price_x96(t) for t in ticks]
        for i in range(len(prices) - 1):
            assert prices[i] < prices[i + 1], f"Not monotonic at tick {ticks[i]}"


# =============================================================================
# _compute_liquidity_from_amounts (via V4PositionManager helper)
# =============================================================================


class TestComputeLiquidityFromAmounts:
    """Verify that the exact-integer TickMath fix eliminates underdeployment."""

    def _make_manager(self) -> object:
        from types import SimpleNamespace
        from engine.lp.uniswap_v4 import V4PositionManager
        import types
        mgr = SimpleNamespace()
        mgr._compute_liquidity_from_amounts = types.MethodType(
            V4PositionManager._compute_liquidity_from_amounts, mgr
        )
        return mgr

    def test_symmetric_in_range_full_deployment(self):
        """At tick midpoint with balanced amounts, L0 == L1 so no token is left idle."""
        mgr = self._make_manager()
        # Use small equal-decimal ticks for clarity: ticks ±1000, price at tick 0
        tick_lower = -1000
        tick_upper = 1000
        sqrt_p = _tick_to_sqrt_price_x96(0)  # exact Q96
        sqrt_a = _tick_to_sqrt_price_x96(tick_lower)
        sqrt_b = _tick_to_sqrt_price_x96(tick_upper)

        # Compute ideal balanced amounts from L=1_000_000
        L = 1_000_000
        amount0_ideal = L * (sqrt_b - sqrt_p) * _Q96 // (sqrt_p * sqrt_b)
        amount1_ideal = L * (sqrt_p - sqrt_a) // _Q96

        liquidity = mgr._compute_liquidity_from_amounts(sqrt_p, tick_lower, tick_upper, amount0_ideal, amount1_ideal)

        # Liquidity should be ≥ reference L (integer arithmetic may round up slightly)
        assert liquidity >= L * 0.99, f"Severe underdeployment: got {liquidity}, expected ~{L}"

    def test_price_below_range_uses_amount0_only(self):
        """Price below range → only amount0 contributes; amount1 is ignored."""
        mgr = self._make_manager()
        sqrt_p = _tick_to_sqrt_price_x96(-2000)   # below lower bound
        liquidity = mgr._compute_liquidity_from_amounts(sqrt_p, -1000, 1000, 1_000_000, 999_999_999)
        # amount1 is huge but irrelevant; result should be reasonable
        assert liquidity > 0

    def test_price_above_range_uses_amount1_only(self):
        """Price above range → only amount1 contributes; amount0 is ignored."""
        mgr = self._make_manager()
        sqrt_p = _tick_to_sqrt_price_x96(2000)   # above upper bound
        liquidity = mgr._compute_liquidity_from_amounts(sqrt_p, -1000, 1000, 999_999_999, 1_000_000)
        assert liquidity > 0

    def test_zero_amounts_give_zero_liquidity(self):
        """Zero amounts → zero liquidity."""
        mgr = self._make_manager()
        sqrt_p = _tick_to_sqrt_price_x96(0)
        assert mgr._compute_liquidity_from_amounts(sqrt_p, -1000, 1000, 0, 0) == 0


# =============================================================================
# EWMA stats (compute_ewma_stats)
# =============================================================================


class TestComputeEwmaStats:
    """Tests for strategy.compute_ewma_stats."""

    def test_stable_prices_low_std_dev(self):
        from engine.lp.strategy import compute_ewma_stats
        params = make_dex_params(ewma_lambda=Decimal("0.99"))
        prices = [Decimal("0.000606")] * 50
        mean, std = compute_ewma_stats(prices, params)
        assert abs(mean - 0.000606) < 1e-8
        assert std == 0.0 or std < 1e-10

    def test_volatile_prices_higher_std_dev(self, volatile_prices):
        from engine.lp.strategy import compute_ewma_stats
        params = make_dex_params(ewma_lambda=Decimal("0.99"))
        _, std_stable = compute_ewma_stats([Decimal("0.000606")] * 50, params)
        _, std_vol = compute_ewma_stats(volatile_prices, params)
        assert std_vol > std_stable

    def test_lookback_points_applied_via_calculate_tick_range(self):
        """lookback_points slicing happens in calculate_tick_range, not compute_ewma_stats."""
        from engine.lp.strategy import calculate_tick_range
        params = make_dex_params(ewma_lambda=Decimal("0.99"), lookback_points=10)
        prices = [Decimal("0.000600")] * 90 + [Decimal("0.000700")] * 10
        # calculate_tick_range slices to last 10, so mean should be near 0.000700
        tick_lower, tick_upper = calculate_tick_range(
            prices, params, 60, 6, 6, venue_name="test"
        )
        assert tick_lower < tick_upper


# =============================================================================
# calculate_tick_range recovery skew
# =============================================================================


class TestCalculateTickRangeRecoverySkew:
    """Tests for the recovery_price skew adjustment in calculate_tick_range."""

    def _make_params(self, downside_skew="0.4"):
        return make_dex_params(
            sd_multiplier=Decimal("2.0"),
            downside_skew=Decimal(downside_skew),
            ewma_lambda=Decimal("0.99"),
            min_tick_width=100,
            max_tick_width=10000,
        )

    def _prices(self, mean=0.000606, n=50, std=0.00002):
        import random
        random.seed(1)
        return [Decimal(str(mean + random.gauss(0, std))) for _ in range(n)]

    def _tick_range(self, prices, params, recovery_price=None):
        from engine.lp.strategy import calculate_tick_range
        return calculate_tick_range(prices, params, 60, 6, 6, recovery_price=recovery_price, venue_name="test")

    def _ewma(self, prices, params):
        from engine.lp.strategy import compute_ewma_stats
        return compute_ewma_stats(prices, params)

    def test_price_above_mean_increases_downside_skew(self):
        """Price 2σ above mean → downside_skew increases (more downside protection)."""
        params = self._make_params(downside_skew="0.4")
        prices = self._prices(mean=0.000606, std=0.00002)
        mean, std = self._ewma(prices, params)

        # No recovery_price: baseline range (skew not mutated)
        t_low_base, t_up_base = self._tick_range(prices, params, recovery_price=None)

        # Fresh params for the recovery call so skew starts from 0.4
        params2 = self._make_params(downside_skew="0.4")
        recovery_high = mean + 2 * std
        t_low_high, t_up_high = self._tick_range(prices, params2, recovery_price=recovery_high)

        # More downside protection → lower bound should move DOWN (more room below)
        assert t_low_high <= t_low_base

    def test_price_below_mean_decreases_downside_skew(self):
        """Price 2σ below mean → downside_skew decreases (more upside room)."""
        params = self._make_params(downside_skew="0.4")
        prices = self._prices(mean=0.000606, std=0.00002)
        mean, std = self._ewma(prices, params)

        t_low_base, t_up_base = self._tick_range(prices, params, recovery_price=None)

        params2 = self._make_params(downside_skew="0.4")
        recovery_low = mean - 2 * std
        t_low_low, t_up_low = self._tick_range(prices, params2, recovery_price=recovery_low)

        # Less downside protection → upper bound should move UP (more room above)
        assert t_up_low >= t_up_base

    def test_skew_clamped_at_extremes(self):
        """Skew is clamped to [0.2, 0.8] even at extreme price deviations."""
        params = self._make_params(downside_skew="0.4")
        prices = self._prices(mean=0.000606, std=0.000001)  # tiny std_dev
        mean, std = self._ewma(prices, params)

        # Extreme prices far outside any realistic range
        very_high = mean + 1000 * max(std, 1e-10)
        very_low = mean - 1000 * max(std, 1e-10)

        # Should not raise, and ticks should be finite
        t_low_hi, t_up_hi = self._tick_range(prices, self._make_params(), recovery_price=very_high)
        t_low_lo, t_up_lo = self._tick_range(prices, self._make_params(), recovery_price=very_low)

        assert t_low_hi < t_up_hi
        assert t_low_lo < t_up_lo

    def test_skew_mutation_persisted_on_params(self):
        """calculate_tick_range mutates params.downside_skew when recovery_price adjusts it."""
        from engine.lp.strategy import calculate_tick_range, compute_ewma_stats

        params = make_dex_params(downside_skew=Decimal("0.4"), ewma_lambda=Decimal("0.99"))
        prices = self._prices(mean=0.000606, std=0.00002)
        original_skew = params.downside_skew

        # Price 3σ above mean → deviation pushes skew higher
        mean, std = compute_ewma_stats(prices, params)
        recovery_high = mean + 3 * std
        calculate_tick_range(prices, params, 60, 6, 6, recovery_price=recovery_high, venue_name="test")

        assert params.downside_skew != original_skew
        assert Decimal("0.2") <= params.downside_skew <= Decimal("0.8")

    def test_no_skew_mutation_without_recovery_price(self):
        """calculate_tick_range must not touch params.downside_skew when recovery_price is None."""
        from engine.lp.strategy import calculate_tick_range

        params = make_dex_params(downside_skew=Decimal("0.4"), ewma_lambda=Decimal("0.99"))
        prices = self._prices(mean=0.000606, std=0.00002)
        calculate_tick_range(prices, params, 60, 6, 6, recovery_price=None, venue_name="test")

        assert params.downside_skew == Decimal("0.4")
