"""Main application entry point."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, cast

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import structlog
import uvicorn

from engine.accounts import AccountManager, AccountRole
from engine.api import api_router
from engine.api.schemas import ArbitrageParams, CexParams
from engine.bot import telegram as bot
from engine.config import DexParams, settings
from engine.db import open_repository
from engine.market.price_aggregation import BlendedPriceCalculator, PriceNormalizer
from engine.market.venue_prices import VenuePriceAggregator, create_venue_aggregator
from engine.runtime import EngineRuntime
from engine.scheduler import SchedulerConfig, TradingScheduler
from engine.arb import ArbitrageEngine
from engine.venues.cex.quidax import QuidaxAdapter
from engine.venues.dex.lp_v4 import V4LPAdapter
from engine.venues.dex.uniswap_base import UniswapBaseV4Adapter
from engine.venues.dex.uniswap_bsc import UniswapBscV4Adapter
from engine.venues.wallet.blockradar import BlockradarAdapter
from engine.ws import ws_manager


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

TOKEN_CONTRACTS: dict[int, dict[str, str]] = {
    8453: {
        "cNGN": settings.cngn_base_address,
        "USDC": settings.usdc_base_address,
        "USDT": settings.usdt_base_address,
    },
    56: {
        "cNGN": settings.cngn_bsc_address,
        "USDT": settings.usdt_bsc_address,
    },
}


def broadcast_event(event: dict[str, Any]) -> None:
    """Broadcast event to all connected WebSocket clients and Telegram."""
    ws_manager.broadcast(event)
    asyncio.create_task(bot.forward_alert(event))


async def init_venues(
    acct_manager: AccountManager | None,
    *,
    alert_store: Any,
) -> dict[str, Any]:
    """Initialize venue adapters. All secrets come from env vars."""
    venues: dict[str, Any] = {}

    if acct_manager:
        try:
            lp_key = acct_manager.get_private_key(AccountRole.UNI_BASE_LP)
            trade_key = acct_manager.get_private_key(AccountRole.UNI_BASE_TRADE)
            venues["uni-base"] = UniswapBaseV4Adapter(
                lp_private_key=lp_key,
                trade_private_key=trade_key,
                rpc_url=settings.base_rpc_url,
            )
            logger.info("venue_initialized", venue="uni-base")
        except ValueError as exc:
            logger.warning("uni_base_init_skipped", reason=str(exc))

    if acct_manager:
        try:
            lp_key = acct_manager.get_private_key(AccountRole.UNI_BSC_LP)
            trade_key = acct_manager.get_private_key(AccountRole.UNI_BSC_TRADE)
            venues["uni-bsc"] = UniswapBscV4Adapter(
                lp_private_key=lp_key,
                trade_private_key=trade_key,
            )
            logger.info("venue_initialized", venue="uni-bsc")
        except ValueError as exc:
            logger.warning("uni_bsc_init_skipped", reason=str(exc))

    if settings.quidax_api_key:
        venues["quidax"] = QuidaxAdapter(
            api_key=settings.quidax_api_key,
            params=CexParams(),
            name="quidax",
            funding_role="quidax-trade-fund",
            alert_store=alert_store,
        )
        logger.info("venue_initialized", venue="quidax")

    if settings.quidax_lp_api_key:
        venues["quidax-lp"] = QuidaxAdapter(
            api_key=settings.quidax_lp_api_key,
            params=CexParams(),
            name="quidax-lp",
            funding_role="quidax-lp",
            alert_store=alert_store,
        )
        logger.info("venue_initialized", venue="quidax-lp")

    venues["blockradar"] = BlockradarAdapter(
        api_key=settings.blockradar_api_key,
        wallet_id=settings.blockradar_wallet_id,
    )
    logger.info("venue_initialized", venue="blockradar", rate_setting=bool(settings.blockradar_api_key))
    return venues


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager."""
    start_time = time.time()
    logger.info("application_starting")

    db = await open_repository(settings.db_path)
    logger.info("database_connected")

    account_manager: AccountManager | None = None
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
        except Exception as exc:
            logger.error("account_manager_init_failed", error=str(exc))
    else:
        logger.info("account_manager_skipped", reason="no mnemonic configured")

    venues = await init_venues(account_manager, alert_store=db.alerts)

    for venue_name, venue_adapter in venues.items():
        if isinstance(venue_adapter, V4LPAdapter):
            config = await db.venue_config.get_venue_config(venue_name)
            if config and config.get("params"):
                venue_adapter.params = DexParams(**config["params"])
                logger.info("venue_params_restored", venue=venue_name)

    from engine.market.dex_volume import seed_dex_volume_24h
    from engine.market.pool_state import seed_pool_states

    await seed_pool_states()  # type: ignore[no-untyped-call]
    await seed_dex_volume_24h()

    price_aggregator: VenuePriceAggregator = create_venue_aggregator(
        bybit_enabled=True,
        quidax_enabled=True,
        blockradar_adapter=venues.get("blockradar"),
    )
    logger.info("price_aggregator_initialized", venues=list(price_aggregator.sources.keys()))

    normalizer = PriceNormalizer()
    blended_calculator = BlendedPriceCalculator(
        price_aggregator=price_aggregator,
        normalizer=normalizer,
        price_store=db.prices,
    )
    logger.info("blended_price_calculator_initialized")

    arbitrage_engine: ArbitrageEngine | None = None
    if settings.arb_detection_enabled:
        arbitrage_engine = ArbitrageEngine(
            venues=venues,
            params=ArbitrageParams(),
            broadcast=broadcast_event,
            execute_cex_dex_enabled=settings.arb_execute_cex_dex_enabled,
            execute_dex_dex_enabled=settings.arb_execute_dex_dex_enabled,
            arbitrage_store=db.arbitrage,
            history_store=db.history,
            price_store=db.prices,
        )
        logger.info(
            "arbitrage_engine_initialized",
            execute_cex_dex_enabled=settings.arb_execute_cex_dex_enabled,
            execute_dex_dex_enabled=settings.arb_execute_dex_dex_enabled,
        )

    quidax_lp = venues.get("quidax-lp")
    scheduler = TradingScheduler(
        price_aggregator=price_aggregator,
        venues=venues,
        config=SchedulerConfig(),
        broadcast=broadcast_event,
        blended_calculator=blended_calculator,
        arbitrage_engine=arbitrage_engine,
        account_manager=account_manager,
        token_contracts=TOKEN_CONTRACTS,
        quidax_lp=quidax_lp,
        system_state_store=db.system_state,
        price_store=db.prices,
        position_store=db.positions,
        alert_store=db.alerts,
        venue_config_store=db.venue_config,
        action_store=db.actions,
    )

    runtime = EngineRuntime(
        db=db,
        scheduler=scheduler,
        venues=venues,
        price_aggregator=price_aggregator,
        start_time=start_time,
        arbitrage_engine=arbitrage_engine,
        account_manager=account_manager,
        token_contracts=TOKEN_CONTRACTS,
        blended_calculator=blended_calculator,
        normalizer=normalizer,
        quidax_lp=quidax_lp,
    )
    app.state.runtime = runtime

    trading_state = await db.system_state.get_system_state("trading_enabled")
    if trading_state == "false":
        scheduler.state.trading_enabled = False
        logger.info("trading_restored_paused")

    scheduler.start()

    if settings.telegram_bot_token:
        try:
            await bot.start(settings, runtime)
        except Exception as exc:
            logger.error("telegram_bot_start_failed", error=str(exc))

    logger.info("application_started", startup_time=time.time() - start_time)

    yield

    logger.info("application_stopping")

    await bot.stop()
    scheduler.stop()
    await price_aggregator.close()
    logger.info("price_aggregator_closed")

    for name, venue in venues.items():
        if hasattr(venue, "close"):
            await cast(Any, venue).close()
            logger.info("venue_closed", venue=name)

    await db.close()
    app.state.runtime = None
    logger.info("application_stopped")


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

app.include_router(api_router, prefix="/api")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Stream real-time events (prices, positions, alerts, arbitrage) to clients."""
    await ws_manager.handle(ws)


dashboard_path = Path(__file__).parent.parent / "dashboard" / "out"
if dashboard_path.exists():
    app.mount("/", StaticFiles(directory=str(dashboard_path), html=True), name="dashboard")


def main() -> None:
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
