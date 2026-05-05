"""Pure LP strategy math — no web3 imports, no adapter state."""

import math
from decimal import Decimal

import structlog

from engine.config import DexParams
from engine.venues.dex.shared import price_to_tick

logger = structlog.get_logger()


def compute_ewma_stats(prices: list[Decimal], params: DexParams) -> tuple[float, float]:
    """Return (ewma_mean, std_dev) from price history.

    mean    — EWMA of price levels (used as range center, in price units).
    std_dev — EWMA std dev of log-returns (tick-aligned; floored at 3 bps).

    Log-returns make std_dev consistent with Uniswap tick space (tick = log(price) / log(1.0001))
    and direction-agnostic: inverting a price series negates all log-returns, leaving variance unchanged.
    Caller is responsible for applying lookback_points slicing before calling.
    """
    float_prices = [float(p) for p in prices]
    lam = float(params.ewma_lambda)
    mean = float_prices[0]
    var = 0.0
    for i in range(1, len(float_prices)):
        x = float_prices[i]
        prev = float_prices[i - 1]
        if prev <= 0 or x <= 0:
            raise ValueError(f"Non-positive price in log-return computation: prev={prev}, x={x}")
        r = math.log(x / prev)
        mean = lam * mean + (1 - lam) * x
        var = lam * var + (1 - lam) * r * r
    std_dev = max(math.sqrt(var), 3e-4)  # floor: ~3 bps minimum, prevents range collapse in quiet markets
    return mean, std_dev


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
        mean = 1.0 / mean  # invert the mean; std_dev is unchanged (log-return variance is direction-agnostic)

    multiplier = float(params.sd_multiplier)
    skew = float(params.downside_skew)
    range_width = std_dev * multiplier * 2
    if recovery_price is not None and std_dev > 0:
        if invert_price:
            if recovery_price <= 0:
                raise ValueError("Cannot invert non-positive recovery price")
            recovery_price = 1.0 / recovery_price
        # deviation: log-ratio of recovery_price to current mean, normalized by σ
        deviation = 2.0 * math.log(recovery_price / mean) / range_width if mean > 0 else 0.0
        skew = max(0.2, min(0.8, skew + deviation * 0.15))
        params.downside_skew = Decimal(str(round(skew, 4)))

    lower_price = max(mean * math.exp(-range_width * skew), 0.0001)
    upper_price = mean * math.exp(range_width * (1 - skew))

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
