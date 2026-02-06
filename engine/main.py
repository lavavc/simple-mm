"""Main application entry point."""

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import structlog
import uvicorn

from engine.config import settings
from engine.ws import ws_manager
from engine.db import get_db
from engine.core.venue_prices import create_venue_aggregator, VenuePriceAggregator
from engine.core.price_aggregation import PriceNormalizer, BlendedPriceCalculator
from engine.core.scheduler import TradingScheduler, SchedulerConfig
from engine.core.arbitrage import ArbitrageEngine
from engine.core.accounts import AccountManager, AccountRole
from engine.venues.dex.aerodrome import AerodromeAdapter, AERODROME_POOL_READ_CONFIG
from engine.venues.dex.pancakeswap import PANCAKESWAP_POOL_READ_CONFIG
from engine.venues.dex.base import PoolPriceReader
from engine.venues.cex.quidax import QuidaxAdapter
from engine.venues.wallet.blockradar import BlockradarAdapter
from engine.api import routes
from engine.api.schemas import DexParams, CexParams, WalletParams, ArbitrageParams

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
price_aggregator: VenuePriceAggregator | None = None
blended_calculator: BlendedPriceCalculator | None = None
normalizer: PriceNormalizer | None = None
scheduler: TradingScheduler | None = None
arbitrage_engine: ArbitrageEngine | None = None
account_manager: AccountManager | None = None

# Token contract addresses for balance monitoring
TOKEN_CONTRACTS: dict[str, str] = {
    "cNGN": settings.cngn_contract_address,
    "USDC": settings.usdc_contract_address,
    "USDT": settings.usdt_contract_address,
}


def broadcast_event(event: dict):
    """Broadcast event to all connected WebSocket clients."""
    ws_manager.broadcast(event)


