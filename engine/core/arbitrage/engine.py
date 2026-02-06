"""Main arbitrage engine orchestrating detection and execution."""

import time
from decimal import Decimal
from typing import Any, Callable, Optional

import structlog

from engine.api.schemas import ArbitrageParams, ArbitrageOpportunity, ArbitrageStatus
from engine.core.arbitrage.detector import ArbitrageDetector
from engine.core.arbitrage.executor import ArbitrageExecutor
from engine.core.arbitrage.inventory import InventoryTracker
from engine.core.venue_prices import VenuePriceAggregator
from engine.core.price_aggregation import PriceNormalizer, BlendedPriceCalculator
from engine.db import get_db
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


class ArbitrageEngine:
    """
    Main arbitrage engine that orchestrates detection and execution.

    Phase 1 (Current): Detection-only mode
    - Scans for price divergences across venues
    - Logs all detected opportunities to database
    - Broadcasts opportunities via WebSocket
    - NO actual trades executed

    Phase 2+ (Future): Execution mode
    - Execute profitable opportunities
    - Track inventory and P&L
    - Circuit breakers for risk management
    """

    def __init__(
        self,
        price_aggregator: VenuePriceAggregator,
        venues: dict[str, VenueAdapter],
        params: ArbitrageParams,
        broadcast: Callable[[dict], Any],
        execution_enabled: bool = False,
        normalizer: PriceNormalizer | None = None,
        blended_calculator: BlendedPriceCalculator | None = None,
    ):
        """
        Initialize arbitrage engine.

        Args:
            price_aggregator: Venue price aggregator for all venue prices
            venues: Dict of venue name to adapter
            params: Arbitrage parameters
            broadcast: Function to broadcast events to WebSocket clients
            execution_enabled: If True, execute trades (Phase 2+)
            normalizer: Shared price normalizer
            blended_calculator: Blended price calculator for fair-value detection
        """
        self.price_aggregator = price_aggregator
        self.venues = venues
        self.params = params
        self.broadcast = broadcast
        self.execution_enabled = execution_enabled

        # Initialize components
        self.detector = ArbitrageDetector(
            price_aggregator,
            params,
            normalizer=normalizer,
            blended_calculator=blended_calculator,
        )
        self.executor = ArbitrageExecutor(venues, execution_enabled)
        self.inventory = InventoryTracker(params)

        # State
        self._enabled = True
        self._last_scan_timestamp: Optional[int] = None

    @property
    def enabled(self) -> bool:
        """Whether arbitrage scanning is enabled."""
        return self._enabled

    def enable(self):
        """Enable arbitrage scanning."""
        self._enabled = True
        logger.info("arbitrage_engine_enabled")

    def disable(self):
        """Disable arbitrage scanning."""
        self._enabled = False
        logger.info("arbitrage_engine_disabled")

    async def scan(self) -> list[ArbitrageOpportunity]:
        """
        Perform a single arbitrage scan cycle.

        This is called by the scheduler at regular intervals.

        Returns:
            List of detected opportunities
        """
        if not self._enabled:
            return []

        self._last_scan_timestamp = int(time.time() * 1000)

        try:
            # Detect opportunities
            opportunities = await self.detector.detect_opportunities()

            if not opportunities:
                logger.debug("no_arbitrage_opportunities")
                return []

            # Log and broadcast each opportunity
            db = await get_db()
            for opp in opportunities:
                # Save to database
                await db.insert_arbitrage_opportunity(opp)

                # Broadcast to WebSocket clients
                self.broadcast({
                    "type": "arbitrage_opportunity",
                    "data": {
                        "id": opp.id,
                        "buy_venue": opp.buy_venue,
                        "sell_venue": opp.sell_venue,
                        "gross_spread_bps": opp.gross_spread_bps,
                        "net_spread_bps": opp.net_spread_bps,
                        "expected_profit_usd": float(opp.expected_profit_usd),
                        "recommended_size_usd": float(opp.recommended_size_usd),
                        "timestamp": opp.timestamp,
                    },
                })

                logger.info(
                    "arbitrage_opportunity_logged",
                    id=opp.id,
                    buy_venue=opp.buy_venue,
                    sell_venue=opp.sell_venue,
                    gross_spread_bps=opp.gross_spread_bps,
                    net_spread_bps=opp.net_spread_bps,
                    expected_profit=float(opp.expected_profit_usd),
                )

                # Phase 1: Detection only - do not execute
                if not self.execution_enabled:
                    # Mark as expired since we're not executing
                    await db.update_arbitrage_opportunity(
                        opp.id,
                        status="expired",
                        reason="Detection-only mode",
                    )
                    continue

                # Phase 2+: Would execute here
                await self._execute_opportunity(opp)

            return opportunities

        except Exception as e:
            logger.error("arbitrage_scan_failed", error=str(e))
            self.broadcast({
                "type": "alert",
                "severity": "warning",
                "message": f"Arbitrage scan error: {e}",
            })
            return []

    async def _execute_opportunity(self, opp: ArbitrageOpportunity):
        """
        Execute an arbitrage opportunity (Phase 2+).

        Args:
            opp: The opportunity to execute
        """
        db = await get_db()

        # Check if we can trade
        can_trade, reason = self.inventory.can_trade(opp.recommended_size_usd)
        if not can_trade:
            logger.info(
                "arbitrage_execution_blocked",
                opportunity_id=opp.id,
                reason=reason,
            )
            await db.update_arbitrage_opportunity(
                opp.id,
                status="abandoned",
                reason=reason,
            )
            return

        # Record trade start
        self.inventory.record_trade_start(
            opp.id,
            opp.recommended_size_usd,
            opp.buy_venue,
            opp.sell_venue,
        )

        # Update status to executing
        await db.update_arbitrage_opportunity(opp.id, status="executing")

        # Execute
        success, actual_profit, error = await self.executor.execute(opp)

        if success and actual_profit is not None:
            # Record successful trade
            self.inventory.record_trade_complete(
                opp.id,
                opp.recommended_size_usd,
                actual_profit,
                Decimal("0"),  # cNGN delta - would be calculated from actual trades
            )
            await db.update_arbitrage_opportunity(
                opp.id,
                status="completed",
                actual_profit_usd=float(actual_profit),
            )

            self.broadcast({
                "type": "arbitrage_completed",
                "data": {
                    "id": opp.id,
                    "profit_usd": float(actual_profit),
                },
            })
        else:
            # Record failure
            self.inventory.record_trade_failure(opp.id, error or "Unknown error")
            await db.update_arbitrage_opportunity(
                opp.id,
                status="abandoned",
                reason=error,
            )

    async def get_status(self) -> ArbitrageStatus:
        """
        Get current arbitrage engine status.

        Returns:
            ArbitrageStatus with current state
        """
        db = await get_db()

        # Get 24h stats
        now = int(time.time() * 1000)
        day_ago = now - 86400000
        stats = await db.get_arbitrage_stats(day_ago)

        inventory_status = self.inventory.get_status_dict()

        return ArbitrageStatus(
            enabled=self._enabled,
            detection_only=not self.execution_enabled,
            last_scan_timestamp=self._last_scan_timestamp,
            opportunities_detected_24h=stats["opportunities_detected"],
            opportunities_executed_24h=stats["opportunities_executed"],
            total_profit_24h_usd=stats["total_profit_usd"],
            daily_volume_usd=inventory_status["daily_volume_usd"],
            inventory_imbalance_usd=inventory_status["cngn_imbalance_usd"],
            circuit_breaker_active=inventory_status["circuit_breaker_active"],
            consecutive_failures=inventory_status["consecutive_failures"],
            params=self.params,
        )

    def update_params(self, params: ArbitrageParams):
        """
        Update arbitrage parameters.

        Args:
            params: New parameters
        """
        self.params = params
        self.detector.params = params
        self.inventory.params = params
        logger.info("arbitrage_params_updated")

    def reset_circuit_breaker(self):
        """Manually reset circuit breaker."""
        self.inventory.reset_circuit_breaker()
