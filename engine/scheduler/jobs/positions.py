"""Position and portfolio monitoring scheduler jobs."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

import structlog

from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import SchedulerState
from engine.venues.base import DepthVenue

logger = structlog.get_logger()


class PositionJobs:
    def __init__(self, context: SchedulerContext, state: SchedulerState) -> None:
        self.context = context
        self.state = state

    async def sync_positions(self) -> None:
        positions = []
        for name, venue in self.context.venues.items():
            try:
                lp_manager = self.context.lp_managers.get(name)
                if lp_manager is not None:
                    position = await lp_manager.get_position_as_schema()
                else:
                    position = await venue.get_position()
                positions.append(position)
                await self.context.position_store.insert_position(position)
            except Exception as exc:
                logger.error("position_sync_failed", venue=name, error=str(exc))

        self.context.broadcast({"type": "positions", "data": [position.model_dump() for position in positions]})

    async def broadcast_portfolio_exposure(self) -> None:
        calculator = self.context.portfolio_exposure_calculator
        if calculator is None:
            return
        try:
            exposure = await calculator.calculate()
            self.context.broadcast({"type": "portfolio_exposure", "data": asdict(exposure)})
        except Exception as exc:
            logger.error("portfolio_reporting_failed", error=str(exc))

    async def get_balances_for_valuation(self, quidax: DepthVenue) -> list[Any]:
        from types import SimpleNamespace

        last_balances = self.state.last_balances
        if not last_balances and self.context.account_manager:
            try:
                last_balances = await self.context.account_manager.check_all_balances(
                    self.context.token_contracts
                )
                self.state.last_balances = list(last_balances)
                logger.info("balances_eagerly_seeded_for_valuation_calc")
            except Exception as exc:
                logger.warning("balance_seed_failed_for_valuation", error=str(exc))

        balances: list[Any] = list(last_balances) if last_balances else []
        try:
            quidax_position = await quidax.get_position()
            if quidax_position and quidax_position.balances:
                balances.append(
                    SimpleNamespace(
                        role="quidax-trade",
                        token_balances={
                            "cNGN": Decimal(str(quidax_position.balances.get("cngn", 0))),
                            "USDT": Decimal(str(quidax_position.balances.get("usdt", 0))),
                        },
                    )
                )
        except Exception as exc:
            logger.warning("quidax_position_fetch_failed", error=str(exc))

        return balances
