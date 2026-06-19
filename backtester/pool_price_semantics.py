"""Canonical price semantics for exported V4 pool-history rows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal, Mapping

from backtester.clmm_math import sqrt_price_x96_to_native_price


StoredPriceModel = Literal["sqrt_mid", "swap_amount_ratio", "unexplained"]

PRICE_REL_TOLERANCE = Decimal("1e-9")
PRICE_ABS_TOLERANCE = Decimal("1e-12")


@dataclass(frozen=True)
class PoolPriceSemantics:
    raw_sqrt_mid: Decimal
    stored_cngn_usd_price: Decimal
    amount_ratio_price: Decimal | None
    stored_price_model: StoredPriceModel


def classify_pool_price_row(row: Mapping[str, str]) -> PoolPriceSemantics:
    raw_sqrt_mid = raw_sqrt_mid_from_row(row)
    stored_price = Decimal(str(row["cngn_usd_price"]))
    amount_ratio = swap_amount_ratio_price(row)

    if decimal_prices_match(stored_price, raw_sqrt_mid):
        model: StoredPriceModel = "sqrt_mid"
    elif amount_ratio is not None and decimal_prices_match(stored_price, amount_ratio):
        model = "swap_amount_ratio"
    else:
        model = "unexplained"

    return PoolPriceSemantics(
        raw_sqrt_mid=raw_sqrt_mid,
        stored_cngn_usd_price=stored_price,
        amount_ratio_price=amount_ratio,
        stored_price_model=model,
    )


def raw_sqrt_mid_from_row(row: Mapping[str, str]) -> Decimal:
    sqrt_price_x96 = int(row["sqrt_price_x96"])
    if sqrt_price_x96 <= 0:
        raise ValueError("sqrt_price_x96 must be positive")

    chain = row["chain"].strip()
    token0_symbol = row["token0_symbol"].strip()
    token1_symbol = row["token1_symbol"].strip()
    token0_decimals = token_decimals(token0_symbol, chain)
    token1_decimals = token_decimals(token1_symbol, chain)
    native = sqrt_price_x96_to_native_price(
        sqrt_price_x96,
        token0_decimals,
        token1_decimals,
    )
    if native <= 0:
        raise ValueError("sqrt-derived mid must be positive")
    if token1_symbol.upper() == "CNGN":
        with localcontext() as context:
            context.prec = 60
            return Decimal("1") / native
    return native


def swap_amount_ratio_price(row: Mapping[str, str]) -> Decimal | None:
    if row["event_type"].strip().lower() != "swap":
        return None

    amount0 = abs(Decimal(str(row["amount0"])))
    amount1 = abs(Decimal(str(row["amount1"])))
    token0_symbol = row["token0_symbol"].strip().upper()
    token1_symbol = row["token1_symbol"].strip().upper()
    if token0_symbol == "CNGN":
        stable_amount = amount1
        cngn_amount = amount0
    elif token1_symbol == "CNGN":
        stable_amount = amount0
        cngn_amount = amount1
    else:
        raise ValueError("Cannot derive swap amount ratio without cNGN token")

    if stable_amount <= 0 or cngn_amount <= 0:
        return None
    with localcontext() as context:
        context.prec = 60
        return stable_amount / cngn_amount


def fee_adjusted_bid_ask(mid: Decimal, fee_rate: Decimal) -> tuple[Decimal, Decimal]:
    fee_multiplier = Decimal("1") - fee_rate
    if fee_multiplier <= 0 or fee_rate < 0:
        raise ValueError("fee_rate must be at least 0 and less than 1")
    with localcontext() as context:
        context.prec = 60
        return mid * fee_multiplier, mid / fee_multiplier


def decimal_prices_match(left: Decimal, right: Decimal) -> bool:
    difference = abs(left - right)
    return difference <= max(
        PRICE_ABS_TOLERANCE,
        PRICE_REL_TOLERANCE * max(abs(left), abs(right)),
    )


def token_decimals(symbol: str, chain: str) -> int:
    normalized_symbol = symbol.strip().upper()
    normalized_chain = chain.strip().lower()
    if normalized_symbol in {"CNGN", "USDC"}:
        return 6
    if normalized_symbol == "USDT":
        return 18 if normalized_chain in {"bsc", "bnb"} else 6
    raise ValueError(f"Cannot infer decimals for token symbol {symbol!r} on chain {chain!r}")
