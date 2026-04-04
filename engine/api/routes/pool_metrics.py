"""Pool metrics routes."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query

from engine.api.deps import get_repository, get_runtime
from engine.config import settings
from engine.db.repository import DatabaseRepository
from engine.runtime import EngineRuntime

router = APIRouter()

_DEX_POOLS = [
    {"venue": "uni-base", "chain": "base", "pool_address": settings.uni_base_pool_id},
    {"venue": "uni-bsc", "chain": "bsc", "pool_address": settings.uni_bsc_pool_id},
]


@router.get("/pool-metrics/history")
async def get_pool_metrics_history(
    minutes: int = Query(1440, ge=1440, le=43200),
    db: DatabaseRepository = Depends(get_repository),
) -> list[dict[str, Any]]:
    from_ts = int((time.time() - minutes * 60) * 1000)
    return await db.pool_metrics.get_pool_metrics_history(["uni-base", "uni-bsc"], from_ts)


@router.get("/pool-metrics")
async def get_pool_metrics(runtime: EngineRuntime = Depends(get_runtime)) -> list[dict[str, Any]]:
    del runtime
    results: list[dict[str, Any]] = []
    for pool in _DEX_POOLS:
        results.append(
            {
                "venue": pool["venue"],
                "chain": pool["chain"],
                "position_value_usd": None,
                "volume_24h_usd": None,
            }
        )
    return results
