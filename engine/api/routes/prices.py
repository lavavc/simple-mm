"""Price-related routes."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
import structlog

from engine.api.deps import get_repository, require_blended_calculator, require_normalizer, require_price_aggregator
from engine.api.schemas import BlendedPriceResponse, NormalizedPriceResponse, VenuePriceResponse
from engine.db.repository import DatabaseRepository
from engine.market.price_aggregation import BlendedPriceCalculator, PriceNormalizer
from engine.market.venue_prices import VenuePriceAggregator

logger = structlog.get_logger()
router = APIRouter()


@router.get("/prices", response_model=list[VenuePriceResponse])
async def get_all_prices(
    price_aggregator: VenuePriceAggregator = Depends(require_price_aggregator),
) -> list[VenuePriceResponse]:
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
    except Exception as exc:
        logger.error("prices_fetch_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Price fetch failed")


@router.get("/prices/blended", response_model=BlendedPriceResponse)
async def get_blended_price(
    blended_calculator: BlendedPriceCalculator = Depends(require_blended_calculator),
) -> BlendedPriceResponse:
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
    except Exception as exc:
        logger.error("blended_price_fetch_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/prices/normalized", response_model=list[NormalizedPriceResponse])
async def get_normalized_prices(
    normalizer: PriceNormalizer = Depends(require_normalizer),
    price_aggregator: VenuePriceAggregator = Depends(require_price_aggregator),
) -> list[NormalizedPriceResponse]:
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
    except Exception as exc:
        logger.error("normalized_prices_fetch_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/prices/refresh", response_model=list[VenuePriceResponse])
async def refresh_prices(
    price_aggregator: VenuePriceAggregator = Depends(require_price_aggregator),
) -> list[VenuePriceResponse]:
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
    except Exception as exc:
        logger.error("prices_refresh_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Price refresh failed")


@router.get("/price/history")
async def get_price_history(
    venue: Optional[str] = Query(None, description="Filter by venue"),
    from_ts: Optional[int] = Query(None, description="Start timestamp (ms)"),
    to_ts: Optional[int] = Query(None, description="End timestamp (ms)"),
    limit: int = Query(100, le=1000),
    db: DatabaseRepository = Depends(get_repository),
) -> list[dict[str, Any]]:
    del venue
    return await db.prices.get_price_history(from_ts, to_ts, limit)


@router.get("/prices/{venue}", response_model=VenuePriceResponse)
async def get_venue_price(
    venue: str,
    price_aggregator: VenuePriceAggregator = Depends(require_price_aggregator),
) -> VenuePriceResponse:
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
