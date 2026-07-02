"""EWMA calculator and tick range computation."""

import math
from decimal import Decimal

from engine.math.v3 import price_to_tick, align_tick, constrain_tick_width

# 3 bps minimum std floor — matches production engine/lp/strategy.py
_STD_FLOOR = 3e-4


class EWMACalculator:
    """Online exponentially-weighted mean and variance of log-returns.

    Variance is computed on log-returns (not raw price deviations) to match
    the production engine in engine/lp/strategy.py. This makes σ scale-invariant
    and consistent with tick-space geometry.
    """

    def __init__(self, lam: float) -> None:
        self.lam = lam
        self._mean: float = 0.0
        self._var: float = 0.0      # variance of log-returns
        self._n: int = 0
        self._prev: float | None = None   # previous price for log-return computation

    def update(self, x: float) -> None:
        if self._n == 0:
            self._mean = x
            self._var = 0.0
        else:
            r = math.log(x / self._prev) if self._prev and self._prev > 0 else 0.0
            self._var = self.lam * self._var + (1 - self.lam) * r * r
            self._mean = self.lam * self._mean + (1 - self.lam) * x
        self._prev = x
        self._n += 1

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def std(self) -> float:
        return max(self._var ** 0.5, _STD_FLOOR)

    @property
    def ready(self) -> bool:
        return self._n >= 2


def calculate_tick_range(
    ewma: EWMACalculator,
    sd_multiplier: float,
    downside_skew: float,
    token0_decimals: int,
    token1_decimals: int,
    tick_spacing: int,
    min_tick_width: int,
    max_tick_width: int,
    center_price: float | None = None,
) -> tuple[int, int]:
    """Compute asymmetric tick range from EWMA state.

    Uses log-space bounds (consistent with tick geometry) to match the production
    engine in engine/lp/strategy.py. The optional center_price parameter overrides
    ewma.mean as the range center — used by the fair price validation grid to inject
    inventory-skewed center prices without altering the EWMA state.
    """
    mean = center_price if center_price is not None else ewma.mean
    std = ewma.std
    range_width = std * sd_multiplier * 2

    # Log-space asymmetric bounds: consistent with how ticks are defined (log-price)
    lower_price = max(mean * math.exp(-range_width * downside_skew), 0.0001)
    upper_price = mean * math.exp(range_width * (1 - downside_skew))

    tick_lower = price_to_tick(Decimal(str(lower_price)), token0_decimals, token1_decimals)
    tick_upper = price_to_tick(Decimal(str(upper_price)), token0_decimals, token1_decimals)

    tick_lower = align_tick(tick_lower, tick_spacing, "down")
    tick_upper = align_tick(tick_upper, tick_spacing, "up")

    tick_lower, tick_upper = constrain_tick_width(
        tick_lower, tick_upper, min_tick_width, max_tick_width, tick_spacing
    )
    return tick_lower, tick_upper


def calculate_fixed_pct_tick_range(
    center_price: float,
    width_pct: float,
    token0_decimals: int,
    token1_decimals: int,
    tick_spacing: int,
    min_tick_width: int,
    max_tick_width: int,
    lower_width_pct: float | None = None,
    upper_width_pct: float | None = None,
) -> tuple[int, int]:
    """Compute a tick range from normalized percent width around a native price.

    ``width_pct`` is interpreted as the full range width. If explicit
    lower/upper widths are not provided, the range is symmetric around
    ``center_price``.
    """
    if center_price <= 0:
        raise ValueError("center_price must be positive")
    if width_pct <= 0 and (lower_width_pct is None or upper_width_pct is None):
        raise ValueError("width_pct must be positive")

    lower_pct = lower_width_pct if lower_width_pct is not None else width_pct / 2
    upper_pct = upper_width_pct if upper_width_pct is not None else width_pct / 2
    if lower_pct < 0 or upper_pct < 0:
        raise ValueError("range widths must be non-negative")
    if lower_pct == 0 and upper_pct == 0:
        raise ValueError("range width cannot be zero")

    lower_price = max(center_price * (1 - lower_pct), 1e-18)
    upper_price = center_price * (1 + upper_pct)
    tick_lower = price_to_tick(Decimal(str(lower_price)), token0_decimals, token1_decimals)
    tick_upper = price_to_tick(Decimal(str(upper_price)), token0_decimals, token1_decimals)

    tick_lower = align_tick(tick_lower, tick_spacing, "down")
    tick_upper = align_tick(tick_upper, tick_spacing, "up")
    tick_lower, tick_upper = constrain_tick_width(
        tick_lower, tick_upper, min_tick_width, max_tick_width, tick_spacing
    )
    return tick_lower, tick_upper


def calculate_fixed_tick_range(
    center_tick: int,
    tick_width: int,
    tick_spacing: int,
    min_tick_width: int,
    max_tick_width: int,
    downside_skew: float = 0.5,
) -> tuple[int, int]:
    """Compute an aligned tick range with a fixed total tick width."""
    if tick_width <= 0:
        raise ValueError("tick_width must be positive")
    if not 0 <= downside_skew <= 1:
        raise ValueError("downside_skew must be between 0 and 1")

    lower_width = int(round(tick_width * downside_skew))
    upper_width = tick_width - lower_width
    tick_lower = align_tick(center_tick - lower_width, tick_spacing, "down")
    tick_upper = align_tick(center_tick + upper_width, tick_spacing, "up")
    tick_lower, tick_upper = constrain_tick_width(
        tick_lower, tick_upper, min_tick_width, max_tick_width, tick_spacing
    )
    return tick_lower, tick_upper
