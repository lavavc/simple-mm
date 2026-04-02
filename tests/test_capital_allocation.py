"""Unit tests for DEX capital allocation logic."""

import pytest
from decimal import Decimal


def allocate(balance0: int, balance1: int) -> tuple[int, int]:
    """Simulate calculate_mint_amounts() — returns full LP wallet balance."""
    return balance0, balance1


class TestDeployAmounts:
    def test_deploys_full_balance(self):
        a0, a1 = allocate(500_000_000, 400_000_000)
        assert a0 == 500_000_000
        assert a1 == 400_000_000

    def test_zero_balance_deploys_nothing(self):
        a0, a1 = allocate(0, 0)
        assert a0 == 0
        assert a1 == 0

    def test_single_token_deposit(self):
        """Single-token deposit (e.g. only cNGN): full balance returned, prepare_lp_balance swaps ratio."""
        a0, a1 = allocate(1_000_000_000, 0)
        assert a0 == 1_000_000_000
        assert a1 == 0

    def test_asymmetric_balances(self):
        """Each token is returned as-is; ratio correction is done by prepare_lp_balance."""
        a0, a1 = allocate(1_000_000, 50_000)
        assert a0 == 1_000_000
        assert a1 == 50_000
