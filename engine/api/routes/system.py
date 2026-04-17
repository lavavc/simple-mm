"""System and control routes."""

from __future__ import annotations

import time
from decimal import Decimal
import json
from typing import Any, cast

from fastapi import APIRouter, Depends
from engine.api.deps import get_runtime, require_scheduler, verify_token
from engine.api.schemas import SystemStatus, VenuePriceResponse, VenueStatus
from engine.runtime import EngineRuntime
from engine.scheduler import TradingScheduler
from engine.types import CexAnchorSource
import structlog

logger = structlog.get_logger()
router = APIRouter()


async def _get_anchor_price_ngn(
    runtime: EngineRuntime,
    anchor_source: CexAnchorSource,
) -> Decimal | None:
    market_jobs = getattr(runtime.scheduler, "market_jobs", None)
    get_reference_price_ngn = getattr(market_jobs, "get_reference_price_ngn", None)
    if not callable(get_reference_price_ngn):
        return None

    try:
        anchor_price = await get_reference_price_ngn(anchor_source=anchor_source)
    except Exception as exc:
        logger.warning(
            "venue_anchor_price_unavailable",
            anchor_source=anchor_source,
            error=str(exc),
        )
        return None

    return anchor_price if anchor_price and anchor_price > 0 else None


async def _get_last_ladder_anchor_price_ngn(runtime: EngineRuntime, venue_name: str) -> Decimal | None:
    try:
        raw = await runtime.db.system_state.get_system_state(f"{venue_name}:last_ladder_anchor_price_ngn")
    except Exception as exc:
        logger.warning(
            "venue_last_ladder_anchor_unavailable",
            venue=venue_name,
            error=str(exc),
        )
        return None

    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw

    candidate: Any
    if isinstance(payload, dict):
        candidate = payload.get("reference_price_ngn") or payload.get("price") or payload.get("anchor_price_ngn")
    else:
        candidate = payload

    try:
        value = Decimal(str(candidate))
    except Exception:
        return None
    return value if value > 0 else None


@router.get("/status", response_model=SystemStatus)
async def get_status(runtime: EngineRuntime = Depends(get_runtime)) -> SystemStatus:
    trading_enabled = await runtime.db.system_state.get_system_state("trading_enabled")
    venue_prices = runtime.price_aggregator.get_all_prices() if runtime.price_aggregator else {}

    venue_statuses: list[VenueStatus] = []
    anchor_price_cache: dict[CexAnchorSource, Decimal | None] = {}
    for name, venue in runtime.venues.items():
        lp_manager = runtime.lp_managers.get(name)
        try:
            if lp_manager is not None:
                position = await lp_manager.get_position_as_schema()
            else:
                position = await venue.get_position()
        except Exception:
            position = None

        price_data = venue_prices.get(name)
        price_response = None
        if price_data:
            price_response = VenuePriceResponse(
                venue=price_data.venue,
                pair=price_data.pair,
                quote=price_data.quote,
                error=price_data.error,
                age_seconds=price_data.age_seconds,
            )

        if lp_manager is not None:
            params = lp_manager.params.model_dump()
        elif hasattr(venue, "params") and venue.params:
            params = venue.params.model_dump()
        else:
            params = None

        anchor_price_ngn = None
        anchor_source = cast(CexAnchorSource | None, params.get("anchor_source") if params else None)
        if anchor_source is not None:
            if anchor_source not in anchor_price_cache:
                anchor_price_cache[anchor_source] = await _get_anchor_price_ngn(runtime, anchor_source)
            anchor_price_ngn = anchor_price_cache[anchor_source]

        last_ladder_anchor_price_ngn = await _get_last_ladder_anchor_price_ngn(runtime, name)

        venue_statuses.append(
            VenueStatus(
                name=name,
                enabled=venue.enabled,
                paused=venue.paused,
                position=position,
                price=price_response,
                params=params,
                anchor_price_ngn=anchor_price_ngn,
                last_ladder_anchor_price_ngn=last_ladder_anchor_price_ngn,
            )
        )

    for name, price_data in venue_prices.items():
        if name not in runtime.venues:
            venue_statuses.append(
                VenueStatus(
                    name=name,
                    enabled=True,
                    paused=False,
                    position=None,
                    price=VenuePriceResponse(
                        venue=price_data.venue,
                        pair=price_data.pair,
                        quote=price_data.quote,
                        error=price_data.error,
                        age_seconds=price_data.age_seconds,
                    ),
                    anchor_price_ngn=None,
                    last_ladder_anchor_price_ngn=None,
                )
            )

    return SystemStatus(
        trading_enabled=trading_enabled != "false",
        uptime=int(time.time() - runtime.start_time),
        venues=venue_statuses,
        last_price_update=int(runtime.price_aggregator.last_fetch_time * 1000)
        if runtime.price_aggregator
        else None,
    )


@router.post("/trading/pause", dependencies=[Depends(verify_token)])
async def pause_trading(
    scheduler: TradingScheduler = Depends(require_scheduler),
) -> dict[str, str]:
    await scheduler.pause()
    return {"status": "paused"}


@router.post("/trading/resume", dependencies=[Depends(verify_token)])
async def resume_trading(
    scheduler: TradingScheduler = Depends(require_scheduler),
) -> dict[str, str]:
    await scheduler.resume()
    return {"status": "running"}


@router.post("/shutdown", dependencies=[Depends(verify_token)])
async def shutdown(
    unwind: bool = False,
    runtime: EngineRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    import asyncio
    import os
    import signal

    if unwind:
        await runtime.scheduler.pause()
        unwind_results = await runtime.scheduler.lp_rebalancer.unwind_all_positions(
            list(runtime.lp_managers.values()),
            triggered_by="api:shutdown_unwind",
        )
        for venue_name, removed in unwind_results.items():
            for item in removed:
                logger.info(
                    "shutdown_unwind_position",
                    venue=venue_name,
                    token_id=item["token_id"],
                    status=item["status"],
                )

        runtime.scheduler.broadcast(
            {
                "type": "alert",
                "severity": "warning",
                "message": "Engine shutting down — all LP positions unwound.",
            }
        )
        logger.info("shutdown_unwind_complete", results=unwind_results)
    else:
        runtime.scheduler.broadcast(
            {
                "type": "alert",
                "severity": "warning",
                "message": "Engine shutting down — LP positions left in place.",
            }
        )

    logger.info("shutdown_requested", unwind=unwind)
    asyncio.get_event_loop().call_later(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"status": "shutting_down", "unwind": unwind}


@router.get("/health")
async def health_check(runtime: EngineRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return {
        "status": "healthy",
        "timestamp": int(time.time() * 1000),
        "trading_enabled": runtime.scheduler.trading_enabled,
        "arbitrage_enabled": runtime.arbitrage_engine.enabled if runtime.arbitrage_engine else False,
    }
