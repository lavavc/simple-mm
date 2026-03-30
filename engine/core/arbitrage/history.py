"""Lifecycle history helpers for routed arbitrage attempts."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

import structlog

from engine.api.schemas import (
    ArbitrageHistoryEvent,
    ArbitrageHistoryWalletSnapshot,
)
from engine.core.arbitrage.router import SelectedRoute


_STABLE_SYMBOLS = {
    "quidax": "USDT",
    "uni-base": "USDC",
    "uni-bsc": "USDT",
}

logger = structlog.get_logger()


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


class ArbitrageHistoryRecorder:
    """Builds and stores append-only lifecycle events for executed routes."""

    def __init__(
        self,
        inventory,
        broadcast: Callable[[dict], Any],
        db_getter: Callable[[], Awaitable[Any]],
    ):
        self.inventory = inventory
        self.broadcast = broadcast
        self.db_getter = db_getter

    def _stable_symbol_for_venue(self, venue_name: str) -> Optional[str]:
        return _STABLE_SYMBOLS.get(venue_name)

    def _wallet_snapshot(self, venue_name: str) -> ArbitrageHistoryWalletSnapshot:
        return ArbitrageHistoryWalletSnapshot(
            stable_symbol=self._stable_symbol_for_venue(venue_name),
            stable_balance=self.inventory.state.per_account_stable.get(venue_name),
            cngn_balance=self.inventory.state.per_account_cngn.get(venue_name),
        )

    def _base_event(
        self,
        opp_id: str,
        route: SelectedRoute,
        *,
        event_type: str,
        status: str,
        reason: Optional[str] = None,
        actual_profit_usd: Optional[Decimal] = None,
        executed_size_usd: Optional[Decimal] = None,
        buy_tx_hash: Optional[str] = None,
        sell_tx_hash: Optional[str] = None,
    ) -> ArbitrageHistoryEvent:
        optimal = route.candidate.signal.get("optimal_arb", {})
        return ArbitrageHistoryEvent(
            opportunity_id=opp_id,
            pipeline=route.candidate.pipeline,
            event_type=event_type,
            timestamp=int(time.time() * 1000),
            direction=route.candidate.direction,
            buy_venue=route.candidate.buy_venue,
            sell_venue=route.candidate.sell_venue,
            status=status,
            optimal_size_usd=route.candidate.optimal_size_usd,
            routed_size_usd=route.adjusted_size_usd,
            executed_size_usd=executed_size_usd,
            expected_profit_usd=route.expected_profit_usd,
            actual_profit_usd=actual_profit_usd,
            net_profit_usd=route.net_profit_usd,
            net_spread_bps=optimal.get("net_spread_bps"),
            reason=reason,
            buy_wallet=self._wallet_snapshot(route.candidate.buy_venue) if event_type == "routed" else None,
            sell_wallet=self._wallet_snapshot(route.candidate.sell_venue) if event_type == "routed" else None,
            buy_tx_hash=buy_tx_hash,
            sell_tx_hash=sell_tx_hash,
        )

    async def _store(self, event: ArbitrageHistoryEvent) -> None:
        try:
            db = await self.db_getter()
            await db.upsert_arbitrage_history_event(event)
            self.broadcast({"type": "arb_history_updated", "data": {"opportunity_id": event.opportunity_id}})
        except Exception as exc:
            logger.warning(
                "arb_history_record_failed",
                opportunity_id=event.opportunity_id,
                event_type=event.event_type,
                error=str(exc),
            )

    async def record_routed(self, opp_id: str, route: SelectedRoute) -> None:
        await self._store(self._base_event(opp_id, route, event_type="routed", status="routed"))

    async def record_failed(
        self,
        opp_id: str,
        route: SelectedRoute,
        *,
        status: str,
        reason: Optional[str] = None,
        executed_size_usd: Optional[Decimal] = None,
        buy_tx_hash: Optional[str] = None,
        sell_tx_hash: Optional[str] = None,
    ) -> None:
        await self._store(
            self._base_event(
                opp_id,
                route,
                event_type="failed",
                status=status,
                reason=reason,
                executed_size_usd=executed_size_usd,
                buy_tx_hash=buy_tx_hash,
                sell_tx_hash=sell_tx_hash,
            )
        )

    async def record_executed(
        self,
        opp_id: str,
        route: SelectedRoute,
        *,
        actual_profit_usd: Any,
        buy_tx_hash: Optional[str] = None,
        sell_tx_hash: Optional[str] = None,
    ) -> None:
        await self._store(
            self._base_event(
                opp_id,
                route,
                event_type="executed",
                status="completed",
                actual_profit_usd=_to_decimal(actual_profit_usd),
                executed_size_usd=route.adjusted_size_usd,
                buy_tx_hash=buy_tx_hash,
                sell_tx_hash=sell_tx_hash,
            )
        )

    async def _record_snapshot(
        self,
        *,
        opp_id: str,
        event_type: str,
        pipeline: str,
        direction: str,
        buy_venue: str,
        sell_venue: str,
        status: str,
        optimal_size_usd: Any = None,
        routed_size_usd: Any = None,
        executed_size_usd: Any = None,
        expected_profit_usd: Any = None,
        actual_profit_usd: Any = None,
        net_profit_usd: Any = None,
        net_spread_bps: Optional[int] = None,
        reason: Optional[str] = None,
        buy_tx_hash: Optional[str] = None,
        sell_tx_hash: Optional[str] = None,
    ) -> None:
        await self._store(
            ArbitrageHistoryEvent(
                opportunity_id=opp_id,
                pipeline=pipeline,
                event_type=event_type,
                timestamp=int(time.time() * 1000),
                direction=direction,
                buy_venue=buy_venue,
                sell_venue=sell_venue,
                status=status,
                optimal_size_usd=_to_decimal(optimal_size_usd),
                routed_size_usd=_to_decimal(routed_size_usd),
                executed_size_usd=_to_decimal(executed_size_usd),
                expected_profit_usd=_to_decimal(expected_profit_usd),
                actual_profit_usd=_to_decimal(actual_profit_usd),
                net_profit_usd=_to_decimal(net_profit_usd),
                net_spread_bps=net_spread_bps,
                reason=reason,
                buy_tx_hash=buy_tx_hash,
                sell_tx_hash=sell_tx_hash,
            )
        )

    async def record_executed_snapshot(
        self,
        *,
        opp_id: str,
        pipeline: str,
        direction: str,
        buy_venue: str,
        sell_venue: str,
        optimal_size_usd: Any = None,
        routed_size_usd: Any = None,
        executed_size_usd: Any = None,
        expected_profit_usd: Any = None,
        actual_profit_usd: Any = None,
        net_profit_usd: Any = None,
        net_spread_bps: Optional[int] = None,
        reason: Optional[str] = None,
        buy_tx_hash: Optional[str] = None,
        sell_tx_hash: Optional[str] = None,
    ) -> None:
        await self._record_snapshot(
            opp_id=opp_id,
            event_type="executed",
            pipeline=pipeline,
            direction=direction,
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            status="completed",
            optimal_size_usd=optimal_size_usd,
            routed_size_usd=routed_size_usd,
            executed_size_usd=executed_size_usd,
            expected_profit_usd=expected_profit_usd,
            actual_profit_usd=actual_profit_usd,
            net_profit_usd=net_profit_usd,
            net_spread_bps=net_spread_bps,
            reason=reason,
            buy_tx_hash=buy_tx_hash,
            sell_tx_hash=sell_tx_hash,
        )

    async def record_failed_snapshot(
        self,
        *,
        opp_id: str,
        pipeline: str,
        direction: str,
        buy_venue: str,
        sell_venue: str,
        status: str,
        optimal_size_usd: Any = None,
        routed_size_usd: Any = None,
        executed_size_usd: Any = None,
        expected_profit_usd: Any = None,
        actual_profit_usd: Any = None,
        net_profit_usd: Any = None,
        net_spread_bps: Optional[int] = None,
        reason: Optional[str] = None,
        buy_tx_hash: Optional[str] = None,
        sell_tx_hash: Optional[str] = None,
    ) -> None:
        await self._record_snapshot(
            opp_id=opp_id,
            event_type="failed",
            pipeline=pipeline,
            direction=direction,
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            status=status,
            optimal_size_usd=optimal_size_usd,
            routed_size_usd=routed_size_usd,
            executed_size_usd=executed_size_usd,
            expected_profit_usd=expected_profit_usd,
            actual_profit_usd=actual_profit_usd,
            net_profit_usd=net_profit_usd,
            net_spread_bps=net_spread_bps,
            reason=reason,
            buy_tx_hash=buy_tx_hash,
            sell_tx_hash=sell_tx_hash,
        )
