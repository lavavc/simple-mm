"""Shared FastAPI dependencies for the engine API."""

from __future__ import annotations

from typing import Any, cast

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from engine.config import settings
from engine.db.repository import DatabaseRepository
from engine.market.price_aggregation import BlendedPriceCalculator, PriceNormalizer
from engine.market.venue_prices import VenuePriceAggregator
from engine.runtime import EngineRuntime
from engine.scheduler import TradingScheduler

security = HTTPBearer()


def get_runtime(request: Request) -> EngineRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Engine runtime not configured")
    return cast(EngineRuntime, runtime)


def get_repository(runtime: EngineRuntime = Depends(get_runtime)) -> DatabaseRepository:
    return runtime.db


def require_scheduler(runtime: EngineRuntime = Depends(get_runtime)) -> TradingScheduler:
    return runtime.scheduler


def require_price_aggregator(runtime: EngineRuntime = Depends(get_runtime)) -> VenuePriceAggregator:
    if runtime.price_aggregator is None:
        raise HTTPException(status_code=503, detail="Price aggregator not configured")
    return runtime.price_aggregator


def require_blended_calculator(
    runtime: EngineRuntime = Depends(get_runtime),
) -> BlendedPriceCalculator:
    if runtime.blended_calculator is None:
        raise HTTPException(status_code=503, detail="Blended price calculator not configured")
    return runtime.blended_calculator


def require_normalizer(runtime: EngineRuntime = Depends(get_runtime)) -> PriceNormalizer:
    if runtime.normalizer is None:
        raise HTTPException(status_code=503, detail="Price normalizer not configured")
    return runtime.normalizer


def require_arbitrage_engine(runtime: EngineRuntime = Depends(get_runtime)) -> Any:
    if runtime.arbitrage_engine is None:
        raise HTTPException(status_code=503, detail="Arbitrage engine not configured")
    return runtime.arbitrage_engine


def require_account_manager(runtime: EngineRuntime = Depends(get_runtime)) -> Any:
    if runtime.account_manager is None:
        raise HTTPException(status_code=503, detail="Account manager not configured")
    return runtime.account_manager


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> bool:
    if not settings.engine_api_token:
        raise HTTPException(status_code=500, detail="DASHBOARD_API_TOKEN is not configured")
    if credentials.credentials != settings.engine_api_token:
        raise HTTPException(status_code=401, detail="Invalid token")
    return True
