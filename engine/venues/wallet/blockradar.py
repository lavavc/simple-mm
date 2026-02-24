"""Blockradar wallet system adapter for B2C swap rates and quotes."""

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

import httpx
import structlog

from engine.api.schemas import Position, PriceQuote
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


# =============================================================================
# Route definitions for multi-rate fetching
# =============================================================================

@dataclass
class BlockradarRoute:
    key: str        # e.g. "cngn_usdc"
    currency: str   # API query param
    assets: str     # API query param
    outer_key: str  # first key in response data dict
    inner_key: str  # second key in response data dict
    invert: bool    # True → cNGN/USD = 1 / raw_rate


_ROUTES: list[BlockradarRoute] = [
    BlockradarRoute("cngn_usdc", "cNGN", "USDC", "USDC", "CNGN", False),
    BlockradarRoute("cngn_usdt", "cNGN", "USDT", "USDT", "CNGN", False),
    BlockradarRoute("usdt_cngn", "USDT", "cNGN", "cNGN", "USDT", True),
    BlockradarRoute("usdc_cngn", "USDC", "cNGN", "cNGN", "USDC", True),
]

_MIN_AMOUNTS: dict[str, str] = {
    "cngn_usdc": "100",
    "cngn_usdt": "100",
    "usdt_cngn": "1",
    "usdc_cngn": "1",
}


# =============================================================================
# Asset IDs (Blockradar-specific identifiers)
# =============================================================================

class BlockradarAsset(str, Enum):
    """Blockradar asset identifiers."""
    USDC = "fef8958b-0aba-4b8b-96f5-36c46a3a5e59"
    CNGN = "984e7fcc-67a9-4102-9e94-78207dc520f7"


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

    def __init__(self, api_key: str, wallet_id: str = ""):
        self.api_key = api_key
        self.wallet_id = wallet_id
        self.base_url = "https://api.blockradar.co/v1"
        self._client: Optional[httpx.AsyncClient] = None
        self.enabled = True
        self.paused = False
        self._current_rates_usd: dict[str, Decimal] = {}  # per-route, normalised to cNGN/USD
        self._current_rates_raw: dict[str, Decimal] = {}  # per-route, as returned by API
        self._rate_ids: dict[str, str] = {}  # route.key → Blockradar rate ID

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
        """Return empty position with per-route rate cache included."""
        return Position(
            venue=self.name,
            pair="cNGN/*",
            timestamp=int(time.time() * 1000),
            balances={"cngn": Decimal("0"), "usdc": Decimal("0")},
            rates=dict(self._current_rates_usd) or None,
        )

    async def get_assets(self) -> list[dict]:
        """Get assets available in the master wallet."""
        client = await self._get_client()
        response = await client.get(f"{self.base_url}/assets")
        response.raise_for_status()
        data = response.json()
        return data.get("data", [])

    async def get_current_price(self) -> Optional[PriceQuote]:
        """Fetch all 4 rate routes in parallel and return blended mid."""
        client = await self._get_client()

        async def _fetch(route: BlockradarRoute) -> Optional[Decimal]:
            try:
                resp = await client.get(
                    f"{self.base_url}/assets/rates",
                    params={"currency": route.currency, "assets": route.assets},
                )
                resp.raise_for_status()
                raw = Decimal(resp.json()["data"][route.outer_key][route.inner_key])
                self._current_rates_raw[route.key] = raw
                usd = (Decimal("1") / raw) if route.invert else raw
                self._current_rates_usd[route.key] = usd
                return usd
            except Exception as e:
                logger.warning("blockradar_rate_fetch_failed", route=route.key, error=str(e))
                return None

        results = await asyncio.gather(*[_fetch(r) for r in _ROUTES])
        valid = [r for r in results if r]
        if not valid:
            return None
        mid = sum(valid) / len(valid)
        return PriceQuote(
            source="blockradar",
            timestamp=int(time.time() * 1000),
            bid=mid, ask=mid, mid=mid,
        )

    async def set_rate(self, route: BlockradarRoute, rate_raw: Decimal) -> bool:
        """Create or update the exchange rate for a route on Blockradar."""
        if not self.api_key:
            return False
        client = await self._get_client()
        rate_str = str(rate_raw.quantize(Decimal("0.000001")))
        try:
            rate_id = self._rate_ids.get(route.key)
            if rate_id:
                resp = await client.patch(
                    f"{self.base_url}/rates/{rate_id}",
                    json={"rate": rate_str},
                )
            else:
                resp = await client.post(
                    f"{self.base_url}/rates",
                    json={
                        "fromAsset": route.currency,
                        "toAsset": route.assets,
                        "rate": rate_str,
                        "minAmount": _MIN_AMOUNTS[route.key],
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data", body)
                new_id = data.get("id")
                if new_id:
                    self._rate_ids[route.key] = str(new_id)
                    logger.info("blockradar_rate_created", route=route.key, id=new_id, rate=rate_str)
                return True

            resp.raise_for_status()
            logger.info("blockradar_rate_updated", route=route.key, rate=rate_str)
            return True
        except Exception as e:
            logger.error("blockradar_set_rate_failed", route=route.key, error=str(e))
            return False

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

        if response.status_code != 200:
            body = response.text
            # 400 "No swap quotes available" means wallet has no liquidity — not an error
            if response.status_code == 400 and "No swap quotes available" in body:
                logger.debug("blockradar_no_liquidity", body=body)
            else:
                logger.error("blockradar_quote_error", status=response.status_code, body=body)
            response.raise_for_status()

        raw = response.json()
        logger.info("blockradar_quote_raw_response", raw=raw)

        # Unwrap {"data": {...}} envelope if present
        data = raw.get("data", raw) if isinstance(raw, dict) else raw

        # API response fields: amount, rate, networkFee, slippage, etc.
        to_amount = Decimal(str(data.get("amount", "0")))
        rate = Decimal(str(data.get("rate", "0")))
        fee = Decimal(str(data.get("networkFee", "0")))

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
        return await self.get_swap_quote(
            from_asset=BlockradarAsset.USDC,
            to_asset=BlockradarAsset.CNGN,
            amount=amount,
            order_type=order_type,
        )

    async def get_cngn_to_usdc_quote(
        self,
        amount: Decimal,
        order_type: SwapOrderType = SwapOrderType.NO_SLIPPAGE,
    ) -> SwapQuote:
        """
        Get quote for swapping cNGN to USDC.

        Args:
            amount: Amount of cNGN to swap
            order_type: Quote optimization preference

        Returns:
            SwapQuote
        """
        return await self.get_swap_quote(
            from_asset=BlockradarAsset.CNGN,
            to_asset=BlockradarAsset.USDC,
            amount=amount,
            order_type=order_type,
        )

