"""Shared test parameter defaults.

This module provides factory functions for creating parameter objects
with test-friendly defaults. Tests should use these instead of DexParams()
directly to avoid breaking when production defaults change.
"""

from decimal import Decimal
from engine.api.schemas import DexParams, ArbitrageParams


def make_dex_params(**overrides) -> DexParams:
    """
    Create DexParams with test-friendly defaults.

    These defaults are isolated from production defaults, so tests
    won't break when we tune production values.

    Usage:
        params = make_dex_params()  # All test defaults
        params = make_dex_params(max_utilization_percent=Decimal("50"))  # Override one
    """
    defaults = {
        "sd_multiplier": Decimal("1.5"),
        "min_tick_width": 100,
        "max_tick_width": 1000,
        "lookback_points": None,
        "rebalance_threshold_percent": Decimal("5.0"),
        "max_slippage_percent": Decimal("1.0"),
        # Test defaults: deploy full balance by default
        "deploy_token0": Decimal("1000000000000"),  # Effectively uncapped for tests
        "deploy_token1": Decimal("1000000000000"),
    }
    defaults.update(overrides)
    return DexParams(**defaults)


def make_arbitrage_params(**overrides) -> ArbitrageParams:
    """
    Create ArbitrageParams with test-friendly defaults.

    Usage:
        params = make_arbitrage_params()
        params = make_arbitrage_params(min_spread_bps=200)
    """
    defaults = {
        "min_spread_bps": 150,
        "min_net_profit_bps": 50,
        "dex_swap_fee_bps": 30,
        "dex_slippage_bps": 20,
        "cex_taker_fee_bps": 25,
        "max_single_trade_usd": Decimal("1000"),
        "max_daily_volume_usd": Decimal("10000"),
        "max_daily_loss_usd": Decimal("50"),
        "max_inventory_imbalance_usd": Decimal("5000"),
        "scan_interval_seconds": 30,
    }
    defaults.update(overrides)
    return ArbitrageParams(**defaults)
