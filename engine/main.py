"""Main application entry point."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, cast

from fastapi import FastAPI, WebSocket
from web3 import Web3
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import structlog
import uvicorn

from engine.accounts import AccountManager, AccountRole
from engine.api import api_router
from engine.types import ArbitrageParams, CexParams
from engine.bot import telegram as bot
from engine.config import DexParams, settings
from engine.db.repository import open_repository
from engine.market.portfolio_exposure import PortfolioExposureCalculator
from engine.market.portfolio_registry import DEFAULT_PORTFOLIO_SOURCE_REGISTRY
from engine.market.price_aggregation import BlendedPriceCalculator, PriceNormalizer
from engine.market.venue_prices import VenuePriceAggregator, create_venue_aggregator
from engine.runtime import EngineRuntime
from engine.scheduler import SchedulerConfig, TradingScheduler
from engine.arb import ArbitrageEngine
from engine.venues.cex.quidax import QuidaxAdapter
from engine.lp.uniswap_v4 import V4PositionManager
from engine.venues.dex.uniswap_base import UniswapBaseV4Adapter, UNISWAP_BASE_EXECUTION_CONFIG
from engine.venues.dex.uniswap_bsc import UniswapBscV4Adapter, UNISWAP_BSC_EXECUTION_CONFIG
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
    system_state_store: Any,
    broadcast: Any = None,
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
            order_user_id=settings.quidax_user_id,
            alert_store=alert_store,
            system_state_store=system_state_store,
            broadcast=broadcast,
        )
        logger.info("venue_initialized", venue="quidax")

    if settings.quidax_lp_api_key:
        venues["quidax-lp"] = QuidaxAdapter(
            api_key=settings.quidax_lp_api_key,
            params=CexParams(),
            name="quidax-lp",
            order_user_id=settings.quidax_lp_user_id,
            alert_store=alert_store,
            system_state_store=system_state_store,
            broadcast=broadcast,
        )
        logger.info("venue_initialized", venue="quidax-lp")

    venues["blockradar"] = BlockradarAdapter(
        api_key=settings.blockradar_api_key,
        wallet_id=settings.blockradar_wallet_id,
    )
    logger.info("venue_initialized", venue="blockradar", rate_setting=bool(settings.blockradar_api_key))
    return venues


async def restore_venue_params(
    db: Any,
    venues: dict[str, Any],
    lp_managers: dict[str, V4PositionManager],
) -> None:
    """Restore persisted venue params for both LP managers and CEX adapters."""
    restored_lp_venues = set()

    for venue_name, lp_manager in lp_managers.items():
        config = await db.venue_config.get_venue_config(venue_name)
        params = config.get("params") if config else None
        if not params:
            continue
        lp_manager.params = DexParams(**params)
        restored_lp_venues.add(venue_name)
        logger.info("venue_params_restored", venue=venue_name)

    for venue_name, venue in venues.items():
        if venue_name in restored_lp_venues:
            continue
        if not isinstance(getattr(venue, "params", None), CexParams):
            continue
        config = await db.venue_config.get_venue_config(venue_name)
        params = config.get("params") if config else None
        if not params:
            continue
        venue.params = CexParams(**params)
        logger.info("venue_params_restored", venue=venue_name)


def init_lp_managers(venues: dict[str, Any]) -> dict[str, V4PositionManager]:
    """Build LP position managers keyed by venue name."""
    lp_managers: dict[str, V4PositionManager] = {}
    for name, config, param_attr in [
        ("uni-base", UNISWAP_BASE_EXECUTION_CONFIG, "uni_base_lp_params"),
        ("uni-bsc", UNISWAP_BSC_EXECUTION_CONFIG, "uni_bsc_lp_params"),
    ]:
        adapter = venues.get(name)
        if adapter is None:
            continue
        pm_contract = adapter.w3.eth.contract(
            address=Web3.to_checksum_address(config.position_manager),
            abi=V4PositionManager.POSITION_MANAGER_ABI,
        )
        lp_managers[name] = V4PositionManager(
            config=config,
            state_view=adapter.state_view,
            position_manager_contract=pm_contract,
            params=getattr(settings, param_attr),
            venue_name=name,
            tx_context=adapter,
        )
        logger.info("lp_manager_initialized", venue=name)
    return lp_managers


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

    venues = await init_venues(
        account_manager,
        alert_store=db.alerts,
        system_state_store=db.system_state,
        broadcast=broadcast_event,
    )
    lp_managers = init_lp_managers(venues)
    await restore_venue_params(db, venues, lp_managers)

    from engine.market.dex_volume import seed_dex_volume_24h
    from engine.market.pool_state import seed_pool_states

    await seed_pool_states()
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
    portfolio_source_registry = DEFAULT_PORTFOLIO_SOURCE_REGISTRY
    portfolio_exposure_calculator = PortfolioExposureCalculator(
        venues=venues,
        account_manager=account_manager,
        token_contracts=TOKEN_CONTRACTS,
        blended_calculator=blended_calculator,
        price_aggregator=price_aggregator,
        portfolio_source_registry=portfolio_source_registry,
        lp_managers=lp_managers,
    )
    logger.info("portfolio_exposure_calculator_initialized")

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

    scheduler = TradingScheduler(
        price_aggregator=price_aggregator,
        venues=venues,
        config=SchedulerConfig(),
        broadcast=broadcast_event,
        blended_calculator=blended_calculator,
        arbitrage_engine=arbitrage_engine,
        account_manager=account_manager,
        token_contracts=TOKEN_CONTRACTS,
        portfolio_exposure_calculator=portfolio_exposure_calculator,
        portfolio_source_registry=portfolio_source_registry,
        lp_managers=lp_managers,
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
        portfolio_exposure_calculator=portfolio_exposure_calculator,
        portfolio_source_registry=portfolio_source_registry,
        lp_managers=lp_managers,
    )
    app.state.runtime = runtime

    trading_state = await db.system_state.get_system_state("trading_enabled")
    if trading_state == "false":
        scheduler.state.trading_enabled = False
        logger.info("trading_restored_paused")

    for venue_name, venue in venues.items():
        paused_state = await db.system_state.get_system_state(f"venue_paused:{venue_name}")
        if paused_state == "true":
            venue.paused = True
            logger.info("venue_restored_paused", venue=venue_name)

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
