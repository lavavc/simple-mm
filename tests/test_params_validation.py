"""Unit tests for parameter validation."""

import pytest
from decimal import Decimal
from pydantic import ValidationError

from engine.config import DexParams
from engine.types import CexParams, WalletParams
from engine.config import settings
from tests.conftest_params import make_dex_params


class TestDexParamsValidation:
    """Test DexParams validation."""

    def test_all_fields_required(self):
        """DexParams has no defaults — all fields except lookback_points must be supplied."""
        with pytest.raises((ValidationError, TypeError)):
            DexParams()

    def test_lookback_points_optional(self):
        """lookback_points defaults to None (use all prices)."""
        params = make_dex_params()
        assert params.lookback_points is None

    def test_uni_base_strategy_params_from_settings(self):
        """uni-base LP strategy params are read from settings, not DexParams defaults."""
        assert settings.uni_base_sd_multiplier == Decimal("2.75")
        assert settings.uni_base_ewma_lambda == Decimal("0.975")
        assert settings.uni_base_downside_skew == Decimal("0.45")

    def test_uni_bsc_strategy_params_from_settings(self):
        """uni-bsc LP strategy params are read from settings, not DexParams defaults."""
        assert settings.uni_bsc_sd_multiplier == Decimal("3.0")
        assert settings.uni_bsc_ewma_lambda == Decimal("0.975")
        assert settings.uni_bsc_downside_skew == Decimal("0.5")

    def test_custom_values(self):
        params = make_dex_params(
            sd_multiplier=Decimal("2.5"),
            min_tick_width=200,
            max_tick_width=2000,
            lookback_points=50,
            rebalance_threshold_percent=Decimal("10.0"),
            max_slippage_percent=Decimal("0.5"),
        )

        assert params.sd_multiplier == Decimal("2.5")
        assert params.min_tick_width == 200
        assert params.max_tick_width == 2000
        assert params.lookback_points == 50

    def test_decimal_from_string(self):
        params = make_dex_params(sd_multiplier="2.0")
        assert params.sd_multiplier == Decimal("2.0")

    def test_decimal_from_float(self):
        params = make_dex_params(sd_multiplier=2.0)
        assert float(params.sd_multiplier) == pytest.approx(2.0)

    def test_decimal_from_int(self):
        params = make_dex_params(sd_multiplier=2)
        assert params.sd_multiplier == Decimal("2")

    def test_serialization(self):
        params = make_dex_params(sd_multiplier=Decimal("2.5"))
        data = params.model_dump()
        assert data["sd_multiplier"] == Decimal("2.5")

    def test_json_serialization(self):
        params = make_dex_params(sd_multiplier=Decimal("2.5"))
        json_str = params.model_dump_json()
        assert "sd_multiplier" in json_str


class TestCexParamsValidation:
    """Test CexParams validation and defaults."""

    def test_default_values(self):
        params = CexParams()

        assert params.ladder_enabled is False
        assert params.spread_offset_ngn == 50
        assert params.ladder_step_ngn == 1
        assert params.ladder_levels_per_side == 1
        assert params.resolved_ladder_offsets_ngn == [50]
        assert params.anchor_source == "blended"
        assert params.anchor_requote_threshold_bps == 0
        assert params.anchor_requote_cooldown_seconds == 30
        assert params.order_size_cngn == Decimal("0")
        assert params.order_size_usdt == Decimal("0")

    def test_custom_values(self):
        params = CexParams(
            ladder_enabled=True,
            spread_offset_ngn=1,
            ladder_step_ngn=1,
            ladder_levels_per_side=20,
            anchor_source="quidax",
            anchor_requote_threshold_bps=15,
            anchor_requote_cooldown_seconds=12,
            order_size_cngn=Decimal("10000"),
            order_size_usdt=Decimal("100"),
        )

        assert params.ladder_enabled is True
        assert params.spread_offset_ngn == 1
        assert params.ladder_step_ngn == 1
        assert params.ladder_levels_per_side == 20
        assert params.resolved_ladder_offsets_ngn == list(range(1, 21))
        assert params.anchor_source == "quidax"
        assert params.anchor_requote_threshold_bps == 15
        assert params.anchor_requote_cooldown_seconds == 12
        assert params.order_size_cngn == Decimal("10000")
        assert params.order_size_usdt == Decimal("100")

    def test_serialization(self):
        params = CexParams(order_size_cngn=Decimal("5000"))
        data = params.model_dump()

        assert data["order_size_cngn"] == Decimal("5000")
        assert data["ladder_enabled"] is False
        assert data["spread_offset_ngn"] == 50
        assert data["ladder_step_ngn"] == 1
        assert data["ladder_levels_per_side"] == 1
        assert data["anchor_source"] == "blended"
        assert data["anchor_requote_cooldown_seconds"] == 30
        assert "ladder_offsets_ngn" not in data


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
        original = make_dex_params(sd_multiplier=Decimal("2.0"))

        copied = original.model_copy()
        assert copied.sd_multiplier == original.sd_multiplier

        copied_data = copied.model_dump()
        copied_data["sd_multiplier"] = Decimal("3.0")
        modified = DexParams(**copied_data)

        assert modified.sd_multiplier == Decimal("3.0")
        assert original.sd_multiplier == Decimal("2.0")

    def test_dex_params_update(self):
        original = make_dex_params()
        updated = DexParams(**{**original.model_dump(), "sd_multiplier": Decimal("2.5")})
        assert updated.sd_multiplier == Decimal("2.5")

    def test_params_from_dict(self):
        """DB always stores a full DexParams dump; partial dicts are not valid."""
        stored_config = make_dex_params(sd_multiplier="1.8", min_tick_width=150).model_dump()

        params = DexParams(**stored_config)

        assert params.sd_multiplier == Decimal("1.8")
        assert params.min_tick_width == 150


class TestStartupParamRestoration:
    """Verify that persisted DexParams survive a round-trip through the DB serialisation."""

    @pytest.mark.asyncio
    async def test_persisted_params_restored_on_startup(self):
        """Params saved to DB as JSON are correctly reconstructed into DexParams on startup."""
        from tests.fakes import FakeDexAdapter

        venue = FakeDexAdapter()
        original = make_dex_params(sd_multiplier=Decimal("4.5"), downside_skew=Decimal("0.6"))
        # Simulate what update_venue_config stores: model_dump(mode="json") → json-safe dict
        stored = original.model_dump(mode="json")

        # Simulate what startup does: reconstruct from stored dict
        venue.params = DexParams(**stored)

        assert venue.params.sd_multiplier == Decimal("4.5")
        assert venue.params.downside_skew == Decimal("0.6")

    def test_model_dump_json_is_fully_serialisable(self):
        """model_dump(mode='json') must produce a dict json.dumps can handle."""
        import json
        params = make_dex_params(sd_multiplier=Decimal("3.14"), ewma_lambda=Decimal("0.975"))
        serialised = params.model_dump(mode="json")
        # Must not raise
        json.dumps(serialised)
        # Must round-trip
        restored = DexParams(**serialised)
        assert restored.sd_multiplier == params.sd_multiplier
        assert restored.ewma_lambda == params.ewma_lambda
