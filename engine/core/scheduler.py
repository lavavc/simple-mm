"""Trading scheduler and orchestrator using APScheduler."""

from dataclasses import dataclass
from typing import Callable, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import structlog

from engine.core.price_feed import PriceFeed
from engine.db import get_db
from engine.venues.base import VenueAdapter
from engine.venues.dex.base import BaseDexAdapter

logger = structlog.get_logger()


@dataclass
class SchedulerConfig:
    """Configuration for scheduler intervals (in seconds)."""

    price_update_interval: int = 30
    position_sync_interval: int = 60
    dex_check_interval: int = 120
    cex_sync_interval: int = 300
    rate_sync_interval: int = 300
    rebalance_check_interval: int = 120


class TradingScheduler:
    """
    Orchestrates all automated trading tasks.

    Manages scheduled jobs for:
    - Price feed updates
    - Position synchronization
    - DEX rebalancing checks
    - CEX order ladder syncing
    - Blockradar rate syncing
    """

    def __init__(
        self,
        price_feed: PriceFeed,
        venues: dict[str, VenueAdapter],
        config: SchedulerConfig,
        broadcast: Callable[[dict], Any],
    ):
        """
        Initialize trading scheduler.

        Args:
            price_feed: Price feed instance
            venues: Dict of venue name to adapter
            config: Scheduler configuration
            broadcast: Function to broadcast events to WebSocket clients
        """
        self.price_feed = price_feed
        self.venues = venues
        self.config = config
        self.broadcast = broadcast

        self.scheduler = AsyncIOScheduler()
        self._trading_enabled = True
        self._started = False

    @property
    def trading_enabled(self) -> bool:
        """Whether trading is currently enabled."""
        return self._trading_enabled

    def start(self):
        """Start all scheduled jobs."""
        if self._started:
            return

        # Price updates
        self.scheduler.add_job(
            self._update_price,
            IntervalTrigger(seconds=self.config.price_update_interval),
            id="price_update",
            replace_existing=True,
        )

        # Position sync
        self.scheduler.add_job(
            self._sync_positions,
            IntervalTrigger(seconds=self.config.position_sync_interval),
            id="position_sync",
            replace_existing=True,
        )

        # DEX rebalance check
        self.scheduler.add_job(
            self._check_dex_rebalance,
            IntervalTrigger(seconds=self.config.dex_check_interval),
            id="dex_rebalance",
            replace_existing=True,
        )

        # CEX order ladder sync
        self.scheduler.add_job(
            self._sync_cex_orders,
            IntervalTrigger(seconds=self.config.cex_sync_interval),
            id="cex_sync",
            replace_existing=True,
        )

        # Blockradar rate sync
        self.scheduler.add_job(
            self._sync_blockradar_rates,
            IntervalTrigger(seconds=self.config.rate_sync_interval),
            id="rate_sync",
            replace_existing=True,
        )

        self.scheduler.start()
        self._started = True
        logger.info("scheduler_started")

    def stop(self):
        """Stop all scheduled jobs."""
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False
            logger.info("scheduler_stopped")

    async def pause(self):
        """Pause all trading operations."""
        self._trading_enabled = False
        db = await get_db()
        await db.set_system_state("trading_enabled", "false")
        self.broadcast({"type": "system", "status": "paused"})
        logger.info("trading_paused")

    async def resume(self):
        """Resume trading operations."""
        self._trading_enabled = True
        db = await get_db()
        await db.set_system_state("trading_enabled", "true")
        self.broadcast({"type": "system", "status": "running"})
        logger.info("trading_resumed")

    async def _update_price(self):
        """Fetch and broadcast current price."""
        try:
            price = await self.price_feed.get_price()
            self.broadcast({"type": "price", "data": price.model_dump()})

            # Store in database
            db = await get_db()
            await db.insert_price_snapshot(price)

        except Exception as e:
            logger.error("price_update_failed", error=str(e))
            self.broadcast({
                "type": "alert",
                "severity": "warning",
                "message": f"Price feed error: {e}",
            })

    async def _sync_positions(self):
        """Sync positions from all venues."""
        positions = []
        db = await get_db()

        for name, venue in self.venues.items():
            try:
                pos = await venue.get_position()
                positions.append(pos)
                await db.insert_position(pos)
            except Exception as e:
                logger.error("position_sync_failed", venue=name, error=str(e))

        self.broadcast({
            "type": "positions",
            "data": [p.model_dump() for p in positions],
        })

    async def _check_dex_rebalance(self):
        """Check if DEX positions need rebalancing."""
        if not self._trading_enabled:
            return

        try:
            price = await self.price_feed.get_price()
        except Exception as e:
            logger.error("price_fetch_failed_for_rebalance", error=str(e))
            return

        for name in ["aerodrome", "pancakeswap"]:
            if name not in self.venues:
                continue

            venue = self.venues[name]
            if not isinstance(venue, BaseDexAdapter):
                continue

            if venue.paused:
                continue

            try:
                # Get current position
                token_ids = venue.get_owned_positions()
                if not token_ids:
                    logger.debug("no_dex_position", venue=name)
                    continue

                # Check if in range
                position = venue.get_position_state(token_ids[0])
                if position and not position.in_range:
                    logger.info(
                        "position_out_of_range",
                        venue=name,
                        token_id=position.token_id,
                        current_price=float(price.mid),
                        range_lower=float(position.price_lower),
                        range_upper=float(position.price_upper),
                    )
                    # TODO: Implement automatic rebalancing
                    # For now, just alert
                    self.broadcast({
                        "type": "alert",
                        "severity": "warning",
                        "message": f"{name} position out of range",
                    })

            except Exception as e:
                logger.error("dex_rebalance_check_failed", venue=name, error=str(e))

    async def _sync_cex_orders(self):
        """Sync Quidax order ladder."""
        if not self._trading_enabled:
            return

        quidax = self.venues.get("quidax")
        if not quidax or quidax.paused:
            return

        try:
            price = await self.price_feed.get_price()
            await quidax.sync_order_ladder(price.mid)
        except Exception as e:
            logger.error("cex_sync_failed", error=str(e))

    async def _sync_blockradar_rates(self):
        """Sync Blockradar swap rates."""
        if not self._trading_enabled:
            return

        blockradar = self.venues.get("blockradar")
        if not blockradar or blockradar.paused:
            return

        try:
            price = await self.price_feed.get_price()
            await blockradar.sync_rates(price.mid)
        except Exception as e:
            logger.error("blockradar_sync_failed", error=str(e))
