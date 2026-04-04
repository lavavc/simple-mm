"""Narrow database store protocols used across the engine."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from engine.api.schemas import ArbitrageHistoryEvent, ArbitrageOpportunity, DexArbOpportunity, Position, PriceQuote


@runtime_checkable
class SystemStateStoreProtocol(Protocol):
    async def get_system_state(self, key: str) -> str | None: ...
    async def set_system_state(self, key: str, value: Any) -> None: ...


@runtime_checkable
class PriceStoreProtocol(Protocol):
    async def insert_price_snapshot(self, quote: PriceQuote, metadata: dict[str, Any] | None = None) -> None: ...
    async def get_recent_prices(self, limit: int = 100) -> list[Any]: ...
    async def get_recent_prices_for_source(self, source: str, limit: int = 100) -> list[Any]: ...
    async def get_price_history(
        self,
        from_ts: int | None = None,
        to_ts: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class PriceHistoryStoreProtocol(Protocol):
    async def get_price_snapshots_in_window(
        self,
        from_ts: int,
        to_ts: int,
        source: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class PositionStoreProtocol(Protocol):
    async def insert_position(self, position: Position) -> None: ...


@runtime_checkable
class ActionStoreProtocol(Protocol):
    async def insert_action(self, **kwargs: Any) -> int | None: ...
    async def get_actions(
        self,
        venue: str | None = None,
        action_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class AlertStoreProtocol(Protocol):
    async def insert_alert(self, **kwargs: Any) -> int | None: ...
    async def get_alerts(self, limit: int = 20) -> list[Any]: ...
    async def acknowledge_alert(self, alert_id: int) -> None: ...


@runtime_checkable
class VenueConfigStoreProtocol(Protocol):
    async def get_venue_config(self, venue: str) -> dict[str, Any] | None: ...
    async def update_venue_config(self, venue: str, params: dict[str, Any]) -> None: ...


@runtime_checkable
class ArbitrageStoreProtocol(Protocol):
    async def insert_arbitrage_opportunity(self, opp: ArbitrageOpportunity) -> None: ...
    async def update_arbitrage_opportunity(self, opp_id: str, **kwargs: Any) -> None: ...
    async def get_arbitrage_opportunities(
        self,
        status: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        limit: int = 50,
    ) -> list[ArbitrageOpportunity]: ...
    async def get_arbitrage_opportunity(self, opp_id: str) -> ArbitrageOpportunity | None: ...
    async def insert_dex_arbitrage_opportunity(self, opp: DexArbOpportunity) -> None: ...
    async def update_dex_arbitrage_execution_state(self, opp_id: str, **kwargs: Any) -> None: ...
    async def expire_old_dex_arbitrage_opportunities(self, cutoff_ts: int) -> None: ...
    async def get_dex_arbitrage_opportunities(
        self,
        status: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        limit: int = 50,
    ) -> list[DexArbOpportunity]: ...
    async def get_dex_arbitrage_opportunity(self, opp_id: str) -> DexArbOpportunity | None: ...
    async def get_active_dex_opportunity(self, direction: str) -> str | None: ...
    async def get_arbitrage_stats(self, from_ts: int) -> dict[str, Any]: ...


@runtime_checkable
class HistoryStoreProtocol(Protocol):
    async def upsert_arbitrage_history_event(self, event: ArbitrageHistoryEvent) -> None: ...
    async def get_arbitrage_history(
        self,
        pipeline: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        limit: int = 50,
    ) -> list[Any]: ...


@runtime_checkable
class PoolMetricsStoreProtocol(Protocol):
    async def get_pool_metrics_history(self, venues: list[str], from_ts: int) -> list[dict[str, Any]]: ...
