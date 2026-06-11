"""Unit tests for backtester modules."""

import math
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from backtester.data import BurnEvent, MintEvent, SwapEvent, V4Event, load_events, load_v4_events
from backtester.clmm_math import cngn_price_from_sqrt_price_x96, tick_to_sqrt_price_x96
from backtester.run import (
    WindowSpec,
    WindowResult,
    aggregate_window_results,
    evaluate_rolling_windows,
    generate_swap_count_windows,
    generate_windows,
)
from engine.math.v3 import (
    tick_to_price,
    price_to_tick,
    tick_to_sqrt_price,
    sqrt_price_x96_to_decimal,
    align_tick,
    constrain_tick_width,
    compute_swap_step,
)
from backtester.strategy import EWMACalculator, calculate_fixed_pct_tick_range, calculate_tick_range
from backtester.pool_state import PoolState
from backtester.params import BacktestParams, TransactionCostModel, generate_grid
from backtester.simulator import (
    PANCAKESWAP_POOL,
    UNISWAP_BASE_POOL,
    UNISWAP_BSC_POOL,
    VirtualPosition,
    _exact_clmm_output_costs,
    _event_fee_wallet,
    _range_traversal_fraction,
    simulate_pool,
)
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
        assert align_tick(100, 10, "up") == 100
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

    def test_exact_output_price_impact_uses_same_range_clmm_math(self):
        fee_usd, impact_usd = _exact_clmm_output_costs(
            output_notional_usd=10.0,
            active_liquidity=1_000_000_000_000,
            fee_rate=0.0,
            current_tick=0,
            current_sqrt_price_x96=2**96,
            current_price=1.0,
            direction="stable_to_cngn",
            pool_config=UNISWAP_BASE_POOL,
        )
        output_raw = 10 * 10**6
        sqrt_next = 1 / (1 - output_raw / 1_000_000_000_000)
        expected_effective_input_usd = (1_000_000_000_000 * (sqrt_next - 1)) / 10**6

        assert fee_usd == pytest.approx(0.0)
        assert impact_usd == pytest.approx(expected_effective_input_usd - 10.0)

    def test_exact_output_returns_none_when_swap_crosses_spacing_boundary(self):
        costs = _exact_clmm_output_costs(
            output_notional_usd=10_000.0,
            active_liquidity=1_000_000_000,
            fee_rate=0.0,
            current_tick=0,
            current_sqrt_price_x96=2**96,
            current_price=1.0,
            direction="stable_to_cngn",
            pool_config=UNISWAP_BASE_POOL,
        )

        assert costs is None


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

    def test_flat_series_returns_std_floor(self):
        """Flat price series → log-return variance = 0 → std floored at 3 bps."""
        e = EWMACalculator(0.99)
        for _ in range(50):
            e.update(0.000606)
        assert e.std == pytest.approx(3e-4, rel=1e-9)

    def test_volatile_series_exceeds_floor(self):
        """Series with real moves → std > 3 bps floor."""
        e = EWMACalculator(0.99)
        import random
        random.seed(42)
        price = 0.000606
        for _ in range(50):
            price *= 1 + random.gauss(0, 0.02)  # 2% per-step moves
            e.update(price)
        assert e.std > 3e-4

    def test_std_scale_invariant(self):
        """1% moves at price 1.0 and at price 0.001 should give the same log-return std."""
        moves = [1.01, 0.99, 1.02, 0.98, 1.015, 0.985]

        e_high = EWMACalculator(0.99)
        price = 1.0
        for m in moves:
            price *= m
            e_high.update(price)

        e_low = EWMACalculator(0.99)
        price = 0.001
        for m in moves:
            price *= m
            e_low.update(price)

        assert abs(e_high.std - e_low.std) / e_high.std < 0.001

    def test_calculate_tick_range_center_price_override(self):
        """center_price parameter overrides ewma.mean as range center."""
        e = EWMACalculator(0.99)
        for _ in range(20):
            e.update(0.000606)

        # Range with default center (ewma.mean ≈ 0.000606)
        t_lo_default, t_hi_default = calculate_tick_range(
            e, sd_multiplier=2.0, downside_skew=0.5,
            token0_decimals=6, token1_decimals=6,
            tick_spacing=10, min_tick_width=100, max_tick_width=10000,
        )

        # Range with override center at a 10% higher price
        override = e.mean * 1.10
        t_lo_override, t_hi_override = calculate_tick_range(
            e, sd_multiplier=2.0, downside_skew=0.5,
            token0_decimals=6, token1_decimals=6,
            tick_spacing=10, min_tick_width=100, max_tick_width=10000,
            center_price=override,
        )

        # Override center → both ticks shift up
        assert t_lo_override > t_lo_default
        assert t_hi_override > t_hi_default

    def test_fixed_percent_width_to_tick_range(self):
        tick_lower, tick_upper = calculate_fixed_pct_tick_range(
            center_price=1.0,
            width_pct=0.01,
            token0_decimals=6,
            token1_decimals=6,
            tick_spacing=10,
            min_tick_width=50,
            max_tick_width=1000,
        )

        assert tick_lower < 0 < tick_upper
        assert 90 <= tick_upper - tick_lower <= 120
        assert tick_lower % 10 == 0
        assert tick_upper % 10 == 0

    def test_lp_holdings_match_uniswap_v3_liquidity_math(self):
        liquidity = 12_345.0
        lower = -100
        upper = 100
        spa = tick_to_sqrt_price(lower)
        spb = tick_to_sqrt_price(upper)

        below = VirtualPosition(lower, upper, liquidity, 1.0, 0.0, datetime.now(), lower - 10, 0, 0.0)
        amount0, amount1 = below.amounts_at_tick(lower - 10)
        assert amount0 == pytest.approx(liquidity * (spb - spa) / (spa * spb))
        assert amount1 == pytest.approx(0.0)

        above = VirtualPosition(lower, upper, liquidity, 1.0, 0.0, datetime.now(), upper + 10, 0, 0.0)
        amount0, amount1 = above.amounts_at_tick(upper + 10)
        assert amount0 == pytest.approx(0.0)
        assert amount1 == pytest.approx(liquidity * (spb - spa))

        current = 0
        sp = tick_to_sqrt_price(current)
        in_range = VirtualPosition(lower, upper, liquidity, 1.0, 0.0, datetime.now(), current, 0, 0.0)
        amount0, amount1 = in_range.amounts_at_tick(current)
        assert amount0 == pytest.approx(liquidity * (spb - sp) / (sp * spb))
        assert amount1 == pytest.approx(liquidity * (sp - spa))


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
        assert len(grid) == 2640

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
        # net_return=0.05 (5% gain), max_dd=0.1, no fees/cost → 0.05 - 0.1 + 0 = -0.05
        val = metrics.composite_objective(0.05, 0.1)
        assert val == pytest.approx(0.05 - 0.1)

    def test_composite_negative_return(self):
        # net_return=-0.2 (20% loss), max_dd=0.3, zero-activity → -0.2 - 0.3 + 0 = -0.5
        val = metrics.composite_objective(-0.2, 0.3)
        assert val == pytest.approx(-0.5)
        # Same loss with less drawdown scores better (less negative)
        val2 = metrics.composite_objective(-0.2, 0.1)
        assert val2 > val

    def test_composite_fee_cost_bonus(self):
        # fees > tx_cost → positive log_ratio bonus on top of ROI−DD term.
        baseline = metrics.composite_objective(0.0, 0.0)
        bonus = metrics.composite_objective(0.0, 0.0, fees=2.0, tx_cost=1.0)
        assert bonus > baseline
        # alpha=0.001 default × log(2) ≈ 6.9e-4 increment
        assert bonus == pytest.approx(0.001 * math.log(2.0), abs=1e-9)

    def test_composite_fee_cost_penalty(self):
        # fees < tx_cost → negative log_ratio penalty
        baseline = metrics.composite_objective(0.0, 0.0)
        underwater = metrics.composite_objective(0.0, 0.0, fees=0.5, tx_cost=2.0)
        assert underwater < baseline
        assert underwater == pytest.approx(0.001 * math.log(0.5 / 2.0), abs=1e-9)

    def test_composite_fee_cost_zero_activity_neutral(self):
        # No activity ⇒ ratio term is exactly 0, matching prior behaviour.
        with_args = metrics.composite_objective(0.01, 0.02, fees=0.0, tx_cost=0.0)
        without_args = metrics.composite_objective(0.01, 0.02)
        assert with_args == pytest.approx(without_args)

    def test_composite_fee_cost_zero_fees_clamped(self):
        # fees ≈ 0 with non-trivial tx_cost would be ln(0/c) = −∞ unclamped.
        # Clamp floor of −3 prevents domination of the ranking.
        capped = metrics.composite_objective(0.0, 0.0, fees=0.0, tx_cost=1.0)
        assert capped == pytest.approx(0.001 * -3.0, abs=1e-9)

    def test_composite_fee_cost_huge_ratio_clamped(self):
        # Fees ≫ tx_cost gets a bonus capped at log(2) ≈ +2 ceil.
        capped = metrics.composite_objective(0.0, 0.0, fees=1e6, tx_cost=1.0)
        assert capped == pytest.approx(0.001 * 2.0, abs=1e-9)

    def test_fee_cost_log_ratio_signs(self):
        assert metrics.fee_cost_log_ratio(2.0, 1.0) > 0  # break-even surplus
        assert metrics.fee_cost_log_ratio(0.5, 1.0) < 0  # underwater
        assert metrics.fee_cost_log_ratio(0.0, 0.0) == 0.0  # neutral


