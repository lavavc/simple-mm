"""LP strategy configuration."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class DexParams(BaseModel):
    """Parameters for DEX position management."""

    # Range calculation
    sd_multiplier: Decimal = Decimal("2.5")
    min_tick_width: int = 100
    max_tick_width: int = 1000
    lookback_points: Optional[int] = None
    rebalance_threshold_percent: Decimal = Decimal("10.0")
    max_slippage_percent: Decimal = Decimal("1.0")
    downside_skew: Decimal = Decimal("0.4")   # fraction of range below current price (0.5 = symmetric)
    ewma_lambda: Decimal = Decimal("0.99")    # EWMA decay factor for volatility estimation
