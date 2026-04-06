"""Pydantic models for API request/response validation."""

from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from engine.config import settings


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
    open_orders: Optional[dict[str, Any]] = None
    position_value_usd: Optional[Decimal] = None
    volume_24h_usd: Optional[Decimal] = None
    rates: Optional[dict[str, Decimal]] = None  # per-route cNGN/USD rates (blockradar only)


class VenuePriceResponse(BaseModel):
    """Price from a specific venue."""

    venue: str
    pair: str
    quote: Optional[PriceQuote] = None
    error: Optional[str] = None
    age_seconds: float = 0


class OrderBookLevel(BaseModel):
    """A single price level in an order book."""
    price: Decimal
    amount: Decimal

class OrderBookDepth(BaseModel):
    """Level 2 Order Book Depth snapshot."""
    venue: str
    pair: str
    timestamp: int
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

class OrderBookDepthResponse(BaseModel):
    """Level 2 Order Book Depth snapshot logic for API endpoints."""
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
    price: Optional[VenuePriceResponse] = None  # Current price at this venue
    params: Optional[dict[str, Any]] = None  # Live venue parameters (DexParams, CexParams, etc.)


class SystemStatus(BaseModel):
    """Overall system status."""

    trading_enabled: bool
    uptime: int
    last_price_update: Optional[int] = None
    venues: list[VenueStatus]


class CexParams(BaseModel):
    """Parameters for CEX order ladder."""

    ladder_enabled: bool = False
    # NGN offsets from current rate, one order placed per offset on each side
    # e.g. [1, 3, 5, 10] → orders at rate±1, rate±3, rate±5, rate±10 NGN
    ladder_offsets_ngn: list[int] = [1, 3, 5, 10]
    order_size_cngn: Decimal = Decimal("0")  # cNGN per sell order (0 = disabled)
    order_size_usdt: Decimal = Decimal("0")  # USDT per buy order (0 = disabled)


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
    total_venues: int = 0
    confidence: float  # 0-1 based on source agreement
    dex_volume_24h_usd: dict[str, Optional[Decimal]] = {}


# === Arbitrage Schemas ===


class ArbitrageParams(BaseModel):
    """Parameters for arbitrage detection and execution.

    All defaults come from engine.config.Settings so there is one source of truth.
    Override via environment variables or the PUT /api/arbitrage/params endpoint.
    """

    # Detection thresholds
    min_profit_usd: Decimal = Decimal(str(settings.arbitrage_min_profit_usd))

    # Position limits
    max_single_trade_usd: Decimal = Decimal(str(settings.arbitrage_max_single_trade_usd))
    max_daily_volume_usd: Decimal = Decimal(str(settings.arbitrage_max_daily_volume_usd))
    max_inventory_imbalance_usd: Decimal = Decimal(str(settings.arbitrage_max_inventory_imbalance_usd))

    # Timing
    scan_interval_seconds: int = settings.arbitrage_scan_interval

    # Circuit breakers
    max_consecutive_failures: int = settings.arbitrage_max_consecutive_failures
    max_daily_loss_usd: Decimal = Decimal(str(settings.arbitrage_max_daily_loss_usd))

    # Cross-chain inventory
    cross_chain_rebalance_bps: int = settings.arbitrage_cross_chain_rebalance_bps
    max_delta_ratio: Decimal = Decimal(str(settings.arbitrage_max_delta_ratio))
    min_account_stablecoin_usd: Decimal = Decimal(str(settings.arbitrage_min_account_stablecoin_usd))


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
    status: Literal["detected", "executing", "completed", "abandoned", "expired", "half_open"]
    actual_profit_usd: Optional[Decimal] = None
    reason: Optional[str] = None  # Why it was abandoned/expired
    buy_amount_cngn: Optional[Decimal] = None
    buy_tx_hash: Optional[str] = None


