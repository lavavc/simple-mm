"""Account routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
import structlog

from engine.api.deps import get_runtime, require_account_manager, verify_token
from engine.types import AccountBalanceResponse, AccountInfo, AccountThresholds
from engine.config import settings
from engine.runtime import EngineRuntime
from engine.venues.cex.quidax_accounting import build_quidax_account_balance

logger = structlog.get_logger()
router = APIRouter()


@router.get("/accounts")
async def list_accounts(account_manager: Any = Depends(require_account_manager)) -> list[AccountInfo]:
    from engine.accounts import AccountRole

    accounts: list[AccountInfo] = []
    for role in AccountRole:
        try:
            config = account_manager.get_config(role)
            accounts.append(
                AccountInfo(
                    role=role.value,
                    address=account_manager.get_address(role),
                    derivation_path=config.derivation_path,
                    chain_id=config.chain_id,
                    tokens=config.tokens,
                )
            )
        except ValueError:
            continue
    return accounts


@router.get("/accounts/balances", response_model=list[AccountBalanceResponse])
async def get_all_account_balances(
    runtime: EngineRuntime = Depends(get_runtime),
) -> list[AccountBalanceResponse]:
    try:
        result: list[AccountBalanceResponse] = []
        if runtime.account_manager is not None:
            balances = await runtime.account_manager.check_all_balances(runtime.token_contracts)
            result.extend(
                AccountBalanceResponse(
                    role=balance.role,
                    address=balance.address,
                    chain_id=balance.chain_id,
                    native_balance=balance.native_balance,
                    native_symbol=balance.native_symbol,
                    token_balances=balance.token_balances,
                    needs_refill=balance.needs_refill,
                    refill_reasons=balance.refill_reasons,
                )
                for balance in balances
            )

        _QUIDAX_VENUE_ROLES = {
            "quidax": ("quidax-trade", settings.quidax_trade_address),
            "quidax-lp": ("quidax-lp", settings.quidax_lp_address),
        }
        for venue_name, (role_name, address) in _QUIDAX_VENUE_ROLES.items():
            adapter = runtime.venues.get(venue_name)
            if adapter is None:
                continue
            try:
                pos = await adapter.get_position()
                result.append(
                    build_quidax_account_balance(
                        role=role_name,
                        address=address,
                        balances=pos.balances,
                    )
                )
            except Exception as exc:
                logger.warning("quidax_balance_fetch_failed", venue=venue_name, error=str(exc))
        if not result and runtime.account_manager is None:
            raise HTTPException(status_code=503, detail="No account balance sources configured")
        return result
    except Exception as exc:
        logger.error("balance_fetch_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/accounts/{role}", response_model=AccountInfo)
async def get_account(
    role: str,
    account_manager: Any = Depends(require_account_manager),
) -> AccountInfo:
    from engine.accounts import AccountRole

    try:
        account_role = AccountRole(role)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role}")

    try:
        config = account_manager.get_config(account_role)
        return AccountInfo(
            role=role,
            address=account_manager.get_address(account_role),
            derivation_path=config.derivation_path,
            chain_id=config.chain_id,
            tokens=config.tokens,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/accounts/{role}/balance", response_model=AccountBalanceResponse)
async def get_account_balance(
    role: str,
    runtime: EngineRuntime = Depends(get_runtime),
) -> AccountBalanceResponse:
    if runtime.account_manager is None:
        raise HTTPException(status_code=503, detail="Account manager not configured")

    from engine.accounts import AccountRole

    try:
        account_role = AccountRole(role)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role}")

    try:
        balance = await runtime.account_manager.get_balance(account_role, runtime.token_contracts)
        return AccountBalanceResponse(
            role=balance.role,
            address=balance.address,
            chain_id=balance.chain_id,
            native_balance=balance.native_balance,
            native_symbol=balance.native_symbol,
            token_balances=balance.token_balances,
            needs_refill=balance.needs_refill,
            refill_reasons=balance.refill_reasons,
        )
    except Exception as exc:
        logger.error("balance_fetch_failed", role=role, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.put("/accounts/{role}/thresholds", dependencies=[Depends(verify_token)])
async def update_account_thresholds(
    role: str,
    thresholds: AccountThresholds,
    account_manager: Any = Depends(require_account_manager),
) -> dict[str, str]:
    from engine.accounts import AccountRole

    try:
        account_role = AccountRole(role)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role}")

    try:
        account_manager.update_thresholds(
            account_role,
            min_balance_eth=thresholds.min_balance_eth,
            min_balance_tokens=thresholds.min_balance_tokens,
        )
        return {"status": "updated", "role": role}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
