"""Arbitrage execution across DEX and CEX venues."""

import time
from decimal import Decimal
from typing import Optional

import structlog

from engine.api.schemas import ArbitrageOpportunity, ArbitrageTrade
from engine.venues.base import VenueAdapter
from engine.venues.dex.base import BaseDexAdapter
from engine.venues.cex.quidax import QuidaxAdapter

logger = structlog.get_logger()

# Slippage tolerance applied to arb swaps (separate from LP slippage)
_ARB_SLIPPAGE_BPS = 10  # 0.1% — matches optimizer assumption in cex_dex.py / dex_dex.py


def _now_ms() -> int:
    return int(time.time() * 1000)


class ArbitrageExecutor:
    """Executes arbitrage trades across DEX and CEX venues."""

    def __init__(self, venues: dict[str, VenueAdapter], execution_enabled: bool = False):
        self.venues = venues
        self.execution_enabled = execution_enabled

    async def execute(
        self, opportunity: ArbitrageOpportunity
    ) -> tuple[bool, Optional[Decimal], Optional[str]]:
        """Returns (success, actual_profit_usd, error_message)."""
        if not self.execution_enabled:
            logger.info(
                "arbitrage_execution_skipped",
                opportunity_id=opportunity.id,
                expected_profit=float(opportunity.expected_profit_usd),
            )
            return False, None, "Execution disabled (detection-only mode)"

        buy_venue = self.venues.get(opportunity.buy_venue)
        sell_venue = self.venues.get(opportunity.sell_venue)

        if buy_venue is None or sell_venue is None:
            missing = opportunity.buy_venue if buy_venue is None else opportunity.sell_venue
            return False, None, f"Unknown venue: {missing}"

        size_usd = opportunity.recommended_size_usd
        opp_id = opportunity.id

        try:
            # --- Buy leg ---
            if isinstance(buy_venue, BaseDexAdapter):
                buy_trade = await self.execute_dex_buy(opportunity.buy_venue, size_usd, opp_id)
            else:
                buy_trade = await self.execute_cex_buy(opportunity.buy_venue, size_usd, opportunity.buy_price, opp_id)

            if buy_trade is None or buy_trade.status == "failed":
                err = (buy_trade.error if buy_trade else None) or "unknown"
                return False, None, f"Buy leg failed: {err}"

            # --- Sell leg ---
            amount_cngn = buy_trade.amount
            slippage = Decimal(_ARB_SLIPPAGE_BPS) / Decimal(10000)
            min_out_usd = size_usd * (1 - slippage)

            if isinstance(sell_venue, BaseDexAdapter):
                sell_trade = await self.execute_dex_sell(opportunity.sell_venue, amount_cngn, min_out_usd, opp_id)
            else:
                sell_trade = await self.execute_cex_sell(opportunity.sell_venue, amount_cngn, opportunity.sell_price, opp_id)

            if sell_trade is None or sell_trade.status == "failed":
                err = (sell_trade.error if sell_trade else None) or "unknown"
                buy_tx = buy_trade.tx_hash or ""
                return False, None, f"HALF_OPEN:{buy_tx}:{err}"

            # Use actual fill prices when available (CEX legs populate trade.price from response)
            buy_price_usd = buy_trade.price or opportunity.buy_price
            sell_price_usd = sell_trade.price or opportunity.sell_price
            actual_profit = (sell_trade.amount * sell_price_usd) - (buy_trade.amount * buy_price_usd)

            logger.info(
                "arbitrage_executed",
                opportunity_id=opp_id,
                buy_venue=opportunity.buy_venue,
                sell_venue=opportunity.sell_venue,
                size_usd=float(size_usd),
                amount_cngn=float(amount_cngn),
                actual_profit=float(actual_profit),
            )

            return True, actual_profit, None

        except Exception as e:
            logger.error("arbitrage_execution_failed", opportunity_id=opp_id, error=str(e))
            return False, None, str(e)

    async def execute_dex_buy(
        self,
        venue_name: str,
        amount_usd: Decimal,
        opportunity_id: str = "",
    ) -> Optional[ArbitrageTrade]:
        """Swap stablecoin → cNGN on a DEX."""
        venue: BaseDexAdapter = self.venues[venue_name]  # type: ignore

        price_quote = await venue.get_current_price()
        if price_quote is None or price_quote.mid == 0:
            return ArbitrageTrade(
                id=0, opportunity_id=opportunity_id, venue=venue_name, side="buy",
                amount=Decimal("0"), status="failed", timestamp=_now_ms(),
                error="Could not fetch DEX price",
            )

        current_price = price_quote.mid  # stablecoin per cNGN
        amount_in_raw = int(amount_usd * Decimal(10 ** venue.stable_decimals))

        slippage = Decimal(_ARB_SLIPPAGE_BPS) / Decimal(10000)
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
        venue: BaseDexAdapter = self.venues[venue_name]  # type: ignore

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
