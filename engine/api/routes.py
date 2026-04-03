"""FastAPI routes for trading engine API."""

import time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, field_validator
from web3 import Web3
import structlog

from engine.config import settings
from engine.db.repository import DatabaseRepository
from engine.runtime import EngineRuntime
from engine.venues.dex.lp_v4 import V4LPAdapter
from engine.config import DexParams
from engine.api.schemas import (
    PriceQuote,
    Position,
    VenueStatus,
    VenuePriceResponse,
    SystemStatus,
    CexParams,
    Alert,
    GlobalPosition,
    ArbitrageParams,
    ArbitrageHistoryItem,
    ArbitrageOpportunity,
    ArbitrageStatus,
    AccountInfo,
    AccountBalanceResponse,
    AccountThresholds,
    NormalizedPriceResponse,
    BlendedPriceResponse,
    DexArbOpportunity,
    OrderBookDepthResponse,
)

logger = structlog.get_logger()
security = HTTPBearer()

router = APIRouter()


def get_runtime(request: Request) -> EngineRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Engine runtime not configured")
    return runtime


def get_repository(runtime: EngineRuntime = Depends(get_runtime)) -> DatabaseRepository:
    return runtime.db


def require_scheduler(runtime: EngineRuntime = Depends(get_runtime)):
    return runtime.scheduler


def require_price_aggregator(runtime: EngineRuntime = Depends(get_runtime)):
    if runtime.price_aggregator is None:
        raise HTTPException(status_code=503, detail="Price aggregator not configured")
    return runtime.price_aggregator


def require_blended_calculator(runtime: EngineRuntime = Depends(get_runtime)):
    if runtime.blended_calculator is None:
        raise HTTPException(status_code=503, detail="Blended price calculator not configured")
    return runtime.blended_calculator


def require_normalizer(runtime: EngineRuntime = Depends(get_runtime)):
    if runtime.normalizer is None:
        raise HTTPException(status_code=503, detail="Price normalizer not configured")
    return runtime.normalizer


def require_arbitrage_engine(runtime: EngineRuntime = Depends(get_runtime)):
    if runtime.arbitrage_engine is None:
        raise HTTPException(status_code=503, detail="Arbitrage engine not configured")
    return runtime.arbitrage_engine