class DexArbOpportunity(BaseModel):
    """Detected DEX V4 arbitrage opportunity."""

    id: str
    timestamp: int
    direction: str
    optimal_size_usd: Decimal
    expected_profit_usd: Decimal
    cngn_transferred: Decimal
    expected_usd_out: Decimal
    status: Literal["detected", "executing", "buy_filled", "half_open", "completed", "abandoned", "expired"]
    net_spread_bps: int
    actual_profit_usd: Optional[Decimal] = None
    reason: Optional[str] = None
    uni_bsc_price: Optional[Decimal] = None
    uni_base_price: Optional[Decimal] = None
    buy_tx_hash: Optional[str] = None
    sell_tx_hash: Optional[str] = None
    slippage_tolerance_bps: Optional[int] = None
    uni_bsc_fee_bps: Optional[int] = None
    uni_base_fee_bps: Optional[int] = None
    gas_usd: Optional[Decimal] = None
    buy_amount_cngn: Optional[Decimal] = None
    executed_size_usd: Optional[Decimal] = None

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


class ArbitrageHistoryWalletSnapshot(BaseModel):
    """Wallet balances captured when a route was selected."""

    stable_symbol: Optional[str] = None
    stable_balance: Optional[Decimal] = None
    cngn_balance: Optional[Decimal] = None


class ArbitrageHistoryEvent(BaseModel):
    """Single lifecycle event for a routed arbitrage attempt."""

    id: Optional[int] = None
    opportunity_id: str
    pipeline: Literal["cex_dex", "dex_dex"]
    event_type: Literal["routed", "executed", "failed"]
    timestamp: int
    direction: str
    buy_venue: str
    sell_venue: str
    status: str
    optimal_size_usd: Optional[Decimal] = None
    routed_size_usd: Optional[Decimal] = None
    executed_size_usd: Optional[Decimal] = None
    expected_profit_usd: Optional[Decimal] = None
    actual_profit_usd: Optional[Decimal] = None
    net_profit_usd: Optional[Decimal] = None
    net_spread_bps: Optional[int] = None
    reason: Optional[str] = None
    buy_wallet: Optional[ArbitrageHistoryWalletSnapshot] = None
    sell_wallet: Optional[ArbitrageHistoryWalletSnapshot] = None
    buy_tx_hash: Optional[str] = None
    sell_tx_hash: Optional[str] = None


class ArbitrageHistoryItem(BaseModel):
    """Grouped lifecycle view for a single arbitrage attempt."""

    opportunity_id: str
    pipeline: Literal["cex_dex", "dex_dex"]
    direction: str
    buy_venue: str
    sell_venue: str
    latest_status: str
    latest_event_type: Literal["routed", "executed", "failed"]
    routed_at: int
    updated_at: int
    optimal_size_usd: Optional[Decimal] = None
    routed_size_usd: Optional[Decimal] = None
    executed_size_usd: Optional[Decimal] = None
    expected_profit_usd: Optional[Decimal] = None
    actual_profit_usd: Optional[Decimal] = None
    net_profit_usd: Optional[Decimal] = None
    net_spread_bps: Optional[int] = None
    reason: Optional[str] = None
    buy_wallet: Optional[ArbitrageHistoryWalletSnapshot] = None
    sell_wallet: Optional[ArbitrageHistoryWalletSnapshot] = None
    buy_tx_hash: Optional[str] = None
    sell_tx_hash: Optional[str] = None


class ArbitrageStatus(BaseModel):
    """Current status of the arbitrage engine."""

    enabled: bool
    execute_cex_dex: bool
    execute_dex_dex: bool
    last_scan_timestamp: Optional[int] = None
    opportunities_detected_24h: int
    opportunities_executed_24h: int
    total_profit_24h_usd: Decimal
    daily_volume_usd: Decimal
    inventory_imbalance_usd: Decimal
    circuit_breaker_active: bool
    consecutive_failures: int
    params: ArbitrageParams
    low_inventory_venues: list[str] = []

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