async def init_venues(acct_manager: AccountManager | None = None):
    """Initialize venue adapters. All secrets come from env vars."""
    global venues

    # Aerodrome (Base DEX) — requires HD wallet
    if acct_manager:
        try:
            lp_key = acct_manager.get_private_key(AccountRole.AERODROME_LP)
            trade_key = acct_manager.get_private_key(AccountRole.AERODROME_TRADE)
            venues["aerodrome"] = AerodromeAdapter(
                lp_private_key=lp_key,
                trade_private_key=trade_key,
                rpc_url=settings.base_rpc_url,
                params=DexParams(),
            )
            logger.info("venue_initialized", venue="aerodrome")
        except ValueError as e:
            logger.warning("aerodrome_init_skipped", reason=str(e))

    # Quidax (CEX)
    if settings.quidax_api_key:
        venues["quidax"] = QuidaxAdapter(
            api_key=settings.quidax_api_key,
            params=CexParams(),
        )
        logger.info("venue_initialized", venue="quidax")

    # Blockradar (wallet system)
    if settings.blockradar_api_key:
        venues["blockradar"] = BlockradarAdapter(
            api_key=settings.blockradar_api_key,
            params=WalletParams(),
        )
        logger.info("venue_initialized", venue="blockradar")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global price_aggregator, blended_calculator, normalizer
    global scheduler, arbitrage_engine, account_manager

    start_time = time.time()
    logger.info("application_starting")

    db = await get_db()
    logger.info("database_connected")

    # Account manager (HD wallet)
    if settings.use_test_accounts or settings.wallet_mnemonic:
        try:
            account_manager = AccountManager(
                mnemonic=settings.wallet_mnemonic if settings.wallet_mnemonic else None,
                use_test_accounts=settings.use_test_accounts,
            )
            logger.info(
                "account_manager_initialized",
                test_mode=settings.use_test_accounts,
                accounts=list(account_manager.list_accounts().keys()),
            )
        except Exception as e:
            logger.error("account_manager_init_failed", error=str(e))
            account_manager = None
    else:
        logger.info("account_manager_skipped", reason="no mnemonic configured")

    await init_venues(account_manager)

    # Read-only pool price readers (always created -- no keys needed)
    aerodrome_reader = PoolPriceReader(
        config=AERODROME_POOL_READ_CONFIG, source_name="aerodrome"
    )
    pancakeswap_reader = PoolPriceReader(
        config=PANCAKESWAP_POOL_READ_CONFIG, source_name="pancakeswap"
    )
    logger.info(
        "pool_price_readers_initialized",
        aerodrome_rpc=AERODROME_POOL_READ_CONFIG.rpc_url,
        pancakeswap_rpc=PANCAKESWAP_POOL_READ_CONFIG.rpc_url,
    )

    # Price aggregator: adapter > reader for each DEX
    price_aggregator = create_venue_aggregator(
        bybit_enabled=True,
        quidax_enabled=True,
        aerodrome_adapter=venues.get("aerodrome"),
        aerodrome_reader=aerodrome_reader,
        pancakeswap_adapter=venues.get("pancakeswap"),
        pancakeswap_reader=pancakeswap_reader,
    )
    logger.info("price_aggregator_initialized", venues=list(price_aggregator.sources.keys()))

    # Blended price calculator
    normalizer = PriceNormalizer()
    blended_calculator = BlendedPriceCalculator(
        price_aggregator=price_aggregator,
        normalizer=normalizer,
    )
    logger.info("blended_price_calculator_initialized")

    # Arbitrage engine
    if settings.arbitrage_enabled:
        from decimal import Decimal

        arb_params = ArbitrageParams(
            min_spread_bps=settings.arbitrage_min_spread_bps,
            min_net_profit_bps=settings.arbitrage_min_net_profit_bps,
            max_single_trade_usd=Decimal(str(settings.arbitrage_max_single_trade_usd)),
            max_daily_volume_usd=Decimal(str(settings.arbitrage_max_daily_volume_usd)),
            max_inventory_imbalance_usd=Decimal(str(settings.arbitrage_max_inventory_imbalance_usd)),
            scan_interval_seconds=settings.arbitrage_scan_interval,
        )

        arbitrage_engine = ArbitrageEngine(
            price_aggregator=price_aggregator,
            venues=venues,
            params=arb_params,
            broadcast=broadcast_event,
            execution_enabled=settings.arbitrage_execution_enabled,
            normalizer=normalizer,
            blended_calculator=blended_calculator,
        )
        logger.info(
            "arbitrage_engine_initialized",
            execution_enabled=settings.arbitrage_execution_enabled,
        )

    # Scheduler
    scheduler_config = SchedulerConfig(
        price_update_interval=settings.price_update_interval,
        position_sync_interval=settings.position_sync_interval,
        dex_check_interval=settings.dex_check_interval,
        cex_sync_interval=settings.cex_sync_interval,
        rate_sync_interval=settings.rate_sync_interval,
        rebalance_check_interval=settings.rebalance_check_interval,
        arbitrage_scan_interval=settings.arbitrage_scan_interval,
        balance_check_interval=settings.balance_check_interval,
    )

    scheduler = TradingScheduler(
        price_aggregator=price_aggregator,
        venues=venues,
        config=scheduler_config,
        broadcast=broadcast_event,
        blended_calculator=blended_calculator,
        arbitrage_engine=arbitrage_engine,
        account_manager=account_manager,
        token_contracts=TOKEN_CONTRACTS,
    )

    routes.init_routes(
        scheduler,
        venues,
        price_aggregator,
        start_time,
        arbitrage_engine,
        account_manager,
        TOKEN_CONTRACTS,
        blended_calculator=blended_calculator,
        normalizer=normalizer,
    )

    # Restore trading state
    trading_state = await db.get_system_state("trading_enabled")
    if trading_state == "false":
        scheduler._trading_enabled = False
        logger.info("trading_restored_paused")

    scheduler.start()
    logger.info("application_started", startup_time=time.time() - start_time)

    yield

    # Shutdown
    logger.info("application_stopping")

    if scheduler:
        scheduler.stop()

    if price_aggregator:
        await price_aggregator.close()
        logger.info("price_aggregator_closed")

    for name, venue in venues.items():
        if hasattr(venue, "close"):
            await venue.close()
            logger.info("venue_closed", venue=name)

    await db.close()
    logger.info("application_stopped")


# Create FastAPI app
app = FastAPI(
    title="CNGN Trading Engine",
    description="Automated trading engine for CNGN stablecoin management",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes.router, prefix="/api")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Stream real-time events (prices, positions, alerts, arbitrage) to clients."""
    await ws_manager.handle(ws)


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
