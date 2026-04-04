"""Position and portfolio monitoring scheduler jobs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import DepthVenueProtocol, SchedulerState

logger = structlog.get_logger()


class PositionJobs:
    def __init__(self, context: SchedulerContext, state: SchedulerState) -> None:
        self.context = context
        self.state = state

    async def sync_positions(self) -> None:
        positions = []
        for name, venue in self.context.venues.items():
            try:
                position = await venue.get_position()
                positions.append(position)
                await self.context.position_store.insert_position(position)
            except Exception as exc:
                logger.error("position_sync_failed", venue=name, error=str(exc))

        self.context.broadcast({"type": "positions", "data": [position.model_dump() for position in positions]})

    async def check_portfolio_delta(self) -> None:
        if not self.context.portfolio_exposure_calculator:
            return

        try:
            exposure = await self.context.portfolio_exposure_calculator.calculate()
            total_cngn = exposure.total_cngn
            total_usdt = exposure.total_usdt
            total_usdc = exposure.total_usdc
            total_usd_value = exposure.total_usd_value
            if total_usd_value <= 0:
                return

            target = exposure.target_delta
            delta_ratio = exposure.delta_ratio
            cngn_usd_value = total_usd_value - total_usdt - total_usdc
            deviation_percent = abs(delta_ratio - target) / target * 100 if target > 0 else Decimal("0")

            self.context.broadcast(
                {
                    "type": "portfolio_delta",
                    "data": {
                        "total_cngn": float(total_cngn),
                        "total_usdt": float(total_usdt),
                        "total_usdc": float(total_usdc),
                        "cngn_usd_value": float(cngn_usd_value),
                        "total_usd_value": float(total_usd_value),
                        "delta_ratio": float(delta_ratio),
                        "target_delta": float(target),
                        "deviation_percent": float(deviation_percent),
                        "sources": [source.model_dump(mode="json") for source in exposure.sources],
                    },
                }
            )

            if self.context.arbitrage_engine and total_usd_value > 0:
                self.context.arbitrage_engine.update_portfolio_snapshot(
                    cngn_usd_value,
                    total_usd_value,
                    (cngn_usd_value / total_cngn) if total_cngn > 0 else Decimal("0"),
                )

            logger.info(
                "portfolio_delta_checked",
                delta_ratio=float(delta_ratio),
                target=float(target),
                deviation_percent=float(deviation_percent),
                total_usd=float(total_usd_value),
            )

            if deviation_percent > self.context.config.delta_alert_threshold_percent:
                direction = "overweight cNGN" if delta_ratio > target else "underweight cNGN"
                message = (
                    f"Portfolio delta {float(delta_ratio):.1%} deviates "
                    f"{float(deviation_percent):.1f}% from target {float(target):.1%} "
                    f"({direction})"
                )
                logger.warning("portfolio_delta_alert", message=message)
                alert_id = await self.context.alert_store.insert_alert(
                    severity="warning",
                    category="delta",
                    message=message,
                    dedup=True,
                )
                if alert_id:
                    self.context.broadcast(
                        {"type": "alert", "severity": "warning", "message": message}
                    )
        except Exception as exc:
            logger.error("portfolio_delta_check_failed", error=str(exc))

    async def get_balances_for_valuation(self, quidax: DepthVenueProtocol) -> list[Any]:
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
                        role="quidax-exchange",
                        token_balances={
                            "cNGN": Decimal(str(quidax_position.balances.get("cngn", 0))),
                            "USDT": Decimal(str(quidax_position.balances.get("usdt", 0))),
                        },
                    )
                )
        except Exception as exc:
            logger.warning("quidax_position_fetch_failed", error=str(exc))

        return balances
