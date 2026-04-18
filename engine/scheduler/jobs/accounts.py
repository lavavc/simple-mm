"""Account monitoring and funding jobs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from engine.types import AccountBalanceResponse
from engine.config import settings
from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import SchedulerState

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

        quidax_venues = [("quidax", "quidax-trade", settings.quidax_trade_address)]
        if settings.quidax_lp_is_separate:
            quidax_venues.append(("quidax-lp", "quidax-lp", settings.quidax_lp_address))
        for venue_name, role_name, address in quidax_venues:
            adapter = self.context.venues.get(venue_name)
            if adapter is None:
                continue
            try:
                position = await adapter.get_position()
                if position and position.balances:
                    payload.append(
                        AccountBalanceResponse(
                            role=role_name,
                            address=address,
                            chain_id=0,
                            native_balance=Decimal("0"),
                            native_symbol="",
                            token_balances={
                                "cNGN": position.balances.get("cngn", Decimal("0")),
                                "USDT": position.balances.get("usdt", Decimal("0")),
                            },
                            needs_refill=False,
                            refill_reasons=[],
                        ).model_dump()
                    )
            except Exception as exc:
                logger.warning("quidax_exchange_balance_broadcast_failed", venue=venue_name, error=str(exc))

        self.context.broadcast({"type": "account_balances", "data": payload})

    async def check_balances(self) -> None:
        if not self.context.account_manager:
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
        except Exception as exc:
            logger.error("balance_check_failed", error=str(exc))

    def _lp_account_has_active_position(self, role: str) -> bool:
        """Return True if role is an LP account whose tokens are deployed in an active LP position."""
        if not role.endswith("-lp"):
            return False
        venue_name = role[: -len("-lp")]
        manager = self.context.lp_managers.get(venue_name)
        if manager is None:
            return False
        return manager.get_active_lp_position_snapshot() is not None

