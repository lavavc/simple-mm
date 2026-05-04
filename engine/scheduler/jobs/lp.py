"""LP-specific scheduler jobs."""

from __future__ import annotations

import structlog

from engine.lp.rebalancer import LPRebalancer
from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import SchedulerState

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

        for name, lp_manager in self.context.lp_managers.items():
            if not self.state.trading_enabled:
                return
            venue = self.context.venues.get(name)
            if venue is None or venue.paused:
                continue
            try:
                await self.lp_rebalancer.check_and_rebalance(lp_manager)
            except Exception as exc:
                logger.error("dex_rebalance_check_failed", venue=name, error=str(exc))