class TestV4Loader:
    def test_v4_loader_sorts_and_preserves_fields(self, tmp_path):
        csv_path = tmp_path / "v4.csv"
        csv_path.write_text(
            "\n".join(
                [
                    "block_time,chain,pool_id,event_type,tx_hash,log_index,block_number,sqrt_price_x96,tick,active_liquidity,fee_rate,amount0,amount1,amount_usd,cngn_usd_price,token0_symbol,token1_symbol",
                    f"2026-01-01T00:00:01+00:00,base,pool,swap,0x2,4,11,{2**96},20,2000,0.0015,1,2,3,0.0007,cNGN,USDC",
                    f"2026-01-01T00:00:01+00:00,base,pool,swap,0x1,1,10,{2**96},10,1000,0.0015,1,2,3,0.0006,cNGN,USDC",
                ]
            )
        )
        events = load_v4_events(str(csv_path), pool_id="pool")
        assert [event.tx_hash for event in events] == ["0x1", "0x2"]
        assert events[0].active_liquidity == 1000
        assert events[1].fee_rate == pytest.approx(0.0015)
        assert events[0].cngn_usd_price == pytest.approx(1.0)

    def test_legacy_loader_infers_cngn_price(self, tmp_path):
        csv_path = tmp_path / "legacy.csv"
        csv_path.write_text(
            "\n".join(
                [
                    "block_time,blockchain,pool_address,event_type,amount_usd,token_bought_symbol,token_bought_amount,token_sold_symbol,token_sold_amount,tick_lower,tick_upper,liquidity_delta,mint_burn_amount0,mint_burn_amount1",
                    "2026-01-01T00:00:00+00:00,base,pool,swap,10,USDC,1,cNGN,1500,,,,,",
                ]
            )
        )
        events = load_events(str(csv_path), pool_address="pool")
        assert len(events) == 1
        assert events[0].cngn_usd_price == pytest.approx(1 / 1500)


