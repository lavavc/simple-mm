"""Account monitoring and funding jobs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from engine.types import AccountBalanceResponse
from engine.config import settings
from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import SchedulerState
from engine.venues.cex.quidax_accounting import build_quidax_account_balance

logger = structlog.get_logger()


class AccountJobs:
    def __init__(self, context: SchedulerContext, state: SchedulerState) -> None:
        self.context = context
        self.state = state

    async def broadcast_account_balances(self, balances: list[Any]) -> None:
        payload = [
            AccountBalanceResponse(
                role=balance.role,
                address=balance.address,
                chain_id=balance.chain_id,
                native_balance=balance.native_balance,
                native_symbol=balance.native_symbol,
                token_balances=balance.token_balances,
                needs_refill=balance.needs_refill,
                refill_reasons=balance.refill_reasons,
            ).model_dump()
            for balance in balances
        ]

        _QUIDAX_VENUE_ROLES = {
            "quidax": ("quidax-trade", settings.quidax_trade_address),
            "quidax-lp": ("quidax-lp", settings.quidax_lp_address),
        }
        for venue_name, (role_name, address) in _QUIDAX_VENUE_ROLES.items():
            adapter = self.context.venues.get(venue_name)
            if adapter is None:
                continue
            try:
                position = await adapter.get_position()
                if position and position.balances:
                    payload.append(
                        build_quidax_account_balance(
                            role=role_name,
                            address=address,
                            balances=position.balances,
                        ).model_dump()
                    )
            except Exception as exc:
                logger.warning("quidax_balance_broadcast_failed", venue=venue_name, error=str(exc))

        self.context.broadcast({"type": "account_balances", "data": payload})

    async def check_balances(self) -> None:
        if not self.context.account_manager:
            await self._check_quidax_cex_balances()
            return

        try:
            balances = await self.context.account_manager.check_all_balances(self.context.token_contracts)
            self.state.last_balances = list(balances)
            for balance in balances:
                if balance.needs_refill and self._lp_account_has_active_position(balance.role):
                    continue
                if balance.needs_refill:
                    logger.warning(
                        "account_needs_refill",
                        role=balance.role,
                        address=balance.address,
                        reasons=balance.refill_reasons,
                    )
                    await self.context.alert_store.insert_alert(
                        severity="warning",
                        category="refill",
                        message=f"Account {balance.role} needs refill: {', '.join(balance.refill_reasons)}",
                        dedup=True,
                    )
                    self.context.broadcast(
                        {
                            "type": "refill_alert",
                            "data": {
                                "role": balance.role,
                                "address": balance.address,
                                "chain_id": balance.chain_id,
                                "native_balance": float(balance.native_balance),
                                "token_balances": {key: float(value) for key, value in balance.token_balances.items()},
                                "reasons": balance.refill_reasons,
                            },
                        }
                    )

            await self.broadcast_account_balances(list(balances))
            await self._check_quidax_cex_balances()
        except Exception as exc:
            logger.error("balance_check_failed", error=str(exc))

    async def _check_quidax_cex_balances(self) -> None:
        thresholds = {"cngn": settings.quidax_min_cngn, "usdt": settings.quidax_min_usdt}
        venues_to_check = {"quidax": "quidax-trade", "quidax-lp": "quidax-lp"}
        for venue_name, role_name in venues_to_check.items():
            adapter = self.context.venues.get(venue_name)
            if adapter is None:
                continue
            try:
                position = await adapter.get_position()
                if not position or not position.balances:
                    continue
                for token, minimum in thresholds.items():
                    balance = Decimal(str(position.balances.get(token, "0")))
                    if balance < minimum:
                        symbol = token.upper()
                        await self.context.alert_store.insert_alert(
                            severity="warning",
                            category="refill",
                            message=f"Quidax {role_name} {symbol} balance below minimum",
                            dedup=True,
                        )
                        self.context.broadcast(
                            {
                                "type": "refill_alert",
                                "data": {"role": role_name, "token": symbol},
                            }
                        )
            except Exception as exc:
                logger.warning("quidax_cex_balance_check_failed", venue=venue_name, error=str(exc))

    def _lp_account_has_active_position(self, role: str) -> bool:
        """Return True if role is an LP account whose tokens are deployed in an active LP position."""
        if not role.endswith("-lp"):
            return False
        venue_name = role[: -len("-lp")]
        manager = self.context.lp_managers.get(venue_name)
        if manager is None:
            return False
        return manager.get_active_lp_position_snapshot() is not None
