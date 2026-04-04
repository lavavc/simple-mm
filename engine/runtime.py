"""Shared runtime container for long-lived engine services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from engine.db.repository import DatabaseRepository

if TYPE_CHECKING:
    from engine.accounts import AccountManager
    from engine.arb import ArbitrageEngine
    from engine.market.price_aggregation import BlendedPriceCalculator, PriceNormalizer
    from engine.market.venue_prices import VenuePriceAggregator
    from engine.scheduler import TradingScheduler
    from engine.venues.base import VenueAdapter


@dataclass(slots=True)
class EngineRuntime:
    db: DatabaseRepository
    scheduler: "TradingScheduler"
    venues: dict[str, "VenueAdapter"]
    price_aggregator: "VenuePriceAggregator | None"
    start_time: float
    arbitrage_engine: "ArbitrageEngine | None"
    account_manager: "AccountManager | None"
    token_contracts: dict[int, dict[str, str]]
    blended_calculator: "BlendedPriceCalculator | None"
    normalizer: "PriceNormalizer | None"
    quidax_lp: Any | None