class TestRollingWindows:
    def _make_v4_event(self, day: int, minute: int, event_type: str = "swap", price: float = 0.0007, tick: int = 0):
        timestamp = datetime(2026, 1, 1, tzinfo=None).astimezone()
        block_time = timestamp + timedelta(days=day, minutes=minute)
        return V4Event(
            block_time=block_time,
            chain="base",
            pool_id="pool",
            event_type=event_type,
            tx_hash=f"0x{day:02d}{minute:02d}{event_type}",
            log_index=minute,
            block_number=day * 100 + minute,
            sqrt_price_x96=tick_to_sqrt_price_x96(0),
            tick=tick,
            active_liquidity=1_000_000,
            fee_rate=0.0015,
            amount0=1.0,
            amount1=1.0,
            amount_usd=100.0,
            cngn_usd_price=price,
            token0_symbol="cNGN",
            token1_symbol="USDC",
        )

    def test_window_generation_30_7_7(self):
        base = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        events = [
            V4Event(base + timedelta(days=i), "base", "pool", "swap", f"0x{i}", 0, i, 1, 0, 1, 0.0015, 1, 1, 1, 0.0007, "cNGN", "USDC")
            for i in range(60)
        ]
        windows = generate_windows(events, WindowSpec())
        assert windows[0].train_start == base
        assert windows[0].val_start == base + timedelta(days=30)
        assert windows[1].train_start == base + timedelta(days=7)

    def test_swap_count_window_generation(self):
        base = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        events = [
            V4Event(base + timedelta(minutes=i), "base", "pool", "swap", f"0x{i}", 0, i, 1, 0, 1, 0.0015, 1, 1, 1, 0.0007, "cNGN", "USDC")
            for i in range(12)
        ]
        spec = WindowSpec(mode="swap_count", train_swaps=5, val_swaps=3, stride_swaps=2, min_train_swaps=5, min_train_liquidity_events=0, min_val_swaps=3)
        windows = generate_swap_count_windows(events, spec)

        assert len(windows) == 3
        assert windows[0].train_start_index == 0
        assert windows[0].train_end_index == 5
        assert windows[0].val_start_index == 5
        assert windows[0].val_end_index == 8
        assert windows[1].train_start_index == 2

    def test_skip_behavior_when_thresholds_fail(self):
        base = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        events = [
            V4Event(base + timedelta(days=i), "base", "pool", "swap", f"0x{i}", 0, i, 1, 0, 1, 0.0015, 1, 1, 1, 0.0007, "cNGN", "USDC")
            for i in range(40)
        ]
        results = evaluate_rolling_windows(
            events,
            UNISWAP_BASE_POOL,
            [BacktestParams()],
            WindowSpec(min_train_swaps=500, min_train_liquidity_events=1, min_val_swaps=100),
            top_n=1,
            max_windows=1,
        )
        assert len(results) == 1
        assert results[0].skipped_reason == "train_swaps_below_min"

    def test_aggregate_selection_prefers_better_score_then_tiebreakers(self):
        p1 = BacktestParams(sd_multiplier=1.0)
        p2 = BacktestParams(sd_multiplier=2.0)
        rows = [
            WindowResult(0, datetime.now(), datetime.now(), 10, 10, 5, 5, 5, None, p1, {"composite": 1.0}, {"composite": 0.6, "net_return": 0.1, "max_drawdown": 0.1, "divergent_loss": -0.01, "time_in_range": 0.5, "rebalance_count": 1}, 1, 1),
            WindowResult(1, datetime.now(), datetime.now(), 10, 10, 5, 5, 5, None, p1, {"composite": 1.0}, {"composite": 0.7, "net_return": 0.11, "max_drawdown": 0.12, "divergent_loss": -0.02, "time_in_range": 0.5, "rebalance_count": 1}, 1, 2),
            WindowResult(0, datetime.now(), datetime.now(), 10, 10, 5, 5, 5, None, p2, {"composite": 0.9}, {"composite": 0.5, "net_return": 0.09, "max_drawdown": 0.1, "divergent_loss": -0.03, "time_in_range": 0.4, "rebalance_count": 2}, 2, 2),
            WindowResult(1, datetime.now(), datetime.now(), 10, 10, 5, 5, 5, None, p2, {"composite": 0.9}, {"composite": 0.4, "net_return": 0.08, "max_drawdown": 0.09, "divergent_loss": -0.02, "time_in_range": 0.4, "rebalance_count": 2}, 2, 1),
        ]
        aggregates = aggregate_window_results(rows)
        assert aggregates[0]["sd_multiplier"] == 1.0

    def test_aggregate_eligibility_requires_fee_cost_break_even(self):
        viable = BacktestParams(sd_multiplier=1.0)
        underwater = BacktestParams(sd_multiplier=2.0)
        # Both configs are net-return positive in every window, drawdown OK.
        # `viable` earns more in fees than it pays in tx_cost (ratio 2.0).
        # `underwater` pays more in tx_cost than it earns in fees (ratio 0.5).
        viable_metric = {"composite": 0.5, "net_return": 0.01, "max_drawdown": 0.005, "divergent_loss": -0.001, "time_in_range": 0.5, "rebalance_count": 1, "total_fees": 2.0, "total_transaction_cost": 1.0}
        underwater_metric = {"composite": 0.5, "net_return": 0.01, "max_drawdown": 0.005, "divergent_loss": -0.001, "time_in_range": 0.5, "rebalance_count": 5, "total_fees": 0.5, "total_transaction_cost": 1.0}
        rows = [
            WindowResult(0, datetime.now(), datetime.now(), 10, 10, 5, 5, 5, None, viable, {"composite": 0.5}, viable_metric, 1, 1),
            WindowResult(1, datetime.now(), datetime.now(), 10, 10, 5, 5, 5, None, viable, {"composite": 0.5}, viable_metric, 1, 1),
            WindowResult(0, datetime.now(), datetime.now(), 10, 10, 5, 5, 5, None, underwater, {"composite": 0.5}, underwater_metric, 1, 1),
            WindowResult(1, datetime.now(), datetime.now(), 10, 10, 5, 5, 5, None, underwater, {"composite": 0.5}, underwater_metric, 1, 1),
        ]
        aggregates = aggregate_window_results(rows)
        by_sd = {agg["sd_multiplier"]: agg for agg in aggregates}
        assert by_sd[1.0]["eligible"] is True
        assert by_sd[1.0]["mean_validation_fee_to_tx_cost_ratio"] == pytest.approx(2.0)
        assert by_sd[2.0]["eligible"] is False  # ratio 0.5 < 1.0 gate
        assert by_sd[2.0]["mean_validation_fee_to_tx_cost_ratio"] == pytest.approx(0.5)

    def test_aggregate_eligibility_fee_cost_gate_tunable(self):
        params = BacktestParams(sd_multiplier=1.0)
        # Ratio 1.5 — should be ineligible at min_fee_cost_ratio=2.0 but eligible at 1.0.
        metric = {"composite": 0.5, "net_return": 0.01, "max_drawdown": 0.005, "divergent_loss": -0.001, "time_in_range": 0.5, "rebalance_count": 1, "total_fees": 1.5, "total_transaction_cost": 1.0}
        rows = [WindowResult(0, datetime.now(), datetime.now(), 10, 10, 5, 5, 5, None, params, {"composite": 0.5}, metric, 1, 1)]
        assert aggregate_window_results(rows, min_fee_cost_ratio=1.0)[0]["eligible"] is True
        assert aggregate_window_results(rows, min_fee_cost_ratio=2.0)[0]["eligible"] is False


