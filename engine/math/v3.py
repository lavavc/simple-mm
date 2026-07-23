"""Pure Uniswap V3 / CL math — no web3 dependency."""

import math
from decimal import Decimal


_Q96 = 2**96
_MAX_TICK = 887272


def _tick_to_sqrt_price_x96(tick: int) -> int:
    """Exact integer TickMath matching Uniswap V4 TickMath.getSqrtPriceAtTick()."""
    abs_tick = abs(tick)
    if abs_tick > _MAX_TICK:
        raise ValueError(f"tick {tick} out of range")

    ratio = (
        0x100000000000000000000000000000000
        if (abs_tick & 0x1) == 0
        else 0xFFFcb933BD6FAD37AA2D162D1A594001
    )
    if abs_tick & 0x2:
        ratio = ratio * 0xFFF97272373D413259A46990580E213A >> 128
    if abs_tick & 0x4:
        ratio = ratio * 0xFFF2E50F5F656932EF12357CF3C7FDCC >> 128
    if abs_tick & 0x8:
        ratio = ratio * 0xFFE5CACA7E10E4E61C3624EAA0941CD0 >> 128
    if abs_tick & 0x10:
        ratio = ratio * 0xFFCB9843D60F6159C9DB58835C926644 >> 128
    if abs_tick & 0x20:
        ratio = ratio * 0xFF973B41FA98C081472E6896DFB254C0 >> 128
    if abs_tick & 0x40:
        ratio = ratio * 0xFF2EA16466C96A3843EC78B326B52861 >> 128
    if abs_tick & 0x80:
        ratio = ratio * 0xFE5DEE046A99A2A811C461F1969C3053 >> 128
    if abs_tick & 0x100:
        ratio = ratio * 0xFCBE86C7900A88AEDCFFC83B479AA3A4 >> 128
    if abs_tick & 0x200:
        ratio = ratio * 0xF987A7253AC413176F2B074CF7815E54 >> 128
    if abs_tick & 0x400:
        ratio = ratio * 0xF3392B0822B70005940C7A398E4B70F3 >> 128
    if abs_tick & 0x800:
        ratio = ratio * 0xE7159475A2C29B7443B29C7FA6E889D9 >> 128
    if abs_tick & 0x1000:
        ratio = ratio * 0xD097F3BDFD2022B8845AD8F792AA5825 >> 128
    if abs_tick & 0x2000:
        ratio = ratio * 0xA9F746462D870FDF8A65DC1F90E061E5 >> 128
    if abs_tick & 0x4000:
        ratio = ratio * 0x70D869A156D2A1B890BB3DF62BAF32F7 >> 128
    if abs_tick & 0x8000:
        ratio = ratio * 0x31BE135F97D08FD981231505542FCFA6 >> 128
    if abs_tick & 0x10000:
        ratio = ratio * 0x9AA508B5B7A84E1C677DE54F3E99BC9 >> 128
    if abs_tick & 0x20000:
        ratio = ratio * 0x5D6AF8DEDB81196699C329225EE604 >> 128
    if abs_tick & 0x40000:
        ratio = ratio * 0x2216E584F5FA1EA926041BEDFE98 >> 128
    if abs_tick & 0x80000:
        ratio = ratio * 0x48A170391F7DC42444E8FA2 >> 128

    if tick > 0:
        ratio = (2**256 - 1) // ratio

    remainder = ratio & ((1 << 32) - 1)
    return (ratio >> 32) + (1 if remainder else 0)


def sqrt_price_x96_to_decimal(
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
) -> Decimal:
    """Convert sqrtPriceX96 to a human-readable token1/token0 price."""
    price = (Decimal(sqrt_price_x96) / Decimal(_Q96)) ** 2
    price *= Decimal(10 ** (token0_decimals - token1_decimals))
    return price


def tick_to_price(tick: int, token0_decimals: int, token1_decimals: int) -> Decimal:
    """Convert a tick index to a human-readable token1/token0 price."""
    decimal_diff = token0_decimals - token1_decimals
    return Decimal("1.0001") ** tick * Decimal(10**decimal_diff)


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
    return -(-tick // tick_spacing) * tick_spacing


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
    liquidity: float,
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
