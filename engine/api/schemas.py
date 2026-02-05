"""Pydantic models for API request/response validation."""

from pydantic import BaseModel
from typing import Optional, Literal
from decimal import Decimal


class PriceQuote(BaseModel):
    """Price quote from aggregated sources."""

    source: str
    timestamp: int
    bid: Decimal
    ask: Decimal
    mid: Decimal


class LPPosition(BaseModel):
    """DEX liquidity position details."""

    token_id: str
    liquidity: str  # BigInt as string
    range_min: Decimal
    range_max: Decimal
    in_range: bool


class Position(BaseModel):
    """Venue position state."""

    venue: str
    pair: str
    timestamp: int
    balances: dict[str, Decimal]
    lp_position: Optional[LPPosition] = None
    open_orders: Optional[dict] = None


class VenueStatus(BaseModel):
    """Status of a trading venue."""

    name: str
    enabled: bool
    paused: bool
    last_action: Optional[int] = None
    position: Optional[Position] = None


class SystemStatus(BaseModel):
    """Overall system status."""

    trading_enabled: bool
    uptime: int
    last_price_update: Optional[int] = None
    venues: list[VenueStatus]


class DexParams(BaseModel):
    """Parameters for DEX position management."""

    sd_multiplier: Decimal = Decimal("1.5")
    min_tick_width: int = 100
    max_tick_width: int = 1000
    lookback_points: Optional[int] = None
    rebalance_threshold_percent: Decimal = Decimal("5.0")
    max_slippage_percent: Decimal = Decimal("1.0")


class CexParams(BaseModel):
    """Parameters for CEX order ladder."""

    ladder_levels: int = 10
    ladder_increment_ngn: Decimal = Decimal("1.0")
    liquidity_per_level_percent: Decimal = Decimal("5.0")


class WalletParams(BaseModel):
    """Parameters for wallet system rate setting."""

    spread_bps: int = 15


class TxResult(BaseModel):
    """Transaction result."""

    hash: str
    status: Literal["pending", "confirmed", "failed"]
    gas_used: Optional[int] = None
    error: Optional[str] = None


class GlobalPosition(BaseModel):
    """Global portfolio position summary."""

    total_cngn: Decimal
    total_usdt: Decimal
    total_usdc: Decimal
    total_usd_value: Decimal
    delta_ratio: Decimal
    target_delta: Decimal


class Alert(BaseModel):
    """System alert."""

    id: int
    timestamp: int
    severity: Literal["info", "warning", "critical"]
    category: str
    message: str
    acknowledged: bool = False
