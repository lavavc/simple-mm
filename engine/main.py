"""Main application entry point."""

import asyncio
import time
from decimal import Decimal
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
from engine.venues.dex.aerodrome import AerodromeAdapter
from engine.venues.dex.pancakeswap import PancakeSwapAdapter
from engine.venues.dex.assetchain import AssetChainAdapter
from engine.venues.cex.quidax import QuidaxAdapter
from engine.venues.wallet.blockradar import BlockradarAdapter
from engine.api import routes
from engine.api.schemas import DexParams, CexParams, ArbitrageParams
from engine.bot import telegram as bot

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

# Token contract addresses for balance monitoring, keyed by chain_id
TOKEN_CONTRACTS: dict[int, dict[str, str]] = {
    8453: {  # Base
        "cNGN": settings.cngn_base_address,
        "USDC": settings.usdc_base_address,
        "USDT": settings.usdt_base_address,
    },
    56: {  # BSC (PancakeSwap LP/trade + Quidax arb/lp on-chain wallets)
        "cNGN": settings.cngn_bsc_address,
        "USDT": settings.usdt_bsc_address,
    },
}


def broadcast_event(event: dict):
    """Broadcast event to all connected WebSocket clients."""
    ws_manager.broadcast(event)
    asyncio.create_task(bot.forward_alert(event))


async def init_venues(acct_manager: AccountManager | None = None):
    """Initialize venue adapters. All secrets come from env vars."""
    global venues

    # Uniswap V4 Base (uni-base) — requires HD wallet
    if acct_manager:
        try:
            lp_key = acct_manager.get_private_key(AccountRole.UNI_BASE_LP)
            trade_key = acct_manager.get_private_key(AccountRole.UNI_BASE_TRADE)
            venues["uni-base"] = AerodromeAdapter(
                lp_private_key=lp_key,
                trade_private_key=trade_key,
                rpc_url=settings.base_rpc_url,
                params=DexParams(
                    sd_multiplier=Decimal("2.75"),
                    ewma_lambda=Decimal("0.975"),
                    downside_skew=Decimal("0.3"),
                ),
            )
            logger.info("venue_initialized", venue="uni-base")
        except ValueError as e:
            logger.warning("uni_base_init_skipped", reason=str(e))

    # Uniswap V4 BSC (uni-bsc) — requires HD wallet
    if acct_manager:
        try:
            lp_key = acct_manager.get_private_key(AccountRole.UNI_BSC_LP)
            trade_key = acct_manager.get_private_key(AccountRole.UNI_BSC_TRADE)
            venues["uni-bsc"] = PancakeSwapAdapter(
                lp_private_key=lp_key,
                trade_private_key=trade_key,
                params=DexParams(
                    sd_multiplier=Decimal("3.0"),
                    ewma_lambda=Decimal("0.975"),
                    downside_skew=Decimal("0.5"),
                ),
            )
            logger.info("venue_initialized", venue="uni-bsc")
        except ValueError as e:
            logger.warning("uni_bsc_init_skipped", reason=str(e))

    # Quidax arb adapter (used only by arb engine)
    if settings.quidax_api_key:
        venues["quidax"] = QuidaxAdapter(
            api_key=settings.quidax_api_key,
            params=CexParams(),
            name="quidax",
            funding_role="quidax-trade-fund",
        )
        logger.info("venue_initialized", venue="quidax")

    # Quidax LP adapter (used by order ladder; separate funds from arb)
    if settings.quidax_lp_api_key:
        venues["quidax-lp"] = QuidaxAdapter(
            api_key=settings.quidax_lp_api_key,
            params=CexParams(),
            name="quidax-lp",
            funding_role="quidax-lp",
        )
        logger.info("venue_initialized", venue="quidax-lp")

    # Blockradar (wallet system) — public rate endpoints need no key
    venues["blockradar"] = BlockradarAdapter(
        api_key=settings.blockradar_api_key,
        wallet_id=settings.blockradar_wallet_id,
    )
    logger.info("venue_initialized", venue="blockradar", rate_setting=bool(settings.blockradar_api_key))


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

    # Seed the globally cached DEX pool states first so the aggregator zero-latency hook works instantly
    from engine.core.arbitrage.pool_state import seed_pool_states
    await seed_pool_states()

    # Price aggregator: reads directly from the simulator cache for DEXs
    price_aggregator = create_venue_aggregator(
        bybit_enabled=True,
        quidax_enabled=True,
        blockradar_adapter=venues.get("blockradar"),
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
    if settings.arb_detection_enabled:
        arb_params = ArbitrageParams()

        arbitrage_engine = ArbitrageEngine(
            venues=venues,
            params=arb_params,
            broadcast=broadcast_event,
            execute_cex_dex_enabled=settings.arb_execute_cex_dex_enabled,
            execute_dex_dex_enabled=settings.arb_execute_dex_dex_enabled,
        )
        logger.info(
            "arbitrage_engine_initialized",
            execute_cex_dex_enabled=settings.arb_execute_cex_dex_enabled,
            execute_dex_dex_enabled=settings.arb_execute_dex_dex_enabled,
        )

    _quidax_lp = venues.get("quidax-lp")

    # Scheduler
    scheduler_config = SchedulerConfig()

    scheduler = TradingScheduler(
        price_aggregator=price_aggregator,
        venues=venues,
        config=scheduler_config,
        broadcast=broadcast_event,
        blended_calculator=blended_calculator,
        arbitrage_engine=arbitrage_engine,
        account_manager=account_manager,
        token_contracts=TOKEN_CONTRACTS,
        quidax_lp=_quidax_lp,
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
        quidax_lp=_quidax_lp,
    )

    # Restore trading state
    trading_state = await db.get_system_state("trading_enabled")
    if trading_state == "false":
        scheduler._trading_enabled = False
        logger.info("trading_restored_paused")

    scheduler.start()

    if settings.telegram_bot_token:
        try:
            await bot.start(settings, scheduler, venues, arbitrage_engine, account_manager, TOKEN_CONTRACTS)
        except Exception as e:
            logger.error("telegram_bot_start_failed", error=str(e))

    logger.info("application_started", startup_time=time.time() - start_time)

    yield

    # Shutdown
    logger.info("application_stopping")

    await bot.stop()

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
