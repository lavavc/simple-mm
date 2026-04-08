"""Venue control, sync, and deposit routes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from web3 import Web3
import structlog

from engine.api.deps import get_repository, get_runtime, require_account_manager, verify_token
from engine.api.helpers.pricing import get_reference_price_ngn
from engine.api.protocols import DepthVenue, SyncOrderLadderVenue, WebhookVenue
from engine.api.schemas import (
    CexParams,
    OrderBookDepthResponse,
    OrderBookLevel,
    VenueOrderSummary,
    VenueOrdersResponse,
)
from engine.config import DexParams, settings
from engine.db.repository import DatabaseRepository
from engine.runtime import EngineRuntime
from engine.venues.dex.lp_v4 import V4LPAdapter
from engine.venues.cex.order_values import decimal_from_order_value

logger = structlog.get_logger()
router = APIRouter()


class WithdrawRequest(BaseModel):
    to_address: str

    @field_validator("to_address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if not Web3.is_address(value):
            raise ValueError(f"Invalid Ethereum address: {value!r}")
        return value


class DepositRequest(BaseModel):
    role: str
    token: str
    amount: Decimal


def get_deposit_token_address(token: str) -> str:
    token_map = {
        "USDC": settings.usdc_base_address,
        "cNGN": settings.cngn_base_address,
    }
    address = token_map.get(token)
    if not address:
        raise HTTPException(status_code=400, detail=f"Unsupported token: {token}. Use USDC or cNGN.")
    return address


def _normalize_generic_order_summary(order: dict[str, Any], venue: str) -> VenueOrderSummary | None:
    price = decimal_from_order_value(order.get("price"))
    if price <= 0:
        return None

    volume = decimal_from_order_value(order.get("volume"))
    origin_volume = decimal_from_order_value(order.get("origin_volume"))
    remaining_volume = decimal_from_order_value(order.get("remaining_volume"))
    executed_volume = decimal_from_order_value(order.get("executed_volume"))
    if volume <= 0:
        volume = origin_volume
    if remaining_volume <= 0:
        base_volume = origin_volume if origin_volume > 0 else volume
        remaining_volume = base_volume - executed_volume if base_volume > executed_volume else base_volume
    if remaining_volume < 0:
        remaining_volume = Decimal("0")
    if volume <= 0:
        volume = origin_volume if origin_volume > 0 else remaining_volume + executed_volume

    return VenueOrderSummary(
        id=str(order.get("id", "")),
        market=str(order.get("market")) if order.get("market") is not None else None,
        side=str(order.get("side", "")).lower() or "unknown",
        status=str(order.get("state") or order.get("status") or "").lower() or None,
        price=price,
        volume=volume,
        remaining_volume=remaining_volume,
        executed_volume=executed_volume,
        notional=price * (remaining_volume if remaining_volume > 0 else volume),
        created_at=None,
    )


@router.post("/venues/{venue}/withdraw", dependencies=[Depends(verify_token)])
async def withdraw_venue_position(
    venue: str,
    body: WithdrawRequest,
    runtime: EngineRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    adapter = runtime.venues[venue]
    if not isinstance(adapter, V4LPAdapter):
        raise HTTPException(status_code=400, detail=f"{venue} is not a DEX venue")

    results = await runtime.scheduler.lp_rebalancer.withdraw_positions(
        adapter,
        recipient=body.to_address,
        action_type="manual_withdraw",
        triggered_by="api:withdraw",
    )
    if not results:
        return {"venue": venue, "removed": [], "message": "No active positions"}
    for item in results:
        if item["status"] != "confirmed":
            logger.error(
                "withdraw_position_failed",
                venue=venue,
                token_id=item["token_id"],
                error=item["error"],
            )

    runtime.scheduler.broadcast(
        {
            "type": "alert",
            "severity": "warning",
            "message": f"LP positions withdrawn on {venue} to {body.to_address}: {[r['token_id'] for r in results]}",
        }
    )
    logger.info("venue_positions_withdrawn", venue=venue, to_address=body.to_address, results=results)
    return {"venue": venue, "removed": results}


@router.post("/venues/{venue}/pause", dependencies=[Depends(verify_token)])
async def pause_venue(venue: str, runtime: EngineRuntime = Depends(get_runtime)) -> dict[str, Any]:
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")
    venue_adapter = runtime.venues[venue]
    venue_adapter.paused = True

    cancelled_orders: int | None = None
    cancel_all_orders = getattr(venue_adapter, "cancel_all_orders", None)
    if callable(cancel_all_orders):
        try:
            cancelled_orders = await cancel_all_orders()
        except Exception as exc:
            logger.error("venue_pause_cancel_failed", venue=venue, error=str(exc))
            raise HTTPException(
                status_code=500,
                detail="Venue paused, but failed to cancel open orders",
            ) from exc

    logger.info("venue_paused", venue=venue, cancelled_orders=cancelled_orders)
    return {"venue": venue, "paused": True, "cancelled_orders": cancelled_orders}


@router.post("/venues/{venue}/resume", dependencies=[Depends(verify_token)])
async def resume_venue(venue: str, runtime: EngineRuntime = Depends(get_runtime)) -> dict[str, Any]:
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")
    runtime.venues[venue].paused = False
    logger.info("venue_resumed", venue=venue)
    return {"venue": venue, "paused": False}


@router.put("/venues/{venue}/params", dependencies=[Depends(verify_token)])
async def update_venue_params(
    venue: str,
    params: dict[str, Any],
    runtime: EngineRuntime = Depends(get_runtime),
    db: DatabaseRepository = Depends(get_repository),
) -> dict[str, Any]:
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    venue_adapter = runtime.venues[venue]
    if hasattr(venue_adapter, "params"):
        if isinstance(venue_adapter, V4LPAdapter):
            merged = venue_adapter.params.model_dump()
            merged.update(params)
            venue_adapter.params = DexParams(**merged)
            await db.venue_config.update_venue_config(
                venue,
                venue_adapter.params.model_dump(mode="json"),
            )
        else:
            merged = venue_adapter.params.to_params_payload(mode="json")
            ladder_migration_fields = {"spread_offset_ngn", "ladder_step_ngn", "ladder_levels_per_side"}
            provided_ladder_migration_fields = ladder_migration_fields.intersection(params)
            if (
                merged.get("ladder_offsets_ngn")
                and provided_ladder_migration_fields
                and provided_ladder_migration_fields != ladder_migration_fields
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Legacy custom ladder offsets must be migrated by setting "
                        "spread_offset_ngn, ladder_step_ngn, and ladder_levels_per_side together."
                    ),
                )
            merged.update(params)
            venue_adapter.params = CexParams(**merged)
            await db.venue_config.update_venue_config(
                venue,
                venue_adapter.params.to_params_payload(mode="json"),
            )
    else:
        await db.venue_config.update_venue_config(venue, params)

    logger.info("venue_params_updated", venue=venue, params=params)
    return {"venue": venue, "params": params}


@router.post("/venues/blockradar/deposit", dependencies=[Depends(verify_token)])
async def deposit_to_blockradar(
    req: DepositRequest,
    account_manager: Any = Depends(require_account_manager),
) -> dict[str, str]:
    from engine.accounts import AccountRole

    try:
        role = AccountRole(req.role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown role: {req.role}")

    if not settings.blockradar_deposit_address:
        raise HTTPException(status_code=503, detail="BLOCKRADAR_DEPOSIT_ADDRESS not configured")

    token_address = get_deposit_token_address(req.token)
    try:
        tx_hash = await account_manager.transfer_erc20(
            role=role,
            token_address=token_address,
            to_address=settings.blockradar_deposit_address,
            amount=req.amount,
        )
        return {"status": "sent", "tx_hash": tx_hash, "to": settings.blockradar_deposit_address}
    except Exception as exc:
        logger.error("blockradar_deposit_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/venues/quidax/deposit-address/{currency}")
async def get_quidax_deposit_address(currency: str) -> dict[str, Any]:
    if not settings.quidax_deposit_address:
        raise HTTPException(status_code=503, detail="QUIDAX_DEPOSIT_ADDRESS not configured")

    return {
        "status": "success",
        "data": {
            "currency": currency.upper(),
            "address": settings.quidax_deposit_address,
        },
    }


@router.post("/webhooks/quidax")
async def quidax_webhook(
    event: dict[str, Any],
    runtime: EngineRuntime = Depends(get_runtime),
) -> dict[str, str]:
    if "quidax" in runtime.venues:
        await cast(WebhookVenue, runtime.venues["quidax"]).handle_webhook(event)
    if runtime.quidax_lp is not None:
        await cast(WebhookVenue, runtime.quidax_lp).handle_webhook(event)
    return {"status": "ok"}


@router.post("/venues/{venue}/sync", dependencies=[Depends(verify_token)])
async def trigger_venue_sync(
    venue: str,
    runtime: EngineRuntime = Depends(get_runtime),
) -> dict[str, str]:
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    try:
        ref_price = await get_reference_price_ngn(runtime)
        venue_adapter = runtime.venues[venue]
        if hasattr(venue_adapter, "sync_order_ladder") and ref_price:
            await cast(SyncOrderLadderVenue, venue_adapter).sync_order_ladder(ref_price)
        else:
            await venue_adapter.get_position()

        logger.info("manual_sync_triggered", venue=venue)
        return {"status": "synced", "venue": venue}
    except Exception as exc:
        logger.error("manual_sync_failed", venue=venue, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/venues/{venue}/orders", response_model=VenueOrdersResponse, dependencies=[Depends(verify_token)])
async def get_venue_orders(
    venue: str,
    runtime: EngineRuntime = Depends(get_runtime),
) -> VenueOrdersResponse:
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    venue_adapter = runtime.venues[venue]
    get_open_order_summaries = getattr(venue_adapter, "get_open_order_summaries", None)
    if callable(get_open_order_summaries):
        try:
            orders = await get_open_order_summaries()
            return VenueOrdersResponse(
                venue=venue,
                market=getattr(venue_adapter, "market", None),
                count=len(orders),
                orders=orders,
            )
        except Exception as exc:
            logger.error("venue_order_summaries_failed", venue=venue, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    get_open_orders = getattr(venue_adapter, "get_open_orders", None)
    if callable(get_open_orders):
        try:
            raw_orders = await get_open_orders()
            orders = [
                summary
                for order in raw_orders
                if (summary := _normalize_generic_order_summary(order, venue)) is not None
            ]
            return VenueOrdersResponse(
                venue=venue,
                market=getattr(venue_adapter, "market", None),
                count=len(orders),
                orders=orders,
            )
        except Exception as exc:
            logger.error("venue_open_orders_failed", venue=venue, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    raise HTTPException(status_code=400, detail="Venue does not expose order inspection")


@router.get("/venues/{venue}/orders/debug", dependencies=[Depends(verify_token)])
async def get_venue_orders_debug(
    venue: str,
    runtime: EngineRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    venue_adapter = runtime.venues[venue]
    get_orders_debug = getattr(venue_adapter, "get_orders_debug", None)
    if not callable(get_orders_debug):
        raise HTTPException(status_code=400, detail="Venue does not expose order debug inspection")

    try:
        return await get_orders_debug()
    except Exception as exc:
        logger.error("venue_orders_debug_failed", venue=venue, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/venues/quidax/depth", response_model=OrderBookDepthResponse)
async def get_quidax_order_book_depth(
    limit: int = Query(50, le=200),
    runtime: EngineRuntime = Depends(get_runtime),
) -> OrderBookDepthResponse:
    if "quidax" not in runtime.venues:
        raise HTTPException(status_code=404, detail="Quidax venue not configured")

    try:
        depth = await cast(DepthVenue, runtime.venues["quidax"]).get_order_book_depth(limit=limit)
        if not depth:
            raise HTTPException(status_code=503, detail="Failed to fetch Quidax order book depth")

        return OrderBookDepthResponse(
            venue=depth.venue,
            pair=depth.pair,
            timestamp=depth.timestamp,
            bids=[OrderBookLevel(price=bid.price, amount=bid.amount) for bid in depth.bids],
            asks=[OrderBookLevel(price=ask.price, amount=ask.amount) for ask in depth.asks],
        )
    except Exception as exc:
        logger.error("quidax_depth_route_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
