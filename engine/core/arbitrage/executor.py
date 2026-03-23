"""Arbitrage execution across DEX and CEX venues."""

import time
from decimal import Decimal
from typing import Optional

import structlog

from engine.api.schemas import ArbitrageTrade
from engine.venues.base import VenueAdapter
from engine.venues.cex.quidax import QuidaxAdapter

logger = structlog.get_logger()

# Slippage tolerance applied to arb swaps (separate from LP slippage)
_ARB_SLIPPAGE_BPS = 10  # 0.1% — matches optimizer assumption in cex_dex.py / dex_dex.py


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dex_execution_slippage(venue: VenueAdapter) -> Decimal:
    params = getattr(venue, "params", None)
    max_slippage_percent = getattr(params, "max_slippage_percent", None)
    if max_slippage_percent is not None:
        return Decimal(max_slippage_percent) / Decimal(100)
    return Decimal(_ARB_SLIPPAGE_BPS) / Decimal(10000)


def _is_dex_execution_venue(venue: VenueAdapter) -> bool:
    return all(
        hasattr(venue, attr)
        for attr in ("swap", "stable_address", "cngn_address", "stable_decimals", "cngn_decimals")
    )


class ArbitrageExecutor:
    """Executes individual DEX and CEX trade legs for the arbitrage engine."""

    def __init__(self, venues: dict[str, VenueAdapter]):
        self.venues = venues

    async def execute_dex_buy(
        self,
        venue_name: str,
        amount_usd: Decimal,
        opportunity_id: str = "",
    ) -> Optional[ArbitrageTrade]:
        """Swap stablecoin → cNGN on a DEX."""
        venue = self.venues[venue_name]

        price_quote = await venue.get_current_price()
        if price_quote is None or price_quote.mid == 0:
            return ArbitrageTrade(
                id=0, opportunity_id=opportunity_id, venue=venue_name, side="buy",
                amount=Decimal("0"), status="failed", timestamp=_now_ms(),
                error="Could not fetch DEX price",
            )

        current_price = price_quote.mid  # stablecoin per cNGN
        amount_in_raw = int(amount_usd * Decimal(10 ** venue.stable_decimals))

        slippage = _dex_execution_slippage(venue)
        expected_cngn = amount_usd / current_price
        min_out_raw = int(expected_cngn * (1 - slippage) * Decimal(10 ** venue.cngn_decimals))

        result = await venue.swap(venue.stable_address, amount_in_raw, min_out_raw)

        actual_cngn = (
            Decimal(result.output_raw) / Decimal(10 ** venue.cngn_decimals)
            if result.output_raw
            else expected_cngn
        )

        return ArbitrageTrade(
            id=0,
            opportunity_id=opportunity_id,
            venue=venue_name,
            side="buy",
            amount=actual_cngn,
            price=current_price,
            tx_hash=result.hash or None,
            status="confirmed" if result.status == "confirmed" else "failed",
            timestamp=_now_ms(),
            error=result.error,
        )

    async def execute_dex_sell(
        self,
        venue_name: str,
        amount_cngn: Decimal,
        min_amount_out_usd: Decimal,
        opportunity_id: str = "",
    ) -> Optional[ArbitrageTrade]:
        """Swap cNGN → stablecoin on a DEX."""
        venue = self.venues[venue_name]

        price_quote = await venue.get_current_price()
        current_price = price_quote.mid if price_quote else Decimal("0")

        amount_in_raw = int(amount_cngn * Decimal(10 ** venue.cngn_decimals))
        min_out_raw = int(min_amount_out_usd * Decimal(10 ** venue.stable_decimals))

        result = await venue.swap(venue.cngn_address, amount_in_raw, min_out_raw)

        return ArbitrageTrade(
            id=0,
            opportunity_id=opportunity_id,
            venue=venue_name,
            side="sell",
            amount=amount_cngn,
            price=current_price,
            tx_hash=result.hash or None,
            status="confirmed" if result.status == "confirmed" else "failed",
            timestamp=_now_ms(),
            error=result.error,
        )

    async def execute_cex_buy(
        self,
        venue_name: str,
        amount_usd: Decimal,
        limit_price: Decimal,
        opportunity_id: str = "",
    ) -> Optional[ArbitrageTrade]:
        """Place a market buy order on a CEX."""
        venue: QuidaxAdapter = self.venues[venue_name]  # type: ignore

        amount_cngn = amount_usd / limit_price
        success, executed_cngn, avg_price, error = await venue.place_market_order("buy", amount_cngn)
        return ArbitrageTrade(
            id=0,
            opportunity_id=opportunity_id,
            venue=venue_name,
            side="buy",
            amount=executed_cngn if success else amount_cngn,
            price=avg_price if success else limit_price,
            status="submitted" if success else "failed",
            timestamp=_now_ms(),
            error=error,
        )

    async def execute_cex_sell(
        self,
        venue_name: str,
        amount_cngn: Decimal,
        limit_price: Decimal,
        opportunity_id: str = "",
    ) -> Optional[ArbitrageTrade]:
        """Place a market sell order on a CEX."""
        venue: QuidaxAdapter = self.venues[venue_name]  # type: ignore

        success, executed_cngn, avg_price, error = await venue.place_market_order("sell", amount_cngn)
        return ArbitrageTrade(
            id=0,
            opportunity_id=opportunity_id,
            venue=venue_name,
            side="sell",
            amount=executed_cngn if success else amount_cngn,
            price=avg_price if success else limit_price,
            status="submitted" if success else "failed",
            timestamp=_now_ms(),
            error=error,
        )
