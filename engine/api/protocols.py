"""Shared protocols used across API route modules."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol


class SyncOrderLadderVenue(Protocol):
    async def sync_order_ladder(self, reference_price_ngn: Decimal) -> None: ...


class WebhookVenue(Protocol):
    async def handle_webhook(self, event: dict[str, Any]) -> None: ...


class DepthVenue(Protocol):
    async def get_order_book_depth(self, limit: int = 50) -> Any: ...
