"""Inventory tracking and risk management for arbitrage."""

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import structlog

from engine.api.schemas import ArbitrageParams

logger = structlog.get_logger()


@dataclass
class InventoryState:
    """Current inventory state for risk management."""

    # Rolling trade log: list of (timestamp_ms, size_usd) for 24h volume tracking
    trade_log: list = field(default_factory=list)

    # Daily P&L (resets at midnight; volume uses rolling window instead)
    daily_profit_usd: Decimal = Decimal("0")
    daily_loss_usd: Decimal = Decimal("0")

    # Inventory imbalance (positive = long cNGN, negative = short cNGN)
    cngn_imbalance_usd: Decimal = Decimal("0")

    # Circuit breaker state
    consecutive_failures: int = 0
    circuit_breaker_active: bool = False
    circuit_breaker_reason: Optional[str] = None

    # Timestamps
    day_start_timestamp: int = 0
    last_trade_timestamp: int = 0

    # Per-account stablecoin tracking
    per_account_stable: dict[str, Decimal] = field(default_factory=dict)
    initial_account_stable: dict[str, Decimal] = field(default_factory=dict)
    low_inventory_venues: set[str] = field(default_factory=set)

    # Portfolio snapshot (fed from scheduler every 120s)
    cngn_value_usd: Decimal = Decimal("0")
    total_portfolio_usd: Decimal = Decimal("0")


