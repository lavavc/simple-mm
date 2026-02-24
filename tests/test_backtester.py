"""Unit tests for backtester modules."""

import math
from decimal import Decimal

import pytest

from engine.math.v3 import (
    tick_to_price,
    price_to_tick,
    tick_to_sqrt_price,
    sqrt_price_x96_to_decimal,
    align_tick,
    constrain_tick_width,
    compute_swap_step,
)
from backtester.strategy import EWMACalculator, calculate_tick_range
from backtester.pool_state import PoolState
from backtester.params import generate_grid, BacktestParams
from backtester import metrics


# ─── V3 math ────────────────────────────────────────────────────────────

class TestTickMath:
    def test_roundtrip_price_tick(self):
        """price → tick → price should be close to original."""
        for p in [0.0007, 0.001, 1.0, 1500.0]:
            tick = price_to_tick(Decimal(str(p)), 6, 6)
            recovered = float(tick_to_price(tick, 6, 6))
            assert abs(recovered / p - 1) < 0.01, f"Failed for {p}"

    def test_tick_to_sqrt_price(self):
        """tick_to_sqrt_price(0) == 1.0"""
        assert tick_to_sqrt_price(0) == pytest.approx(1.0)
        # tick 100 → 1.0001^50
        assert tick_to_sqrt_price(100) == pytest.approx(1.0001**50, rel=1e-6)

    def test_align_tick_down(self):
        assert align_tick(105, 10, "down") == 100
        assert align_tick(-15, 10, "down") == -20

    def test_align_tick_up(self):
        assert align_tick(100, 10, "up") == 110
        assert align_tick(101, 10, "up") == 110

    def test_constrain_tick_width_min(self):
        lo, hi = constrain_tick_width(50, 60, 100, 1000, 10)
        assert hi - lo >= 100

    def test_constrain_tick_width_max(self):
        lo, hi = constrain_tick_width(0, 2000, 100, 1000, 10)
        assert hi - lo <= 1010  # aligned, so may be slightly over max_width

    def test_sqrt_price_x96_equal_decimals(self):
        """Known sqrtPriceX96 for price=1 with equal decimals."""
        # sqrtPriceX96 = sqrt(1) * 2^96 = 2^96
        val = sqrt_price_x96_to_decimal(2**96, 6, 6)
        assert float(val) == pytest.approx(1.0)


class TestSwapStep:
    def test_zero_input(self):
        sp, consumed, out, fee = compute_swap_step(1.0, 1000, 0, 0.003, True)
        assert consumed == 0 and out == 0 and fee == 0

    def test_zero_liquidity(self):
        sp, consumed, out, fee = compute_swap_step(1.0, 0, 100, 0.003, True)
        assert out == 0

    def test_sell_token1_price_up(self):
        """Selling token1 → price goes UP (√P increases)."""
        sqrt_p = 1.0
        L = 10000
        amt = 100.0
        sp_next, _, out, fee = compute_swap_step(sqrt_p, L, amt, 0.003, False)
        assert sp_next > sqrt_p
        assert out > 0
        assert fee == pytest.approx(100 * 0.003)

    def test_sell_token0_price_down(self):
        """Selling token0 → price goes DOWN (√P decreases)."""
        sqrt_p = 1.0
        L = 10000
        amt = 100.0
        sp_next, _, out, fee = compute_swap_step(sqrt_p, L, amt, 0.003, True)
        assert sp_next < sqrt_p
        assert out > 0

    def test_single_tick_execution_price(self):
        """Execution price should equal √P_old × √P_new for single tick."""
        sqrt_p = 1.5
        L = 50000
        amt = 10.0
        sp_next, _, _, _ = compute_swap_step(sqrt_p, L, amt, 0.0, False)
        exec_price = sqrt_p * sp_next
        spot = sqrt_p ** 2
        # Execution price should be close to but slightly above spot (price impact)
        assert exec_price >= spot


# ─── EWMA ────────────────────────────────────────────────────────────────