class TestSimulationCompatibility:
    def test_v4_pool_fee_assumptions(self):
        assert UNISWAP_BASE_POOL.fee_rate == pytest.approx(0.0015)
        assert PANCAKESWAP_POOL.fee_rate == pytest.approx(0.0001)

    def test_divergent_loss_zero_for_cash_only(self):
        event = V4Event(
            block_time=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
            chain="base",
            pool_id="pool",
            event_type="swap",
            tx_hash="0x1",
            log_index=0,
            block_number=1,
            sqrt_price_x96=tick_to_sqrt_price_x96(0),
            tick=0,
            active_liquidity=1_000_000,
            fee_rate=0.0015,
            amount0=1.0,
            amount1=1.0,
            amount_usd=100.0,
            cngn_usd_price=0.0007,
            token0_symbol="cNGN",
            token1_symbol="USDC",
        )
        sim = simulate_pool([event], BacktestParams(gas_cost_usd=1000.0), UNISWAP_BASE_POOL, initial_capital_usd=500.0)
        assert sim.final_value == pytest.approx(500.0)
        assert sim.divergent_loss == pytest.approx(0.0)


class TestPaperStyleSimulation:
    def _event(self, minute: int, tick: int, amount_usd: float = 10_000.0) -> V4Event:
        price = float(tick_to_price(tick, 6, 6))
        return V4Event(
            block_time=datetime.fromisoformat("2026-01-01T00:00:00+00:00") + timedelta(minutes=minute),
            chain="base",
            pool_id="pool",
            event_type="swap",
            tx_hash=f"0x{minute}",
            log_index=minute,
            block_number=minute,
            sqrt_price_x96=tick_to_sqrt_price_x96(tick),
            tick=tick,
            active_liquidity=1_000_000_000,
            fee_rate=0.0015,
            amount0=1.0,
            amount1=1.0,
            amount_usd=amount_usd,
            cngn_usd_price=price,
            token0_symbol="cNGN",
            token1_symbol="USDC",
        )

    def _params(self, **overrides) -> BacktestParams:
        values = {
            "strategy_mode": "paper",
            "range_mode": "fixed_tick_width",
            "center_mode": "spot",
            "fixed_tick_width": 1000,
            "harvest_upward_range_fraction": 0.10,
            "profit_take_return": 0.0,
            "require_profit_after_cost": False,
            "out_of_range_overshoot_fraction": 0.0,
            "min_tick_width": 100,
            "max_tick_width": 1000,
            "initial_capital_usd": 500.0,
            "gas_cost_usd": 0.0,
        }
        values.update(overrides)
        return BacktestParams(**values)

    def test_bsc_token1_cngn_flips_upward_tick_traversal(self):
        entry_tick = -204_000
        lower_tick = entry_tick - 200
        upper_tick = entry_tick + 200
        entry_price = cngn_price_from_sqrt_price_x96(
            tick_to_sqrt_price_x96(entry_tick),
            UNISWAP_BSC_POOL.token0_decimals,
            UNISWAP_BSC_POOL.token1_decimals,
            UNISWAP_BSC_POOL.invert_price,
        )
        lower_tick_price = cngn_price_from_sqrt_price_x96(
            tick_to_sqrt_price_x96(lower_tick),
            UNISWAP_BSC_POOL.token0_decimals,
            UNISWAP_BSC_POOL.token1_decimals,
            UNISWAP_BSC_POOL.invert_price,
        )
        upper_tick_price = cngn_price_from_sqrt_price_x96(
            tick_to_sqrt_price_x96(upper_tick),
            UNISWAP_BSC_POOL.token0_decimals,
            UNISWAP_BSC_POOL.token1_decimals,
            UNISWAP_BSC_POOL.invert_price,
        )
        position = VirtualPosition(
            tick_lower=entry_tick - 500,
            tick_upper=entry_tick + 500,
            liquidity_L=1.0,
            entry_price=entry_price,
            entry_value=1.0,
            entry_time=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
            entry_tick=entry_tick,
            entry_active_liquidity=1_000_000,
            deployed_capital=1.0,
        )

        assert lower_tick_price > entry_price
        assert upper_tick_price < entry_price
        assert _range_traversal_fraction(position, lower_tick, UNISWAP_BSC_POOL) == pytest.approx(0.2)
        assert _range_traversal_fraction(position, upper_tick, UNISWAP_BSC_POOL) == pytest.approx(-0.2)

    def test_upward_in_range_harvest_records_episode_accounting(self):
        events = [self._event(0, 0), self._event(1, 0), self._event(2, 120)]
        sim = simulate_pool(events, self._params(), UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 1
        first = sim.episodes[0]
        assert first.exit_reason == "harvest_upward"
        assert first.range_traversal_fraction == pytest.approx(120 / 1020)
        assert first.fees_earned > 0
        assert first.net_pnl == pytest.approx(
            first.exit_value - first.exit_transaction_cost - first.entry_value - first.entry_transaction_cost
        )
        assert first.total_transaction_cost == pytest.approx(first.entry_transaction_cost + first.exit_transaction_cost)
        assert sim.total_transaction_cost == pytest.approx(sum(episode.total_transaction_cost for episode in sim.episodes))
        assert first.inventory_pnl == pytest.approx(first.exit_value - first.fees_earned - first.entry_value)

    def test_transaction_cost_price_impact_uses_active_liquidity(self):
        events = [self._event(0, 0), self._event(1, 0), self._event(2, 120)]
        base_params = {
            "transaction_costs": TransactionCostModel(mint_gas_usd=0.0, remove_gas_usd=0.0),
            "require_profit_after_cost": False,
        }
        deep_liquidity = simulate_pool(
            events,
            self._params(**base_params),
            UNISWAP_BASE_POOL,
            initial_capital_usd=500.0,
        )
        shallow_events = [
            replace(event, active_liquidity=1_000_000)
            for event in [self._event(0, 0), self._event(1, 0), self._event(2, 120)]
        ]
        shallow_liquidity = simulate_pool(
            shallow_events,
            self._params(**base_params),
            UNISWAP_BASE_POOL,
            initial_capital_usd=500.0,
        )

        assert shallow_liquidity.total_price_impact_cost > deep_liquidity.total_price_impact_cost

    def test_recenter_entry_routes_only_inventory_delta_by_default(self):
        events = [self._event(0, 0), self._event(1, 0), self._event(2, 120), self._event(3, 120)]
        true_route = simulate_pool(
            events,
            self._params(
                transaction_costs=TransactionCostModel(mint_gas_usd=0.0, remove_gas_usd=0.0),
                require_profit_after_cost=False,
            ),
            UNISWAP_BASE_POOL,
            initial_capital_usd=500.0,
        )
        cash_unwind = simulate_pool(
            events,
            self._params(
                transaction_costs=TransactionCostModel(
                    mint_gas_usd=0.0,
                    remove_gas_usd=0.0,
                    unwind_to_cash_on_exit=True,
                ),
                require_profit_after_cost=False,
            ),
            UNISWAP_BASE_POOL,
            initial_capital_usd=500.0,
        )

        assert len(true_route.episodes) == 2
        assert len(cash_unwind.episodes) == 2
        assert true_route.episodes[1].swap_notional_usd < cash_unwind.episodes[1].swap_notional_usd
        assert true_route.total_transaction_cost < cash_unwind.total_transaction_cost

    def test_v4_swap_fees_accrue_in_input_token(self):
        stable_input = replace(
            self._event(0, 0),
            amount0=-1000.0,
            amount1=1.0,
            amount_usd=1.0,
            cngn_usd_price=0.001,
        )
        stable_fee = _event_fee_wallet(
            stable_input,
            stable_input.fee_rate,
            liquidity_share=0.10,
            current_price=0.001,
            pool_config=UNISWAP_BASE_POOL,
        )
        assert stable_fee.stable_usd == pytest.approx(0.00015)
        assert stable_fee.cngn_amount == pytest.approx(0.0)

        cngn_input = replace(
            self._event(1, 0),
            amount0=1000.0,
            amount1=-1.0,
            amount_usd=1.0,
            cngn_usd_price=0.001,
        )
        cngn_fee = _event_fee_wallet(
            cngn_input,
            cngn_input.fee_rate,
            liquidity_share=0.10,
            current_price=0.001,
            pool_config=UNISWAP_BASE_POOL,
        )
        assert cngn_fee.stable_usd == pytest.approx(0.0)
        assert cngn_fee.cngn_amount == pytest.approx(0.15)

    def test_profit_after_cost_gates_harvest(self):
        events = [self._event(0, 0), self._event(1, 0), self._event(2, 120)]
        params = self._params(require_profit_after_cost=True, gas_cost_usd=300.0)
        sim = simulate_pool(events, params, UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 0
        assert all(episode.exit_reason != "harvest_upward" for episode in sim.episodes)

    def test_favorable_out_of_range_exit_is_profit_harvest(self):
        events = [self._event(0, 0), self._event(1, 0), self._event(2, 600)]
        params = self._params(profit_take_return=-1.0, require_profit_after_cost=False)
        sim = simulate_pool(events, params, UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 1
        assert sim.episodes[0].exit_reason == "harvest_upward"
        assert sim.episodes[0].range_traversal_fraction > 0

    def test_adverse_out_of_range_exit_is_defensive(self):
        events = [self._event(0, 0), self._event(1, 0), self._event(2, -600)]
        params = self._params(stop_loss_return=None, require_profit_after_cost=False)
        sim = simulate_pool(events, params, UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 1
        assert sim.episodes[0].exit_reason == "adverse_out_of_range"
        assert sim.episodes[0].range_traversal_fraction < 0

    def test_stop_loss_exits_in_range_position(self):
        events = [self._event(0, 0, amount_usd=0), self._event(1, 0, amount_usd=0), self._event(2, -100, amount_usd=0)]
        params = self._params(harvest_upward_range_fraction=None, stop_loss_return=0.0)
        sim = simulate_pool(events, params, UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 1
        assert sim.episodes[0].exit_reason == "stop_loss"

    def test_min_exit_swap_volume_filters_dust_stop_loss(self):
        events = [
            self._event(0, 0, amount_usd=0),
            self._event(1, 0, amount_usd=0),
            self._event(2, -100, amount_usd=0.01),
            self._event(3, -100, amount_usd=0.01),
        ]
        params = self._params(
            harvest_upward_range_fraction=None,
            stop_loss_return=0.0,
            min_exit_swap_volume_usd=1.0,
        )
        sim = simulate_pool(events, params, UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 0
        assert sim.episodes[0].exit_reason == "end_of_data"

    def test_qualified_pool_twap_exit_price_ignores_dust_stop_loss_mark(self):
        events = [
            self._event(0, 0, amount_usd=100.0),
            self._event(1, 0, amount_usd=100.0),
            self._event(2, -200, amount_usd=0.01),
        ]
        params = self._params(
            harvest_upward_range_fraction=None,
            stop_loss_return=-0.005,
            exit_price_mode="qualified_pool_twap",
            exit_price_min_swap_volume_usd=1.0,
            exit_price_twap_lookback_minutes=60.0,
        )
        sim = simulate_pool(events, params, UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 0
        assert sim.episodes[0].exit_reason == "end_of_data"

    def test_fair_price_exit_mark_can_override_dust_pool_price(self):
        fair_marked = replace(self._event(2, -200, amount_usd=0.01), fair_price_usd=float(tick_to_price(0, 6, 6)))
        events = [
            self._event(0, 0, amount_usd=100.0),
            self._event(1, 0, amount_usd=100.0),
            fair_marked,
        ]
        params = self._params(
            harvest_upward_range_fraction=None,
            stop_loss_return=-0.005,
            exit_price_mode="fair_price",
        )
        sim = simulate_pool(events, params, UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 0
        assert sim.episodes[0].exit_reason == "end_of_data"

    def test_exit_confirmation_requires_multiple_defensive_signals(self):
        events = [
            self._event(0, 0),
            self._event(1, 0),
            self._event(2, -100),
            self._event(3, -100),
        ]
        params = self._params(
            harvest_upward_range_fraction=None,
            stop_loss_return=0.0,
            exit_confirmation_swaps=2,
        )
        sim = simulate_pool(events, params, UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 1
        assert sim.episodes[0].exit_reason == "stop_loss"
        assert sim.episodes[0].exit_time == events[3].block_time

    def test_out_of_range_exit_respects_cooldown(self):
        events = [
            self._event(0, 0),
            self._event(1, 0),
            self._event(2, 600),
            self._event(3, 0),
        ]
        params = self._params(harvest_upward_range_fraction=None, cooldown_minutes=10.0)
        sim = simulate_pool(events, params, UNISWAP_BASE_POOL, initial_capital_usd=500.0)

        assert sim.rebalance_count == 1
        assert len(sim.episodes) == 1
        assert sim.episodes[0].exit_reason == "out_of_range"