class InventoryTracker:
    """
    Tracks inventory, daily limits, and circuit breakers for arbitrage.

    Manages risk by enforcing:
    - Daily volume limits
    - Daily loss limits
    - Inventory imbalance limits
    - Circuit breakers on consecutive failures
    """

    def __init__(self, params: ArbitrageParams):
        """
        Initialize inventory tracker.

        Args:
            params: Arbitrage parameters with limits
        """
        self.params = params
        self._state = InventoryState()
        self._reset_daily_if_needed()

    @property
    def state(self) -> InventoryState:
        """Get current inventory state."""
        self._reset_daily_if_needed()
        return self._state

    def _reset_daily_if_needed(self):
        """Reset daily counters at midnight UTC."""
        now = int(time.time() * 1000)
        day_start = (now // 86400000) * 86400000  # Start of day in ms

        if self._state.day_start_timestamp < day_start:
            logger.info(
                "resetting_daily_inventory_counters",
                previous_profit=float(self._state.daily_profit_usd),
            )
            self._state.daily_profit_usd = Decimal("0")
            self._state.daily_loss_usd = Decimal("0")
            self._state.day_start_timestamp = day_start

            # Reset circuit breaker at day start (give it another chance)
            if self._state.circuit_breaker_active:
                self._state.circuit_breaker_active = False
                self._state.circuit_breaker_reason = None
                self._state.consecutive_failures = 0
                logger.info("circuit_breaker_reset_daily")

    def _rolling_volume_usd(self) -> Decimal:
        """Sum of trade sizes in the past 24 hours."""
        cutoff = int(time.time() * 1000) - 86_400_000
        return sum((v for ts, v in self._state.trade_log if ts > cutoff), Decimal("0"))

    def can_trade(
        self,
        trade_size_usd: Decimal,
        buy_venue: str = "",
        sell_venue: str = "",
    ) -> tuple[bool, Optional[str]]:
        """
        Check if a trade is allowed given current limits.

        Args:
            trade_size_usd: Size of proposed trade in USD

        Returns:
            (allowed, reason) - reason is None if allowed, else explanation
        """
        self._reset_daily_if_needed()

        # 1. Check circuit breaker
        if self._state.circuit_breaker_active:
            return False, f"Circuit breaker active: {self._state.circuit_breaker_reason}"

        # 2. Check rolling 24h volume limit
        new_volume = self._rolling_volume_usd() + trade_size_usd
        if new_volume > self.params.max_daily_volume_usd:
            return False, (
                f"Would exceed daily volume limit: "
                f"{float(new_volume):.2f} > {float(self.params.max_daily_volume_usd):.2f}"
            )

        # 3. Check inventory imbalance
        # Assume worst case: trade increases imbalance
        potential_imbalance = abs(self._state.cngn_imbalance_usd) + trade_size_usd
        if potential_imbalance > self.params.max_inventory_imbalance_usd:
            return False, (
                f"Would exceed inventory imbalance limit: "
                f"{float(potential_imbalance):.2f} > {float(self.params.max_inventory_imbalance_usd):.2f}"
            )

        # 4. Check daily loss limit
        if self._state.daily_loss_usd >= self.params.max_daily_loss_usd:
            return False, (
                f"Daily loss limit reached: "
                f"{float(self._state.daily_loss_usd):.2f} >= {float(self.params.max_daily_loss_usd):.2f}"
            )

        # 5. Per-account stablecoin — block if buy-side venue is flagged low
        if buy_venue and buy_venue in self._state.low_inventory_venues:
            return False, f"Low stablecoin inventory on {buy_venue} — rebalance needed"

        # 6. Global delta ratio — pre-trade portfolio guard
        if self._state.total_portfolio_usd > 0:
            current_ratio = self._state.cngn_value_usd / self._state.total_portfolio_usd
            if current_ratio >= self.params.max_delta_ratio:
                return False, (
                    f"Portfolio already at {float(current_ratio):.0%} cNGN — "
                    f"above max delta ratio {float(self.params.max_delta_ratio):.0%}"
                )

        return True, None

    def record_trade_start(
        self,
        opportunity_id: str,
        size_usd: Decimal,
        buy_venue: str,
        sell_venue: str,
    ):
        """
        Record that a trade has started (for tracking).

        Args:
            opportunity_id: ID of the arbitrage opportunity
            size_usd: Trade size in USD
            buy_venue: Venue buying from
            sell_venue: Venue selling to
        """
        self._state.last_trade_timestamp = int(time.time() * 1000)

        logger.info(
            "arbitrage_trade_started",
            opportunity_id=opportunity_id,
            size_usd=float(size_usd),
            buy_venue=buy_venue,
            sell_venue=sell_venue,
        )

    def record_trade_complete(
        self,
        opportunity_id: str,
        size_usd: Decimal,
        profit_usd: Decimal,
        cngn_delta: Decimal,
        cngn_price_usd: Decimal | None = None,
    ):
        """
        Record a completed trade for daily tracking.

        Args:
            opportunity_id: ID of the arbitrage opportunity
            size_usd: Trade size in USD
            profit_usd: Actual profit (can be negative)
            cngn_delta: Change in cNGN holdings (positive = bought, negative = sold)
            cngn_price_usd: cNGN/USD price for proper imbalance calculation
        """
        self._reset_daily_if_needed()

        self._state.trade_log.append((int(time.time() * 1000), size_usd))

        if profit_usd >= 0:
            self._state.daily_profit_usd += profit_usd
        else:
            self._state.daily_loss_usd += abs(profit_usd)

        # Update inventory imbalance in USD terms
        # cngn_delta is in cNGN units, convert to USD using reference price
        if cngn_price_usd and cngn_price_usd > 0:
            imbalance_delta_usd = cngn_delta * cngn_price_usd
        else:
            # Fallback: estimate at ~0.0006 USD/cNGN (1650 NGN/USD)
            imbalance_delta_usd = cngn_delta * Decimal("0.0006")
            logger.warning(
                "inventory_imbalance_estimated",
                cngn_delta=float(cngn_delta),
                estimated_usd=float(imbalance_delta_usd),
            )
        self._state.cngn_imbalance_usd += imbalance_delta_usd

        # Reset consecutive failures on success
        if profit_usd >= 0:
            self._state.consecutive_failures = 0

        logger.info(
            "arbitrage_trade_complete",
            opportunity_id=opportunity_id,
            size_usd=float(size_usd),
            profit_usd=float(profit_usd),
            rolling_volume_24h=float(self._rolling_volume_usd()),
            daily_profit=float(self._state.daily_profit_usd),
            inventory_imbalance=float(self._state.cngn_imbalance_usd),
        )

        # Check if we should trigger circuit breaker on loss
        if self._state.daily_loss_usd >= self.params.max_daily_loss_usd:
            self._trigger_circuit_breaker(
                f"Daily loss limit reached: ${float(self._state.daily_loss_usd):.2f}"
            )

    def record_trade_failure(self, opportunity_id: str, error: str):
        """
        Record a failed trade for circuit breaker tracking.

        Args:
            opportunity_id: ID of the arbitrage opportunity
            error: Error description
        """
        self._state.consecutive_failures += 1

        logger.warning(
            "arbitrage_trade_failed",
            opportunity_id=opportunity_id,
            error=error,
            consecutive_failures=self._state.consecutive_failures,
        )

        if self._state.consecutive_failures >= self.params.max_consecutive_failures:
            self._trigger_circuit_breaker(
                f"Too many consecutive failures: {self._state.consecutive_failures}"
            )

    def _trigger_circuit_breaker(self, reason: str):
        """
        Activate circuit breaker to stop trading.

        Args:
            reason: Why the circuit breaker was triggered
        """
        self._state.circuit_breaker_active = True
        self._state.circuit_breaker_reason = reason

        logger.error(
            "circuit_breaker_triggered",
            reason=reason,
            rolling_volume_24h=float(self._rolling_volume_usd()),
            daily_loss=float(self._state.daily_loss_usd),
            consecutive_failures=self._state.consecutive_failures,
        )

    def reset_circuit_breaker(self):
        """Manually reset circuit breaker (for operator intervention)."""
        self._state.circuit_breaker_active = False
        self._state.circuit_breaker_reason = None
        self._state.consecutive_failures = 0
        logger.info("circuit_breaker_manually_reset")

    def trip_circuit_breaker(self, reason: str):
        """Manually trip the circuit breaker for operator safety conditions."""
        self._trigger_circuit_breaker(reason)

    def initialize_account_stable(self, venue_balances: dict[str, Decimal]):
        """Seed per-account stablecoin from on-chain balances (called once at startup)."""
        self._state.per_account_stable = dict(venue_balances)
        self._state.initial_account_stable = dict(venue_balances)
        logger.info("account_stable_initialized", venues=list(venue_balances.keys()))

    def reconcile_stables(self, venue_balances: dict[str, Decimal]):
        """Refresh per-account stablecoin from a periodic balance fetch.

        Unlike initialize_account_stable, does not touch initial_account_stable
        so get_rebalance_cost_bps() retains its startup baseline.
        """
        for venue, amount in venue_balances.items():
            self._state.per_account_stable[venue] = amount
            if amount < self.params.min_account_stablecoin_usd:
                self._state.low_inventory_venues.add(venue)
            else:
                self._state.low_inventory_venues.discard(venue)

    def update_account_inventory(self, venue: str, delta_usd: Decimal, is_buy: bool):
        """Adjust estimated stablecoin balance after a trade leg. Flags low inventory."""
        current = self._state.per_account_stable.get(venue, Decimal("0"))
        if is_buy:
            current -= delta_usd  # Spending stablecoin to buy cNGN
        else:
            current += delta_usd  # Receiving stablecoin from selling cNGN
        self._state.per_account_stable[venue] = current

        if current < self.params.min_account_stablecoin_usd:
            self._state.low_inventory_venues.add(venue)
            logger.warning("low_stablecoin_inventory", venue=venue, balance_usd=float(current))
        else:
            self._state.low_inventory_venues.discard(venue)

    def get_rebalance_cost_bps(self, buy_venue: str) -> int:
        """Returns 0 when fully stocked, cross_chain_rebalance_bps when empty."""
        initial = self._state.initial_account_stable.get(buy_venue)
        if not initial or initial <= 0:
            return self.params.cross_chain_rebalance_bps  # Conservative fallback
        current = self._state.per_account_stable.get(buy_venue, Decimal("0"))
        fraction = min(Decimal("1"), current / initial)
        cost = self.params.cross_chain_rebalance_bps * (1 - float(fraction))
        return round(cost)

    def update_portfolio_snapshot(self, cngn_value_usd: Decimal, total_usd: Decimal):
        """Called by scheduler every 120s to keep delta ratio check current."""
        self._state.cngn_value_usd = cngn_value_usd
        self._state.total_portfolio_usd = total_usd

    def get_status_dict(self) -> dict:
        """Get current status as a dict for API responses."""
        self._reset_daily_if_needed()
        return {
            "daily_volume_usd": self._rolling_volume_usd(),
            "daily_profit_usd": self._state.daily_profit_usd,
            "daily_loss_usd": self._state.daily_loss_usd,
            "cngn_imbalance_usd": self._state.cngn_imbalance_usd,
            "consecutive_failures": self._state.consecutive_failures,
            "circuit_breaker_active": self._state.circuit_breaker_active,
            "circuit_breaker_reason": self._state.circuit_breaker_reason,
            "low_inventory_venues": sorted(self._state.low_inventory_venues),
        }
