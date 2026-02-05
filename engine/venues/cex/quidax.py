"""Quidax CEX adapter for order ladder management."""

import time
from decimal import Decimal
from typing import Optional, Protocol

import httpx
import structlog

from engine.api.schemas import Position, PriceQuote, CexParams
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


class QuidaxHttpClient(Protocol):
    """Protocol for Quidax HTTP client (allows mocking)."""

    async def get(self, url: str, **kwargs) -> httpx.Response: ...
    async def post(self, url: str, **kwargs) -> httpx.Response: ...


class QuidaxAdapter(VenueAdapter):
    """
    Quidax CEX adapter for order ladder management.

    Manages limit orders across a price ladder to provide liquidity
    on the CNGN/USDT market.
    """

    name = "quidax"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        params: CexParams | None = None,
        market: str = "cngnusdt",
        base_url: str | None = None,
        http_client: QuidaxHttpClient | None = None,
    ):
        """
        Initialize Quidax adapter.

        Args:
            api_key: Quidax API key
            api_secret: Quidax API secret
            params: Order ladder parameters
            market: Trading pair (lowercase, no underscore, e.g., "cngnusdt")
            base_url: Override base URL (useful for testing)
            http_client: Override HTTP client (useful for mocking)
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.params = params or CexParams()
        self.market = market
        self.base_url = base_url or "https://app.quidax.io/api/v1"
        self._client: Optional[httpx.AsyncClient] = None
        self._mock_client: Optional[QuidaxHttpClient] = http_client
        self.enabled = True
        self.paused = False

    async def _get_client(self) -> QuidaxHttpClient:
        """Get or create HTTP client with auth headers."""
        if self._mock_client is not None:
            return self._mock_client
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
        """Get current balances on Quidax."""
        client = await self._get_client()

        response = await client.get(f"{self.base_url}/users/me/wallets")
        response.raise_for_status()
        data = response.json()

        balances = {
            "cngn": Decimal("0"),
            "usdt": Decimal("0"),
            "usdc": Decimal("0"),
        }

        for wallet in data.get("data", []):
            currency = wallet["currency"].lower()
            if currency in balances:
                balances[currency] = Decimal(str(wallet.get("balance", "0")))

        # Get open orders
        orders = await self.get_open_orders()
        buy_orders = [o for o in orders if o.get("side") == "buy"]
        sell_orders = [o for o in orders if o.get("side") == "sell"]

        return Position(
            venue=self.name,
            pair="CNGN/USDT",
            timestamp=int(time.time() * 1000),
            balances=balances,
            open_orders={
                "buy_count": len(buy_orders),
                "sell_count": len(sell_orders),
                "buy_volume": sum(
                    Decimal(str(o.get("remaining_volume", "0"))) for o in buy_orders
                ),
                "sell_volume": sum(
                    Decimal(str(o.get("remaining_volume", "0"))) for o in sell_orders
                ),
            },
        )

    async def get_current_price(self) -> Optional[PriceQuote]:
        """Quidax doesn't provide a direct price feed, return None."""
        return None

    async def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        client = await self._get_client()

        response = await client.get(
            f"{self.base_url}/users/me/orders",
            params={"market": self.market, "state": "wait"},
        )
        response.raise_for_status()
        return response.json().get("data", [])

    async def cancel_all_orders(self) -> int:
        """
        Cancel all open orders.

        Returns:
            Number of orders cancelled
        """
        orders = await self.get_open_orders()
        client = await self._get_client()

        cancelled = 0
        for order in orders:
            try:
                response = await client.post(
                    f"{self.base_url}/users/me/orders/{order['id']}/cancel"
                )
                if response.status_code == 200:
                    cancelled += 1
            except Exception as e:
                logger.warning("cancel_order_failed", order_id=order["id"], error=str(e))

        logger.info("cancelled_orders", count=cancelled, total=len(orders))
        return cancelled

    async def place_order(
        self,
        side: str,
        price: Decimal,
        amount: Decimal,
    ) -> dict:
        """
        Place a limit order.

        Args:
            side: "buy" or "sell"
            price: Order price in USDT
            amount: Order amount in CNGN

        Returns:
            Order result from API
        """
        client = await self._get_client()

        response = await client.post(
            f"{self.base_url}/users/me/orders",
            json={
                "market": self.market,
                "side": side,
                "ord_type": "limit",
                "price": str(price),
                "volume": str(amount),
            },
        )

        result = response.json()

        logger.debug(
            "order_placed",
            side=side,
            price=float(price),
            amount=float(amount),
            success=bool(result.get("data")),
        )

        return result

    async def sync_order_ladder(self, reference_price: Decimal) -> None:
        """
        Sync order ladder to current reference price.

        Creates buy and sell orders at incremental price levels.

        Args:
            reference_price: Mid-market price to center ladder around
        """
        if self.paused:
            logger.info("quidax_paused_skipping_sync")
            return

        # Cancel existing orders
        await self.cancel_all_orders()

        # Get current balances
        position = await self.get_position()
        total_cngn = position.balances["cngn"]
        total_usdt = position.balances["usdt"]

        liquidity_per_level = Decimal(str(self.params.liquidity_per_level_percent)) / 100
        increment = self.params.ladder_increment_ngn

        orders_placed = 0

        # Build sell ladder (selling CNGN for USDT)
        for i in range(self.params.ladder_levels):
            price_offset = Decimal(str(i + 1)) * increment
            price = reference_price + price_offset
            amount = total_cngn * liquidity_per_level

            if amount > 0:
                try:
                    await self.place_order("sell", price, amount)
                    orders_placed += 1
                except Exception as e:
                    logger.warning("place_sell_order_failed", level=i, error=str(e))

        # Build buy ladder (buying CNGN with USDT)
        for i in range(self.params.ladder_levels):
            price_offset = Decimal(str(i + 1)) * increment
            price = reference_price - price_offset
            usdt_amount = total_usdt * liquidity_per_level
            cngn_amount = usdt_amount / price if price > 0 else Decimal("0")

            if cngn_amount > 0:
                try:
                    await self.place_order("buy", price, cngn_amount)
                    orders_placed += 1
                except Exception as e:
                    logger.warning("place_buy_order_failed", level=i, error=str(e))

        logger.info(
            "order_ladder_synced",
            reference_price=float(reference_price),
            levels=self.params.ladder_levels,
            orders_placed=orders_placed,
        )

    async def handle_webhook(self, event: dict) -> None:
        """
        Handle Quidax webhook for order fills.

        Args:
            event: Webhook event data
        """
        event_type = event.get("event")

        if event_type == "order.filled":
            order = event.get("data", {})
            logger.info(
                "order_filled",
                order_id=order.get("id"),
                side=order.get("side"),
                price=order.get("price"),
                volume=order.get("executed_volume"),
            )
