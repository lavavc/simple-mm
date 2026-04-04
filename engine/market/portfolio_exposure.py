"""Portfolio exposure aggregation built on top of fair-value pricing."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from engine.api.schemas import GlobalPosition, PortfolioExposure, PortfolioExposureSource
from engine.config import settings
from engine.venues.cex.quidax import QuidaxAdapter
from engine.venues.dex.lp_v4 import V4LPAdapter

if TYPE_CHECKING:
    from engine.accounts import AccountManager
    from engine.market.price_aggregation import BlendedPriceCalculator
    from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


class PortfolioExposureCalculator:
    """Aggregate all managed balances into one fair-value portfolio snapshot."""

    def __init__(
        self,
        venues: dict[str, "VenueAdapter"],
        account_manager: "AccountManager | None",
        token_contracts: dict[int, dict[str, str]],
        blended_calculator: "BlendedPriceCalculator | None",
    ) -> None:
        self.venues = venues
        self.account_manager = account_manager
        self.token_contracts = token_contracts
        self.blended_calculator = blended_calculator

    async def calculate(self) -> PortfolioExposure:
        """Return the current portfolio exposure with per-source breakdown."""
        cngn_usd_rate = await self._get_cngn_usd_rate()
        total_balances = {"cngn": Decimal("0"), "usdt": Decimal("0"), "usdc": Decimal("0")}
        sources: list[PortfolioExposureSource] = []

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
                    PortfolioExposureSource(
                        source=getattr(account_balance, "role", "unknown-account"),
                        kind="account",
                        balances=normalized,
                        usd_value=self._balances_to_usd_value(normalized, cngn_usd_rate),
                    )
                )

        for venue_name, venue in self.venues.items():
            if not self._include_venue_position(venue_name, venue):
                continue

            try:
                position = await venue.get_position()
            except Exception as exc:
                logger.warning("portfolio_venue_position_failed", venue=venue_name, error=str(exc))
                continue

            normalized = self._normalize_balances(getattr(position, "balances", {}))
            if not self._has_meaningful_balance(normalized):
                continue
            self._accumulate(total_balances, normalized)
            sources.append(
                PortfolioExposureSource(
                    source=venue_name,
                    kind=self._source_kind_for_venue(venue_name, venue),
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

        return PortfolioExposure(
            total_cngn=total_cngn,
            total_usdt=total_usdt,
            total_usdc=total_usdc,
            total_usd_value=total_usd_value,
            delta_ratio=delta_ratio,
            target_delta=Decimal(str(settings.target_delta_ratio)),
            sources=sources,
        )

    async def calculate_global_position(self) -> GlobalPosition:
        """Return the legacy aggregate shape without source breakdowns."""
        exposure = await self.calculate()
        return GlobalPosition(
            total_cngn=exposure.total_cngn,
            total_usdt=exposure.total_usdt,
            total_usdc=exposure.total_usdc,
            total_usd_value=exposure.total_usd_value,
            delta_ratio=exposure.delta_ratio,
            target_delta=exposure.target_delta,
        )

    async def _get_cngn_usd_rate(self) -> Decimal:
        if self.blended_calculator is None:
            return Decimal("0")
        try:
            blended = await self.blended_calculator.get_blended_price()
        except Exception as exc:
            logger.warning("portfolio_blended_price_unavailable", error=str(exc))
            return Decimal("0")

        for candidate in (blended.vwap, blended.twap_5m, blended.twap_1h):
            if candidate > 0:
                return candidate
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

    def _include_venue_position(self, venue_name: str, venue: Any) -> bool:
        if isinstance(venue, V4LPAdapter):
            return True
        if isinstance(venue, QuidaxAdapter):
            return True
        return venue_name in {"quidax", "quidax-lp"}

    def _source_kind_for_venue(self, venue_name: str, venue: Any) -> str:
        if isinstance(venue, V4LPAdapter):
            return "venue_position"
        if isinstance(venue, QuidaxAdapter):
            return "exchange"
        if venue_name in {"quidax", "quidax-lp"}:
            return "exchange"
        return "venue_position"
