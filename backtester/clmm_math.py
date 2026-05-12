"""Concentrated-liquidity math helpers for backtests.

The simulator works in raw Uniswap liquidity units. Human-readable token
amounts are obtained only after converting the raw token amounts by decimals.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from engine.venues.dex.shared import _Q96, _tick_to_sqrt_price_x96


def sqrt_price_x96_to_native_price(
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
) -> Decimal:
    """Return human-readable token1/token0 price from pool sqrtPriceX96."""
    if sqrt_price_x96 <= 0:
        return Decimal("0")
    with localcontext() as ctx:
        ctx.prec = 80
        price = (Decimal(sqrt_price_x96) / Decimal(_Q96)) ** 2
        price *= Decimal(10) ** Decimal(token0_decimals - token1_decimals)
        return +price


def cngn_price_from_sqrt_price_x96(
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
    invert_price: bool,
) -> float:
    """Return cNGN/USD from pool sqrtPriceX96.

    For cNGN-token0 pools the native token1/token0 price is already cNGN/USD.
    For cNGN-token1 pools the native price is cNGN per stablecoin, so invert it.
    """
    native = sqrt_price_x96_to_native_price(sqrt_price_x96, token0_decimals, token1_decimals)
    if native <= 0:
        return 0.0
    price = Decimal(1) / native if invert_price else native
    return float(price)


def tick_to_sqrt_price_x96(tick: int) -> int:
    """Exact integer sqrtPriceX96 at the tick boundary."""
    return _tick_to_sqrt_price_x96(tick)


def sqrt_price_x96_to_float(sqrt_price_x96: int) -> float:
    """Convert Q64.96 sqrt price to an unscaled float sqrt ratio."""
    return float(Decimal(sqrt_price_x96) / Decimal(_Q96)) if sqrt_price_x96 > 0 else 0.0


def liquidity_amounts_raw(
    liquidity: float,
    tick_lower: int,
    tick_upper: int,
    sqrt_price_x96: int,
) -> tuple[float, float]:
    """Return raw token0/token1 amounts for liquidity at an exact pool price."""
    if liquidity <= 0 or tick_lower >= tick_upper or sqrt_price_x96 <= 0:
        return 0.0, 0.0

    sqrt_lower = tick_to_sqrt_price_x96(tick_lower)
    sqrt_upper = tick_to_sqrt_price_x96(tick_upper)
    sqrt_current = max(sqrt_lower, min(int(sqrt_price_x96), sqrt_upper))

    with localcontext() as ctx:
        ctx.prec = 80
        liquidity_d = Decimal(str(liquidity))
        q96 = Decimal(_Q96)
        sqrt_lower_d = Decimal(sqrt_lower)
        sqrt_upper_d = Decimal(sqrt_upper)
        sqrt_current_d = Decimal(sqrt_current)

        if sqrt_current <= sqrt_lower:
            amount0 = liquidity_d * q96 * (sqrt_upper_d - sqrt_lower_d) / (sqrt_lower_d * sqrt_upper_d)
            amount1 = Decimal(0)
        elif sqrt_current >= sqrt_upper:
            amount0 = Decimal(0)
            amount1 = liquidity_d * (sqrt_upper_d - sqrt_lower_d) / q96
        else:
            amount0 = liquidity_d * q96 * (sqrt_upper_d - sqrt_current_d) / (sqrt_current_d * sqrt_upper_d)
            amount1 = liquidity_d * (sqrt_current_d - sqrt_lower_d) / q96

    return max(float(amount0), 0.0), max(float(amount1), 0.0)
