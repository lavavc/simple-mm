"""FastAPI routes for trading engine API."""

import time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import structlog

from engine.config import settings
from engine.db import get_db
from engine.api.schemas import (
    PriceQuote,
    Position,
    VenueStatus,
    VenuePriceResponse,
    SystemStatus,
    DexParams,
    CexParams,
    Alert,
    GlobalPosition,
    ArbitrageParams,
    ArbitrageOpportunity,
    ArbitrageStatus,
    AccountInfo,
    AccountBalanceResponse,
    AccountThresholds,
    NormalizedPriceResponse,
    BlendedPriceResponse,
)

logger = structlog.get_logger()
security = HTTPBearer()

router = APIRouter()

# Injected by main.py during app initialization
_scheduler = None
_venues = None
_price_aggregator = None
_start_time = None
_arbitrage_engine = None
_account_manager = None
_token_contracts = None
_blended_calculator = None
_normalizer = None


def init_routes(
    scheduler,
    venues,
    price_aggregator,
    start_time,
    arbitrage_engine=None,
    account_manager=None,
    token_contracts=None,
    blended_calculator=None,
    normalizer=None,
):
    """Initialize route dependencies."""
    global _scheduler, _venues, _price_aggregator, _start_time, _arbitrage_engine
    global _account_manager, _token_contracts, _blended_calculator, _normalizer
    _scheduler = scheduler
    _venues = venues
    _price_aggregator = price_aggregator
    _start_time = start_time
    _arbitrage_engine = arbitrage_engine
    _account_manager = account_manager
    _token_contracts = token_contracts or {}
    _blended_calculator = blended_calculator
    _normalizer = normalizer


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify API token for protected routes."""
    if not settings.dashboard_api_token:
        return True
    if credentials.credentials != settings.dashboard_api_token:
        raise HTTPException(status_code=401, detail="Invalid token")
    return True


async def _get_cngn_usd_rate() -> Decimal:
    """Get cNGN/USD rate, preferring blended VWAP with single-venue fallback."""
    if _blended_calculator:
        try:
            blended = await _blended_calculator.get_blended_price()
            if blended.vwap > 0:
                return blended.vwap
        except Exception as e:
            logger.warning("blended_price_unavailable", error=str(e))

    if _price_aggregator:
        # Quidax reports cNGN/USDT directly (mid ≈ 0.0007)
        quidax = _price_aggregator.get_price("quidax")
        if quidax and quidax.quote and quidax.quote.mid > 0:
            return quidax.quote.mid

        # Bybit reports USDT/NGN, invert to get cNGN/USD (cNGN ≈ NGN)
        bybit = _price_aggregator.get_price("bybit")
        if bybit and bybit.quote and bybit.quote.mid > 0:
            return Decimal("1") / bybit.quote.mid

    return Decimal("0")


async def _get_reference_price_ngn() -> Optional[Decimal]:
    """Get USDT/NGN reference price for CEX/rate syncing."""
    if _blended_calculator:
        try:
            blended = await _blended_calculator.get_blended_price()
            if blended.reference_price_ngn > 0:
                return blended.reference_price_ngn
        except Exception as e:
            logger.warning("blended_reference_price_unavailable", error=str(e))

    if _price_aggregator:
        # Bybit reports USDT/NGN directly (mid ≈ 1436)
        bybit = _price_aggregator.get_price("bybit")
        if bybit and bybit.quote and bybit.quote.mid > 0:
            return bybit.quote.mid

        # Quidax reports cNGN/USDT, invert to get USDT/NGN (cNGN ≈ NGN)
        quidax = _price_aggregator.get_price("quidax")
        if quidax and quidax.quote and quidax.quote.mid > 0:
            return Decimal("1") / quidax.quote.mid

    return None


# === Status Routes ===


@router.get("/status", response_model=SystemStatus)
async def get_status():
    """Get overall system status including per-venue prices."""
    db = await get_db()
    trading_enabled = await db.get_system_state("trading_enabled")

    venue_prices = _price_aggregator.get_all_prices() if _price_aggregator else {}

    venue_statuses = []
    for name, venue in _venues.items():
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
            )
        )

    # Add price-only venues (e.g. bybit) not in trading venues
    for name, price_data in venue_prices.items():
        if name not in _venues:
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
        uptime=int(time.time() - _start_time) if _start_time else 0,
        venues=venue_statuses,
        last_price_update=int(_price_aggregator.last_fetch_time * 1000) if _price_aggregator else None,
    )


@router.get("/prices", response_model=list[VenuePriceResponse])
async def get_all_prices():
    """Get current prices from all venues."""
    if not _price_aggregator:
        raise HTTPException(status_code=503, detail="Price aggregator not configured")

    try:
        venue_prices = await _price_aggregator.fetch_all()
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
async def get_blended_price():
    """Get the blended composite price (TWAP + VWAP across all venues)."""
    if not _blended_calculator:
        raise HTTPException(status_code=503, detail="Blended price calculator not configured")

    try:
        blended = await _blended_calculator.get_blended_price()
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
        )
    except Exception as e:
        logger.error("blended_price_fetch_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prices/normalized", response_model=list[NormalizedPriceResponse])
async def get_normalized_prices():
    """Get all venue prices normalized to cNGN/USD basis."""
    if not _normalizer or not _price_aggregator:
        raise HTTPException(status_code=503, detail="Price normalizer not configured")

    try:
        venue_prices = await _price_aggregator.fetch_all()
        normalized = _normalizer.normalize(venue_prices)

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
async def refresh_prices():
    """Force refresh prices from all venues."""
    if not _price_aggregator:
        raise HTTPException(status_code=503, detail="Price aggregator not configured")

    try:
        venue_prices = await _price_aggregator.fetch_all()
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
):
    """Get price history from stored snapshots."""
    db = await get_db()
    return await db.get_price_history(from_ts, to_ts, limit)


@router.get("/prices/{venue}", response_model=VenuePriceResponse)
async def get_venue_price(venue: str):
    """Get current price from a specific venue."""
    if not _price_aggregator:
        raise HTTPException(status_code=503, detail="Price aggregator not configured")

    price = _price_aggregator.get_price(venue)
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

    cngn_usd_rate = await _get_cngn_usd_rate()

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
async def get_venue_position(venue: str):
    """Get position from a specific venue."""
    if venue not in _venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    try:
        return await _venues[venue].get_position()
    except Exception as e:
        logger.error("position_fetch_failed", venue=venue, error=str(e))
        raise HTTPException(status_code=503, detail=str(e))


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

    venue_adapter = _venues[venue]
    if hasattr(venue_adapter, "params"):
        if venue in ["aerodrome", "pancakeswap"]:
            venue_adapter.params = DexParams(**params)
        elif venue == "quidax":
            venue_adapter.params = CexParams(**params)

    logger.info("venue_params_updated", venue=venue, params=params)
    return {"venue": venue, "params": params}


# === Deposit Routes ===


class DepositRequest(BaseModel):
    role: str  # Account role to send from (e.g. "aerodrome-lp")
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
async def deposit_to_blockradar(req: DepositRequest):
    """Transfer USDC or cNGN from an HD wallet account to the Blockradar deposit address."""
    if not _account_manager:
        raise HTTPException(status_code=503, detail="Account manager not configured")

    from engine.core.accounts import AccountRole

    try:
        role = AccountRole(req.role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown role: {req.role}")

    if not settings.blockradar_deposit_address:
        raise HTTPException(status_code=503, detail="BLOCKRADAR_DEPOSIT_ADDRESS not configured")

    token_address = _get_deposit_token_address(req.token)

    try:
        tx_hash = await _account_manager.transfer_erc20(
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
    """Get or create a deposit address for a currency on Quidax."""
    if "quidax" not in _venues:
        raise HTTPException(status_code=503, detail="Quidax not configured")

    try:
        return await _venues["quidax"].get_deposit_address(currency.lower())
    except Exception as e:
        logger.error("quidax_deposit_address_failed", currency=currency, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# === Manual Action Routes ===


@router.post("/venues/{venue}/sync", dependencies=[Depends(verify_token)])
async def trigger_venue_sync(venue: str):
    """Manually trigger sync for a venue."""
    if venue not in _venues:
        raise HTTPException(status_code=404, detail="Venue not found")

    try:
        ref_price = await _get_reference_price_ngn()

        if venue == "quidax" and ref_price:
            await _venues[venue].sync_order_ladder(ref_price)
        else:
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


# === Arbitrage Routes ===


@router.get("/arbitrage/status", response_model=ArbitrageStatus)
async def get_arbitrage_status():
    """Get arbitrage engine status."""
    if not _arbitrage_engine:
        raise HTTPException(status_code=503, detail="Arbitrage engine not configured")

    try:
        return await _arbitrage_engine.get_status()
    except Exception as e:
        logger.error("arbitrage_status_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/arbitrage/opportunities", response_model=list[ArbitrageOpportunity])
async def get_arbitrage_opportunities(
    status: Optional[str] = Query(None, description="Filter by status"),
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(50, le=200),
):
    """Get detected arbitrage opportunities."""
    db = await get_db()
    return await db.get_arbitrage_opportunities(status, from_ts, to_ts, limit)


@router.get("/arbitrage/opportunities/{opportunity_id}", response_model=ArbitrageOpportunity)
async def get_arbitrage_opportunity(opportunity_id: str):
    """Get a specific arbitrage opportunity."""
    db = await get_db()
    all_opps = await db.get_arbitrage_opportunities(limit=1000)
    for opp in all_opps:
        if opp.id == opportunity_id:
            return opp
    raise HTTPException(status_code=404, detail="Opportunity not found")


@router.post("/arbitrage/enable", dependencies=[Depends(verify_token)])
async def enable_arbitrage():
    """Enable arbitrage scanning."""
    if not _arbitrage_engine:
        raise HTTPException(status_code=503, detail="Arbitrage engine not configured")

    _arbitrage_engine.enable()
    logger.info("arbitrage_enabled_via_api")
    return {"status": "enabled"}


@router.post("/arbitrage/disable", dependencies=[Depends(verify_token)])
async def disable_arbitrage():
    """Disable arbitrage scanning."""
    if not _arbitrage_engine:
        raise HTTPException(status_code=503, detail="Arbitrage engine not configured")

    _arbitrage_engine.disable()
    logger.info("arbitrage_disabled_via_api")
    return {"status": "disabled"}


@router.put("/arbitrage/params", dependencies=[Depends(verify_token)])
async def update_arbitrage_params(params: ArbitrageParams):
    """Update arbitrage parameters."""
    if not _arbitrage_engine:
        raise HTTPException(status_code=503, detail="Arbitrage engine not configured")

    _arbitrage_engine.update_params(params)
    logger.info("arbitrage_params_updated_via_api")
    return {"status": "updated", "params": params.model_dump()}


@router.post("/arbitrage/reset-circuit-breaker", dependencies=[Depends(verify_token)])
async def reset_arbitrage_circuit_breaker():
    """Manually reset the arbitrage circuit breaker."""
    if not _arbitrage_engine:
        raise HTTPException(status_code=503, detail="Arbitrage engine not configured")

    _arbitrage_engine.reset_circuit_breaker()
    logger.info("arbitrage_circuit_breaker_reset_via_api")
    return {"status": "reset"}


@router.post("/arbitrage/scan", dependencies=[Depends(verify_token)])
async def trigger_arbitrage_scan():
    """Manually trigger an arbitrage scan."""
    if not _arbitrage_engine:
        raise HTTPException(status_code=503, detail="Arbitrage engine not configured")

    try:
        opportunities = await _arbitrage_engine.scan()
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
async def list_accounts():
    """List all configured accounts."""
    if not _account_manager:
        raise HTTPException(status_code=503, detail="Account manager not configured")

    from engine.core.accounts import AccountRole

    accounts = []
    for role in AccountRole:
        try:
            config = _account_manager.get_config(role)
            accounts.append(AccountInfo(
                role=role.value,
                address=_account_manager.get_address(role),
                derivation_path=config.derivation_path,
                chain_id=config.chain_id,
                tokens=config.tokens,
            ))
        except ValueError:
            continue

    return accounts


@router.get("/accounts/balances", response_model=list[AccountBalanceResponse])
async def get_all_account_balances():
    """Get balances for all accounts."""
    if not _account_manager:
        raise HTTPException(status_code=503, detail="Account manager not configured")

    try:
        balances = await _account_manager.check_all_balances(_token_contracts)
        return [
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
    except Exception as e:
        logger.error("balance_fetch_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accounts/{role}", response_model=AccountInfo)
async def get_account(role: str):
    """Get account info for a specific role."""
    if not _account_manager:
        raise HTTPException(status_code=503, detail="Account manager not configured")

    from engine.core.accounts import AccountRole

    try:
        account_role = AccountRole(role)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role}")

    try:
        config = _account_manager.get_config(account_role)
        return AccountInfo(
            role=role,
            address=_account_manager.get_address(account_role),
            derivation_path=config.derivation_path,
            chain_id=config.chain_id,
            tokens=config.tokens,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/accounts/{role}/balance", response_model=AccountBalanceResponse)
async def get_account_balance(role: str):
    """Get balance for a specific account."""
    if not _account_manager:
        raise HTTPException(status_code=503, detail="Account manager not configured")

    from engine.core.accounts import AccountRole

    try:
        account_role = AccountRole(role)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role}")

    try:
        balance = await _account_manager.get_balance(account_role, _token_contracts)
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
async def update_account_thresholds(role: str, thresholds: AccountThresholds):
    """Update refill thresholds for an account."""
    if not _account_manager:
        raise HTTPException(status_code=503, detail="Account manager not configured")

    from engine.core.accounts import AccountRole

    try:
        account_role = AccountRole(role)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown role: {role}")

    try:
        _account_manager.update_thresholds(
            account_role,
            min_balance_eth=thresholds.min_balance_eth,
            min_balance_tokens=thresholds.min_balance_tokens,
        )
        return {"status": "updated", "role": role}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# === Pool Metrics ===

# Static config: chain and pool address for each DEX we track
_DEX_POOLS = [
    {"venue": "aerodrome", "chain": "base", "pool_address": settings.aerodrome_pool_address},
    {"venue": "pancakeswap", "chain": "bsc", "pool_address": settings.pancakeswap_pool_address},
]


@router.get("/pool-metrics")
async def get_pool_metrics():
    """Fetch 24h volume and TVL for all DEX pools from DexScreener (no keys required)."""
    import httpx

    results = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        for pool in _DEX_POOLS:
            entry = {"venue": pool["venue"], "chain": pool["chain"], "pool_tvl_usd": None, "volume_24h_usd": None}
            try:
                url = f"https://api.dexscreener.com/latest/dex/pairs/{pool['chain']}/{pool['pool_address']}"
                resp = await client.get(url)
                pairs = resp.json().get("pairs") or []
                if pairs:
                    entry["pool_tvl_usd"] = pairs[0].get("liquidity", {}).get("usd")
                    entry["volume_24h_usd"] = pairs[0].get("volume", {}).get("h24")
            except Exception as e:
                logger.warning("pool_metrics_fetch_failed", venue=pool["venue"], error=str(e))
            results.append(entry)
    return results


# === Health Check ===


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": int(time.time() * 1000),
        "trading_enabled": _scheduler.trading_enabled if _scheduler else False,
        "arbitrage_enabled": _arbitrage_engine.enabled if _arbitrage_engine else False,
    }
