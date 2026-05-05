"""LP-specific scheduler jobs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from engine.lp.rebalancer import LPRebalancer
from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import SchedulerState

if TYPE_CHECKING:
    from engine.market.fair_price import StrategyFairPrice

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

        strategy_fair_price = self._compute_strategy_fair_price()

        for name, lp_manager in self.context.lp_managers.items():
            if not self.state.trading_enabled:
                return
            venue = self.context.venues.get(name)
            if venue is None or venue.paused:
                continue
            try:
                await self.lp_rebalancer.check_and_rebalance(lp_manager, strategy_fair_price=strategy_fair_price)
            except Exception as exc:
                logger.error("dex_rebalance_check_failed", venue=name, error=str(exc))

    def _compute_strategy_fair_price(self) -> "StrategyFairPrice | None":
        """Compute StrategyFairPrice from cached venue prices. Returns None if unavailable."""
        from engine.market.fair_price import StrategyFairPrice as _SFP  # noqa: F401
        if (
            self.context.market_fair_price_calculator is None
            or self.context.strategy_price_calculator is None
            or self.context.blended_calculator is None
        ):
            return None
        try:
            cached = self.context.price_aggregator.get_all_prices()
            if not cached:
                return None
            normalized = self.context.blended_calculator.normalizer.normalize(cached)
            if not normalized:
                return None
            market_fp = self.context.market_fair_price_calculator.compute(normalized, cached)
            # Use flat inventory (net_cngn=0) as baseline; full inventory wiring is a future step.
            from decimal import Decimal
            return self.context.strategy_price_calculator.compute(market_fp, net_cngn=Decimal("0"))
        except (ValueError, Exception):
            return None
