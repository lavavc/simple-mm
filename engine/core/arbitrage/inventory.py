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

    # Running totals for the day
    daily_volume_usd: Decimal = Decimal("0")
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
                previous_volume=float(self._state.daily_volume_usd),
                previous_profit=float(self._state.daily_profit_usd),
            )
            self._state.daily_volume_usd = Decimal("0")
            self._state.daily_profit_usd = Decimal("0")
            self._state.daily_loss_usd = Decimal("0")
            self._state.day_start_timestamp = day_start

            # Reset circuit breaker at day start (give it another chance)
            if self._state.circuit_breaker_active:
                self._state.circuit_breaker_active = False
                self._state.circuit_breaker_reason = None
                self._state.consecutive_failures = 0
                logger.info("circuit_breaker_reset_daily")

    def can_trade(self, trade_size_usd: Decimal) -> tuple[bool, Optional[str]]:
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

        # 2. Check daily volume limit
        new_volume = self._state.daily_volume_usd + trade_size_usd
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
    ):
        """
        Record a completed trade for daily tracking.

        Args:
            opportunity_id: ID of the arbitrage opportunity
            size_usd: Trade size in USD
            profit_usd: Actual profit (can be negative)
            cngn_delta: Change in cNGN holdings (positive = bought, negative = sold)
        """
        self._reset_daily_if_needed()

        self._state.daily_volume_usd += size_usd

        if profit_usd >= 0:
            self._state.daily_profit_usd += profit_usd
        else:
            self._state.daily_loss_usd += abs(profit_usd)

        # Update inventory imbalance
        # Convert cNGN delta to USD value (rough estimate)
        self._state.cngn_imbalance_usd += cngn_delta

        # Reset consecutive failures on success
        if profit_usd >= 0:
            self._state.consecutive_failures = 0

        logger.info(
            "arbitrage_trade_complete",
            opportunity_id=opportunity_id,
            size_usd=float(size_usd),
            profit_usd=float(profit_usd),
            daily_volume=float(self._state.daily_volume_usd),
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
            daily_volume=float(self._state.daily_volume_usd),
            daily_loss=float(self._state.daily_loss_usd),
            consecutive_failures=self._state.consecutive_failures,
        )

    def reset_circuit_breaker(self):
        """Manually reset circuit breaker (for operator intervention)."""
        self._state.circuit_breaker_active = False
        self._state.circuit_breaker_reason = None
        self._state.consecutive_failures = 0
        logger.info("circuit_breaker_manually_reset")

    def get_status_dict(self) -> dict:
        """Get current status as a dict for API responses."""
        self._reset_daily_if_needed()
        return {
            "daily_volume_usd": self._state.daily_volume_usd,
            "daily_profit_usd": self._state.daily_profit_usd,
            "daily_loss_usd": self._state.daily_loss_usd,
            "cngn_imbalance_usd": self._state.cngn_imbalance_usd,
            "consecutive_failures": self._state.consecutive_failures,
            "circuit_breaker_active": self._state.circuit_breaker_active,
            "circuit_breaker_reason": self._state.circuit_breaker_reason,
        }
