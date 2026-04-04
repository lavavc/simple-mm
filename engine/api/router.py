"""Top-level API router composition."""

from __future__ import annotations

from fastapi import APIRouter

from engine.api.routes.accounts import router as accounts_router
from engine.api.routes.actions import router as actions_router
from engine.api.routes.alerts import router as alerts_router
from engine.api.routes.arbitrage import router as arbitrage_router
from engine.api.routes.pool_metrics import router as pool_metrics_router
from engine.api.routes.positions import router as positions_router
from engine.api.routes.prices import router as prices_router
from engine.api.routes.system import router as system_router
from engine.api.routes.venues import router as venues_router

api_router = APIRouter()
api_router.include_router(system_router)
api_router.include_router(prices_router)
api_router.include_router(positions_router)
api_router.include_router(venues_router)
api_router.include_router(actions_router)
api_router.include_router(alerts_router)
api_router.include_router(arbitrage_router)
api_router.include_router(accounts_router)
api_router.include_router(pool_metrics_router)
