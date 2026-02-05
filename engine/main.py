"""Main application entry point."""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import structlog
import uvicorn

from engine.config import settings
from engine.db import get_db
from engine.core.price_feed import PriceFeed, PriceFeedConfig
from engine.core.scheduler import TradingScheduler, SchedulerConfig
from engine.venues.dex.aerodrome import AerodromeAdapter
from engine.venues.cex.quidax import QuidaxAdapter
from engine.venues.wallet.blockradar import BlockradarAdapter
from engine.api import routes
from engine.api.schemas import DexParams, CexParams, WalletParams

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Global state
venues: dict[str, Any] = {}
price_feed: PriceFeed | None = None
scheduler: TradingScheduler | None = None
websocket_clients: set = set()


def broadcast_event(event: dict):
    """Broadcast event to all WebSocket clients."""
    # For now, just log - WebSocket implementation can be added later
    logger.debug("broadcast_event", event_type=event.get("type"))


async def init_venues():
    """Initialize venue adapters."""
    global venues

    # Load encrypted keys
    keys = await load_keys()

    # Initialize Aerodrome (Base DEX)
    if keys.get("base_private_key"):
        venues["aerodrome"] = AerodromeAdapter(
            private_key=keys["base_private_key"],
            rpc_url=settings.base_rpc_url,
            params=DexParams(),
        )
        logger.info("venue_initialized", venue="aerodrome")

    # Initialize Quidax (CEX)
    if keys.get("quidax_api_key") and keys.get("quidax_api_secret"):
        venues["quidax"] = QuidaxAdapter(
            api_key=keys["quidax_api_key"],
            api_secret=keys["quidax_api_secret"],
            params=CexParams(),
        )
        logger.info("venue_initialized", venue="quidax")

    # Initialize Blockradar (Wallet system)
    if keys.get("blockradar_api_key"):
        venues["blockradar"] = BlockradarAdapter(
            api_key=keys["blockradar_api_key"],
            params=WalletParams(),
        )
        logger.info("venue_initialized", venue="blockradar")


async def load_keys() -> dict:
    """Load encrypted keys from file."""
    import json
    from pathlib import Path

    keys_path = Path(settings.keys_file)
    if not keys_path.exists():
        logger.warning("keys_file_not_found", path=settings.keys_file)
        return {}

    try:
        # For production, implement proper decryption using settings.key_encryption_key
        # This is a placeholder that expects plain JSON for development
        with open(keys_path) as f:
            return json.load(f)
    except Exception as e:
        logger.error("keys_load_failed", error=str(e))
        return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global price_feed, scheduler

    start_time = time.time()
    logger.info("application_starting")

    # Initialize database
    db = await get_db()
    logger.info("database_connected")

    # Initialize price feed
    price_feed = PriceFeed(config=PriceFeedConfig())
    logger.info("price_feed_initialized")

    # Initialize venues
    await init_venues()

    # Initialize scheduler
    scheduler_config = SchedulerConfig(
        price_update_interval=settings.price_update_interval,
        position_sync_interval=settings.position_sync_interval,
        dex_check_interval=settings.dex_check_interval,
        cex_sync_interval=settings.cex_sync_interval,
        rate_sync_interval=settings.rate_sync_interval,
        rebalance_check_interval=settings.rebalance_check_interval,
    )

    scheduler = TradingScheduler(
        price_feed=price_feed,
        venues=venues,
        config=scheduler_config,
        broadcast=broadcast_event,
    )

    # Initialize routes with dependencies
    routes.init_routes(scheduler, venues, price_feed, start_time)

    # Restore trading state from DB
    trading_state = await db.get_system_state("trading_enabled")
    if trading_state == "false":
        scheduler._trading_enabled = False
        logger.info("trading_restored_paused")

    # Start scheduler
    scheduler.start()
    logger.info("scheduler_started")

    logger.info("application_started", startup_time=time.time() - start_time)

    yield

    # Shutdown
    logger.info("application_stopping")

    if scheduler:
        scheduler.stop()

    # Close venue connections
    for name, venue in venues.items():
        if hasattr(venue, "close"):
            await venue.close()
            logger.info("venue_closed", venue=name)

    # Close database
    await db.close()
    logger.info("database_closed")

    logger.info("application_stopped")


# Create FastAPI app
app = FastAPI(
    title="CNGN Trading Engine",
    description="Automated trading engine for CNGN stablecoin management",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routes
app.include_router(routes.router, prefix="/api")

# Mount static files for dashboard (if exists)
dashboard_path = Path(__file__).parent.parent / "dashboard" / "out"
if dashboard_path.exists():
    app.mount("/", StaticFiles(directory=str(dashboard_path), html=True), name="dashboard")


def main():
    """Run the application."""
    uvicorn.run(
        "engine.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()
