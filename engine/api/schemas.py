"""HTTP-specific response and wrapper types for the API layer.

Domain types used outside engine/api/ live in engine/types.py.
schemas.py may import from engine.types for field type annotations but must not
add those symbols to its public interface (__all__).
"""

from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from engine.types import LPPosition, OrderBookLevel, Position, PriceQuote

__all__ = [
    "VenuePriceResponse",
    "OrderBookDepthResponse",
    "VenueStatus",
    "SystemStatus",
    "GlobalPosition",
    "PortfolioExposureSource",
    "PortfolioExposure",
    "NormalizedPriceResponse",
    "BlendedPriceResponse",
]


class VenuePriceResponse(BaseModel):
    """Price from a specific venue, shaped for the API."""

    venue: str
    pair: str
    quote: Optional[PriceQuote] = None
    error: Optional[str] = None
    age_seconds: float = 0


class OrderBookDepthResponse(BaseModel):
    """Level 2 order book depth shaped for the API."""

    venue: str
    pair: str
    timestamp: int
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]


class VenueStatus(BaseModel):
    """Status of a trading venue."""

    name: str
    enabled: bool
    paused: bool
    last_action: Optional[int] = None
    position: Optional[Position] = None
    price: Optional[VenuePriceResponse] = None
    params: Optional[dict[str, Any]] = None  # Live venue parameters (DexParams, CexParams, etc.)


class SystemStatus(BaseModel):
    """Overall system status."""

    trading_enabled: bool
    uptime: int
    last_price_update: Optional[int] = None
    venues: list[VenueStatus]


class GlobalPosition(BaseModel):
    """Global portfolio position summary."""

    total_cngn: Decimal
    total_usdt: Decimal
    total_usdc: Decimal
    total_usd_value: Decimal
    delta_ratio: Decimal
    target_delta: Decimal


class PortfolioExposureSource(BaseModel):
    """One contributing balance source in the global portfolio view."""

    source: str
    kind: Literal["account", "lp_position", "exchange"]
    balances: dict[str, Decimal]
    usd_value: Decimal


class PortfolioExposure(GlobalPosition):
    """Expanded global portfolio position with per-source breakdown."""

    sources: list[PortfolioExposureSource] = Field(default_factory=list)


class NormalizedPriceResponse(BaseModel):
    """A venue price normalized to cNGN/USD basis."""

    venue: str
    cngn_usd: Decimal
    basis: str  # Original pair, e.g. "USDT/NGN", "cNGN/USDC"
    raw_mid: Decimal  # Original mid price from the venue
    timestamp: int


class BlendedPriceResponse(BaseModel):
    """Composite blended price combining TWAP and VWAP across venues."""

    vwap: Decimal  # Cross-venue volume-weighted average
    twap_5m: Decimal  # 5-minute time-weighted average
    twap_1h: Decimal  # 1-hour time-weighted average
    reference_price_ngn: Decimal  # USDT/NGN equivalent (1/VWAP)
    venue_prices: dict[str, Decimal]  # Per-venue normalized cNGN/USD
    timestamp: int
    num_sources: int
    total_venues: int = 0
    confidence: float  # 0-1 based on source agreement
    dex_volume_24h_usd: dict[str, Optional[Decimal]] = Field(default_factory=dict)
