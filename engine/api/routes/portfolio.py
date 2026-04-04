"""Portfolio exposure routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from engine.api.deps import get_runtime
from engine.api.helpers.portfolio import get_portfolio_exposure_calculator
from engine.api.schemas import PortfolioExposure
from engine.runtime import EngineRuntime

router = APIRouter()


@router.get("/portfolio/exposure", response_model=PortfolioExposure)
async def get_portfolio_exposure(runtime: EngineRuntime = Depends(get_runtime)) -> PortfolioExposure:
    calculator = get_portfolio_exposure_calculator(runtime)
    return await calculator.calculate()
