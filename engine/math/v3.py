"""Pure Uniswap V3 / CL math — no web3 dependency."""

import math
from decimal import Decimal


def sqrt_price_x96_to_decimal(
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
) -> Decimal:
    """Convert sqrtPriceX96 → human-readable token0-per-token1 price."""
    price = (Decimal(sqrt_price_x96) / Decimal(2**96)) ** 2
    price *= Decimal(10 ** (token0_decimals - token1_decimals))
    return price


def tick_to_price(tick: int, token0_decimals: int, token1_decimals: int) -> Decimal:
    """tick → human-readable price (token1 per token0)."""
    # Use float pow for speed (Decimal ** large_int is extremely slow for |tick| > 1000)
    raw = math.pow(1.0001, tick) * (10 ** (token0_decimals - token1_decimals))
    return Decimal(str(raw))


def price_to_tick(price: Decimal, token0_decimals: int, token1_decimals: int) -> int:
    """Human-readable price → nearest tick (rounds toward zero)."""
    adjusted = float(price) / (10 ** (token0_decimals - token1_decimals))
    return int(math.log(adjusted) / math.log(1.0001))


def tick_to_sqrt_price(tick: int) -> float:
    """tick → √P  (whitepaper eq 6.2: √(1.0001^tick))."""
    return 1.0001 ** (tick / 2)


def align_tick(tick: int, tick_spacing: int, direction: str = "down") -> int:
    """Snap *tick* to the nearest initialised tick boundary.

    direction='down' rounds toward -∞, 'up' rounds toward +∞.
    """
    if direction == "down":
        return (tick // tick_spacing) * tick_spacing
    return ((tick // tick_spacing) + 1) * tick_spacing


def constrain_tick_width(
    tick_lower: int,
    tick_upper: int,
    min_width: int,
    max_width: int,
    tick_spacing: int,
) -> tuple[int, int]:
    """Clamp width to [min_width, max_width] then re-align."""
    width = tick_upper - tick_lower
    if width < min_width:
        mid = (tick_lower + tick_upper) // 2
        tick_lower = mid - min_width // 2
        tick_upper = mid + min_width // 2
    elif width > max_width:
        mid = (tick_lower + tick_upper) // 2
        tick_lower = mid - max_width // 2
        tick_upper = mid + max_width // 2
    tick_lower = align_tick(tick_lower, tick_spacing, "down")
    tick_upper = align_tick(tick_upper, tick_spacing, "up")
    return tick_lower, tick_upper


def compute_swap_step(
    sqrt_price: float,
    liquidity: int,
    amount_in: float,
    fee_rate: float,
    zero_for_one: bool,
) -> tuple[float, float, float, float]:
    """Single-tick-range exact swap (whitepaper §6.2.3).

    Returns (sqrt_price_next, amount_in_consumed, amount_out, fee_amount).
    """
    if liquidity == 0 or amount_in <= 0:
        return sqrt_price, 0.0, 0.0, 0.0

    fee_amount = amount_in * fee_rate
    effective = amount_in - fee_amount

    if zero_for_one:
        # Sell token0 → get token1, price goes DOWN (√P decreases)
        sqrt_price_next = sqrt_price * liquidity / (liquidity + effective * sqrt_price)
        amount_out = liquidity * (sqrt_price - sqrt_price_next)
    else:
        # Sell token1 → get token0, price goes UP (√P increases)
        sqrt_price_next = sqrt_price + effective / liquidity
        amount_out = liquidity * (1.0 / sqrt_price - 1.0 / sqrt_price_next)

    return sqrt_price_next, amount_in, amount_out, fee_amount
