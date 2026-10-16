"""Read-only portfolio reporting."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from engine.api.deps import get_runtime
from engine.api.schemas import PortfolioExposure
from engine.runtime import EngineRuntime

router = APIRouter()


@router.get("/portfolio/exposure", response_model=PortfolioExposure)
async def get_portfolio_exposure(runtime: EngineRuntime = Depends(get_runtime)) -> PortfolioExposure:
    calculator = runtime.portfolio_exposure_calculator
    if calculator is None:
        raise HTTPException(status_code=503, detail="Portfolio reporting not initialized")
    return PortfolioExposure.model_validate(asdict(await calculator.calculate()))
