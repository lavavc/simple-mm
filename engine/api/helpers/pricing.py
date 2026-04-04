"""Shared pricing helpers for API routes."""

from __future__ import annotations

from decimal import Decimal

import structlog

from engine.runtime import EngineRuntime

logger = structlog.get_logger()


async def get_cngn_usd_rate(runtime: EngineRuntime) -> Decimal:
    """Get cNGN/USD rate, preferring blended history before live single-venue fallbacks."""
    if runtime.blended_calculator:
        try:
            blended = await runtime.blended_calculator.get_blended_price()
            for candidate in (blended.vwap, blended.twap_5m, blended.twap_1h):
                if candidate > 0:
                    return candidate
        except Exception as exc:
            logger.warning("blended_price_unavailable", error=str(exc))

    if runtime.price_aggregator:
        quidax = runtime.price_aggregator.get_price("quidax")
        if quidax and quidax.quote and quidax.quote.mid > 0:
            return quidax.quote.mid

        bybit = runtime.price_aggregator.get_price("bybit")
        if bybit and bybit.quote and bybit.quote.mid > 0:
            return Decimal("1") / bybit.quote.mid

    return Decimal("0")


async def get_reference_price_ngn(runtime: EngineRuntime) -> Decimal | None:
    """Get the best available USDT/NGN reference price for rate syncing."""
    if runtime.blended_calculator:
        try:
            blended = await runtime.blended_calculator.get_blended_price()
            if blended.reference_price_ngn > 0:
                return blended.reference_price_ngn
        except Exception as exc:
            logger.warning("blended_reference_price_unavailable", error=str(exc))

    if runtime.price_aggregator:
        bybit = runtime.price_aggregator.get_price("bybit")
        if bybit and bybit.quote and bybit.quote.mid > 0:
            return bybit.quote.mid

        quidax = runtime.price_aggregator.get_price("quidax")
        if quidax and quidax.quote and quidax.quote.mid > 0:
            return Decimal("1") / quidax.quote.mid

    return None
