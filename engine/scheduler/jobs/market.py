"""Market-facing scheduler jobs."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Callable, cast

import structlog

from engine.types import CexAnchorSource
from engine.market.venue_prices import VenuePrice
from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import SchedulerState
from engine.venues.base import SyncOrderLadderVenue

logger = structlog.get_logger()

DEX_MM_ANCHOR_VENUES = frozenset({"uni-base", "uni-bsc"})


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
        self._cex_sync_lock = asyncio.Lock()

    async def update_gas_oracle(self) -> None:
        from engine.market import gas_oracle

        try:
            await gas_oracle.update()
            self._schedule_dex_bootstrap()
        except RuntimeError as exc:
            logger.error("gas_oracle_update_failed", error=str(exc))
            if gas_oracle.gas_usd_base() is None or gas_oracle.gas_usd_bsc() is None:
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
            await self.sync_cex_orders()
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

        if self._cex_sync_lock.locked():
            logger.debug("cex_sync_skipped_lock_held")
            return

        async with self._cex_sync_lock:
            quidax_mm = self.context.venues.get("quidax-lp")
            if not quidax_mm or quidax_mm.paused:
                return

            try:
                anchor_source: CexAnchorSource = getattr(getattr(quidax_mm, "params", None), "anchor_source", "blended")
                reference_price = await self.get_reference_price_ngn(anchor_source=anchor_source)
                if reference_price:
                    await cast(SyncOrderLadderVenue, quidax_mm).sync_order_ladder(reference_price)
            except Exception as exc:
                logger.error("cex_sync_failed", error=str(exc))

    async def get_reference_price_ngn(
        self,
        anchor_source: CexAnchorSource = "blended",
    ) -> Decimal | None:
        if anchor_source == "blended":
            return await self._get_blended_reference_price_ngn()
        if anchor_source == "dex_vwap":
            return await self._get_dex_vwap_reference_price_ngn()
        if anchor_source == "quidax":
            return await self._get_quidax_reference_price_ngn()

        logger.warning("unknown_cex_anchor_source", anchor_source=anchor_source)
        return None

    async def _get_blended_reference_price_ngn(self) -> Decimal | None:
        if not self.context.blended_calculator:
            return None
        try:
            blended = await self.context.blended_calculator.get_blended_price()
        except Exception as exc:
            logger.warning("blended_reference_unavailable", error=str(exc))
            return None
        if blended.reference_price_ngn > 0:
            return blended.reference_price_ngn
        return None

    async def _get_dex_vwap_reference_price_ngn(self) -> Decimal | None:
        if not self.context.blended_calculator:
            return None

        prices = await self._get_cached_or_live_prices()
        normalized = self.context.blended_calculator.normalizer.normalize(prices)
        dex_normalized = {
            venue: price
            for venue, price in normalized.items()
            if venue in DEX_MM_ANCHOR_VENUES
        }
        if not dex_normalized:
            logger.warning("dex_vwap_anchor_unavailable")
            return None

        vwap = self.context.blended_calculator.compute_vwap(dex_normalized)
        if vwap > 0:
            return Decimal("1") / vwap
        return None

    async def _get_quidax_reference_price_ngn(self) -> Decimal | None:
        prices = await self._get_cached_or_live_prices()
        quidax = prices.get("quidax")
        if quidax and quidax.quote and quidax.quote.mid > 0:
            return Decimal("1") / quidax.quote.mid
        logger.warning("quidax_anchor_unavailable")
        return None

    async def _get_cached_or_live_prices(self) -> dict[str, VenuePrice]:
        prices = self.context.price_aggregator.get_all_prices()
        if prices:
            return prices
        return await self.context.price_aggregator.fetch_all()

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
