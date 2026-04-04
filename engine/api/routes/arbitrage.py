"""Arbitrage routes."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Query
import structlog

from engine.api.deps import get_repository, get_runtime, require_arbitrage_engine, verify_token
from engine.api.protocols import DepthVenue
from engine.api.schemas import (
    ArbitrageHistoryItem,
    ArbitrageOpportunity,
    ArbitrageParams,
    ArbitrageStatus,
    DexArbOpportunity,
)
from engine.db.repository import DatabaseRepository
from engine.runtime import EngineRuntime

logger = structlog.get_logger()
router = APIRouter()


@router.get("/arbitrage/status", response_model=ArbitrageStatus)
async def get_arbitrage_status(
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> ArbitrageStatus:
    try:
        return cast(ArbitrageStatus, await arbitrage_engine.get_status())
    except Exception as exc:
        logger.error("arbitrage_status_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/arbitrage/opportunities", response_model=list[ArbitrageOpportunity])
async def get_arbitrage_opportunities(
    status: Optional[str] = Query(None, description="Filter by status"),
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(50, le=200),
    db: DatabaseRepository = Depends(get_repository),
) -> list[ArbitrageOpportunity]:
    return await db.arbitrage.get_arbitrage_opportunities(status, from_ts, to_ts, limit)


@router.get("/arbitrage/dex-opportunities", response_model=list[DexArbOpportunity])
async def get_dex_arbitrage_opportunities(
    status: Optional[str] = Query(None, description="Filter by status"),
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(50, le=200),
    db: DatabaseRepository = Depends(get_repository),
) -> list[DexArbOpportunity]:
    return await db.arbitrage.get_dex_arbitrage_opportunities(status, from_ts, to_ts, limit)


@router.get("/arbitrage/history", response_model=list[ArbitrageHistoryItem])
async def get_arbitrage_history(
    pipeline: Optional[str] = Query(None, description="Filter by pipeline"),
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(30, le=200),
    db: DatabaseRepository = Depends(get_repository),
) -> list[ArbitrageHistoryItem]:
    return await db.history.get_arbitrage_history(pipeline, from_ts, to_ts, limit)


@router.get("/arbitrage/liquidation")
async def get_liquidation_valuation(runtime: EngineRuntime = Depends(get_runtime)) -> dict[str, Any]:
    from engine.arb.detection.cex_dex import QUIDAX_FEE
    from engine.arb.valuation import cex_holdings_value, dex_holdings_value
    from engine.market.pool_state import get_cached_pool_state
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG

    if runtime.account_manager is None:
        return {"error": "account_manager_not_configured", "venues": {}}

    quidax_venue = runtime.venues.get("quidax")
    quidax_asks: list[Any] = []
    if quidax_venue:
        try:
            depth = await cast(DepthVenue, quidax_venue).get_order_book_depth(limit=50)
            if depth and depth.asks:
                quidax_asks = depth.asks
        except Exception:
            pass

    bsc_sqrt, bsc_liq, _, bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    base_sqrt, base_liq, _, base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)

    try:
        balances = await runtime.account_manager.check_all_balances(runtime.token_contracts)
    except Exception as exc:
        return {"error": str(exc), "venues": {}}

    valuation_balances: list[Any] = list(balances)
    if quidax_venue:
        try:
            qx_pos = await quidax_venue.get_position()
            if qx_pos and qx_pos.balances:
                from types import SimpleNamespace

                valuation_balances.append(
                    SimpleNamespace(
                        role="quidax-exchange",
                        token_balances={
                            "cNGN": Decimal(str(qx_pos.balances.get("cngn", 0))),
                            "USDT": Decimal(str(qx_pos.balances.get("usdt", 0))),
                        },
                    )
                )
        except Exception:
            pass

    result: dict[str, Any] = {}
    for balance in valuation_balances:
        role = balance.role
        tokens = balance.token_balances or {}
        venue_result: dict[str, dict[str, float]] = {}
        for token, amt in tokens.items():
            amount = Decimal(str(amt)) if amt is not None else Decimal("0")
            if token.lower() == "cngn" and amount > 0:
                value_usd = Decimal("0")
                try:
                    if role in ("quidax-exchange", "quidax-lp", "quidax-trade-fund") and quidax_asks:
                        value_usd = cex_holdings_value(quidax_asks, amount, QUIDAX_FEE)
                    elif role in ("uni-bsc-trade", "uni-bsc-lp") and bsc_sqrt and bsc_liq is not None and bsc_fee is not None:
                        value_usd = dex_holdings_value(amount, bsc_sqrt, bsc_liq, bsc_fee, 18, 6, cngn_is_token0=False)
                    elif role in ("uni-base-trade", "uni-base-lp") and base_sqrt and base_liq is not None and base_fee is not None:
                        value_usd = dex_holdings_value(amount, base_sqrt, base_liq, base_fee, 6, 6, cngn_is_token0=True)
                except Exception:
                    value_usd = Decimal("0")
                venue_result[token] = {"amount": float(amount), "value_usd": float(value_usd)}
            else:
                as_float = float(amount) if amt is not None else 0.0
                venue_result[token] = {"amount": as_float, "value_usd": as_float}
        result[role] = venue_result

    return {"timestamp": int(time.time() * 1000), "venues": result}


@router.get("/arbitrage/opportunities/{opportunity_id}", response_model=ArbitrageOpportunity)
async def get_arbitrage_opportunity(
    opportunity_id: str,
    db: DatabaseRepository = Depends(get_repository),
) -> ArbitrageOpportunity:
    opp = await db.arbitrage.get_arbitrage_opportunity(opportunity_id)
    if opp is not None:
        return opp
    raise HTTPException(status_code=404, detail="Opportunity not found")


@router.post("/arbitrage/enable", dependencies=[Depends(verify_token)])
async def enable_arbitrage(
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> dict[str, str]:
    arbitrage_engine.enable()
    logger.info("arbitrage_enabled_via_api")
    return {"status": "enabled"}


@router.post("/arbitrage/disable", dependencies=[Depends(verify_token)])
async def disable_arbitrage(
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> dict[str, str]:
    arbitrage_engine.disable()
    logger.info("arbitrage_disabled_via_api")
    return {"status": "disabled"}


@router.post("/arbitrage/execute-cex-dex/enable", dependencies=[Depends(verify_token)])
async def enable_execute_cex_dex(
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> dict[str, str]:
    arbitrage_engine.set_execution_enabled("cex_dex", True)
    return {"status": "enabled"}


@router.post("/arbitrage/execute-cex-dex/disable", dependencies=[Depends(verify_token)])
async def disable_execute_cex_dex(
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> dict[str, str]:
    arbitrage_engine.set_execution_enabled("cex_dex", False)
    return {"status": "disabled"}


@router.post("/arbitrage/execute-dex-dex/enable", dependencies=[Depends(verify_token)])
async def enable_execute_dex_dex(
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> dict[str, str]:
    arbitrage_engine.set_execution_enabled("dex_dex", True)
    return {"status": "enabled"}


@router.post("/arbitrage/execute-dex-dex/disable", dependencies=[Depends(verify_token)])
async def disable_execute_dex_dex(
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> dict[str, str]:
    arbitrage_engine.set_execution_enabled("dex_dex", False)
    return {"status": "disabled"}


@router.put("/arbitrage/params", dependencies=[Depends(verify_token)])
async def update_arbitrage_params(
    params: ArbitrageParams,
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> dict[str, Any]:
    arbitrage_engine.update_params(params)
    logger.info("arbitrage_params_updated_via_api")
    return {"status": "updated", "params": params.model_dump()}


@router.post("/arbitrage/reset-circuit-breaker", dependencies=[Depends(verify_token)])
async def reset_arbitrage_circuit_breaker(
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> dict[str, str]:
    arbitrage_engine.reset_circuit_breaker()
    logger.info("arbitrage_circuit_breaker_reset_via_api")
    return {"status": "reset"}


@router.post("/arbitrage/scan", dependencies=[Depends(verify_token)])
async def trigger_arbitrage_scan(
    arbitrage_engine: Any = Depends(require_arbitrage_engine),
) -> dict[str, Any]:
    try:
        opportunities = await arbitrage_engine.scan()
        return {
            "status": "scanned",
            "opportunities_found": len(opportunities),
            "opportunities": [opp.model_dump() for opp in opportunities],
        }
    except Exception as exc:
        logger.error("manual_arbitrage_scan_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
