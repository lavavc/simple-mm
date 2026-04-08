"""Domain types shared across venues, LP, market, DB, arb, and scheduler layers.

All types that are used outside engine/api/ live here.
engine/api/schemas.py contains only HTTP-specific response wrappers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from engine.config import settings
from engine.venues.cex.ladder_config import (
    cex_params_payload,
    hydrate_ladder_fields_from_legacy_offsets,
    resolve_ladder_offsets,
)


# === Venue / position types ===


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


class TxResult(BaseModel):
    """Transaction result."""

    hash: str
    status: Literal["pending", "confirmed", "failed"]
    gas_used: Optional[int] = None
    error: Optional[str] = None
    output_raw: Optional[int] = None  # Raw token output units parsed from the V4 Swap event


# === Order book types ===


class OrderBookLevel(BaseModel):
    """A single price level in an order book."""

    price: Decimal
    amount: Decimal


class OrderBookDepth(BaseModel):
    """Level 2 order book depth snapshot."""

    venue: str
    pair: str
    timestamp: int
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]


# === Venue parameter types ===


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


# === Alert types ===


class Alert(BaseModel):
    """System alert."""

    id: int
    timestamp: int
    severity: Literal["info", "warning", "critical"]
    category: str
    message: str
    acknowledged: bool = False


# === Account types ===


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


# === Arbitrage types ===


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
    low_inventory_venues: list[str] = Field(default_factory=list)
