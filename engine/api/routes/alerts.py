"""Alert routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from engine.api.deps import get_repository
from engine.types import Alert
from engine.db.repository import DatabaseRepository

router = APIRouter()


@router.get("/alerts", response_model=list[Alert])
async def get_alerts(
    limit: int = Query(20, le=100),
    db: DatabaseRepository = Depends(get_repository),
) -> list[Alert]:
    return await db.alerts.get_alerts(limit)


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: int,
    db: DatabaseRepository = Depends(get_repository),
) -> dict[str, Any]:
    await db.alerts.acknowledge_alert(alert_id)
    return {"status": "acknowledged", "alert_id": alert_id}
