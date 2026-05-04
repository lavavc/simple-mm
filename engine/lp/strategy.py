"""Pure LP strategy math — no web3 imports, no adapter state."""

import math
from decimal import Decimal

import structlog

from engine.config import DexParams
from engine.venues.dex.shared import price_to_tick, _Q96

logger = structlog.get_logger()


def compute_ewma_stats(prices: list[Decimal], params: DexParams) -> tuple[float, float]:
    """Return (ewma_mean, std_dev) from price history using configured lambda.

    Caller is responsible for applying lookback_points slicing before calling.
    """
    float_prices = [float(p) for p in prices]
    lam = float(params.ewma_lambda)
    mean = float_prices[0]
    var = 0.0
    for x in float_prices[1:]:
        delta = x - mean
        mean = lam * mean + (1 - lam) * x
        var = lam * var + (1 - lam) * delta * delta
    return mean, math.sqrt(var)


def calculate_tick_range(
    prices: list[Decimal],
    params: DexParams,
    tick_spacing: int,
    token0_decimals: int,
    token1_decimals: int,
    invert_price: bool = False,
    recovery_price: float | None = None,
    venue_name: str = "",
) -> tuple[int, int]:
    """Calculate optimal tick range using EWMA SD-based strategy.

    When recovery_price is provided and causes a skew adjustment, params.downside_skew
    is updated in-place so the caller can persist the new value.
    """
    if params.lookback_points:
        prices = prices[-params.lookback_points:]

    if len(prices) < 2:
        raise ValueError("Insufficient price history for SD calculation")

    mean, std_dev = compute_ewma_stats(prices, params)

    if invert_price:
        if mean <= 0:
            raise ValueError("Cannot invert non-positive mean price")
        transformed_prices = [Decimal(1) / price for price in prices if price > 0]
        if len(transformed_prices) < 2:
            raise ValueError("Insufficient positive price history for inverted SD calculation")
        mean, std_dev = compute_ewma_stats(transformed_prices, params)

    multiplier = float(params.sd_multiplier)
    skew = float(params.downside_skew)
    if recovery_price is not None and std_dev > 0:
        if invert_price:
            if recovery_price <= 0:
                raise ValueError("Cannot invert non-positive recovery price")
            recovery_price = 1 / recovery_price
        deviation = (recovery_price - mean) / (std_dev * multiplier)
        skew = max(0.2, min(0.8, skew + deviation * 0.15))
        params.downside_skew = Decimal(str(round(skew, 4)))

    total = std_dev * multiplier * 2
    lower_price = max(mean - total * skew, 0.0001)
    upper_price = mean + total * (1 - skew)

    tick_lower = price_to_tick(Decimal(str(lower_price)), token0_decimals, token1_decimals)
    tick_upper = price_to_tick(Decimal(str(upper_price)), token0_decimals, token1_decimals)

    tick_lower = math.floor(tick_lower / tick_spacing) * tick_spacing
    tick_upper = math.ceil(tick_upper / tick_spacing) * tick_spacing

    tick_width = tick_upper - tick_lower
    if tick_width < params.min_tick_width:
        mid = (tick_lower + tick_upper) // 2
        tick_lower = mid - params.min_tick_width // 2
        tick_upper = mid + params.min_tick_width // 2
    elif tick_width > params.max_tick_width:
        mid = (tick_lower + tick_upper) // 2
        tick_lower = mid - params.max_tick_width // 2
        tick_upper = mid + params.max_tick_width // 2

    tick_lower = math.floor(tick_lower / tick_spacing) * tick_spacing
    tick_upper = math.ceil(tick_upper / tick_spacing) * tick_spacing

    logger.info(
        "calculated_tick_range",
        venue=venue_name,
        mean_price=mean,
        std_dev=std_dev,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
    )

    return tick_lower, tick_upper
