"""Portfolio exposure aggregation built on top of fair-value pricing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Sequence

import structlog

from engine.config import settings
from engine.market.portfolio_registry import PortfolioSourceDescriptor

if TYPE_CHECKING:
    from engine.accounts import AccountManager
    from engine.market.price_aggregation import BlendedPriceCalculator
    from engine.market.venue_prices import VenuePriceAggregator
    from engine.venues.base import VenueAdapter

logger = structlog.get_logger()

PortfolioExposureSourceKind = Literal["account", "lp_position", "exchange"]


@dataclass(frozen=True, slots=True)
class PortfolioExposureSourceSnapshot:
    """One contributing balance source in the global portfolio view."""

    source: str
    kind: PortfolioExposureSourceKind
    balances: dict[str, Decimal]
    usd_value: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioExposureSnapshot:
    """Expanded global portfolio snapshot with per-source breakdown."""

    total_cngn: Decimal
    total_usdt: Decimal
    total_usdc: Decimal
    total_usd_value: Decimal
    delta_ratio: Decimal
    target_delta: Decimal
    sources: tuple[PortfolioExposureSourceSnapshot, ...]


class PortfolioExposureCalculator:
    """Aggregate all managed balances into one fair-value portfolio snapshot."""

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
        self.lp_managers: dict[str, Any] = lp_managers or {}

    async def calculate(self) -> PortfolioExposureSnapshot:
        """Return the current portfolio exposure with per-source breakdown."""
        cngn_usd_rate = await self._get_cngn_usd_rate()
        total_balances = {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}
        sources: list[PortfolioExposureSourceSnapshot] = []

        if self.account_manager is not None:
            try:
                account_balances = await self.account_manager.check_all_balances(self.token_contracts)
            except Exception as exc:
                logger.warning("portfolio_account_balances_failed", error=str(exc))
                account_balances = []

            for account_balance in account_balances:
                normalized = self._normalize_balances(getattr(account_balance, "token_balances", {}))
                if not self._has_meaningful_balance(normalized):
                    continue
                self._accumulate(total_balances, normalized)
                sources.append(
                    PortfolioExposureSourceSnapshot(
                        source=getattr(account_balance, "role", "unknown-account"),
                        kind="account",
                        balances=normalized,
                        usd_value=self._balances_to_usd_value(normalized, cngn_usd_rate),
                    )
                )

        seen_registered_venues: set[str] = set()
        for descriptor in self.portfolio_source_registry:
            if descriptor.venue in seen_registered_venues:
                logger.warning("duplicate_portfolio_source_descriptor", venue=descriptor.venue)
                continue
            seen_registered_venues.add(descriptor.venue)

            venue = self.venues.get(descriptor.venue)
            if venue is None:
                continue

            raw_balances: dict[str, Any] | None
            if descriptor.source_kind == "lp_position":
                lp_manager = self.lp_managers.get(descriptor.venue)
                if lp_manager is None:
                    logger.warning(
                        "portfolio_lp_manager_missing",
                        venue=descriptor.venue,
                    )
                    continue
                try:
                    raw_balances = lp_manager.get_portfolio_balances()
                except Exception as exc:
                    logger.warning(
                        "portfolio_lp_balances_failed",
                        venue=descriptor.venue,
                        error=str(exc),
                    )
                    continue
            else:
                try:
                    position = await venue.get_position()
                except Exception as exc:
                    logger.warning("portfolio_venue_position_failed", venue=descriptor.venue, error=str(exc))
                    continue
                raw_balances = getattr(position, "balances", {})

            normalized = self._normalize_balances(raw_balances)
            if not self._has_meaningful_balance(normalized):
                continue
            self._accumulate(total_balances, normalized)
            sources.append(
                PortfolioExposureSourceSnapshot(
                    source=descriptor.venue,
                    kind=descriptor.source_kind,
                    balances=normalized,
                    usd_value=self._balances_to_usd_value(normalized, cngn_usd_rate),
                )
            )

        total_cngn = total_balances["cngn"]
        total_usdt = total_balances["usdt"]
        total_usdc = total_balances["usdc"]
        total_usd_value = total_usdt + total_usdc + (total_cngn * cngn_usd_rate)
        delta_ratio = (
            (total_cngn * cngn_usd_rate) / total_usd_value
            if total_usd_value > 0
            else Decimal("0")
        )

        return PortfolioExposureSnapshot(
            total_cngn=total_cngn,
            total_usdt=total_usdt,
            total_usdc=total_usdc,
            total_usd_value=total_usd_value,
            delta_ratio=delta_ratio,
            target_delta=Decimal(str(settings.target_delta_ratio)),
            sources=tuple(sources),
        )

    async def _get_cngn_usd_rate(self) -> Decimal:
        if self.blended_calculator is not None:
            try:
                blended = await self.blended_calculator.get_blended_price()
                for candidate in (blended.vwap, blended.twap_5m, blended.twap_1h):
                    if candidate > 0:
                        return candidate
            except Exception as exc:
                logger.warning("portfolio_blended_price_unavailable", error=str(exc))

        if self.price_aggregator is not None:
            quidax = self.price_aggregator.get_price("quidax")
            if quidax and quidax.quote and quidax.quote.mid > 0:
                return quidax.quote.mid

            bybit = self.price_aggregator.get_price("bybit")
            if bybit and bybit.quote and bybit.quote.mid > 0:
                return Decimal("1") / bybit.quote.mid

        return Decimal("0")

    def _normalize_balances(self, raw_balances: dict[str, Any] | None) -> dict[str, Decimal]:
        normalized = {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}
        if not raw_balances:
            return normalized

        for raw_symbol, raw_amount in raw_balances.items():
            symbol = str(raw_symbol).lower()
            if symbol not in normalized:
                continue
            amount = Decimal(str(raw_amount))
            if amount < 0:
                continue
            normalized[symbol] += amount
        return normalized

    def _balances_to_usd_value(self, balances: dict[str, Decimal], cngn_usd_rate: Decimal) -> Decimal:
        return balances["usdt"] + balances["usdc"] + (balances["cngn"] * cngn_usd_rate)

    def _accumulate(self, totals: dict[str, Decimal], balances: dict[str, Decimal]) -> None:
        for symbol, amount in balances.items():
            totals[symbol] += amount

    def _has_meaningful_balance(self, balances: dict[str, Decimal]) -> bool:
        return any(amount > 0 for amount in balances.values())
