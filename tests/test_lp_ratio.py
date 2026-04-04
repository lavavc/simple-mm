"""Unit tests for compute_required_ratio and prepare_lp_balance."""

import math
import pytest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from engine.api.schemas import TxResult
from engine.venues.dex.shared import _Q96, compute_required_ratio
from engine.venues.dex.lp_v4 import LPBalanceSwapResult, V4LPAdapter


def _price_to_sqrt_x96(price: float, token0_decimals: int = 6, token1_decimals: int = 6) -> int:
    """Convert a human-readable token1-per-token0 price to sqrtPriceX96."""
    dec_adj = 10 ** (token0_decimals - token1_decimals)
    return int(math.sqrt(price * dec_adj) * _Q96)


def _make_balance_adapter(
    sqrt_price_x96: int,
    balance0_raw: int,
    balance1_raw: int,
    swap_result: TxResult | None = None,
    token0_decimals: int = 6,
    token1_decimals: int = 6,
) -> SimpleNamespace:
    """Minimal self-mock for prepare_lp_balance."""
    pool_id = "0x" + "ab" * 32
    token0_addr = "0x" + "aa" * 20
    token1_addr = "0x" + "bb" * 20
    lp_addr = "0x" + "cc" * 20

    state_view = MagicMock()
    state_view.functions.getSlot0.return_value.call.return_value = [sqrt_price_x96, 0, 0, 0]

    token0 = MagicMock()
    token0.functions.balanceOf.return_value.call.return_value = balance0_raw
    token1 = MagicMock()
    token1.functions.balanceOf.return_value.call.return_value = balance1_raw

    adapter = SimpleNamespace(
        config=SimpleNamespace(
            pool_id=pool_id,
            token0_decimals=token0_decimals,
            token1_decimals=token1_decimals,
            token0_address=token0_addr,
            token1_address=token1_addr,
        ),
        name="uni-base",
        lp_account=SimpleNamespace(address=lp_addr),
        state_view=state_view,
        token0=token0,
        token1=token1,
        _swap_from_lp=AsyncMock(
            return_value=swap_result or TxResult(hash="0xswap", status="confirmed")
        ),
    )
    adapter._compute_required_ratio = lambda tl, tu, sp: compute_required_ratio(
        tl, tu, sp, adapter.config.token0_decimals, adapter.config.token1_decimals
    )
    return adapter


