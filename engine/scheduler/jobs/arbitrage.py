"""Arbitrage-related scheduler jobs."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, cast

import structlog

from engine.types import WalletActivitySubscription
from engine.scheduler.context import SchedulerContext
from engine.scheduler.types import SchedulerState
from engine.venues.base import DepthVenue
from engine.venues.dex.v4 import BaseV4DexAdapter

logger = structlog.get_logger()


class ArbitrageJobs:
    def __init__(
        self,
        context: SchedulerContext,
        state: SchedulerState,
        *,
        update_gas_oracle: Callable[[], Awaitable[None]],
        get_balances_for_valuation: Callable[[DepthVenue], Awaitable[list[Any]]],
        broadcast_account_balances: Callable[[list[Any]], Awaitable[None]],
    ) -> None:
        self.context = context
        self.state = state
        self._update_gas_oracle = update_gas_oracle
        self._get_balances_for_valuation = get_balances_for_valuation
        self._broadcast_account_balances = broadcast_account_balances
        self.ws_listener: Any | None = None

    def build_wallet_ws_subscriptions(self) -> dict[str, list[WalletActivitySubscription]]:
        subscriptions: dict[str, list[WalletActivitySubscription]] = {}
        for venue_name in ("uni-base", "uni-bsc"):
            venue = self.context.venues.get(venue_name)
            if not isinstance(venue, BaseV4DexAdapter):
                continue

            chain_name = getattr(getattr(venue, "config", None), "chain_name", "")
            if chain_name not in ("base", "bsc"):
                continue

            subscriptions.setdefault(chain_name, []).extend(
                [
                    WalletActivitySubscription(
                        venue_name=venue_name,
                        wallet_address=venue.trade_account.address,
                        token_address=venue.stable_address,
                    ),
                    WalletActivitySubscription(
                        venue_name=venue_name,
                        wallet_address=venue.trade_account.address,
                        token_address=venue.cngn_address,
                    ),
                ]
            )
        return subscriptions

    def schedule_dex_bootstrap(self) -> None:
        if not self.context.arbitrage_engine or not self.state.dex_bootstrap_pending:
            return
        if self.state.dex_bootstrap_task and not self.state.dex_bootstrap_task.done():
            return
        self.state.dex_bootstrap_task = asyncio.create_task(self.bootstrap_dex_arb_curve())

    async def bootstrap_dex_arb_curve(self) -> None:
        from engine.market import gas_oracle
        from engine.market.pool_state import seed_dex_pool_states

        if not self.context.arbitrage_engine or not self.state.dex_bootstrap_pending:
            return
        try:
            await seed_dex_pool_states()
            await self._update_gas_oracle()
            if gas_oracle.gas_usd_base() is None or gas_oracle.gas_usd_bsc() is None:
                logger.warning("dex_arb_bootstrap_waiting_for_gas")
                return
            await self.context.arbitrage_engine.on_dex_dex_update()
            self.state.dex_bootstrap_pending = False
        except Exception as exc:
            logger.error("dex_arb_bootstrap_failed", error=str(exc), exc_info=True)
            self.context.broadcast({
                "type": "alert",
                "severity": "critical",
                "message": f"DEX arb bootstrap failed — DEX arbitrage will not run until next retry: {exc}",
            })

    async def stream_dex_arb_curve(self) -> None:
        try:
            active_connections = self.ws_listener.active_connections if self.ws_listener else set()
            ws_healthy = {"base", "bsc"}.issubset(active_connections)
            if self.context.arbitrage_engine and not ws_healthy:
                await self.context.arbitrage_engine.on_dex_dex_update()
        except Exception as exc:
            logger.error("dex_arb_curve_stream_failed", error=str(exc), exc_info=True)

    async def stream_quidax_depth(self) -> None:
        try:
            quidax = self.context.venues.get("quidax")
            if not quidax:
                return

            depth = await cast(DepthVenue, quidax).get_order_book_depth(limit=20)
            if not depth:
                if self.state.quidax_depth_ok:
                    self.state.quidax_depth_ok = False
                    logger.warning("quidax_depth_unavailable")
                return

            if not self.state.quidax_depth_ok:
                self.state.quidax_depth_ok = True
                logger.info("quidax_depth_restored")

            self.context.broadcast(
                {
                    "type": "quidax_orderbook_depth",
                    "data": {
                        "venue": depth.venue,
                        "pair": depth.pair,
                        "timestamp": depth.timestamp,
                        "bids": [{"price": float(bid.price), "amount": float(bid.amount)} for bid in depth.bids],
                        "asks": [{"price": float(ask.price), "amount": float(ask.amount)} for ask in depth.asks],
                    },
                }
            )

            if self.context.arbitrage_engine:
                balances = await self._get_balances_for_valuation(cast(DepthVenue, quidax))
                await self.context.arbitrage_engine.on_cex_dex_depth(depth, balances)
        except Exception as exc:
            logger.error("quidax_depth_stream_failed", error=str(exc))
            if self.state.quidax_depth_ok:
                self.state.quidax_depth_ok = False
                logger.warning("quidax_depth_fetch_error", error=str(exc))

    async def handle_wallet_activity(self, venue_names: list[str]) -> None:
        if self.context.arbitrage_engine:
            await self.context.arbitrage_engine.on_wallet_activity(venue_names)

        if not self.context.account_manager:
            return

        try:
            balances = await self.context.account_manager.check_all_balances(self.context.token_contracts)
            self.state.last_balances = list(balances)
            await self._broadcast_account_balances(list(balances))
        except Exception as exc:
            logger.error("wallet_activity_balance_refresh_failed", venues=venue_names, error=str(exc))
