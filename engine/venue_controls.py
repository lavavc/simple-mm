"""Shared venue control helpers used by operator-facing flows."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol, cast

import structlog

from engine.runtime import EngineRuntime
from engine.venues.base import SyncOrderLadderVenue, VenueAdapter

logger = structlog.get_logger()

SyncVenueOutcome = Literal["sync_triggered", "position_refreshed"]


class CancellableVenue(Protocol):
    paused: bool

    async def cancel_all_orders(self) -> int: ...


def get_runtime_venue(runtime: EngineRuntime, venue_name: str) -> VenueAdapter:
    venue = runtime.venues.get(venue_name)
    if venue is None:
        raise ValueError(f"Venue not found: {venue_name}")
    return venue


async def _get_reference_price_ngn(runtime: EngineRuntime) -> Decimal | None:
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


async def sync_venue_now(runtime: EngineRuntime, venue_name: str) -> SyncVenueOutcome:
    venue = get_runtime_venue(runtime, venue_name)
    anchor_source = getattr(getattr(venue, "params", None), "anchor_source", "blended")
    market_jobs = getattr(runtime.scheduler, "market_jobs", None)
    get_anchor_reference_price = getattr(market_jobs, "get_reference_price_ngn", None)
    if callable(get_anchor_reference_price):
        ref_price = await get_anchor_reference_price(anchor_source=anchor_source)
    else:
        ref_price = await _get_reference_price_ngn(runtime)

    sync_order_ladder = getattr(venue, "sync_order_ladder", None)
    should_sync_ladder = venue_name != "quidax" or "quidax-lp" not in runtime.venues
    if should_sync_ladder and callable(sync_order_ladder) and ref_price:
        await cast(SyncOrderLadderVenue, venue).sync_order_ladder(ref_price)
        return "sync_triggered"

    await venue.get_position()
    return "position_refreshed"


async def pause_venue_now(
    runtime: EngineRuntime,
    venue_name: str,
    *,
    set_paused: bool,
) -> int | None:
    venue = get_runtime_venue(runtime, venue_name)
    if set_paused:
        venue.paused = True
        await runtime.db.system_state.set_system_state(f"venue_paused:{venue_name}", "true")

    cancel_all_orders = getattr(venue, "cancel_all_orders", None)
    if callable(cancel_all_orders):
        return await cast(CancellableVenue, venue).cancel_all_orders()
    return None


async def resume_venue_now(
    runtime: EngineRuntime,
    venue_name: str,
) -> tuple[SyncVenueOutcome | None, str | None]:
    venue = get_runtime_venue(runtime, venue_name)
    venue.paused = False
    await runtime.db.system_state.set_system_state(f"venue_paused:{venue_name}", "false")
    if not runtime.scheduler.trading_enabled:
        return None, "trading_paused"

    try:
        sync_outcome = await sync_venue_now(runtime, venue_name)
        return sync_outcome, None
    except Exception as exc:
        return None, str(exc)
