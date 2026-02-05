"""Blockradar wallet system adapter for B2C swap rates."""

import time
from decimal import Decimal
from typing import Optional

import httpx
import structlog

from engine.api.schemas import Position, PriceQuote, WalletParams
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


class BlockradarAdapter(VenueAdapter):
    """
    Blockradar wallet system adapter for B2C swap rates.

    Manages swap rates for CNGN pairs on the Blockradar platform.
    """

    name = "blockradar"

    def __init__(
        self,
        api_key: str,
        params: WalletParams | None = None,
    ):
        """
        Initialize Blockradar adapter.

        Args:
            api_key: Blockradar API key
            params: Rate setting parameters
        """
        self.api_key = api_key
        self.params = params or WalletParams()
        self.base_url = "https://api.blockradar.co/v1"
        self._client: Optional[httpx.AsyncClient] = None
        self.enabled = True
        self.paused = False

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with auth headers."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
        return self._client

    async def close(self):
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_position(self) -> Position:
        """
        Get current position on Blockradar.

        Note: Blockradar is a rate-setting service, not a custody venue,
        so balances are typically managed separately.
        """
        # Blockradar doesn't hold funds - return empty position
        return Position(
            venue=self.name,
            pair="CNGN/USDT",
            timestamp=int(time.time() * 1000),
            balances={
                "cngn": Decimal("0"),
                "usdt": Decimal("0"),
                "usdc": Decimal("0"),
            },
        )

    async def get_current_price(self) -> Optional[PriceQuote]:
        """Get current swap rate."""
        try:
            rate = await self.get_rate("CNGN/USDT")
            return PriceQuote(
                source="blockradar",
                timestamp=int(time.time() * 1000),
                bid=rate["buy"],
                ask=rate["sell"],
                mid=(rate["buy"] + rate["sell"]) / 2,
            )
        except Exception:
            return None

    async def get_rate(self, pair: str) -> dict:
        """
        Get current swap rate for a pair.

        Args:
            pair: Trading pair (e.g., "CNGN/USDT")

        Returns:
            Dict with "buy" and "sell" rates
        """
        client = await self._get_client()

        response = await client.get(
            f"{self.base_url}/swap/rates",
            params={"pair": pair},
        )
        response.raise_for_status()
        data = response.json()

        return {
            "buy": Decimal(str(data.get("buy_rate", "0"))),
            "sell": Decimal(str(data.get("sell_rate", "0"))),
        }

    async def set_rate(
        self,
        pair: str,
        buy_rate: Decimal,
        sell_rate: Decimal,
    ) -> bool:
        """
        Set swap rate for a pair.

        Args:
            pair: Trading pair (e.g., "CNGN/USDT")
            buy_rate: Rate at which users can buy CNGN
            sell_rate: Rate at which users can sell CNGN

        Returns:
            True if successful
        """
        if self.paused:
            logger.info("blockradar_paused_skipping_rate_update")
            return False

        client = await self._get_client()

        try:
            response = await client.post(
                f"{self.base_url}/swap/rates",
                json={
                    "pair": pair,
                    "buy_rate": str(buy_rate),
                    "sell_rate": str(sell_rate),
                },
            )

            success = response.status_code == 200

            logger.info(
                "rate_updated",
                pair=pair,
                buy=float(buy_rate),
                sell=float(sell_rate),
                success=success,
            )

            return success

        except Exception as e:
            logger.error("set_rate_failed", pair=pair, error=str(e))
            return False

    async def sync_rates(self, reference_price: Decimal) -> None:
        """
        Sync Blockradar rates based on reference price and spread.

        Args:
            reference_price: Mid-market USDT/NGN price
        """
        if self.paused:
            logger.info("blockradar_paused_skipping_sync")
            return

        spread_decimal = Decimal(str(self.params.spread_bps)) / Decimal("10000")

        buy_rate = reference_price * (1 - spread_decimal)
        sell_rate = reference_price * (1 + spread_decimal)

        # Set rates for both USDT and USDC pairs
        await self.set_rate("CNGN/USDT", buy_rate, sell_rate)
        await self.set_rate("CNGN/USDC", buy_rate, sell_rate)

        logger.info(
            "rates_synced",
            reference=float(reference_price),
            spread_bps=self.params.spread_bps,
        )
