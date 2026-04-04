"""Position routes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
import structlog

from engine.api.deps import get_runtime
from engine.api.helpers.pricing import get_cngn_usd_rate
from engine.api.schemas import GlobalPosition, Position
from engine.config import settings
from engine.runtime import EngineRuntime

logger = structlog.get_logger()
router = APIRouter()


@router.get("/positions")
async def get_all_positions(runtime: EngineRuntime = Depends(get_runtime)) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for name, venue in runtime.venues.items():
        try:
            pos = await venue.get_position()
            positions.append(pos.model_dump())
        except Exception as exc:
            logger.error("position_fetch_failed", venue=name, error=str(exc))
    return positions


@router.get("/positions/global", response_model=GlobalPosition)
async def get_global_position(runtime: EngineRuntime = Depends(get_runtime)) -> GlobalPosition:
    total_cngn = Decimal("0")
    total_usdt = Decimal("0")
    total_usdc = Decimal("0")

    for name, venue in runtime.venues.items():
        try:
            pos = await venue.get_position()
            total_cngn += pos.balances.get("cngn", Decimal("0"))
            total_usdt += pos.balances.get("usdt", Decimal("0"))
            total_usdc += pos.balances.get("usdc", Decimal("0"))
        except Exception as exc:
            logger.warning("position_fetch_failed_global", venue=name, error=str(exc))

    cngn_usd_rate = await get_cngn_usd_rate(runtime)
    if cngn_usd_rate > 0:
        cngn_usd_value = total_cngn * cngn_usd_rate
        total_usd_value = cngn_usd_value + total_usdt + total_usdc
    else:
        cngn_usd_value = Decimal("0")
        total_usd_value = total_usdt + total_usdc

    delta_ratio = cngn_usd_value / total_usd_value if total_usd_value > 0 else Decimal("0")
    return GlobalPosition(
        total_cngn=total_cngn,
        total_usdt=total_usdt,
        total_usdc=total_usdc,
        total_usd_value=total_usd_value,
        delta_ratio=delta_ratio,
        target_delta=Decimal(str(settings.target_delta_ratio)),
    )


@router.get("/positions/{venue}", response_model=Position)
async def get_venue_position(
    venue: str,
    runtime: EngineRuntime = Depends(get_runtime),
) -> Position:
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    try:
        return await runtime.venues[venue].get_position()
    except Exception as exc:
        logger.error("position_fetch_failed", venue=venue, error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc))
