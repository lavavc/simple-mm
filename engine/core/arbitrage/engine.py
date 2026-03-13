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
        execute_cex_dex_enabled: bool = False,
        execute_dex_dex_enabled: bool = False,
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
            execute_cex_dex_enabled: If True, execute CEX-DEX trades
            execute_dex_dex_enabled: If True, execute DEX-DEX trades
            normalizer: Shared price normalizer
            blended_calculator: Blended price calculator for fair-value detection
        """
        self.price_aggregator = price_aggregator
        self.venues = venues
        self.params = params
        self.broadcast = broadcast
        self.execute_cex_dex_enabled = execute_cex_dex_enabled
        self.execute_dex_dex_enabled = execute_dex_dex_enabled

        # Initialize components
        self.inventory = InventoryTracker(params)
        from engine.venues.dex.base import BaseDexAdapter
        dex_venues = {k: v for k, v in venues.items() if isinstance(v, BaseDexAdapter)}
        self.detector = ArbitrageDetector(
            price_aggregator,
            params,
            normalizer=normalizer,
            blended_calculator=blended_calculator,
            inventory_tracker=self.inventory,
            dex_venues=dex_venues,
        )
        self.executor = ArbitrageExecutor(venues, self.execute_cex_dex_enabled or self.execute_dex_dex_enabled)

        # State
        self._enabled = True
        self._last_scan_timestamp: Optional[int] = None
        self._inventory_seeded = False

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

    def enable_execute_cex_dex(self):
        self.execute_cex_dex_enabled = True
        self.executor.execution_enabled = self.execute_cex_dex_enabled or self.execute_dex_dex_enabled
        logger.info("execution_cex_dex_enabled")

    def disable_execute_cex_dex(self):
        self.execute_cex_dex_enabled = False
        self.executor.execution_enabled = self.execute_cex_dex_enabled or self.execute_dex_dex_enabled
        logger.info("execution_cex_dex_disabled")

    def enable_execute_dex_dex(self):
        self.execute_dex_dex_enabled = True
        self.executor.execution_enabled = self.execute_cex_dex_enabled or self.execute_dex_dex_enabled
        logger.info("execution_dex_dex_enabled")

    def disable_execute_dex_dex(self):
        self.execute_dex_dex_enabled = False
        self.executor.execution_enabled = self.execute_cex_dex_enabled or self.execute_dex_dex_enabled
        logger.info("execution_dex_dex_disabled")

    async def scan(self) -> list[ArbitrageOpportunity]:
        """
        Perform a single arbitrage scan cycle.

        This is called by the scheduler at regular intervals.

        Returns:
            List of detected opportunities
        """
        if not self._enabled:
            return []

        if not self._inventory_seeded:
            await self._seed_account_inventory()

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

                # Detect which lane the arb is in
                is_dex_dex = opp.buy_venue in self.detector.dex_venues and opp.sell_venue in self.detector.dex_venues
                is_cex_dex = (opp.buy_venue in self.detector.dex_venues) != (opp.sell_venue in self.detector.dex_venues)
                
                execution_enabled_for_opp = False
                if is_dex_dex and self.execute_dex_dex_enabled:
                    execution_enabled_for_opp = True
                elif is_cex_dex and self.execute_cex_dex_enabled:
                    execution_enabled_for_opp = True

                # Phase 1: Detection only - do not execute
                if not execution_enabled_for_opp:
                    # Mark as expired since we're not executing
                    await db.update_arbitrage_opportunity(
                        opp.id,
                        status="expired",
                        reason="Detection-only mode",
                    )
                    continue

                # Phase 2+: Would execute here
                await self._execute_opportunity(opp)

            # Follow-on execution loop: re-detect with fresh prices until market clears
            if self.execute_cex_dex_enabled or self.execute_dex_dex_enabled:
                for _ in range(4):
                    more = await self.detector.detect_opportunities()
                    if not more:
                        break
                    for opp in more:
                        await self._execute_opportunity(opp, track=False)
                    opportunities.extend(more)

            return opportunities

        except Exception as e:
            logger.error("arbitrage_scan_failed", error=str(e))
            self.broadcast({
                "type": "alert",
                "severity": "warning",
                "message": f"Arbitrage scan error: {e}",
            })
            return []

    async def _execute_opportunity(self, opp: ArbitrageOpportunity, track: bool = True):
        """Execute an arbitrage opportunity. Set track=False for follow-on executions."""
        db = await get_db() if track else None

        can_trade, reason = self.inventory.can_trade(
            opp.recommended_size_usd,
            buy_venue=opp.buy_venue,
            sell_venue=opp.sell_venue,
        )
        if not can_trade:
            logger.info("arbitrage_execution_blocked", opportunity_id=opp.id, reason=reason)
            if track:
                await db.update_arbitrage_opportunity(opp.id, status="abandoned", reason=reason)
            return

        self.inventory.record_trade_start(opp.id, opp.recommended_size_usd, opp.buy_venue, opp.sell_venue)
        if track:
            await db.update_arbitrage_opportunity(opp.id, status="executing")

        success, actual_profit, error = await self.executor.execute(opp)

        if success and actual_profit is not None:
            self.inventory.update_account_inventory(opp.buy_venue, opp.recommended_size_usd, is_buy=True)
            amount_cngn = opp.recommended_size_usd / opp.buy_price if opp.buy_price > 0 else Decimal("0")
            self.inventory.update_account_inventory(opp.sell_venue, amount_cngn * opp.sell_price, is_buy=False)
            self.inventory.record_trade_complete(opp.id, opp.recommended_size_usd, actual_profit, Decimal("0"))
            if track:
                await db.update_arbitrage_opportunity(opp.id, status="completed", actual_profit_usd=float(actual_profit))
                self.broadcast({"type": "arbitrage_completed", "data": {"id": opp.id, "profit_usd": float(actual_profit)}})
        elif error and error.startswith("HALF_OPEN:"):
            parts = error.split(":", 2)
            buy_tx = parts[1] if len(parts) > 1 else ""
            sell_err = parts[2] if len(parts) > 2 else error
            logger.error("arbitrage_half_open", opportunity_id=opp.id, buy_tx=buy_tx, sell_error=sell_err)
            self.inventory.record_trade_failure(opp.id, error)
            if track:
                await db.update_arbitrage_opportunity(opp.id, status="abandoned", reason=error)
            self.broadcast({
                "type": "alert",
                "severity": "critical",
                "message": f"Half-open arb {opp.id}: buy {buy_tx} confirmed but sell failed: {sell_err}",
            })
        else:
            self.inventory.record_trade_failure(opp.id, error or "Unknown error")
            if track:
                await db.update_arbitrage_opportunity(opp.id, status="abandoned", reason=error)

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
            execute_cex_dex=self.execute_cex_dex_enabled,
            execute_dex_dex=self.execute_dex_dex_enabled,
            last_scan_timestamp=self._last_scan_timestamp,
            opportunities_detected_24h=stats["opportunities_detected"],
            opportunities_executed_24h=stats["opportunities_executed"],
            total_profit_24h_usd=stats["total_profit_usd"],
            daily_volume_usd=inventory_status["daily_volume_usd"],
            inventory_imbalance_usd=inventory_status["cngn_imbalance_usd"],
            circuit_breaker_active=inventory_status["circuit_breaker_active"],
            consecutive_failures=inventory_status["consecutive_failures"],
            params=self.params,
            low_inventory_venues=inventory_status["low_inventory_venues"],
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

    def update_portfolio_snapshot(self, cngn_value_usd: Decimal, total_usd: Decimal):
        """Pass portfolio snapshot from scheduler to inventory tracker."""
        self.inventory.update_portfolio_snapshot(cngn_value_usd, total_usd)

    async def _seed_account_inventory(self):
        """Read trade-account stablecoin balances and pre-approve router at first scan."""
        from engine.venues.dex.base import BaseDexAdapter
        balances: dict[str, Decimal] = {}
        for name, venue in self.venues.items():
            if isinstance(venue, BaseDexAdapter):
                try:
                    raw = venue.stable_token.functions.balanceOf(venue.trade_account.address).call()
                    balances[name] = Decimal(raw) / Decimal(10 ** venue.stable_decimals)
                except Exception as e:
                    logger.warning("account_stable_seed_failed", venue=name, error=str(e))
                try:
                    await venue.ensure_trade_approvals()
                except Exception as e:
                    logger.warning("trade_approval_failed", venue=name, error=str(e))
        if balances:
            self.inventory.initialize_account_stable(balances)
        self._inventory_seeded = True
