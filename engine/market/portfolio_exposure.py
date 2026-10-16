"""Read-only portfolio accounting shared by HTTP and WebSocket reporting."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Sequence

from engine.config import settings
from engine.market.portfolio_registry import PortfolioSourceDescriptor
from engine.venues.base import TradeInventoryVenue

if TYPE_CHECKING:
    from engine.accounts import AccountManager
    from engine.market.price_aggregation import BlendedPriceCalculator
    from engine.market.venue_prices import VenuePriceAggregator
    from engine.venues.base import VenueAdapter


@dataclass(frozen=True, slots=True)
class PortfolioExposureSourceSnapshot:
    source: str
    kind: Literal["account", "lp_position", "exchange"]
    balances: dict[str, Decimal | None]
    usd_value: Decimal | None
    error: str | None


@dataclass(frozen=True, slots=True)
class PortfolioExposureSnapshot:
    timestamp: int
    complete: bool
    total_cngn: Decimal | None
    total_usdt: Decimal | None
    total_usdc: Decimal | None
    total_usd_value: Decimal | None
    cngn_share: Decimal | None
    sources: tuple[PortfolioExposureSourceSnapshot, ...]


class PortfolioExposureCalculator:
    def __init__(
        self,
        venues: dict[str, "VenueAdapter"],
        account_manager: "AccountManager | None",
        token_contracts: dict[int, dict[str, str]],
        blended_calculator: "BlendedPriceCalculator | None",
        price_aggregator: "VenuePriceAggregator | None",
        portfolio_source_registry: Sequence[PortfolioSourceDescriptor],
        lp_managers: dict[str, Any] | None = None,
    ) -> None:
        self.venues = venues
        self.account_manager = account_manager
        self.token_contracts = token_contracts
        self.blended_calculator = blended_calculator
        self.price_aggregator = price_aggregator
        self.portfolio_source_registry = tuple(portfolio_source_registry)
        self.lp_managers = lp_managers or {}
        if len({s.venue for s in portfolio_source_registry}) != len(portfolio_source_registry):
            raise ValueError("Duplicate portfolio source")
        self._snapshot: PortfolioExposureSnapshot | None = None
        self._refreshed_at = 0.0
        self._lock = asyncio.Lock()

    async def calculate(self) -> PortfolioExposureSnapshot:
        async with self._lock:
            if self._snapshot is None or time.monotonic() - self._refreshed_at >= settings.portfolio_refresh_interval:
                self._snapshot = await self._collect()
                self._refreshed_at = time.monotonic()
            return self._snapshot

    async def _collect(self) -> PortfolioExposureSnapshot:
        rate = await self._get_cngn_usd_rate()
        sources: list[PortfolioExposureSourceSnapshot] = []
        if self.account_manager is not None:
            expected = set(self.account_manager.list_accounts())
            try:
                wallets = await self.account_manager.check_all_balances(self.token_contracts)
            except Exception:
                wallets = []
            for wallet in wallets:
                expected.discard(wallet.role)
                sources.append(self._source(wallet.role, "account", wallet.token_balances, rate))
            for role in sorted(expected):
                sources.append(self._source(role, "account", None, rate, "Wallet read failed"))

        for descriptor in self.portfolio_source_registry:
            venue = self.venues.get(descriptor.venue)
            if venue is None:
                continue
            try:
                if descriptor.source_kind == "lp_position":
                    balances = self.lp_managers[descriptor.venue].get_portfolio_balances()
                else:
                    if not isinstance(venue, TradeInventoryVenue):
                        raise TypeError("Exchange does not expose total holdings")
                    exchange = await venue.get_trade_inventory()
                    balances = {"cngn": exchange.cngn_total, "usdt": exchange.stable_total, "usdc": Decimal(0)}
                sources.append(self._source(descriptor.venue, descriptor.source_kind, balances, rate))
            except Exception:
                sources.append(self._source(descriptor.venue, descriptor.source_kind, None, rate, "Holdings read failed"))

        def total(symbol: str) -> Decimal | None:
            values = [source.balances[symbol] for source in sources]
            return None if any(value is None for value in values) else sum(
                (value for value in values if value is not None), Decimal(0),
            )

        complete = all(source.usd_value is not None for source in sources)
        usd_value = sum((s.usd_value for s in sources if s.usd_value is not None), Decimal(0)) if complete else None
        cngn = total("cngn")
        share = None
        if usd_value is not None and cngn is not None:
            if usd_value == 0 or cngn == 0:
                share = Decimal(0)
            elif rate is not None:
                share = cngn * rate / usd_value
        return PortfolioExposureSnapshot(
            timestamp=int(time.time() * 1000), complete=complete,
            total_cngn=cngn, total_usdt=total("usdt"), total_usdc=total("usdc"),
            total_usd_value=usd_value, cngn_share=share, sources=tuple(sources),
        )

    @staticmethod
    def _source(
        name: str, kind: Literal["account", "lp_position", "exchange"],
        raw: dict[str, Any] | None, rate: Decimal | None, error: str | None = None,
    ) -> PortfolioExposureSourceSnapshot:
        balances: dict[str, Decimal | None] = {symbol: Decimal(0) for symbol in ("cngn", "usdt", "usdc")}
        if raw is None:
            balances = dict.fromkeys(balances)
            error = error or "Holdings unavailable"
        else:
            for token, value in raw.items():
                symbol = token.lower()
                if symbol not in balances:
                    continue
                amount = Decimal(str(value))
                if not amount.is_finite() or amount < 0:
                    balances[symbol] = None
                    error = "Balance read failed"
                else:
                    current = balances[symbol]
                    if current is not None:
                        balances[symbol] = current + amount
        cngn, usdt, usdc = (balances[s] for s in ("cngn", "usdt", "usdc"))
        value = None
        if cngn is not None and usdt is not None and usdc is not None:
            if cngn == 0:
                value = usdt + usdc
            elif rate is not None:
                value = usdt + usdc + cngn * rate
            else:
                error = "cNGN price unavailable"
        return PortfolioExposureSourceSnapshot(name, kind, balances, value, error)

    async def _get_cngn_usd_rate(self) -> Decimal | None:
        if self.blended_calculator is not None:
            try:
                blended = await self.blended_calculator.get_blended_price()
                for candidate in (blended.vwap, blended.twap_5m, blended.twap_1h):
                    if candidate.is_finite() and candidate > 0:
                        return candidate
            except Exception:
                pass
        if self.price_aggregator is not None:
            for venue in ("quidax", "bybit"):
                price = self.price_aggregator.get_price(venue)
                if price and price.quote and price.quote.mid.is_finite() and price.quote.mid > 0:
                    return Decimal(1) / price.quote.mid if venue == "bybit" else price.quote.mid
        return None
