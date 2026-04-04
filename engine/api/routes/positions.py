"""Position routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
import structlog

from engine.api.deps import get_runtime
from engine.api.schemas import GlobalPosition, Position
from engine.api.helpers.portfolio import get_portfolio_exposure_calculator, to_global_position_response
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
    calculator = get_portfolio_exposure_calculator(runtime)
    return to_global_position_response(await calculator.calculate())


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