class TestEWMA:
    def test_not_ready_initially(self):
        e = EWMACalculator(0.99)
        assert not e.ready

    def test_ready_after_two(self):
        e = EWMACalculator(0.99)
        e.update(1.0)
        e.update(2.0)
        assert e.ready

    def test_converges_to_recent(self):
        """With low lambda, EWMA should track recent values closely."""
        e = EWMACalculator(0.5)
        for _ in range(100):
            e.update(1.0)
        for _ in range(100):
            e.update(2.0)
        assert abs(e.mean - 2.0) < 0.1

    def test_std_positive_for_varying_input(self):
        e = EWMACalculator(0.95)
        for x in [1.0, 2.0, 1.5, 3.0, 0.5]:
            e.update(x)
        assert e.std > 0


# ─── Pool state ──────────────────────────────────────────────────────────

class TestPoolState:
    def test_mint_burn_roundtrip(self):
        ps = PoolState()
        ps.apply_mint(-100, 100, 5000)
        assert ps.get_active_liquidity(0) == 5000
        ps.apply_burn(-100, 100, 5000)
        assert ps.get_active_liquidity(0) == 0

    def test_active_liquidity_outside(self):
        ps = PoolState()
        ps.apply_mint(0, 100, 5000)
        assert ps.get_active_liquidity(-1) == 0
        assert ps.get_active_liquidity(50) == 5000
        assert ps.get_active_liquidity(100) == 0  # at upper tick, net cancels

    def test_simulate_swap_zero(self):
        ps = PoolState()
        ps.apply_mint(-100, 100, 10000)
        res = ps.simulate_swap(1.0, 0, True, 0.003)
        assert res.amount_out == 0

    def test_simulate_swap_basic(self):
        ps = PoolState()
        ps.apply_mint(-1000, 1000, 100000)
        res = ps.simulate_swap(1.0, 10.0, False, 0.003)
        assert res.amount_out > 0
        assert res.new_sqrt_price > 1.0  # price went up


# ─── Params ──────────────────────────────────────────────────────────────

class TestParams:
    def test_grid_size(self):
        grid = generate_grid()
        assert len(grid) == 1760

    def test_grid_gas_override(self):
        grid = generate_grid(gas_cost_usd=0.20)
        assert all(p.gas_cost_usd == 0.20 for p in grid)


# ─── Metrics ─────────────────────────────────────────────────────────────

class TestMetrics:
    def test_sortino_all_positive(self):
        rets = [0.01] * 100
        s = metrics.sortino_ratio(rets)
        assert s == float("inf")

    def test_sortino_mixed(self):
        rets = [0.01, -0.02, 0.015, -0.005, 0.008]
        s = metrics.sortino_ratio(rets)
        assert isinstance(s, float) and s != 0

    def test_max_drawdown_no_loss(self):
        cum = [1.0, 1.1, 1.2, 1.3]
        assert metrics.max_drawdown(cum) == 0.0

    def test_max_drawdown_known(self):
        cum = [1.0, 0.9, 0.8, 1.0]
        assert metrics.max_drawdown(cum) == pytest.approx(0.2)

    def test_calmar_positive(self):
        rets = [0.01, 0.02, -0.005, 0.015]
        c = metrics.calmar_ratio(rets)
        assert c > 0

    def test_omega_all_positive(self):
        rets = [0.01, 0.02, 0.03]
        assert metrics.omega_ratio(rets) == float("inf")

    def test_win_rate(self):
        assert metrics.win_rate([0.01, -0.01, 0.02, -0.005]) == 0.5

    def test_profit_factor(self):
        rets = [0.1, -0.05]
        assert metrics.profit_factor(rets) == pytest.approx(2.0)

    def test_cvar_simple(self):
        rets = [-0.1, -0.05, 0.01, 0.02, 0.03]
        c = metrics.cvar(rets, alpha=0.4)
        assert c > 0

    def test_return_skew_symmetric(self):
        rets = [-1.0, 0.0, 1.0]
        assert abs(metrics.return_skew(rets)) < 0.01

    def test_composite_objective(self):
        val = metrics.composite_objective(2.0, 0.1, 0.8)
        assert val == pytest.approx(2.0 * 0.9 * 0.8)
