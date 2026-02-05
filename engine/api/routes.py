"""FastAPI routes for trading engine API."""

import time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import structlog

from engine.config import settings
from engine.db import get_db
from engine.api.schemas import (
    PriceQuote,
    Position,
    VenueStatus,
    SystemStatus,
    DexParams,
    CexParams,
    WalletParams,
    Alert,
    GlobalPosition,
)

logger = structlog.get_logger()
security = HTTPBearer()

router = APIRouter()

# These will be set by main.py during app initialization
_scheduler = None
_venues = None
_price_feed = None
_start_time = None


def init_routes(scheduler, venues, price_feed, start_time):
    """Initialize route dependencies."""
    global _scheduler, _venues, _price_feed, _start_time
    _scheduler = scheduler
    _venues = venues
    _price_feed = price_feed
    _start_time = start_time


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify API token for protected routes."""
    if not settings.dashboard_api_token:
        # No token configured, allow all requests
        return True
    if credentials.credentials != settings.dashboard_api_token:
        raise HTTPException(status_code=401, detail="Invalid token")
    return True


# === Status Routes ===


@router.get("/status", response_model=SystemStatus)
async def get_status():
    """Get overall system status."""
    db = await get_db()

    trading_enabled = await db.get_system_state("trading_enabled")

    venue_statuses = []
    for name, venue in _venues.items():
        try:
            position = await venue.get_position()
        except Exception:
            position = None

        venue_statuses.append(
            VenueStatus(
                name=name,
                enabled=venue.enabled,
                paused=venue.paused,
                position=position,
            )
        )

    return SystemStatus(
        trading_enabled=trading_enabled != "false",
        uptime=int(time.time() - _start_time) if _start_time else 0,
        venues=venue_statuses,
    )


@router.get("/price", response_model=PriceQuote)
async def get_current_price():
    """Get current aggregated price."""
    try:
        price = await _price_feed.get_price()
        return price
    except Exception as e:
        logger.error("price_fetch_failed", error=str(e))
        raise HTTPException(status_code=503, detail="Price feed unavailable")


@router.get("/price/history")
async def get_price_history(
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(100, le=1000),
):
    """Get price history."""
    db = await get_db()
    return await db.get_price_history(from_ts, to_ts, limit)


# === Position Routes ===


@router.get("/positions")
async def get_all_positions():
    """Get positions from all venues."""
    positions = []
    for name, venue in _venues.items():
        try:
            pos = await venue.get_position()
            positions.append(pos.model_dump())
        except Exception as e:
            logger.error("position_fetch_failed", venue=name, error=str(e))
    return positions


@router.get("/positions/{venue}", response_model=Position)
async def get_venue_position(venue: str):
    """Get position from a specific venue."""
    if venue not in _venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    try:
        return await _venues[venue].get_position()
    except Exception as e:
        logger.error("position_fetch_failed", venue=venue, error=str(e))
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/positions/global", response_model=GlobalPosition)
async def get_global_position():
    """Get aggregated global position across all venues."""
    total_cngn = Decimal("0")
    total_usdt = Decimal("0")
    total_usdc = Decimal("0")

    for name, venue in _venues.items():
        try:
            pos = await venue.get_position()
            total_cngn += pos.balances.get("cngn", Decimal("0"))
            total_usdt += pos.balances.get("usdt", Decimal("0"))
            total_usdc += pos.balances.get("usdc", Decimal("0"))
        except Exception as e:
            logger.warning("position_fetch_failed_global", venue=name, error=str(e))

    # Get current price for USD value calculation
    try:
        price = await _price_feed.get_price()
        cngn_usd_rate = Decimal("1") / price.mid  # CNGN per USDT
        total_usd_value = (total_cngn * cngn_usd_rate) + total_usdt + total_usdc
    except Exception:
        total_usd_value = total_usdt + total_usdc  # Fallback

    # Calculate delta ratio (CNGN value / total value)
    try:
        cngn_usd_value = total_cngn * cngn_usd_rate
        delta_ratio = cngn_usd_value / total_usd_value if total_usd_value > 0 else Decimal("0")
    except Exception:
        delta_ratio = Decimal("0")

    return GlobalPosition(
        total_cngn=total_cngn,
        total_usdt=total_usdt,
        total_usdc=total_usdc,
        total_usd_value=total_usd_value,
        delta_ratio=delta_ratio,
        target_delta=Decimal(str(settings.target_delta_ratio)),
    )


# === Trading Control Routes ===


@router.post("/trading/pause", dependencies=[Depends(verify_token)])
async def pause_trading():
    """Pause all trading operations."""
    await _scheduler.pause()
    return {"status": "paused"}


@router.post("/trading/resume", dependencies=[Depends(verify_token)])
async def resume_trading():
    """Resume trading operations."""
    await _scheduler.resume()
    return {"status": "running"}


# === Venue Control Routes ===


@router.post("/venues/{venue}/pause", dependencies=[Depends(verify_token)])
async def pause_venue(venue: str):
    """Pause a specific venue."""
    if venue not in _venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    _venues[venue].paused = True
    logger.info("venue_paused", venue=venue)
    return {"venue": venue, "paused": True}


@router.post("/venues/{venue}/resume", dependencies=[Depends(verify_token)])
async def resume_venue(venue: str):
    """Resume a specific venue."""
    if venue not in _venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    _venues[venue].paused = False
    logger.info("venue_resumed", venue=venue)
    return {"venue": venue, "paused": False}


@router.put("/venues/{venue}/params", dependencies=[Depends(verify_token)])
async def update_venue_params(venue: str, params: dict):
    """Update venue parameters."""
    if venue not in _venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    db = await get_db()
    await db.update_venue_config(venue, params)

    # Update in-memory params if supported
    venue_adapter = _venues[venue]
    if hasattr(venue_adapter, "params"):
        # Map params to appropriate type
        if venue in ["aerodrome", "pancakeswap"]:
            venue_adapter.params = DexParams(**params)
        elif venue == "quidax":
            venue_adapter.params = CexParams(**params)
        elif venue == "blockradar":
            venue_adapter.params = WalletParams(**params)

    logger.info("venue_params_updated", venue=venue, params=params)
    return {"venue": venue, "params": params}


# === Manual Action Routes ===


@router.post("/venues/{venue}/sync", dependencies=[Depends(verify_token)])
async def trigger_venue_sync(venue: str):
    """Manually trigger sync for a venue."""
    if venue not in _venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    try:
        price = await _price_feed.get_price()

        if venue == "quidax":
            await _venues[venue].sync_order_ladder(price.mid)
        elif venue == "blockradar":
            await _venues[venue].sync_rates(price.mid)
        else:
            # DEX venues - just refresh position
            await _venues[venue].get_position()

        logger.info("manual_sync_triggered", venue=venue)
        return {"status": "synced", "venue": venue}

    except Exception as e:
        logger.error("manual_sync_failed", venue=venue, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# === Action Log Routes ===


@router.get("/actions")
async def get_actions(
    venue: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    """Get action history."""
    db = await get_db()
    return await db.get_actions(venue, action_type, limit)


# === Alert Routes ===


@router.get("/alerts", response_model=list[Alert])
async def get_alerts(limit: int = Query(20, le=100)):
    """Get recent alerts."""
    db = await get_db()
    return await db.get_alerts(limit)


@router.post("/alerts/{alert_id}/acknowledge", dependencies=[Depends(verify_token)])
async def acknowledge_alert(alert_id: int):
    """Acknowledge an alert."""
    db = await get_db()
    await db.acknowledge_alert(alert_id)
    return {"status": "acknowledged", "alert_id": alert_id}


# === Health Check ===


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": int(time.time() * 1000),
        "trading_enabled": _scheduler.trading_enabled if _scheduler else False,
    }
