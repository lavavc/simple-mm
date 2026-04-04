"""LP-specific scheduler jobs."""

from __future__ import annotations

import structlog

from engine.lp.rebalancer import LPRebalancer
from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import SchedulerState
from engine.venues.dex.lp_v4 import V4LPAdapter

logger = structlog.get_logger()


class LpJobs:
    def __init__(
        self,
        context: SchedulerContext,
        state: SchedulerState,
        lp_rebalancer: LPRebalancer,
    ) -> None:
        self.context = context
        self.state = state
        self.lp_rebalancer = lp_rebalancer

    async def check_dex_rebalance(self) -> None:
        if not self.state.trading_enabled:
            return

        for name in ["uni-base", "uni-bsc"]:
            if name not in self.context.venues:
                continue
            venue = self.context.venues[name]
            if not isinstance(venue, V4LPAdapter) or venue.paused:
                continue
            try:
                await self.lp_rebalancer.check_and_rebalance(venue)
            except Exception as exc:
                logger.error("dex_rebalance_check_failed", venue=name, error=str(exc))
