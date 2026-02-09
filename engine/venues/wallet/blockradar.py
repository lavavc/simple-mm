"""Blockradar wallet system adapter for B2C swap rates and quotes."""

import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

import httpx
import structlog

from engine.api.schemas import Position, PriceQuote, WalletParams
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


# =============================================================================
# Asset IDs (Blockradar-specific identifiers)
# =============================================================================

class BlockradarAsset(str, Enum):
    """Blockradar asset identifiers."""
    USDC = "3a18a31a-86ad-44a0-9b9c-cdb69d535c64"
    USDT = "065ff109-9d31-4c7d-bd0e-13314d2ed5f6"
    CNGN = ""  # TODO: Get cNGN asset ID from Blockradar


class SwapOrderType(str, Enum):
    """Swap quote order types."""
    RECOMMENDED = "RECOMMENDED"
    FASTEST = "FASTEST"
    CHEAPEST = "CHEAPEST"
    NO_SLIPPAGE = "NO_SLIPPAGE"


# =============================================================================
# Data classes for API responses
# =============================================================================

@dataclass
class SwapQuote:
    """Response from Blockradar swap quote endpoint."""
    from_asset_id: str
    to_asset_id: str
    from_amount: Decimal
    to_amount: Decimal
    rate: Decimal  # Effective exchange rate
    order_type: str
    fee: Decimal
    expires_at: Optional[int] = None
    raw_response: Optional[dict] = None

    @property
    def spread_bps(self) -> int:
        """Calculate implied spread in basis points."""
        if self.rate <= 0:
            return 0
        # Compare to mid-market rate if available
        return 0  # TODO: Calculate once we have reference prices


# =============================================================================
# Blockradar Adapter
# =============================================================================

