"""EWMA calculator and tick range computation."""

from engine.math.v3 import price_to_tick, align_tick, constrain_tick_width


class EWMACalculator:
    """Online exponentially-weighted mean and variance."""

    def __init__(self, lam: float) -> None:
        self.lam = lam
        self._mean: float = 0.0
        self._var: float = 0.0
        self._n: int = 0

    def update(self, x: float) -> None:
        if self._n == 0:
            self._mean = x
            self._var = 0.0
        else:
            self._var = self.lam * self._var + (1 - self.lam) * (x - self._mean) ** 2
            self._mean = self.lam * self._mean + (1 - self.lam) * x
        self._n += 1

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def std(self) -> float:
        return self._var ** 0.5

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
) -> tuple[int, int]:
    """Compute asymmetric tick range from EWMA state."""
    mean = ewma.mean
    std = ewma.std

    # Asymmetric: heavier downside
    lower_price = mean - sd_multiplier * 2 * downside_skew * std
    upper_price = mean + sd_multiplier * 2 * (1 - downside_skew) * std
    lower_price = max(lower_price, 1e-12)

    from decimal import Decimal
    tick_lower = price_to_tick(Decimal(str(lower_price)), token0_decimals, token1_decimals)
    tick_upper = price_to_tick(Decimal(str(upper_price)), token0_decimals, token1_decimals)

    tick_lower = align_tick(tick_lower, tick_spacing, "down")
    tick_upper = align_tick(tick_upper, tick_spacing, "up")

    tick_lower, tick_upper = constrain_tick_width(
        tick_lower, tick_upper, min_tick_width, max_tick_width, tick_spacing
    )
    return tick_lower, tick_upper
