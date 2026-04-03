"""LP strategy configuration."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class DexParams(BaseModel):
    """Parameters for DEX position management.

    No defaults — all values must be supplied explicitly (from config.py env vars or
    persisted overrides). This prevents silent divergence between the dataclass and
    the operator-visible settings in config.py.
    """

    sd_multiplier: Decimal
    min_tick_width: int
    max_tick_width: int
    lookback_points: Optional[int] = None  # None = use all available prices
    rebalance_threshold_percent: Decimal
    max_slippage_percent: Decimal
    downside_skew: Decimal
    ewma_lambda: Decimal
