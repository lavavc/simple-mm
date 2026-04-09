from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.config import DexParams
from engine.main import restore_venue_params
from engine.types import CexParams


@pytest.mark.asyncio
async def test_restore_venue_params_rehydrates_lp_and_cex_configs():
    quidax = SimpleNamespace(params=CexParams())
    blockradar = SimpleNamespace()
    lp_manager = SimpleNamespace(
        params=DexParams(
            sd_multiplier=Decimal("2.75"),
            min_tick_width=100,
            max_tick_width=1000,
            lookback_points=None,
            rebalance_threshold_percent=Decimal("10.0"),
            max_slippage_percent=Decimal("1.0"),
            downside_skew=Decimal("0.45"),
            ewma_lambda=Decimal("0.975"),
        )
    )
    db = SimpleNamespace(
        venue_config=SimpleNamespace(
            get_venue_config=AsyncMock(
                side_effect=[
                    {
                        "venue": "uni-base",
                        "params": {
                            "sd_multiplier": "3.00",
                            "min_tick_width": 120,
                            "max_tick_width": 1100,
                            "lookback_points": None,
                            "rebalance_threshold_percent": "11.0",
                            "max_slippage_percent": "1.2",
                            "downside_skew": "0.55",
                            "ewma_lambda": "0.970",
                        },
                    },
                    {
                        "venue": "quidax",
                        "params": {
                            "ladder_enabled": True,
                            "spread_offset_ngn": 50,
                            "ladder_step_ngn": 1,
                            "ladder_levels_per_side": 5,
                            "anchor_source": "dex_vwap",
                            "anchor_requote_threshold_bps": 10,
                            "anchor_requote_cooldown_seconds": 30,
                            "order_size_cngn": "2000",
                            "order_size_usdt": "10",
                        },
                    },
                ]
            )
        )
    )

    await restore_venue_params(
        db,
        {"quidax": quidax, "blockradar": blockradar, "uni-base": lp_manager},
        {"uni-base": lp_manager},
    )

    assert lp_manager.params.sd_multiplier == Decimal("3.00")
    assert lp_manager.params.min_tick_width == 120
    assert quidax.params.ladder_enabled is True
    assert quidax.params.spread_offset_ngn == 50
    assert quidax.params.anchor_source == "dex_vwap"
    assert quidax.params.order_size_usdt == Decimal("10")
