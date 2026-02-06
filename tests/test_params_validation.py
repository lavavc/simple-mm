"""Unit tests for parameter validation."""

import pytest
from decimal import Decimal
from pydantic import ValidationError

from engine.api.schemas import DexParams, CexParams, WalletParams


class TestDexParamsValidation:
    """Test DexParams validation and defaults."""

    def test_default_values(self):
        """Test default parameter values."""
        params = DexParams()

        assert params.sd_multiplier == Decimal("1.5")
        assert params.min_tick_width == 100
        assert params.max_tick_width == 1000
        assert params.lookback_points is None
        assert params.rebalance_threshold_percent == Decimal("5.0")
        assert params.max_slippage_percent == Decimal("1.0")
        assert params.max_utilization_percent == Decimal("80.0")
        assert params.min_reserve_token0 == Decimal("0")
        assert params.min_reserve_token1 == Decimal("0")
        assert params.max_position_usd is None

    def test_custom_values(self):
        """Test custom parameter values."""
        params = DexParams(
            sd_multiplier=Decimal("2.5"),
            min_tick_width=200,
            max_tick_width=2000,
            lookback_points=50,
            rebalance_threshold_percent=Decimal("10.0"),
            max_slippage_percent=Decimal("0.5"),
            max_utilization_percent=Decimal("70"),
            min_reserve_token0=Decimal("100000"),
            min_reserve_token1=Decimal("500"),
            max_position_usd=Decimal("25000"),
        )

        assert params.sd_multiplier == Decimal("2.5")
        assert params.min_tick_width == 200
        assert params.max_tick_width == 2000
        assert params.lookback_points == 50
        assert params.max_utilization_percent == Decimal("70")
        assert params.min_reserve_token0 == Decimal("100000")
        assert params.max_position_usd == Decimal("25000")

    def test_decimal_from_string(self):
        """Test that string values are converted to Decimal."""
        params = DexParams(
            sd_multiplier="2.0",
            max_utilization_percent="75.5",
        )

        assert params.sd_multiplier == Decimal("2.0")
        assert params.max_utilization_percent == Decimal("75.5")

    def test_decimal_from_float(self):
        """Test that float values are converted to Decimal."""
        params = DexParams(
            sd_multiplier=2.0,
            max_utilization_percent=75.5,
        )

        # Note: float conversion may have precision issues
        assert float(params.sd_multiplier) == pytest.approx(2.0)
        assert float(params.max_utilization_percent) == pytest.approx(75.5)

    def test_decimal_from_int(self):
        """Test that int values are converted to Decimal."""
        params = DexParams(
            sd_multiplier=2,
            max_utilization_percent=75,
        )

        assert params.sd_multiplier == Decimal("2")
        assert params.max_utilization_percent == Decimal("75")

    def test_serialization(self):
        """Test params can be serialized to dict."""
        params = DexParams(
            sd_multiplier=Decimal("2.5"),
            max_position_usd=Decimal("10000"),
        )

        data = params.model_dump()

        assert "sd_multiplier" in data
        assert "max_position_usd" in data
        assert data["max_position_usd"] == Decimal("10000")

    def test_json_serialization(self):
        """Test params can be serialized to JSON."""
        params = DexParams(
            sd_multiplier=Decimal("2.5"),
            max_position_usd=Decimal("10000"),
        )

        json_str = params.model_dump_json()

        assert "sd_multiplier" in json_str
        assert "max_position_usd" in json_str


class TestCexParamsValidation:
    """Test CexParams validation and defaults."""

    def test_default_values(self):
        """Test default parameter values."""
        params = CexParams()

        assert params.ladder_levels == 10
        assert params.ladder_increment == Decimal("0.000001")
        assert params.liquidity_per_level_percent == Decimal("5.0")

    def test_custom_values(self):
        """Test custom parameter values."""
        params = CexParams(
            ladder_levels=20,
            ladder_increment=Decimal("0.000005"),
            liquidity_per_level_percent=Decimal("2.5"),
        )

        assert params.ladder_levels == 20
        assert params.ladder_increment == Decimal("0.000005")
        assert params.liquidity_per_level_percent == Decimal("2.5")

    def test_serialization(self):
        """Test params can be serialized."""
        params = CexParams(ladder_levels=15)
        data = params.model_dump()

        assert data["ladder_levels"] == 15


class TestWalletParamsValidation:
    """Test WalletParams validation and defaults."""

    def test_default_values(self):
        """Test default parameter values."""
        params = WalletParams()

        assert params.spread_bps == 15

    def test_custom_values(self):
        """Test custom parameter values."""
        params = WalletParams(spread_bps=25)

        assert params.spread_bps == 25

    def test_serialization(self):
        """Test params can be serialized."""
        params = WalletParams(spread_bps=20)
        data = params.model_dump()

        assert data["spread_bps"] == 20


class TestParamsInteroperability:
    """Test that params work correctly when passed between components."""

    def test_dex_params_copy(self):
        """Test that params can be copied."""
        original = DexParams(
            sd_multiplier=Decimal("2.0"),
            max_utilization_percent=Decimal("70"),
        )

        copied = original.model_copy()

        assert copied.sd_multiplier == original.sd_multiplier
        assert copied.max_utilization_percent == original.max_utilization_percent

        # Modify copy doesn't affect original
        copied_data = copied.model_dump()
        copied_data["sd_multiplier"] = Decimal("3.0")
        modified = DexParams(**copied_data)

        assert modified.sd_multiplier == Decimal("3.0")
        assert original.sd_multiplier == Decimal("2.0")

    def test_dex_params_update(self):
        """Test that params can be updated."""
        original = DexParams()

        # Create updated version
        updated = DexParams(
            **{**original.model_dump(), "sd_multiplier": Decimal("2.5")}
        )

        assert updated.sd_multiplier == Decimal("2.5")
        assert updated.max_utilization_percent == original.max_utilization_percent

    def test_params_from_dict(self):
        """Test creating params from dict (e.g., from database)."""
        stored_config = {
            "sd_multiplier": "1.8",
            "min_tick_width": 150,
            "max_utilization_percent": "65",
            "min_reserve_token0": "50000",
        }

        params = DexParams(**stored_config)

        assert params.sd_multiplier == Decimal("1.8")
        assert params.min_tick_width == 150
        assert params.max_utilization_percent == Decimal("65")
        assert params.min_reserve_token0 == Decimal("50000")
