"""Shared scheduler types."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

TokenContracts = dict[int, dict[str, str]]


@dataclass
class SchedulerState:
    trading_enabled: bool = True
    started: bool = False
    quidax_depth_ok: bool = True
    dex_bootstrap_pending: bool = False
    dex_bootstrap_task: asyncio.Task[None] | None = None
    last_balances: list[Any] | None = None
