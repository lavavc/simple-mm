"""Shared dependency container for scheduler jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from engine.db.backend import (
    ActionStoreProtocol,
    AlertStoreProtocol,
    PositionStoreProtocol,
    PriceStoreProtocol,
    SystemStateStoreProtocol,
    VenueConfigStoreProtocol,
)
from engine.market.price_aggregation import BlendedPriceCalculator
from engine.market.venue_prices import VenuePriceAggregator
from engine.venues.base import VenueAdapter

from engine.scheduler.config import SchedulerConfig
from engine.scheduler.types import TokenContracts

if TYPE_CHECKING:
    from engine.accounts import AccountManager
    from engine.arb.engine import ArbitrageEngine


@dataclass
class SchedulerContext:
    config: SchedulerConfig
    price_aggregator: VenuePriceAggregator
    venues: dict[str, VenueAdapter]
    broadcast: Callable[[dict[str, Any]], Any]
    blended_calculator: BlendedPriceCalculator | None
    arbitrage_engine: "ArbitrageEngine | None"
    account_manager: "AccountManager | None"
    token_contracts: TokenContracts
    quidax_lp: Any | None
    system_state_store: SystemStateStoreProtocol
    price_store: PriceStoreProtocol
    position_store: PositionStoreProtocol
    alert_store: AlertStoreProtocol
    venue_config_store: VenueConfigStoreProtocol
    action_store: ActionStoreProtocol
