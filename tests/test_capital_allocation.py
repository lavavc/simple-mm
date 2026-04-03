"""Unit tests for V4LPAdapter.calculate_mint_amounts."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from engine.venues.dex.lp_v4 import V4LPAdapter


def _make_adapter(balance0_raw: int, balance1_raw: int) -> SimpleNamespace:
    """Minimal self-mock for V4LPAdapter.calculate_mint_amounts."""
    token0 = MagicMock()
    token0.functions.balanceOf.return_value.call.return_value = balance0_raw
    token1 = MagicMock()
    token1.functions.balanceOf.return_value.call.return_value = balance1_raw
    return SimpleNamespace(
        token0=token0,
        token1=token1,
        lp_account=SimpleNamespace(address="0xLP"),
        config=SimpleNamespace(token0_decimals=6, token1_decimals=6),
        name="uni-base",
    )


class TestCalculateMintAmounts:
    def test_returns_full_lp_wallet_balance(self):
        adapter = _make_adapter(500_000_000_000, 600_000_000)
        a0, a1 = V4LPAdapter.calculate_mint_amounts(adapter)
        assert a0 == 500_000_000_000
        assert a1 == 600_000_000

    def test_zero_balance_returns_zero(self):
        adapter = _make_adapter(0, 0)
        a0, a1 = V4LPAdapter.calculate_mint_amounts(adapter)
        assert a0 == 0
        assert a1 == 0

    def test_single_token_deposit(self):
        """Only token0 funded — returns full raw balance; ratio swap is handled by prepare_lp_balance."""
        adapter = _make_adapter(1_000_000_000, 0)
        a0, a1 = V4LPAdapter.calculate_mint_amounts(adapter)
        assert a0 == 1_000_000_000
        assert a1 == 0

    def test_asymmetric_balances(self):
        """Each token is returned as-is regardless of ratio."""
        adapter = _make_adapter(1_000_000, 50_000)
        a0, a1 = V4LPAdapter.calculate_mint_amounts(adapter)
        assert a0 == 1_000_000
        assert a1 == 50_000