class TestComputeRequiredRatio:
    """Tests for shared.compute_required_ratio."""

    def test_symmetric_range_price_at_midpoint(self):
        """When price is inside the range, both r0 and r1 are non-zero."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        r0, r1 = compute_required_ratio(-1000, 1000, sqrt_p, 6, 6)
        assert r0 > 0
        assert r1 > 0

    def test_price_below_range_all_token0(self):
        """Price below range: position is entirely token0, r1 == 0."""
        sqrt_p = _price_to_sqrt_x96(0.5)  # below tick_lower=1000
        r0, r1 = compute_required_ratio(1000, 2000, sqrt_p, 6, 6)
        assert r0 > 0
        assert r1 == 0

    def test_price_above_range_all_token1(self):
        """Price above range: position is entirely token1, r0 == 0."""
        sqrt_p = _price_to_sqrt_x96(2.0)  # above tick_upper=-1000
        r0, r1 = compute_required_ratio(-2000, -1000, sqrt_p, 6, 6)
        assert r0 == 0
        assert r1 > 0

    def test_price_near_upper_less_token0(self):
        """Closer to upper tick → less token0 needed than at midpoint."""
        sqrt_mid = _price_to_sqrt_x96(0.5)
        sqrt_near_upper = _price_to_sqrt_x96(1.005)  # tick ≈ 50, close to upper=100
        r0_mid, _ = compute_required_ratio(-2000, 100, sqrt_mid, 6, 6)
        r0_upper, _ = compute_required_ratio(-2000, 100, sqrt_near_upper, 6, 6)
        assert r0_upper < r0_mid

    def test_decimal_adjustment_affects_r0(self):
        """Dec adjustment must change r0 when token decimals differ."""
        sqrt_p_6_6 = _price_to_sqrt_x96(1.0, 6, 6)
        sqrt_p_6_18 = _price_to_sqrt_x96(1.0, 6, 18)
        r0_equal, r1_equal = compute_required_ratio(-1000, 1000, sqrt_p_6_6, 6, 6)
        r0_diff, r1_diff = compute_required_ratio(-1000, 1000, sqrt_p_6_18, 6, 18)
        # For equal-decimal 6/6 at symmetric range: both tokens have equal value weight
        assert abs(float(r0_equal) - float(r1_equal)) / float(r1_equal) < 0.01, "6/6 should give ~50/50"
        # Different decimals produce different r0 (dec_adj changes the ratio)
        assert r0_diff != r0_equal


class TestPrepareLpBalance:
    """Tests for V4LPAdapter.prepare_lp_balance via mocked RPC."""

    @pytest.mark.asyncio
    async def test_no_swap_when_empty(self):
        """Both balances zero: returns immediately, no swap."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        adapter = _make_balance_adapter(sqrt_p, 0, 0)
        await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_swap_when_balanced(self):
        """Balances already at target ratio (within 1%): no swap triggered."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        # Symmetric range at price ≈ 1: target is approximately 50/50 by value.
        # Fund with equal raw amounts — should be close enough to skip the swap.
        adapter = _make_balance_adapter(sqrt_p, 500_000_000, 500_000_000)
        await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_not_called()

    @pytest.mark.asyncio
    async def test_swap_token0_to_token1_when_excess_token0(self):
        """All token0, none of token1: surplus token0 swap is triggered."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        # Entirely in token0, none in token1 — large imbalance
        adapter = _make_balance_adapter(sqrt_p, 1_000_000_000, 0)
        await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_called_once()
        call_args = adapter._swap_from_lp.call_args[0]
        assert call_args[0].lower() == adapter.config.token0_address.lower()

    @pytest.mark.asyncio
    async def test_swap_token1_to_token0_when_excess_token1(self):
        """All token1, none of token0: surplus token1 swap is triggered."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        adapter = _make_balance_adapter(sqrt_p, 0, 1_000_000_000)
        await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_called_once()
        call_args = adapter._swap_from_lp.call_args[0]
        assert call_args[0].lower() == adapter.config.token1_address.lower()

    @pytest.mark.asyncio
    async def test_failed_swap_logs_warning_and_returns(self):
        """If the ratio swap fails, prepare_lp_balance logs and returns False."""
        sqrt_p = _price_to_sqrt_x96(1.0)
        failed = TxResult(hash="", status="failed", error="reverted")
        adapter = _make_balance_adapter(sqrt_p, 1_000_000_000, 0, swap_result=failed)
        result = await V4LPAdapter.prepare_lp_balance(adapter, -1000, 1000)
        adapter._swap_from_lp.assert_called_once()
        assert isinstance(result, LPBalanceSwapResult)
        assert result.tx_result.status == "failed"

    @pytest.mark.asyncio
    async def test_target1_never_negative_near_upper_bound(self):
        """target1 is clamped to 0 when floating-point drift makes it negative."""
        # Price very close to upper tick — r1 ≈ 0, target0 * price ≈ total_value.
        # Without max(0, ...), target1 can go slightly negative and trigger a wrong swap.
        tick_lower, tick_upper = -2000, 100
        sqrt_near_upper = _price_to_sqrt_x96(1.009)  # tick ≈ 90, very close to upper=100
        # Fund only token0 (worst case for the negative-target1 scenario)
        adapter = _make_balance_adapter(sqrt_near_upper, 1_000_000_000, 0)
        await V4LPAdapter.prepare_lp_balance(adapter, tick_lower, tick_upper)
        # Should not have swapped token1→token0 (that would be wrong direction)
        if adapter._swap_from_lp.called:
            call_args = adapter._swap_from_lp.call_args[0]
            assert call_args[0].lower() == adapter.config.token0_address.lower(), \
                "Wrong swap direction: target1 went negative, triggering spurious token1→token0 swap"