def require_account_manager(runtime: EngineRuntime = Depends(get_runtime)):
    if runtime.account_manager is None:
        raise HTTPException(status_code=503, detail="Account manager not configured")
    return runtime.account_manager


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify API token for protected routes."""
    if not settings.engine_api_token:
        raise HTTPException(status_code=500, detail="DASHBOARD_API_TOKEN is not configured")
    if credentials.credentials != settings.engine_api_token:
        raise HTTPException(status_code=401, detail="Invalid token")
    return True


async def _get_cngn_usd_rate(runtime: EngineRuntime) -> Decimal:
    """Get cNGN/USD rate, preferring blended VWAP with single-venue fallback."""
    if runtime.blended_calculator:
        try:
            blended = await runtime.blended_calculator.get_blended_price()
            if blended.vwap > 0:
                return blended.vwap
        except Exception as e:
            logger.warning("blended_price_unavailable", error=str(e))

    if runtime.price_aggregator:
        # Quidax reports cNGN/USDT directly (mid ≈ 0.0007)
        quidax = runtime.price_aggregator.get_price("quidax")
        if quidax and quidax.quote and quidax.quote.mid > 0:
            return quidax.quote.mid

        # Bybit reports USDT/NGN, invert to get cNGN/USD (cNGN ≈ NGN)
        bybit = runtime.price_aggregator.get_price("bybit")
        if bybit and bybit.quote and bybit.quote.mid > 0:
            return Decimal("1") / bybit.quote.mid

    return Decimal("0")


async def _get_reference_price_ngn(runtime: EngineRuntime) -> Optional[Decimal]:
    """Get USDT/NGN reference price for CEX/rate syncing."""
    if runtime.blended_calculator:
        try:
            blended = await runtime.blended_calculator.get_blended_price()
            if blended.reference_price_ngn > 0:
                return blended.reference_price_ngn
        except Exception as e:
            logger.warning("blended_reference_price_unavailable", error=str(e))

    if runtime.price_aggregator:
        # Bybit reports USDT/NGN directly (mid ≈ 1436)
        bybit = runtime.price_aggregator.get_price("bybit")
        if bybit and bybit.quote and bybit.quote.mid > 0:
            return bybit.quote.mid

        # Quidax reports cNGN/USDT, invert to get USDT/NGN (cNGN ≈ NGN)
        quidax = runtime.price_aggregator.get_price("quidax")
        if quidax and quidax.quote and quidax.quote.mid > 0:
            return Decimal("1") / quidax.quote.mid

    return None


# === Status Routes ===


@router.get("/status", response_model=SystemStatus)
async def get_status(runtime: EngineRuntime = Depends(get_runtime)):
    """Get overall system status including per-venue prices."""
    trading_enabled = await runtime.db.system_state.get_system_state("trading_enabled")

    venue_prices = runtime.price_aggregator.get_all_prices() if runtime.price_aggregator else {}

    venue_statuses = []
    for name, venue in runtime.venues.items():
        try:
            position = await venue.get_position()
        except Exception:
            position = None

        price_data = venue_prices.get(name)
        price_response = None
        if price_data:
            price_response = VenuePriceResponse(
                venue=price_data.venue,
                pair=price_data.pair,
                quote=price_data.quote,
                error=price_data.error,
                age_seconds=price_data.age_seconds,
            )

        venue_statuses.append(
            VenueStatus(
                name=name,
                enabled=venue.enabled,
                paused=venue.paused,
                position=position,
                price=price_response,
                params=venue.params.model_dump() if hasattr(venue, "params") and venue.params else None,
            )
        )

    # Add price-only venues (e.g. bybit) not in trading venues
    for name, price_data in venue_prices.items():
        if name not in runtime.venues:
            venue_statuses.append(
                VenueStatus(
                    name=name,
                    enabled=True,
                    paused=False,
                    position=None,
                    price=VenuePriceResponse(
                        venue=price_data.venue,
                        pair=price_data.pair,
                        quote=price_data.quote,
                        error=price_data.error,
                        age_seconds=price_data.age_seconds,
                    ),
                )
            )

    return SystemStatus(
        trading_enabled=trading_enabled != "false",
        uptime=int(time.time() - runtime.start_time),
        venues=venue_statuses,
        last_price_update=int(runtime.price_aggregator.last_fetch_time * 1000) if runtime.price_aggregator else None,
    )


@router.get("/prices", response_model=list[VenuePriceResponse])
async def get_all_prices(price_aggregator=Depends(require_price_aggregator)):
    """Get current prices from all venues."""
    try:
        venue_prices = await price_aggregator.fetch_all()
        return [
            VenuePriceResponse(
                venue=price.venue,
                pair=price.pair,
                quote=price.quote,
                error=price.error,
                age_seconds=price.age_seconds,
            )
            for price in venue_prices.values()
        ]
    except Exception as e:
        logger.error("prices_fetch_failed", error=str(e))
        raise HTTPException(status_code=503, detail="Price fetch failed")


@router.get("/prices/blended", response_model=BlendedPriceResponse)
async def get_blended_price(blended_calculator=Depends(require_blended_calculator)):
    """Get the blended composite price (TWAP + VWAP across all venues)."""
    try:
        blended = await blended_calculator.get_blended_price()
        return BlendedPriceResponse(
            vwap=blended.vwap,
            twap_5m=blended.twap_5m,
            twap_1h=blended.twap_1h,
            reference_price_ngn=blended.reference_price_ngn,
            venue_prices=blended.venue_prices,
            timestamp=blended.timestamp,
            num_sources=blended.num_sources,
            total_venues=blended.total_venues,
            confidence=blended.confidence,
            dex_volume_24h_usd=blended.dex_volume_24h_usd,
        )
    except Exception as e:
        logger.error("blended_price_fetch_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prices/normalized", response_model=list[NormalizedPriceResponse])
async def get_normalized_prices(
    normalizer=Depends(require_normalizer),
    price_aggregator=Depends(require_price_aggregator),
):
    """Get all venue prices normalized to cNGN/USD basis."""
    try:
        venue_prices = await price_aggregator.fetch_all()
        normalized = normalizer.normalize(venue_prices)

        return [
            NormalizedPriceResponse(
                venue=np.venue,
                cngn_usd=np.cngn_usd,
                basis=np.basis,
                raw_mid=np.raw_quote.mid,
                timestamp=np.timestamp,
            )
            for np in normalized.values()
        ]
    except Exception as e:
        logger.error("normalized_prices_fetch_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/prices/refresh", response_model=list[VenuePriceResponse])
async def refresh_prices(price_aggregator=Depends(require_price_aggregator)):
    """Force refresh prices from all venues."""
    try:
        venue_prices = await price_aggregator.fetch_all()
        return [
            VenuePriceResponse(
                venue=price.venue,
                pair=price.pair,
                quote=price.quote,
                error=price.error,
                age_seconds=price.age_seconds,
            )
            for price in venue_prices.values()
        ]
    except Exception as e:
        logger.error("prices_refresh_failed", error=str(e))
        raise HTTPException(status_code=503, detail="Price refresh failed")


@router.get("/price/history")
async def get_price_history(
    venue: Optional[str] = Query(None, description="Filter by venue"),
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(100, le=1000),
    db: DatabaseRepository = Depends(get_repository),
):
    """Get price history from stored snapshots."""
    return await db.prices.get_price_history(from_ts, to_ts, limit)


@router.get("/prices/{venue}", response_model=VenuePriceResponse)
async def get_venue_price(venue: str, price_aggregator=Depends(require_price_aggregator)):
    """Get current price from a specific venue."""
    price = price_aggregator.get_price(venue)
    if not price:
        raise HTTPException(status_code=404, detail=f"Venue '{venue}' not found or no price available")

    return VenuePriceResponse(
        venue=price.venue,
        pair=price.pair,
        quote=price.quote,
        error=price.error,
        age_seconds=price.age_seconds,
    )


# === Position Routes ===


@router.get("/positions")
async def get_all_positions(runtime: EngineRuntime = Depends(get_runtime)):
    """Get positions from all venues."""
    positions = []
    for name, venue in runtime.venues.items():
        try:
            pos = await venue.get_position()
            positions.append(pos.model_dump())
        except Exception as e:
            logger.error("position_fetch_failed", venue=name, error=str(e))
    return positions


@router.get("/positions/global", response_model=GlobalPosition)
async def get_global_position(runtime: EngineRuntime = Depends(get_runtime)):
    """Get aggregated global position across all venues."""
    total_cngn = Decimal("0")
    total_usdt = Decimal("0")
    total_usdc = Decimal("0")

    for name, venue in runtime.venues.items():
        try:
            pos = await venue.get_position()
            total_cngn += pos.balances.get("cngn", Decimal("0"))
            total_usdt += pos.balances.get("usdt", Decimal("0"))
            total_usdc += pos.balances.get("usdc", Decimal("0"))
        except Exception as e:
            logger.warning("position_fetch_failed_global", venue=name, error=str(e))

    cngn_usd_rate = await _get_cngn_usd_rate(runtime)

    if cngn_usd_rate > 0:
        cngn_usd_value = total_cngn * cngn_usd_rate
        total_usd_value = cngn_usd_value + total_usdt + total_usdc
    else:
        cngn_usd_value = Decimal("0")
        total_usd_value = total_usdt + total_usdc

    delta_ratio = cngn_usd_value / total_usd_value if total_usd_value > 0 else Decimal("0")

    return GlobalPosition(
        total_cngn=total_cngn,
        total_usdt=total_usdt,
        total_usdc=total_usdc,
        total_usd_value=total_usd_value,
        delta_ratio=delta_ratio,
        target_delta=Decimal(str(settings.target_delta_ratio)),
    )


@router.get("/positions/{venue}", response_model=Position)
async def get_venue_position(venue: str, runtime: EngineRuntime = Depends(get_runtime)):
    """Get position from a specific venue."""
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    try:
        return await runtime.venues[venue].get_position()
    except Exception as e:
        logger.error("position_fetch_failed", venue=venue, error=str(e))
        raise HTTPException(status_code=503, detail=str(e))


# === Trading Control Routes ===


@router.post("/trading/pause", dependencies=[Depends(verify_token)])
async def pause_trading(scheduler=Depends(require_scheduler)):
    """Pause all trading globally."""
    await scheduler.pause()
    return {"status": "paused"}


@router.post("/trading/resume", dependencies=[Depends(verify_token)])
async def resume_trading(scheduler=Depends(require_scheduler)):
    """Resume all trading globally."""
    await scheduler.resume()
    return {"status": "running"}


# === Venue Control Routes ===


class WithdrawRequest(BaseModel):
    to_address: str  # required — forces explicit destination, prevents accidental re-deployment

    @field_validator("to_address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        if not Web3.is_address(v):
            raise ValueError(f"Invalid Ethereum address: {v!r}")
        return v


@router.post("/venues/{venue}/withdraw", dependencies=[Depends(verify_token)])
async def withdraw_venue_position(
    venue: str,
    body: WithdrawRequest,
    runtime: EngineRuntime = Depends(get_runtime),
):
    """Remove all active LP positions for a DEX venue and send tokens to the specified address."""
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    adapter = runtime.venues[venue]
    if not isinstance(adapter, V4LPAdapter):
        raise HTTPException(status_code=400, detail=f"{venue} is not a DEX venue")

    token_ids = adapter.get_owned_positions()
    if not token_ids:
        return {"venue": venue, "removed": [], "message": "No active positions"}

    results = []
    for token_id in token_ids:
        result = await adapter.remove_position(token_id, recipient=body.to_address)
        results.append({"token_id": token_id, "status": result.status, "hash": result.hash})
        if result.status != "confirmed":
            logger.error("withdraw_position_failed", venue=venue, token_id=token_id, error=result.error)

    runtime.scheduler.broadcast({
        "type": "alert",
        "severity": "warning",
        "message": f"LP positions withdrawn on {venue} to {body.to_address}: {[r['token_id'] for r in results]}",
    })
    logger.info("venue_positions_withdrawn", venue=venue, to_address=body.to_address, results=results)
    return {"venue": venue, "removed": results}


@router.post("/shutdown", dependencies=[Depends(verify_token)])
async def shutdown(unwind: bool = False, runtime: EngineRuntime = Depends(get_runtime)):
    """Stop the engine. If unwind=true, removes all LP positions first."""
    import asyncio, os, signal

    if unwind:
        dex_venues = {k: v for k, v in runtime.venues.items() if isinstance(v, V4LPAdapter)}
        unwind_results = {}
        for venue_name, adapter in dex_venues.items():
            token_ids = adapter.get_owned_positions()
            removed = []
            for token_id in token_ids:
                result = await adapter.remove_position(token_id)
                removed.append({"token_id": token_id, "status": result.status, "hash": result.hash})
                logger.info("shutdown_unwind_position", venue=venue_name, token_id=token_id, status=result.status)
            unwind_results[venue_name] = removed

        runtime.scheduler.broadcast({
            "type": "alert",
            "severity": "warning",
            "message": "Engine shutting down — all LP positions unwound.",
        })
        logger.info("shutdown_unwind_complete", results=unwind_results)
    else:
        runtime.scheduler.broadcast({
            "type": "alert",
            "severity": "warning",
            "message": "Engine shutting down — LP positions left in place.",
        })

    logger.info("shutdown_requested", unwind=unwind)

    # Trigger graceful shutdown after response is sent
    asyncio.get_event_loop().call_later(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM))

    return {"status": "shutting_down", "unwind": unwind}


@router.post("/venues/{venue}/pause", dependencies=[Depends(verify_token)])
async def pause_venue(venue: str, runtime: EngineRuntime = Depends(get_runtime)):
    """Pause a specific venue."""
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    runtime.venues[venue].paused = True
    logger.info("venue_paused", venue=venue)
    return {"venue": venue, "paused": True}


@router.post("/venues/{venue}/resume", dependencies=[Depends(verify_token)])
async def resume_venue(venue: str, runtime: EngineRuntime = Depends(get_runtime)):
    """Resume a specific venue."""
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    runtime.venues[venue].paused = False
    logger.info("venue_resumed", venue=venue)
    return {"venue": venue, "paused": False}


@router.put("/venues/{venue}/params", dependencies=[Depends(verify_token)])
async def update_venue_params(
    venue: str,
    params: dict,
    runtime: EngineRuntime = Depends(get_runtime),
    db: DatabaseRepository = Depends(get_repository),
):
    """Update venue parameters."""
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    await db.venue_config.update_venue_config(venue, params)

    venue_adapter = runtime.venues[venue]
    if hasattr(venue_adapter, "params"):
        if isinstance(venue_adapter, V4LPAdapter):
            merged = venue_adapter.params.model_dump()
            merged.update(params)
            venue_adapter.params = DexParams(**merged)
        else:
            venue_adapter.params = CexParams(**params)

    logger.info("venue_params_updated", venue=venue, params=params)
    return {"venue": venue, "params": params}


# === Deposit Routes ===


class DepositRequest(BaseModel):
    role: str  # Account role to send from (e.g. "uni-base-lp")
    token: str  # Token symbol: "USDC" or "cNGN"
    amount: Decimal


def _get_deposit_token_address(token: str) -> str:
    token_map = {
        "USDC": settings.usdc_contract_address,
        "cNGN": settings.cngn_contract_address,
    }
    address = token_map.get(token)
    if not address:
        raise HTTPException(status_code=400, detail=f"Unsupported token: {token}. Use USDC or cNGN.")
    return address


@router.post("/venues/blockradar/deposit", dependencies=[Depends(verify_token)])
async def deposit_to_blockradar(req: DepositRequest, account_manager=Depends(require_account_manager)):
    """Transfer USDC or cNGN from an HD wallet account to the Blockradar deposit address."""
    from engine.accounts import AccountRole

    try:
        role = AccountRole(req.role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown role: {req.role}")

    if not settings.blockradar_deposit_address:
        raise HTTPException(status_code=503, detail="BLOCKRADAR_DEPOSIT_ADDRESS not configured")

    token_address = _get_deposit_token_address(req.token)

    try:
        tx_hash = await account_manager.transfer_erc20(
            role=role,
            token_address=token_address,
            to_address=settings.blockradar_deposit_address,
            amount=req.amount,
        )
        return {"status": "sent", "tx_hash": tx_hash, "to": settings.blockradar_deposit_address}
    except Exception as e:
        logger.error("blockradar_deposit_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/venues/quidax/deposit-address/{currency}")
async def get_quidax_deposit_address(currency: str):
    """Get the static deposit address for Quidax."""
    if not settings.quidax_deposit_address:
        raise HTTPException(status_code=503, detail="QUIDAX_DEPOSIT_ADDRESS not configured")

    return {
        "status": "success",
        "data": {
            "currency": currency.upper(),
            "address": settings.quidax_deposit_address
        }
    }


@router.post("/webhooks/quidax")
async def quidax_webhook(event: dict, runtime: EngineRuntime = Depends(get_runtime)):
    """Handle Quidax webhook events (order fills, deposit addresses, etc.)."""
    if "quidax" in runtime.venues:
        await runtime.venues["quidax"].handle_webhook(event)
    if runtime.quidax_lp is not None:
        await runtime.quidax_lp.handle_webhook(event)
    return {"status": "ok"}


# === Manual Action Routes ===


@router.post("/venues/{venue}/sync", dependencies=[Depends(verify_token)])
async def trigger_venue_sync(venue: str, runtime: EngineRuntime = Depends(get_runtime)):
    """Manually trigger sync for a venue."""
    if venue not in runtime.venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    try:
        ref_price = await _get_reference_price_ngn(runtime)

        venue_adapter = runtime.venues[venue]
        if hasattr(venue_adapter, "sync_order_ladder") and ref_price:
            await venue_adapter.sync_order_ladder(ref_price)
        else:
            await venue_adapter.get_position()

        logger.info("manual_sync_triggered", venue=venue)
        return {"status": "synced", "venue": venue}

    except Exception as e:
        logger.error("manual_sync_failed", venue=venue, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/venues/quidax/depth", response_model=OrderBookDepthResponse)
async def get_quidax_order_book_depth(limit: int = Query(50, le=200), runtime: EngineRuntime = Depends(get_runtime)):
    """Get the live Level 2 Order Book Depth from Quidax."""
    if "quidax" not in runtime.venues:
        raise HTTPException(status_code=404, detail="Quidax venue not configured")
    
    try:
        depth = await runtime.venues["quidax"].get_order_book_depth(limit=limit)
        if not depth:
            raise HTTPException(status_code=503, detail="Failed to fetch Quidax order book depth")
            
        return OrderBookDepthResponse(
            venue=depth.venue,
            pair=depth.pair,
            timestamp=depth.timestamp,
            bids=[{"price": b.price, "amount": b.amount} for b in depth.bids],
            asks=[{"price": a.price, "amount": a.amount} for a in depth.asks]
        )
    except Exception as e:
        logger.error("quidax_depth_route_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# === Action Log Routes ===


@router.get("/actions")
async def get_actions(
    venue: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: DatabaseRepository = Depends(get_repository),
):
    """Get action history."""
    return await db.actions.get_actions(venue, action_type, limit)


# === Alert Routes ===


@router.get("/alerts", response_model=list[Alert])
async def get_alerts(limit: int = Query(20, le=100), db: DatabaseRepository = Depends(get_repository)):
    """Get recent alerts."""
    return await db.alerts.get_alerts(limit)


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: int, db: DatabaseRepository = Depends(get_repository)):
    """Acknowledge an alert."""
    await db.alerts.acknowledge_alert(alert_id)
    return {"status": "acknowledged", "alert_id": alert_id}


# === Arbitrage Routes ===


@router.get("/arbitrage/status", response_model=ArbitrageStatus)
async def get_arbitrage_status(arbitrage_engine=Depends(require_arbitrage_engine)):
    """Get arbitrage engine status."""
    try:
        return await arbitrage_engine.get_status()
    except Exception as e:
        logger.error("arbitrage_status_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/arbitrage/opportunities", response_model=list[ArbitrageOpportunity])
async def get_arbitrage_opportunities(
    status: Optional[str] = Query(None, description="Filter by status"),
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(50, le=200),
    db: DatabaseRepository = Depends(get_repository),
):
    """Get detected arbitrage opportunities."""
    return await db.arbitrage.get_arbitrage_opportunities(status, from_ts, to_ts, limit)


@router.get("/arbitrage/dex-opportunities", response_model=list[DexArbOpportunity])
async def get_dex_arbitrage_opportunities(
    status: Optional[str] = Query(None, description="Filter by status"),
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(50, le=200),
    db: DatabaseRepository = Depends(get_repository),
):
    """Get detected DEX arbitrage opportunities."""
    return await db.arbitrage.get_dex_arbitrage_opportunities(status, from_ts, to_ts, limit)


@router.get("/arbitrage/history", response_model=list[ArbitrageHistoryItem])
async def get_arbitrage_history(
    pipeline: Optional[str] = Query(None, description="Filter by pipeline"),
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(30, le=200),
    db: DatabaseRepository = Depends(get_repository),
):
    """Get grouped arbitrage lifecycle history."""
    return await db.history.get_arbitrage_history(pipeline, from_ts, to_ts, limit)

@router.get("/arbitrage/liquidation")
async def get_liquidation_valuation(runtime: EngineRuntime = Depends(get_runtime)):
    """
    Returns the mark-to-market USD value of all cNGN holdings across every venue,
    computed on-demand from the live Quidax order book and cached pool state.
    """
    from decimal import Decimal
    import time
    from engine.market.pool_state import get_cached_pool_state
    from engine.arb.valuation import cex_holdings_value, dex_holdings_value
    from engine.arb.detection.cex_dex import QUIDAX_FEE
    from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
    from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG

    if runtime.account_manager is None:
        return {"error": "account_manager_not_configured", "venues": {}}

    quidax_venue = runtime.venues.get("quidax")
    quidax_asks = []
    if quidax_venue:
        try:
            depth = await quidax_venue.get_order_book_depth(limit=50)
            if depth and depth.asks:
                quidax_asks = depth.asks
        except Exception:
            pass

    bsc_sqrt, bsc_liq, _, bsc_fee = get_cached_pool_state(UNISWAP_BSC_POOL_READ_CONFIG.pool_address)
    base_sqrt, base_liq, _, base_fee = get_cached_pool_state(UNISWAP_BASE_POOL_READ_CONFIG.pool_address)

    try:
        balances = await runtime.account_manager.check_all_balances(runtime.token_contracts)
    except Exception as e:
        return {"error": str(e), "venues": {}}

    # Append the Quidax exchange account (CEX balance — not HD-derived, fetched via API).
    if quidax_venue:
        try:
            qx_pos = await quidax_venue.get_position()
            if qx_pos and qx_pos.balances:
                from types import SimpleNamespace
                balances = list(balances) + [SimpleNamespace(
                    role="quidax-exchange",
                    token_balances={
                        "cNGN": Decimal(str(qx_pos.balances.get("cngn", 0))),
                        "USDT": Decimal(str(qx_pos.balances.get("usdt", 0))),
                    }
                )]
        except Exception:
            pass

    result = {}
    for bal in balances:
        role = bal.role
        tokens = bal.token_balances or {}
        venue_result = {}

        for token, amt in tokens.items():
            amount = Decimal(str(amt)) if amt is not None else Decimal(0)
            if token.lower() == "cngn" and amount > 0:
                value_usd = Decimal(0)
                try:
                    if role in ("quidax-exchange", "quidax-lp", "quidax-trade-fund") and quidax_asks:
                        value_usd = cex_holdings_value(quidax_asks, amount, QUIDAX_FEE)
                    elif role in ("uni-bsc-trade", "uni-bsc-lp") and bsc_sqrt:
                        value_usd = dex_holdings_value(amount, bsc_sqrt, bsc_liq, bsc_fee, 18, 6, cngn_is_token0=False)
                    elif role in ("uni-base-trade", "uni-base-lp") and base_sqrt:
                        value_usd = dex_holdings_value(amount, base_sqrt, base_liq, base_fee, 6, 6, cngn_is_token0=True)
                except Exception:
                    value_usd = Decimal(0)
                venue_result[token] = {"amount": float(amount), "value_usd": float(value_usd)}
            else:
                a = float(amount) if amt is not None else 0.0
                venue_result[token] = {"amount": a, "value_usd": a}

        result[role] = venue_result

    return {"timestamp": int(time.time() * 1000), "venues": result}


@router.get("/arbitrage/opportunities/{opportunity_id}", response_model=ArbitrageOpportunity)
async def get_arbitrage_opportunity(opportunity_id: str, db: DatabaseRepository = Depends(get_repository)):
    """Get a specific arbitrage opportunity."""
    opp = await db.arbitrage.get_arbitrage_opportunity(opportunity_id)
    if opp is not None:
        return opp
    raise HTTPException(status_code=404, detail="Opportunity not found")


@router.post("/arbitrage/enable", dependencies=[Depends(verify_token)])
async def enable_arbitrage(arbitrage_engine=Depends(require_arbitrage_engine)):
    """Enable arbitrage scanning."""
    arbitrage_engine.enable()
    logger.info("arbitrage_enabled_via_api")
    return {"status": "enabled"}


@router.post("/arbitrage/disable", dependencies=[Depends(verify_token)])
async def disable_arbitrage(arbitrage_engine=Depends(require_arbitrage_engine)):
    """Disable arbitrage scanning."""
    arbitrage_engine.disable()
    logger.info("arbitrage_disabled_via_api")
    return {"status": "disabled"}


@router.post("/arbitrage/execute-cex-dex/enable", dependencies=[Depends(verify_token)])
async def enable_execute_cex_dex(arbitrage_engine=Depends(require_arbitrage_engine)):
    """Enable execution for CEX-DEX arbitrage."""
    arbitrage_engine.set_execution_enabled("cex_dex", True)
    return {"status": "enabled"}


@router.post("/arbitrage/execute-cex-dex/disable", dependencies=[Depends(verify_token)])
async def disable_execute_cex_dex(arbitrage_engine=Depends(require_arbitrage_engine)):
    """Disable execution for CEX-DEX arbitrage."""
    arbitrage_engine.set_execution_enabled("cex_dex", False)
    return {"status": "disabled"}


@router.post("/arbitrage/execute-dex-dex/enable", dependencies=[Depends(verify_token)])
async def enable_execute_dex_dex(arbitrage_engine=Depends(require_arbitrage_engine)):
    """Enable execution for DEX-DEX arbitrage."""
    arbitrage_engine.set_execution_enabled("dex_dex", True)
    return {"status": "enabled"}


@router.post("/arbitrage/execute-dex-dex/disable", dependencies=[Depends(verify_token)])
async def disable_execute_dex_dex(arbitrage_engine=Depends(require_arbitrage_engine)):
    """Disable execution for DEX-DEX arbitrage."""
    arbitrage_engine.set_execution_enabled("dex_dex", False)
    return {"status": "disabled"}


@router.put("/arbitrage/params", dependencies=[Depends(verify_token)])
async def update_arbitrage_params(params: ArbitrageParams, arbitrage_engine=Depends(require_arbitrage_engine)):
    """Update arbitrage parameters."""
    arbitrage_engine.update_params(params)
    logger.info("arbitrage_params_updated_via_api")
    return {"status": "updated", "params": params.model_dump()}


@router.post("/arbitrage/reset-circuit-breaker", dependencies=[Depends(verify_token)])
async def reset_arbitrage_circuit_breaker(arbitrage_engine=Depends(require_arbitrage_engine)):
    """Manually reset the arbitrage circuit breaker."""
    arbitrage_engine.reset_circuit_breaker()
    logger.info("arbitrage_circuit_breaker_reset_via_api")
    return {"status": "reset"}


@router.post("/arbitrage/scan", dependencies=[Depends(verify_token)])
async def trigger_arbitrage_scan(arbitrage_engine=Depends(require_arbitrage_engine)):
    """Manually trigger an arbitrage scan."""
    try:
        opportunities = await arbitrage_engine.scan()
        return {
            "status": "scanned",
            "opportunities_found": len(opportunities),
            "opportunities": [opp.model_dump() for opp in opportunities],
        }
    except Exception as e:
        logger.error("manual_arbitrage_scan_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# === Account Routes ===


@router.get("/accounts")
async def list_accounts(account_manager=Depends(require_account_manager)):
    """List all configured accounts."""
    from engine.accounts import AccountRole

    accounts = []
    for role in AccountRole:
        try:
            config = account_manager.get_config(role)
            accounts.append(AccountInfo(
                role=role.value,
                address=account_manager.get_address(role),
                derivation_path=config.derivation_path,
                chain_id=config.chain_id,
                tokens=config.tokens,
            ))
        except ValueError:
            continue

    return accounts


@router.get("/accounts/balances", response_model=list[AccountBalanceResponse])
async def get_all_account_balances(runtime: EngineRuntime = Depends(get_runtime)):
    """Get balances for all accounts."""
    if runtime.account_manager is None:
        raise HTTPException(status_code=503, detail="Account manager not configured")

    try:
        balances = await runtime.account_manager.check_all_balances(runtime.token_contracts)
        result = [
            AccountBalanceResponse(
                role=b.role,
                address=b.address,
                chain_id=b.chain_id,
                native_balance=b.native_balance,
                native_symbol=b.native_symbol,
                token_balances=b.token_balances,
                needs_refill=b.needs_refill,
                refill_reasons=b.refill_reasons,
            )
            for b in balances
        ]
        # Append Quidax exchange account balance (not HD-derived, fetched via API)
        quidax_adapter = runtime.venues.get("quidax")
        if quidax_adapter:
            try:
                pos = await quidax_adapter.get_position()
                # Normalize Quidax keys (API returns "cngn"/"usdt") to match HD accounts
                normalized = {"cNGN": pos.balances.get("cngn", Decimal("0")), "USDT": pos.balances.get("usdt", Decimal("0"))}
                result.append(AccountBalanceResponse(
                    role="quidax-exchange",
                    address=settings.quidax_deposit_address,
                    chain_id=0,
                    native_balance=Decimal("0"),
                    native_symbol="",
                    token_balances=normalized,
                    needs_refill=False,
                    refill_reasons=[],
                ))
            except Exception as e:
                logger.warning("quidax_exchange_balance_fetch_failed", error=str(e))
        return result
    except Exception as e:
        logger.error("balance_fetch_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accounts/{role}", response_model=AccountInfo)
async def get_account(role: str, account_manager=Depends(require_account_manager)):
    """Get account info for a specific role."""
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
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/accounts/{role}/balance", response_model=AccountBalanceResponse)
async def get_account_balance(role: str, runtime: EngineRuntime = Depends(get_runtime)):
    """Get balance for a specific account."""
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
    except Exception as e:
        logger.error("balance_fetch_failed", role=role, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/accounts/{role}/thresholds", dependencies=[Depends(verify_token)])
async def update_account_thresholds(
    role: str,
    thresholds: AccountThresholds,
    account_manager=Depends(require_account_manager),
):
    """Update refill thresholds for an account."""
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# === Pool Metrics ===

# Static config: chain and pool id for each DEX we track
_DEX_POOLS = [
    {"venue": "uni-base", "chain": "base", "pool_address": settings.uni_base_pool_id},
    {"venue": "uni-bsc", "chain": "bsc", "pool_address": settings.uni_bsc_pool_id},
]


@router.get("/pool-metrics/history")
async def get_pool_metrics_history(
    minutes: int = Query(1440, ge=1440, le=43200),
    db: DatabaseRepository = Depends(get_repository),
):
    """Return historical pool TVL and volume from stored position snapshots."""
    from_ts = int((time.time() - minutes * 60) * 1000)
    return await db.pool_metrics.get_pool_metrics_history(["uni-base", "uni-bsc"], from_ts)


@router.get("/pool-metrics")
async def get_pool_metrics(runtime: EngineRuntime = Depends(get_runtime)):
    """Return 24h volume and TVL for all DEX pools (reuses venue adapter cache)."""
    results = []
    for pool in _DEX_POOLS:
        name = pool["venue"]
        venue = runtime.venues.get(name)
        entry = {"venue": name, "chain": pool["chain"], "position_value_usd": None, "volume_24h_usd": None}
        results.append(entry)
    return results


# === Health Check ===


@router.get("/health")
async def health_check(runtime: EngineRuntime = Depends(get_runtime)):
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": int(time.time() * 1000),
        "trading_enabled": runtime.scheduler.trading_enabled,
        "arbitrage_enabled": runtime.arbitrage_engine.enabled if runtime.arbitrage_engine else False,
    }
