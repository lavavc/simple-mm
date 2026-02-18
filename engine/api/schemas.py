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


class VenuePriceResponse(BaseModel):
    """Price from a specific venue."""

    venue: str
    pair: str
    quote: Optional[PriceQuote] = None
    error: Optional[str] = None
    age_seconds: float = 0


class VenueStatus(BaseModel):
    """Status of a trading venue."""

    name: str
    enabled: bool
    paused: bool
    last_action: Optional[int] = None
    position: Optional[Position] = None
    price: Optional[VenuePriceResponse] = None  # Current price at this venue


class SystemStatus(BaseModel):
    """Overall system status."""

    trading_enabled: bool
    uptime: int
    last_price_update: Optional[int] = None
    venues: list[VenueStatus]


class DexParams(BaseModel):
    """Parameters for DEX position management."""

    # Range calculation
    sd_multiplier: Decimal = Decimal("1.5")
    min_tick_width: int = 100
    max_tick_width: int = 1000
    lookback_points: Optional[int] = None
    rebalance_threshold_percent: Decimal = Decimal("5.0")
    max_slippage_percent: Decimal = Decimal("1.0")

    # Capital allocation - explicit amounts to deploy (0 = deploy nothing)
    deploy_token0: Decimal = Decimal("0")  # Absolute amount of token0 to use for LP
    deploy_token1: Decimal = Decimal("0")  # Absolute amount of token1 to use for LP


class CexParams(BaseModel):
    """Parameters for CEX order ladder."""

    ladder_levels: int = 10
    ladder_increment: Decimal = Decimal("0.000001")  # Price increment per level (in quote currency)
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


# === Price Aggregation Schemas ===


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
    confidence: float  # 0-1 based on source agreement


# === Arbitrage Schemas ===


class ArbitrageParams(BaseModel):
    """Parameters for arbitrage detection and execution."""

    # Detection thresholds
    min_spread_bps: int = 150  # 1.5% minimum gross spread to consider
    min_net_profit_bps: int = 50  # 0.5% minimum profit after fees

    # Fee estimates (in basis points)
    dex_swap_fee_bps: int = 30  # DEX swap fee (e.g., 0.3% for typical pools)
    dex_slippage_bps: int = 20  # Expected slippage on DEX
    cex_taker_fee_bps: int = 25  # CEX taker fee

    # Position limits
    max_single_trade_usd: Decimal = Decimal("1000")  # Max per opportunity
    max_daily_volume_usd: Decimal = Decimal("10000")  # Daily volume cap
    max_inventory_imbalance_usd: Decimal = Decimal("5000")  # Max one-sided exposure

    # Timing
    scan_interval_seconds: int = 30

    # Circuit breakers
    max_consecutive_failures: int = 3  # Stop after N failures in a row
    max_daily_loss_usd: Decimal = Decimal("500")  # Stop if daily loss exceeds


class ArbitrageOpportunity(BaseModel):
    """Detected arbitrage opportunity."""

    id: str
    timestamp: int
    buy_venue: str
    sell_venue: str
    buy_price: Decimal  # Price in cNGN/USD
    sell_price: Decimal  # Price in cNGN/USD
    gross_spread_bps: int
    net_spread_bps: int  # After estimated fees
    recommended_size_usd: Decimal
    expected_profit_usd: Decimal
    status: Literal["detected", "executing", "completed", "abandoned", "expired"]
    actual_profit_usd: Optional[Decimal] = None
    reason: Optional[str] = None  # Why it was abandoned/expired


class ArbitrageTrade(BaseModel):
    """Individual trade leg of an arbitrage opportunity."""

    id: int
    opportunity_id: str
    venue: str
    side: Literal["buy", "sell"]
    amount: Decimal  # In cNGN
    price: Optional[Decimal] = None  # Actual execution price
    tx_hash: Optional[str] = None
    status: Literal["pending", "submitted", "confirmed", "failed"]
    timestamp: int
    error: Optional[str] = None


class ArbitrageStatus(BaseModel):
    """Current status of the arbitrage engine."""

    enabled: bool
    detection_only: bool  # True = no execution, just logging
    last_scan_timestamp: Optional[int] = None
    opportunities_detected_24h: int
    opportunities_executed_24h: int
    total_profit_24h_usd: Decimal
    daily_volume_usd: Decimal
    inventory_imbalance_usd: Decimal
    circuit_breaker_active: bool
    consecutive_failures: int
    params: ArbitrageParams


# === Account Schemas ===


class AccountInfo(BaseModel):
    """Basic account information."""

    role: str
    address: str
    derivation_path: str
    chain_id: int
    tokens: list[str]


class AccountBalanceResponse(BaseModel):
    """Account balance with refill status."""

    role: str
    address: str
    chain_id: int
    native_balance: Decimal
    native_symbol: str
    token_balances: dict[str, Decimal]
    needs_refill: bool
    refill_reasons: list[str]


class AccountThresholds(BaseModel):
    """Refill thresholds for an account."""

    min_balance_eth: Optional[Decimal] = None
    min_balance_tokens: Optional[dict[str, Decimal]] = None


class RefillAlert(BaseModel):
    """Alert for account needing refill from treasury."""

    id: int
    timestamp: int
    role: str
    address: str
    chain_id: int
    reasons: list[str]
    acknowledged: bool = False
