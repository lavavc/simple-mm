"""Shared test parameter defaults.

This module provides factory functions for creating parameter objects
with test-friendly defaults. Tests should use these instead of DexParams()
directly to avoid breaking when production defaults change.
"""

from decimal import Decimal
from engine.lp.config import DexParams


def make_dex_params(**overrides) -> DexParams:
    """
    Create DexParams with test-friendly defaults.

    These defaults are isolated from production defaults, so tests
    won't break when we tune production values.

    Usage:
        params = make_dex_params()  # All test defaults
        params = make_dex_params(sd_multiplier=Decimal("2.0"))  # Override one
    """
    defaults = {
        "sd_multiplier": Decimal("1.5"),
        "min_tick_width": 100,
        "max_tick_width": 1000,
        "lookback_points": None,
        "rebalance_threshold_percent": Decimal("5.0"),
        "max_slippage_percent": Decimal("1.0"),
        "downside_skew": Decimal("0.4"),
        "ewma_lambda": Decimal("0.99"),
    }
    defaults.update(overrides)
    return DexParams(**defaults)
