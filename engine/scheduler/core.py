"""Trading scheduler shell."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import structlog

from engine.arb.listener import ArbitrageWebSocketListener
from engine.db.backend import (
    ActionStoreProtocol,
    AlertStoreProtocol,
    PositionStoreProtocol,
    PriceStoreProtocol,
    SystemStateStoreProtocol,
    VenueConfigStoreProtocol,
)
from engine.lp.rebalancer import LPRebalancer
from engine.market.portfolio_exposure import PortfolioExposureCalculator
from engine.market.portfolio_registry import (
    DEFAULT_PORTFOLIO_SOURCE_REGISTRY,
    PortfolioSourceDescriptor,
)
from engine.market.price_aggregation import BlendedPriceCalculator
from engine.market.venue_prices import VenuePriceAggregator
from engine.scheduler.config import SchedulerConfig
from engine.scheduler.context import SchedulerContext
from engine.scheduler.jobs.accounts import AccountJobs
from engine.scheduler.jobs.arbitrage import ArbitrageJobs
from engine.scheduler.jobs.lp import LpJobs
from engine.scheduler.jobs.market import MarketJobs
from engine.scheduler.jobs.positions import PositionJobs
from engine.scheduler.types import SchedulerState, TokenContracts
from engine.venues.base import VenueAdapter

if TYPE_CHECKING:
    from engine.accounts import AccountManager
    from engine.arb.engine import ArbitrageEngine

logger = structlog.get_logger()


class TradingScheduler:
    """Orchestrates lifecycle and scheduling while delegating job logic."""

    def __init__(
        self,
        price_aggregator: VenuePriceAggregator,
        venues: dict[str, VenueAdapter],
        config: SchedulerConfig,
        broadcast: Callable[[dict[str, Any]], Any],
        blended_calculator: BlendedPriceCalculator | None = None,
        arbitrage_engine: "ArbitrageEngine | None" = None,
        account_manager: "AccountManager | None" = None,
        token_contracts: TokenContracts | None = None,
        portfolio_exposure_calculator: PortfolioExposureCalculator | None = None,
        portfolio_source_registry: tuple[PortfolioSourceDescriptor, ...] = DEFAULT_PORTFOLIO_SOURCE_REGISTRY,
        quidax_lp: Any | None = None,
        lp_managers: dict[str, Any] | None = None,
        system_state_store: SystemStateStoreProtocol | None = None,
        price_store: PriceStoreProtocol | None = None,
        position_store: PositionStoreProtocol | None = None,
        alert_store: AlertStoreProtocol | None = None,
        venue_config_store: VenueConfigStoreProtocol | None = None,
        action_store: ActionStoreProtocol | None = None,
    ) -> None:
        if (
            system_state_store is None
            or price_store is None
            or position_store is None
            or alert_store is None
            or venue_config_store is None
            or action_store is None
        ):
            raise ValueError(
                "TradingScheduler requires system state, price, position, alert, venue config, and action stores"
            )

        self.config = config
        self.broadcast = broadcast
        self.scheduler = AsyncIOScheduler()
        self.state = SchedulerState(dex_bootstrap_pending=bool(arbitrage_engine))
        _lp_managers = lp_managers or {}
        portfolio_calculator = portfolio_exposure_calculator
        if portfolio_calculator is None and blended_calculator is not None:
            portfolio_calculator = PortfolioExposureCalculator(
                venues=venues,
                account_manager=account_manager,
                token_contracts=token_contracts or {},
                blended_calculator=blended_calculator,
                price_aggregator=price_aggregator,
                portfolio_source_registry=portfolio_source_registry,
                lp_managers=_lp_managers,
            )
        self.context = SchedulerContext(
            config=config,
            price_aggregator=price_aggregator,
            venues=venues,
            broadcast=broadcast,
            blended_calculator=blended_calculator,
            arbitrage_engine=arbitrage_engine,
            account_manager=account_manager,
            token_contracts=token_contracts or {},
            portfolio_exposure_calculator=portfolio_calculator,
            quidax_lp=quidax_lp,
            lp_managers=_lp_managers,
            system_state_store=system_state_store,
            price_store=price_store,
            position_store=position_store,
            alert_store=alert_store,
            venue_config_store=venue_config_store,
            action_store=action_store,
        )
        self.lp_rebalancer = LPRebalancer(
            broadcast=broadcast,
            price_store=price_store,
            venue_config_store=venue_config_store,
            action_store=action_store,
            auto_management_enabled=lambda: self.state.trading_enabled,
        )
        self.position_jobs = PositionJobs(self.context, self.state)
        self.account_jobs = AccountJobs(self.context, self.state)
        self.arbitrage_jobs = ArbitrageJobs(
            self.context,
            self.state,
            update_gas_oracle=self._update_gas_oracle,
            get_balances_for_valuation=self.position_jobs.get_balances_for_valuation,
            broadcast_account_balances=self.account_jobs.broadcast_account_balances,
        )
        self.market_jobs = MarketJobs(
            self.context,
            self.state,
            schedule_dex_bootstrap=self.arbitrage_jobs.schedule_dex_bootstrap,
        )
        self.lp_jobs = LpJobs(self.context, self.state, self.lp_rebalancer)

        self.ws_listener = ArbitrageWebSocketListener(
            broadcast=broadcast,
            on_update=self._update_price,
            on_dex_event=(
                self.context.arbitrage_engine.on_dex_dex_update
                if self.context.arbitrage_engine
                else None
            ),
            on_wallet_event=(
                self._handle_wallet_activity
                if (self.context.arbitrage_engine or self.context.account_manager)
                else None
            ),
            wallet_subscriptions=self.arbitrage_jobs.build_wallet_ws_subscriptions(),
        )
        self.arbitrage_jobs.ws_listener = self.ws_listener

    @property
    def trading_enabled(self) -> bool:
        return self.state.trading_enabled

    def start(self) -> None:
        if self.state.started:
            return

        from datetime import datetime, timezone as tz

        self.scheduler.add_job(
            self._update_gas_oracle,
            IntervalTrigger(seconds=30),
            id="gas_oracle_update",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=15,
            next_run_time=datetime.now(tz.utc),
        )
        self.scheduler.add_job(
            self._update_price,
            IntervalTrigger(seconds=self.config.price_update_interval),
            id="price_update",
            replace_existing=True,
            max_instances=3,
            misfire_grace_time=15,
        )
        self.scheduler.add_job(
            self._sync_positions,
            IntervalTrigger(seconds=self.config.position_sync_interval),
            id="position_sync",
            replace_existing=True,
            max_instances=2,
            misfire_grace_time=15,
        )
        self.scheduler.add_job(
            self._check_dex_rebalance,
            IntervalTrigger(seconds=self.config.dex_check_interval),
            id="dex_rebalance",
            replace_existing=True,
            max_instances=2,
        )
        self.scheduler.add_job(
            self._sync_cex_orders,
            IntervalTrigger(seconds=self.config.cex_sync_interval),
            id="cex_sync",
            replace_existing=True,
            max_instances=2,
            misfire_grace_time=10,
        )

        if self.context.account_manager:
            self.scheduler.add_job(
                self._check_balances,
                IntervalTrigger(seconds=self.config.balance_check_interval),
                id="balance_check",
                replace_existing=True,
                next_run_time=datetime.now(tz.utc),
            )
            logger.info("balance_check_job_registered")

            quidax_arb = self.context.venues.get("quidax")
            if quidax_arb:
                import functools

                self.scheduler.add_job(
                    functools.partial(self.account_jobs.auto_fund_quidax, quidax_arb, "quidax-trade-fund"),
                    IntervalTrigger(seconds=self.config.balance_check_interval),
                    id="auto_fund_quidax_arb",
                    replace_existing=True,
                )
                logger.info("auto_fund_quidax_arb_job_registered")

            quidax_lp = self.context.quidax_lp or self.context.venues.get("quidax-lp")
            if quidax_lp:
                import functools

                self.scheduler.add_job(
                    functools.partial(self.account_jobs.auto_fund_quidax, quidax_lp, "quidax-lp"),
                    IntervalTrigger(seconds=self.config.balance_check_interval),
                    id="auto_fund_quidax_lp",
                    replace_existing=True,
                )
                logger.info("auto_fund_quidax_lp_job_registered")

        if self.context.portfolio_exposure_calculator:
            self.scheduler.add_job(
                self._check_portfolio_delta,
                IntervalTrigger(seconds=self.config.portfolio_delta_interval),
                id="portfolio_delta",
                replace_existing=True,
            )
            logger.info("portfolio_delta_job_registered")

        if "blockradar" in self.context.venues:
            self.scheduler.add_job(
                self._sync_blockradar_rates,
                IntervalTrigger(seconds=self.config.price_update_interval),
                id="blockradar_rate_sync",
                replace_existing=True,
                max_instances=3,
                misfire_grace_time=10,
            )
            logger.info("blockradar_rate_sync_job_registered")

        asyncio.create_task(self.ws_listener.start())
        self.arbitrage_jobs.schedule_dex_bootstrap()
        self.scheduler.add_job(
            self._stream_dex_arb_curve,
            IntervalTrigger(seconds=self.config.dex_arb_curve_interval),
            id="dex_arb_curve_stream",
            replace_existing=True,
            max_instances=2,
            misfire_grace_time=30,
        )
        logger.info("dex_arb_fallback_job_registered")
        self.scheduler.add_job(
            self._stream_quidax_depth,
            IntervalTrigger(seconds=2),
            id="quidax_depth_stream",
            replace_existing=True,
            max_instances=2,
            misfire_grace_time=10,
        )
        logger.info("quidax_depth_stream_job_registered")

        self.scheduler.start()
        self.state.started = True
        logger.info("scheduler_started")

    def stop(self) -> None:
        if self.state.started:
            asyncio.create_task(self.ws_listener.stop())
            self.scheduler.shutdown(wait=False)
            self.state.started = False
            logger.info("scheduler_stopped")

    async def pause(self) -> None:
        self.state.trading_enabled = False
        await self.context.system_state_store.set_system_state("trading_enabled", "false")
        self.broadcast({"type": "system", "status": "paused"})
        logger.info("trading_paused")

    async def resume(self) -> None:
        self.state.trading_enabled = True
        await self.context.system_state_store.set_system_state("trading_enabled", "true")
        self.broadcast({"type": "system", "status": "running"})
        try:
            await self._sync_cex_orders()
        except Exception as exc:
            logger.warning("trading_resume_sync_failed", error=str(exc))
        logger.info("trading_resumed")

    async def _update_gas_oracle(self) -> None:
        await self.market_jobs.update_gas_oracle()

    async def _update_price(self) -> None:
        await self.market_jobs.update_price()

    async def _sync_positions(self) -> None:
        await self.position_jobs.sync_positions()

    async def _check_dex_rebalance(self) -> None:
        await self.lp_jobs.check_dex_rebalance()

    async def _sync_cex_orders(self) -> None:
        await self.market_jobs.sync_cex_orders()

    async def _check_portfolio_delta(self) -> None:
        await self.position_jobs.check_portfolio_delta()

    def _schedule_dex_bootstrap(self) -> None:
        self.arbitrage_jobs.schedule_dex_bootstrap()

    async def _bootstrap_dex_arb_curve(self) -> None:
        await self.arbitrage_jobs.bootstrap_dex_arb_curve()

    async def _stream_dex_arb_curve(self) -> None:
        await self.arbitrage_jobs.stream_dex_arb_curve()

    async def _stream_quidax_depth(self) -> None:
        await self.arbitrage_jobs.stream_quidax_depth()

    async def _handle_wallet_activity(self, venue_names: list[str]) -> None:
        await self.arbitrage_jobs.handle_wallet_activity(venue_names)

    async def _check_balances(self) -> None:
        await self.account_jobs.check_balances()

    async def _sync_blockradar_rates(self) -> None:
        await self.market_jobs.sync_blockradar_rates()
