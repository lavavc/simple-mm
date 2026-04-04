"""Market-facing scheduler jobs."""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, cast

import structlog

from engine.market.venue_prices import VenuePrice
from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import SchedulerState, SyncOrderLadderVenueProtocol

logger = structlog.get_logger()


class MarketJobs:
    def __init__(
        self,
        context: SchedulerContext,
        state: SchedulerState,
        *,
        schedule_dex_bootstrap: Callable[[], None],
    ) -> None:
        self.context = context
        self.state = state
        self._schedule_dex_bootstrap = schedule_dex_bootstrap

    async def update_gas_oracle(self) -> None:
        from engine.market import gas_oracle

        try:
            await gas_oracle.update()
            self._schedule_dex_bootstrap()
        except RuntimeError as exc:
            logger.error("gas_oracle_update_failed", error=str(exc))
            self.context.broadcast(
                {
                    "type": "alert",
                    "severity": "critical",
                    "message": f"Gas oracle fetch failed — trading blocked until prices recover. ({exc})",
                }
            )

    async def update_price(self) -> None:
        try:
            venue_prices = await self.context.price_aggregator.fetch_all()
            prices_data: list[dict[str, object]] = []
            for price in venue_prices.values():
                prices_data.append(
                    {
                        "venue": price.venue,
                        "pair": price.pair,
                        "quote": price.quote.model_dump() if price.quote else None,
                        "error": price.error,
                        "age_seconds": price.age_seconds,
                    }
                )
                if price.quote:
                    await self.context.price_store.insert_price_snapshot(price.quote)

            self.context.broadcast({"type": "venue_prices", "data": prices_data})
            valid_count = sum(1 for price in venue_prices.values() if price.is_valid)
            logger.debug("venue_prices_updated", total=len(venue_prices), valid=valid_count)
        except Exception as exc:
            logger.error("price_update_failed", error=str(exc))
            self.context.broadcast(
                {
                    "type": "alert",
                    "severity": "warning",
                    "message": f"Price fetch error: {exc}",
                }
            )

    async def sync_cex_orders(self) -> None:
        if not self.state.trading_enabled:
            return

        quidax_lp = self.context.quidax_lp or self.context.venues.get("quidax-lp")
        if not quidax_lp or quidax_lp.paused:
            return

        try:
            reference_price = await self.get_reference_price_ngn()
            if reference_price:
                await cast(SyncOrderLadderVenueProtocol, quidax_lp).sync_order_ladder(reference_price)
        except Exception as exc:
            logger.error("cex_sync_failed", error=str(exc))

    async def get_reference_price_ngn(self) -> Decimal | None:
        if self.context.blended_calculator:
            try:
                blended = await self.context.blended_calculator.get_blended_price()
                if blended.vwap > 0:
                    return blended.reference_price_ngn
            except Exception as exc:
                logger.warning("blended_reference_fallback", error=str(exc))

        bybit = self.context.price_aggregator.get_price("bybit")
        if bybit and bybit.quote and bybit.quote.mid > 0:
            return bybit.quote.mid

        quidax = self.context.price_aggregator.get_price("quidax")
        if quidax and quidax.quote and quidax.quote.mid > 0:
            return Decimal("1") / quidax.quote.mid

        return None

    async def sync_blockradar_rates(self) -> None:
        from engine.venues.wallet.blockradar import BlockradarAdapter, _ROUTES

        blockradar = self.context.venues.get("blockradar")
        if not isinstance(blockradar, BlockradarAdapter) or not blockradar._current_rates_usd:
            return
        if not self.context.blended_calculator:
            return

        blended = await self.context.blended_calculator.get_blended_price()
        fair = blended.vwap
        if fair <= 0:
            return

        lower = fair * Decimal("1.0030")
        upper = fair * Decimal("1.0050")
        for route in _ROUTES:
            current_usd = blockradar._current_rates_usd.get(route.key)
            if not current_usd or not (lower <= current_usd <= upper):
                target_raw = Decimal("1") / lower if route.invert else lower
                await blockradar.set_rate(route, target_raw)
