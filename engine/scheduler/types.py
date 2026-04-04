"""Shared scheduler types and small protocols."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

TokenContracts = dict[int, dict[str, str]]


class SyncOrderLadderVenueProtocol(Protocol):
    paused: bool

    async def sync_order_ladder(self, reference_price_ngn: Decimal) -> None: ...


class DepthVenueProtocol(Protocol):
    async def get_order_book_depth(self, limit: int = 50) -> Any: ...
    async def get_position(self) -> Any: ...


@dataclass
class SchedulerState:
    trading_enabled: bool = True
    started: bool = False
    quidax_depth_ok: bool = True
    dex_bootstrap_pending: bool = False
    dex_bootstrap_task: asyncio.Task[None] | None = None
    last_balances: list[Any] | None = None
