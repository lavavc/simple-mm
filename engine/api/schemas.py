"""HTTP-specific response and wrapper types for the API layer.

Domain types used outside engine/api/ live in engine/types.py.
schemas.py may import from engine.types for field type annotations but must not
add those symbols to its public interface (__all__).
"""

from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from engine.config import settings
from engine.venues.cex.ladder_config import (
    cex_params_payload,
    hydrate_ladder_fields_from_legacy_offsets,
    resolve_ladder_offsets,
)


class PriceQuote(BaseModel):
    """Price quote from aggregated sources."""

    source: str
    timestamp: int
    bid: Decimal
    ask: Decimal
    mid: Decimal


class LPPosition(BaseModel):
    """DEX liquidity position details."""

    token_id: Optional[str] = None
    liquidity: Optional[str] = None  # BigInt as string
    range_min: Optional[Decimal] = None
    range_max: Optional[Decimal] = None
    in_range: Optional[bool] = None
    our_share_pct: Optional[Decimal] = None  # our_liquidity / pool_liquidity * 100
    snapshot_status: Literal["live", "degraded"] = "live"
    snapshot_message: Optional[str] = None


class Position(BaseModel):
    """Venue position state."""

    venue: str
    pair: str
    timestamp: int
    balances: dict[str, Decimal]
    lp_position: Optional[LPPosition] = None
    position_value_usd: Optional[Decimal] = None
    volume_24h_usd: Optional[Decimal] = None
    rates: Optional[dict[str, Decimal]] = None  # per-route cNGN/USD rates (blockradar only)
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


class VenueOrderSummary(BaseModel):
    """Normalized venue order row for monitoring surfaces."""

    id: str
    market: Optional[str] = None
    side: str
    status: Optional[str] = None
    price: Decimal
    volume: Decimal
    remaining_volume: Decimal
    executed_volume: Decimal
    notional: Decimal
    created_at: Optional[int] = None


class VenueOrdersResponse(BaseModel):
    """Normalized open-order snapshot for a venue."""

    venue: str
    market: Optional[str] = None
    count: int
    orders: list[VenueOrderSummary]


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


CexAnchorSource = Literal["dex_vwap", "blended", "quidax"]


class CexParams(BaseModel):
    """Parameters for CEX order ladder."""

    ladder_enabled: bool = False
    spread_offset_ngn: int = Field(default=50, ge=0)
    ladder_step_ngn: int = Field(default=1, ge=1)
    ladder_levels_per_side: int = Field(default=1, ge=1)
    # Legacy compatibility for persisted configs that stored explicit offsets.
    ladder_offsets_ngn: Optional[list[int]] = Field(default=None, exclude=True, repr=False)
    anchor_source: CexAnchorSource = "blended"
    anchor_requote_threshold_bps: int = 0
    anchor_requote_cooldown_seconds: int = 30
    order_size_cngn: Decimal = Decimal("0")  # cNGN per sell order (0 = disabled)
    order_size_usdt: Decimal = Decimal("0")  # USDT per buy order (0 = disabled)

    @model_validator(mode="after")
    def _hydrate_new_ladder_fields_from_legacy_offsets(self) -> "CexParams":
        (
            self.spread_offset_ngn,
            self.ladder_step_ngn,
            self.ladder_levels_per_side,
            self.ladder_offsets_ngn,
        ) = hydrate_ladder_fields_from_legacy_offsets(
            spread_offset_ngn=self.spread_offset_ngn,
            ladder_step_ngn=self.ladder_step_ngn,
            ladder_levels_per_side=self.ladder_levels_per_side,
            legacy_offsets=self.ladder_offsets_ngn,
            provided_fields=set(self.model_fields_set),
        )
        return self

    @property
    def resolved_ladder_offsets_ngn(self) -> list[int]:
        return resolve_ladder_offsets(
            spread_offset_ngn=self.spread_offset_ngn,
            ladder_step_ngn=self.ladder_step_ngn,
            ladder_levels_per_side=self.ladder_levels_per_side,
            legacy_offsets=self.ladder_offsets_ngn,
        )

    def to_params_payload(self, *, mode: Literal["python", "json"] = "python") -> dict[str, Any]:
        return cex_params_payload(
            base_payload=self.model_dump(mode=mode),
            legacy_offsets=self.ladder_offsets_ngn,
            mode=mode,
        )


class WalletParams(BaseModel):
    """Parameters for wallet system rate setting."""

    spread_bps: int = 15


class TxResult(BaseModel):
    """Transaction result."""

    hash: str
    status: Literal["pending", "confirmed", "failed"]
    gas_used: Optional[int] = None
    error: Optional[str] = None
    output_raw: Optional[int] = None  # Raw token output units parsed from the V4 Swap event


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
