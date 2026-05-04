"""Action log routes."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from engine.api.deps import get_repository
from engine.db.repository import DatabaseRepository

router = APIRouter()


@router.get("/actions")
async def get_actions(
    venue: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: DatabaseRepository = Depends(get_repository),
) -> list[dict[str, Any]]:
    return await db.actions.get_actions(venue, action_type, limit)
