"""Pure + seeded-cache tests for pool_state.py."""

import time
import pytest
from decimal import Decimal

from engine.core.arbitrage.pool_state import (
    get_cached_pool_state,
    update_pool_state_from_event,
    swap_token0_for_token1,
    swap_token1_for_token0,
    Q96,
)


# =============================================================================
# Swap math (pure — no cache dependency)
# =============================================================================

# Use realistic Base pool values (cNGN/USDC, 6/6 dec, price ≈ 0.000606)
import math as _math

_BASE_SQRT_X96 = Decimal(int(_math.sqrt(0.000606) * (2 ** 96)))
_LIQ = Decimal(10 ** 18)
_FEE = Decimal("0.0005")


class TestSwapToken0ForToken1:
    """Swap cNGN (t0) → USDC (t1) on Base pool."""

    def test_zero_amount_returns_zero(self):
        out = swap_token0_for_token1(Decimal("0"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out < Decimal("1e-10")  # numerically zero (Decimal precision artifact)

    def test_zero_liquidity_returns_zero(self):
        out = swap_token0_for_token1(Decimal("1000"), _BASE_SQRT_X96, Decimal("0"), _FEE, 6, 6)
        assert out == Decimal("0")

    def test_nonzero_amount_gives_reasonable_output(self):
        # Swap 1,000,000 cNGN → USDC. At price 0.000606 we expect ≈ 606 USDC before slippage
        out = swap_token0_for_token1(Decimal("1000000"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out > Decimal("0")
        # Output should be in the right ballpark (within 50% of naive expectation)
        assert Decimal("300") < out < Decimal("900")

    def test_larger_amount_gets_less_per_unit(self):
        """Slippage: larger swap gets fewer tokens per unit due to price impact."""
        out_small = swap_token0_for_token1(Decimal("100"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        out_large = swap_token0_for_token1(Decimal("10000"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        rate_small = out_small / Decimal("100")
        rate_large = out_large / Decimal("10000")
        assert rate_large < rate_small  # price impact

    def test_fee_reduces_output(self):
        out_no_fee = swap_token0_for_token1(Decimal("1000"), _BASE_SQRT_X96, _LIQ, Decimal("0"), 6, 6)
        out_with_fee = swap_token0_for_token1(Decimal("1000"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out_with_fee < out_no_fee


class TestSwapToken1ForToken0:
    """Swap USDC (t1) → cNGN (t0) on Base pool."""

    def test_zero_amount_returns_zero(self):
        out = swap_token1_for_token0(Decimal("0"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out < Decimal("1e-10")  # numerically zero

    def test_nonzero_amount_gives_reasonable_output(self):
        # Swap 100 USDC → cNGN. At price 0.000606, expect ≈ 165,000 cNGN before slippage
        out = swap_token1_for_token0(Decimal("100"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out > Decimal("0")
        assert Decimal("50000") < out < Decimal("300000")

    def test_fee_reduces_output(self):
        out_no_fee = swap_token1_for_token0(Decimal("100"), _BASE_SQRT_X96, _LIQ, Decimal("0"), 6, 6)
        out_with_fee = swap_token1_for_token0(Decimal("100"), _BASE_SQRT_X96, _LIQ, _FEE, 6, 6)
        assert out_with_fee < out_no_fee


# =============================================================================
# update_pool_state_from_event
# =============================================================================


class TestUpdatePoolStateFromEvent:
    """update_pool_state_from_event mutates _POOL_CACHE correctly."""

    def test_creates_new_entry(self, monkeypatch):
        from engine.core.arbitrage import pool_state as _ps
        fake_cache: dict = {}
        monkeypatch.setattr(_ps, "_POOL_CACHE", fake_cache)

        update_pool_state_from_event(
            pool_id="0xdeadbeef",
            sqrt_p=int(_BASE_SQRT_X96),
            liquidity=int(_LIQ),
            tick=-276324,
            fee=500,  # 0.05% in 1e6 units
        )

        assert "0xdeadbeef" in fake_cache
        entry = fake_cache["0xdeadbeef"]
        assert entry["tick"] == -276324
        assert entry["liquidity"] == _LIQ
        assert entry["fee"] == Decimal("500") / Decimal(1000000)
        assert entry["sqrt_p"] == Decimal(int(_BASE_SQRT_X96))

    def test_overwrites_existing_entry(self, monkeypatch):
        from engine.core.arbitrage import pool_state as _ps
        fake_cache = {
            "0xpool": {
                "tick": 0, "liquidity": Decimal(1), "fee": Decimal("0"),
                "sqrt_p": Decimal(1), "timestamp": 0,
            }
        }
        monkeypatch.setattr(_ps, "_POOL_CACHE", fake_cache)

        update_pool_state_from_event("0xpool", int(_BASE_SQRT_X96), int(_LIQ), -100, 500)

        entry = fake_cache["0xpool"]
        assert entry["tick"] == -100

    def test_timestamp_set(self, monkeypatch):
        from engine.core.arbitrage import pool_state as _ps
        fake_cache: dict = {}
        monkeypatch.setattr(_ps, "_POOL_CACHE", fake_cache)

        before = time.time()
        update_pool_state_from_event("0xpool2", 1, 1, 0, 500)
        after = time.time()

        assert before <= fake_cache["0xpool2"]["timestamp"] <= after


# =============================================================================
# get_cached_pool_state
# =============================================================================


class TestGetCachedPoolState:
    """get_cached_pool_state reads from _POOL_CACHE without RPC calls."""

    def test_cache_hit_returns_state(self, seeded_pool_cache):
        base_key = seeded_pool_cache["uni-base"]
        sqrt_p, liq, ts, fee = get_cached_pool_state(base_key)
        assert sqrt_p is not None
        assert liq == Decimal(10 ** 18)
        assert fee == Decimal("0.0005")

    def test_cold_cache_returns_nones(self, monkeypatch):
        from engine.core.arbitrage import pool_state as _ps
        monkeypatch.setattr(_ps, "_POOL_CACHE", {})
        result = get_cached_pool_state("0xnonexistent")
        assert all(v is None for v in result)

    def test_bsc_pool_state_readable(self, seeded_pool_cache):
        bsc_key = seeded_pool_cache["uni-bsc"]
        sqrt_p, liq, ts, fee = get_cached_pool_state(bsc_key)
        assert sqrt_p is not None and sqrt_p > 0
        assert fee == Decimal("0.0005")

    def test_price_in_expected_range(self, seeded_pool_cache):
        """Seeded sqrtPriceX96 should decode to price ≈ 0.000606 for Base pool."""
        base_key = seeded_pool_cache["uni-base"]
        sqrt_p, *_ = get_cached_pool_state(base_key)
        price = (sqrt_p / Q96) ** 2  # 6/6 dec → no adjustment
        assert Decimal("0.0004") < price < Decimal("0.0009")
