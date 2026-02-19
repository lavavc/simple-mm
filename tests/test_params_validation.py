"""Unit tests for parameter validation."""

import pytest
from decimal import Decimal
from pydantic import ValidationError

from engine.api.schemas import DexParams, CexParams, WalletParams


class TestDexParamsValidation:
    """Test DexParams validation and defaults."""

    def test_default_values(self):
        """Test default parameter values.

        NOTE: This test intentionally checks production defaults.
        If it fails, verify the change was intentional and update this test.
        """
        params = DexParams()

        assert params.sd_multiplier == Decimal("1.5")
        assert params.min_tick_width == 100
        assert params.max_tick_width == 1000
        assert params.lookback_points is None
        assert params.rebalance_threshold_percent == Decimal("5.0")
        assert params.max_slippage_percent == Decimal("1.0")
        assert params.deploy_token0 == Decimal("0")
        assert params.deploy_token1 == Decimal("0")

    def test_custom_values(self):
        params = DexParams(
            sd_multiplier=Decimal("2.5"),
            min_tick_width=200,
            max_tick_width=2000,
            lookback_points=50,
            rebalance_threshold_percent=Decimal("10.0"),
            max_slippage_percent=Decimal("0.5"),
            deploy_token0=Decimal("500000"),
            deploy_token1=Decimal("600"),
        )

        assert params.sd_multiplier == Decimal("2.5")
        assert params.min_tick_width == 200
        assert params.max_tick_width == 2000
        assert params.lookback_points == 50
        assert params.deploy_token0 == Decimal("500000")
        assert params.deploy_token1 == Decimal("600")

    def test_decimal_from_string(self):
        params = DexParams(sd_multiplier="2.0", deploy_token0="100000")

        assert params.sd_multiplier == Decimal("2.0")
        assert params.deploy_token0 == Decimal("100000")

    def test_decimal_from_float(self):
        params = DexParams(sd_multiplier=2.0)

        assert float(params.sd_multiplier) == pytest.approx(2.0)

    def test_decimal_from_int(self):
        params = DexParams(sd_multiplier=2, deploy_token0=500000)

        assert params.sd_multiplier == Decimal("2")
        assert params.deploy_token0 == Decimal("500000")

    def test_serialization(self):
        params = DexParams(deploy_token0=Decimal("500000"), deploy_token1=Decimal("600"))

        data = params.model_dump()

        assert "deploy_token0" in data
        assert "deploy_token1" in data
        assert data["deploy_token0"] == Decimal("500000")

    def test_json_serialization(self):
        params = DexParams(deploy_token0=Decimal("500000"))

        json_str = params.model_dump_json()

        assert "deploy_token0" in json_str


class TestCexParamsValidation:
    """Test CexParams validation and defaults."""

    def test_default_values(self):
        params = CexParams()

        assert params.ladder_enabled is False
        assert params.ladder_offsets_ngn == [1, 3, 5, 10]
        assert params.order_size_cngn == Decimal("0")
        assert params.order_size_usdt == Decimal("0")

    def test_custom_values(self):
        params = CexParams(
            ladder_enabled=True,
            ladder_offsets_ngn=[1, 3, 5],
            order_size_cngn=Decimal("10000"),
            order_size_usdt=Decimal("100"),
        )

        assert params.ladder_enabled is True
        assert params.ladder_offsets_ngn == [1, 3, 5]
        assert params.order_size_cngn == Decimal("10000")
        assert params.order_size_usdt == Decimal("100")

    def test_serialization(self):
        params = CexParams(order_size_cngn=Decimal("5000"))
        data = params.model_dump()

        assert data["order_size_cngn"] == Decimal("5000")
        assert data["ladder_enabled"] is False


class TestWalletParamsValidation:
    """Test WalletParams validation and defaults."""

    def test_default_values(self):
        params = WalletParams()

        assert params.spread_bps == 15

    def test_custom_values(self):
        params = WalletParams(spread_bps=25)

        assert params.spread_bps == 25

    def test_serialization(self):
        params = WalletParams(spread_bps=20)
        data = params.model_dump()

        assert data["spread_bps"] == 20


class TestParamsInteroperability:
    def test_dex_params_copy(self):
        original = DexParams(sd_multiplier=Decimal("2.0"), deploy_token0=Decimal("500000"))

        copied = original.model_copy()

        assert copied.sd_multiplier == original.sd_multiplier
        assert copied.deploy_token0 == original.deploy_token0

        copied_data = copied.model_dump()
        copied_data["sd_multiplier"] = Decimal("3.0")
        modified = DexParams(**copied_data)

        assert modified.sd_multiplier == Decimal("3.0")
        assert original.sd_multiplier == Decimal("2.0")

    def test_dex_params_update(self):
        original = DexParams()

        updated = DexParams(**{**original.model_dump(), "sd_multiplier": Decimal("2.5")})

        assert updated.sd_multiplier == Decimal("2.5")
        assert updated.deploy_token0 == original.deploy_token0

    def test_params_from_dict(self):
        stored_config = {
            "sd_multiplier": "1.8",
            "min_tick_width": 150,
            "deploy_token0": "500000",
            "deploy_token1": "600",
        }

        params = DexParams(**stored_config)

        assert params.sd_multiplier == Decimal("1.8")
        assert params.min_tick_width == 150
        assert params.deploy_token0 == Decimal("500000")
        assert params.deploy_token1 == Decimal("600")
