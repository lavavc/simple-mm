"""Unit tests for DEX capital allocation logic."""

import pytest
from decimal import Decimal

from engine.api.schemas import DexParams
from tests.conftest_params import make_dex_params


def allocate(
    balance0: Decimal,
    balance1: Decimal,
    deploy0: Decimal,
    deploy1: Decimal,
    token0_decimals: int = 18,
    token1_decimals: int = 6,
) -> tuple[Decimal, Decimal]:
    """Simulate calculate_mint_amounts() without web3."""
    amount0 = min(deploy0, balance0)
    amount1 = min(deploy1, balance1)
    return (
        Decimal(int(amount0 * Decimal(10**token0_decimals))) / Decimal(10**token0_decimals),
        Decimal(int(amount1 * Decimal(10**token1_decimals))) / Decimal(10**token1_decimals),
    )


class TestDeployAmounts:
    def test_deploys_configured_amount(self):
        a0, a1 = allocate(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            deploy0=Decimal("500000"),
            deploy1=Decimal("400"),
        )
        assert a0 == Decimal("500000")
        assert a1 == Decimal("400")

    def test_capped_by_balance(self):
        """Never deploys more than the wallet holds."""
        a0, a1 = allocate(
            balance0=Decimal("100"),
            balance1=Decimal("50"),
            deploy0=Decimal("999999"),
            deploy1=Decimal("999999"),
        )
        assert a0 == Decimal("100")
        assert a1 == Decimal("50")

    def test_zero_deploy_deploys_nothing(self):
        a0, a1 = allocate(
            balance0=Decimal("1000000"),
            balance1=Decimal("1000"),
            deploy0=Decimal("0"),
            deploy1=Decimal("0"),
        )
        assert a0 == Decimal("0")
        assert a1 == Decimal("0")

    def test_zero_balance_deploys_nothing(self):
        a0, a1 = allocate(
            balance0=Decimal("0"),
            balance1=Decimal("0"),
            deploy0=Decimal("500000"),
            deploy1=Decimal("500"),
        )
        assert a0 == Decimal("0")
        assert a1 == Decimal("0")

    def test_partial_balance(self):
        """Deploy amount partially covered by balance uses what's available."""
        a0, a1 = allocate(
            balance0=Decimal("300"),
            balance1=Decimal("1000"),
            deploy0=Decimal("500"),
            deploy1=Decimal("200"),
        )
        assert a0 == Decimal("300")  # capped by balance
        assert a1 == Decimal("200")  # deploy < balance, use deploy

    def test_asymmetric_deploy(self):
        """Each token is capped independently."""
        a0, a1 = allocate(
            balance0=Decimal("1000"),
            balance1=Decimal("50"),
            deploy0=Decimal("500"),
            deploy1=Decimal("200"),
        )
        assert a0 == Decimal("500")   # deploy < balance
        assert a1 == Decimal("50")    # capped by balance
