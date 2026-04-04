"""Account monitoring and funding jobs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from engine.api.schemas import AccountBalanceResponse
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

        quidax_adapter = self.context.venues.get("quidax")
        if quidax_adapter:
            try:
                position = await quidax_adapter.get_position()
                if position and position.balances:
                    payload.append(
                        AccountBalanceResponse(
                            role="quidax-exchange",
                            address=settings.quidax_deposit_address,
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
                logger.warning("quidax_exchange_balance_broadcast_failed", error=str(exc))

        self.context.broadcast({"type": "account_balances", "data": payload})

    async def check_balances(self) -> None:
        if not self.context.account_manager:
            return

        try:
            balances = await self.context.account_manager.check_all_balances(self.context.token_contracts)
            self.state.last_balances = list(balances)
            for balance in balances:
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

    async def auto_fund_quidax(self, adapter: Any, account_role_str: str) -> None:
        if not self.context.account_manager:
            return

        from engine.accounts import AccountRole

        account_role = AccountRole(account_role_str)
        position = await adapter.get_position()
        balances = position.balances

        token_contracts = {"cNGN": settings.cngn_bsc_address, "USDT": settings.usdt_bsc_address}
        on_chain = await self.context.account_manager.get_balance(account_role, token_contracts)
        on_chain_balances = on_chain.token_balances

        tokens = [
            ("cngn", "cNGN", settings.cngn_bsc_address, settings.quidax_min_cngn, settings.quidax_top_up_cngn, settings.quidax_onchain_min_cngn),
            ("usdt", "USDT", settings.usdt_bsc_address, settings.quidax_min_usdt, settings.quidax_top_up_usdt, settings.quidax_onchain_min_usdt),
        ]

        for cex_key, chain_key, contract, min_cex, top_up, min_onchain in tokens:
            if balances.get(cex_key, Decimal("0")) >= min_cex:
                continue
            chain_amount = on_chain_balances.get(chain_key, Decimal("0"))
            if chain_amount > min_onchain + top_up:
                deposit_address = settings.quidax_deposit_address
                if deposit_address:
                    tx_hash = await self.context.account_manager.transfer_erc20(
                        account_role,
                        contract,
                        deposit_address,
                        top_up,
                    )
                    logger.info(
                        "auto_fund_quidax",
                        role=account_role_str,
                        token=chain_key,
                        amount=float(top_up),
                        tx=tx_hash,
                    )
                else:
                    logger.warning(
                        "quidax_deposit_address_missing",
                        role=account_role_str,
                        token=cex_key,
                    )
            else:
                await self.context.alert_store.insert_alert(
                    severity="warning",
                    category="refill",
                    message=(
                        f"On-chain {chain_key} for {account_role_str} insufficient "
                        f"({float(chain_amount):.2f}); manual refill needed"
                    ),
                    dedup=True,
                )
                self.context.broadcast(
                    {
                        "type": "refill_alert",
                        "data": {"role": account_role_str, "token": chain_key, "on_chain": float(chain_amount)},
                    }
                )
