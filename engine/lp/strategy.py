"""Pure LP strategy math — no web3 imports, no adapter state."""

import math
from decimal import Decimal

import structlog

from .config import DexParams

logger = structlog.get_logger()

_Q96 = 2 ** 96


def _tick_to_sqrt_x96(tick: int) -> float:
    """Return sqrtPriceX96 as a float for a given tick."""
    return math.exp(tick * math.log(1.0001) / 2) * _Q96


def tick_to_price(tick: int, token0_decimals: int, token1_decimals: int) -> Decimal:
    decimal_diff = token0_decimals - token1_decimals
    price = Decimal("1.0001") ** tick
    price *= Decimal(10 ** decimal_diff)
    return price


def price_to_tick(price: Decimal, token0_decimals: int, token1_decimals: int) -> int:
    decimal_diff = token0_decimals - token1_decimals
    adjusted = float(price) / (10 ** decimal_diff)
    return int(math.log(adjusted) / math.log(1.0001))


def compute_ewma_stats(prices: list[Decimal], params: DexParams) -> tuple[float, float]:
    """Return (ewma_mean, std_dev) from price history using configured lambda."""
    if params.lookback_points:
        prices = prices[-params.lookback_points:]
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
    recovery_price: float | None = None,
    venue_name: str = "",
) -> tuple[int, int]:
    """Calculate optimal tick range using EWMA SD-based strategy."""
    if params.lookback_points:
        prices = prices[-params.lookback_points:]

    if len(prices) < 2:
        raise ValueError("Insufficient price history for SD calculation")

    mean, std_dev = compute_ewma_stats(prices, params)

    multiplier = float(params.sd_multiplier)
    skew = float(params.downside_skew)
    if recovery_price is not None and std_dev > 0:
        deviation = (recovery_price - mean) / (std_dev * multiplier)
        skew = max(0.2, min(0.8, skew + deviation * 0.15))
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


def compute_required_ratio(
    tick_lower: int,
    tick_upper: int,
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
) -> tuple[Decimal, Decimal]:
    """Return (r0, r1) — token amounts per unit of liquidity at the current price."""
    sqrt_a = _tick_to_sqrt_x96(tick_lower)
    sqrt_b = _tick_to_sqrt_x96(tick_upper)
    sqrt_p = float(sqrt_price_x96)

    if sqrt_p <= sqrt_a:
        r0 = (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b) * _Q96 if sqrt_a * sqrt_b > 0 else 0.0
        r1 = 0.0
    elif sqrt_p >= sqrt_b:
        r0 = 0.0
        r1 = (sqrt_b - sqrt_a) / _Q96
    else:
        r0 = (sqrt_b - sqrt_p) / (sqrt_p * sqrt_b) * _Q96
        r1 = (sqrt_p - sqrt_a) / _Q96

    dec_adj = Decimal(10 ** token0_decimals) / Decimal(10 ** token1_decimals)
    r0_dec = Decimal(str(r0)) / dec_adj
    r1_dec = Decimal(str(r1))
    return r0_dec, r1_dec