class BlockradarAdapter(VenueAdapter):
    """
    Blockradar wallet system adapter for B2C swap rates and quotes.

    Manages swap rates and retrieves quotes for CNGN pairs on the Blockradar platform.
    """

    name = "blockradar"

    def __init__(
        self,
        api_key: str,
        wallet_id: str = "",
        params: WalletParams | None = None,
    ):
        """
        Initialize Blockradar adapter.

        Args:
            api_key: Blockradar API key
            wallet_id: Blockradar wallet ID for swap operations
            params: Rate setting parameters
        """
        self.api_key = api_key
        self.wallet_id = wallet_id
        self.params = params or WalletParams()
        self.base_url = "https://api.blockradar.co/v1"
        self._client: Optional[httpx.AsyncClient] = None
        self.enabled = True
        self.paused = False

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with auth headers."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "x-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
        return self._client

    async def close(self):
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # -------------------------------------------------------------------------
    # Position / Price (VenueAdapter interface)
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # Swap Quotes
    # -------------------------------------------------------------------------

    async def get_swap_quote(
        self,
        from_asset: BlockradarAsset | str,
        to_asset: BlockradarAsset | str,
        amount: Decimal,
        order_type: SwapOrderType = SwapOrderType.RECOMMENDED,
    ) -> SwapQuote:
        """
        Get a swap quote from Blockradar.

        Args:
            from_asset: Asset to swap from (BlockradarAsset enum or asset ID string)
            to_asset: Asset to swap to (BlockradarAsset enum or asset ID string)
            amount: Amount of from_asset to swap
            order_type: Quote optimization preference (RECOMMENDED, FASTEST, CHEAPEST, NO_SLIPPAGE)

        Returns:
            SwapQuote with rate, amounts, and fees

        Raises:
            ValueError: If wallet_id is not configured
            httpx.HTTPError: If API request fails
        """
        if not self.wallet_id:
            raise ValueError("Blockradar wallet_id not configured")

        # Convert enum to string value if needed
        from_asset_id = from_asset.value if isinstance(from_asset, BlockradarAsset) else from_asset
        to_asset_id = to_asset.value if isinstance(to_asset, BlockradarAsset) else to_asset

        if not from_asset_id:
            raise ValueError(f"Invalid from_asset: {from_asset}")
        if not to_asset_id:
            raise ValueError(f"Invalid to_asset: {to_asset}")

        client = await self._get_client()

        url = f"{self.base_url}/wallets/{self.wallet_id}/swaps/quote"
        payload = {
            "amount": str(amount),
            "fromAssetId": from_asset_id,
            "toAssetId": to_asset_id,
            "order": order_type.value if isinstance(order_type, SwapOrderType) else order_type,
        }

        logger.debug(
            "blockradar_quote_request",
            url=url,
            from_asset=from_asset_id,
            to_asset=to_asset_id,
            amount=str(amount),
            order_type=order_type,
        )

        response = await client.post(url, json=payload)
        response.raise_for_status()

        data = response.json()

        logger.info(
            "blockradar_quote_received",
            from_asset=from_asset_id,
            to_asset=to_asset_id,
            amount=str(amount),
            response_keys=list(data.keys()) if isinstance(data, dict) else None,
        )

        # Parse response - structure may vary, adapt as needed
        # Expected fields: toAmount, rate, fee, etc.
        to_amount = Decimal(str(data.get("toAmount", data.get("to_amount", "0"))))
        rate = Decimal(str(data.get("rate", "0")))
        fee = Decimal(str(data.get("fee", data.get("fees", "0"))))

        # Calculate rate if not provided
        if rate == 0 and to_amount > 0 and amount > 0:
            rate = to_amount / amount

        return SwapQuote(
            from_asset_id=from_asset_id,
            to_asset_id=to_asset_id,
            from_amount=amount,
            to_amount=to_amount,
            rate=rate,
            order_type=order_type.value if isinstance(order_type, SwapOrderType) else order_type,
            fee=fee,
            expires_at=data.get("expiresAt", data.get("expires_at")),
            raw_response=data,
        )

    async def get_usdc_to_cngn_quote(
        self,
        amount: Decimal,
        order_type: SwapOrderType = SwapOrderType.RECOMMENDED,
    ) -> SwapQuote:
        """
        Get quote for swapping USDC to cNGN.

        Args:
            amount: Amount of USDC to swap
            order_type: Quote optimization preference

        Returns:
            SwapQuote
        """
        if not BlockradarAsset.CNGN.value:
            raise ValueError("cNGN asset ID not configured")
        return await self.get_swap_quote(
            from_asset=BlockradarAsset.USDC,
            to_asset=BlockradarAsset.CNGN,
            amount=amount,
            order_type=order_type,
        )

    async def get_cngn_to_usdc_quote(
        self,
        amount: Decimal,
        order_type: SwapOrderType = SwapOrderType.RECOMMENDED,
    ) -> SwapQuote:
        """
        Get quote for swapping cNGN to USDC.

        Args:
            amount: Amount of cNGN to swap
            order_type: Quote optimization preference

        Returns:
            SwapQuote
        """
        if not BlockradarAsset.CNGN.value:
            raise ValueError("cNGN asset ID not configured")
        return await self.get_swap_quote(
            from_asset=BlockradarAsset.CNGN,
            to_asset=BlockradarAsset.USDC,
            amount=amount,
            order_type=order_type,
        )

    async def get_usdt_to_cngn_quote(
        self,
        amount: Decimal,
        order_type: SwapOrderType = SwapOrderType.RECOMMENDED,
    ) -> SwapQuote:
        """
        Get quote for swapping USDT to cNGN.

        Args:
            amount: Amount of USDT to swap
            order_type: Quote optimization preference

        Returns:
            SwapQuote
        """
        if not BlockradarAsset.CNGN.value:
            raise ValueError("cNGN asset ID not configured")
        return await self.get_swap_quote(
            from_asset=BlockradarAsset.USDT,
            to_asset=BlockradarAsset.CNGN,
            amount=amount,
            order_type=order_type,
        )

    async def get_cngn_to_usdt_quote(
        self,
        amount: Decimal,
        order_type: SwapOrderType = SwapOrderType.RECOMMENDED,
    ) -> SwapQuote:
        """
        Get quote for swapping cNGN to USDT.

        Args:
            amount: Amount of cNGN to swap
            order_type: Quote optimization preference

        Returns:
            SwapQuote
        """
        if not BlockradarAsset.CNGN.value:
            raise ValueError("cNGN asset ID not configured")
        return await self.get_swap_quote(
            from_asset=BlockradarAsset.CNGN,
            to_asset=BlockradarAsset.USDT,
            amount=amount,
            order_type=order_type,
        )

    # -------------------------------------------------------------------------
    # Rate Management (existing functionality)
    # -------------------------------------------------------------------------

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
