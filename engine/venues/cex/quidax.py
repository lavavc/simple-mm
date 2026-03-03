"""Quidax CEX adapter for order ladder management."""

import asyncio
import time
from decimal import Decimal
from typing import Optional

import httpx
import structlog

from engine.api.schemas import Position, PriceQuote, CexParams
from engine.db import get_db
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


class QuidaxAdapter(VenueAdapter):
    """
    Quidax CEX adapter.

    Two independent roles:
    - Liquidity provision: sync_order_ladder() places limit orders across a price
      range to keep the CNGN/USDT book filled (market making).
    - Arb execution: place_market_order() hits the book immediately with a market
      order to capture a detected spread (taker, guaranteed fill).
    """

    def __init__(
        self,
        api_key: str,
        params: CexParams | None = None,
        market: str = "cngnusdt",
        base_url: str | None = None,
        name: str = "quidax",
        funding_role: str = "quidax-arb",
    ):
        """
        Initialize Quidax adapter.

        Args:
            api_key: Quidax secret key (used as Bearer token)
            params: Order ladder parameters
            market: Trading pair (lowercase, no underscore, e.g., "cngnusdt")
            base_url: Override base URL (useful for testing)
            name: Adapter name (used in logs and venue registry)
            funding_role: Account role for auto-funding ("quidax-arb" | "quidax-lp")
        """
        self.name = name
        self.api_key = api_key
        self.params = params or CexParams()
        self.market = market
        self.base_url = base_url or "https://app.quidax.io/api/v1"
        self._funding_role = funding_role
        self._client: Optional[httpx.AsyncClient] = None
        self._deposit_addresses: dict[str, str] = {}
        self._last_balances: dict[str, Decimal] = {}
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

    async def init_deposit_addresses(self, currencies: list[str]) -> None:
        """Load or request deposit addresses for each currency.

        Checks system_state for a cached address first; if missing, calls
        get_deposit_address() and stores any synchronous response. Addresses
        that arrive only via webhook are populated by handle_webhook().
        """
        db = await get_db()
        for currency in currencies:
            key = f"quidax_{self._funding_role}_deposit_{currency}"
            stored = await db.get_system_state(key)
            if stored:
                self._deposit_addresses[currency] = stored
                logger.info("quidax_deposit_address_loaded", role=self._funding_role, currency=currency)
            else:
                result = await self.get_deposit_address(currency)
                address = (result.get("data") or {}).get("address", "")
                if address:
                    self._deposit_addresses[currency] = address
                    await db.set_system_state(key, address)
                    logger.info("quidax_deposit_address_fetched", role=self._funding_role, currency=currency)
                else:
                    logger.info("quidax_deposit_address_pending_webhook", role=self._funding_role, currency=currency)

    async def get_position(self) -> Position:
        """Fetch live cNGN and USDT balances from the Quidax wallet API."""
        client = await self._get_client()
        balances: dict[str, Decimal] = {}
        for currency in ["cngn", "usdt"]:
            try:
                resp = await client.get(f"{self.base_url}/users/me/wallets/{currency}")
                resp.raise_for_status()
                balances[currency] = Decimal(str(resp.json().get("data", {}).get("balance", "0")))
            except Exception as e:
                logger.warning("quidax_balance_fetch_failed", currency=currency, error=str(e))
                balances[currency] = self._last_balances.get(currency, Decimal("0"))
        self._last_balances = balances
        return Position(
            venue=self.name,
            pair="CNGN/USDT",
            timestamp=int(time.time() * 1000),
            balances=balances,
        )

    async def get_current_price(self) -> Optional[PriceQuote]:
        """Fetch current cNGN/USDT price from the Quidax public market summary.

        Uses a plain client (no auth header) since this is a public endpoint.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.base_url}/markets/summary/",
                    headers={"accept": "application/json"},
                )
                response.raise_for_status()
                data = response.json()

            if data.get("status") != "success":
                return None

            pair_data = data.get("data", {}).get("CNGN_USDT")
            if not pair_data:
                return None

            bid = Decimal(str(pair_data.get("highest_bid", "0")))
            ask = Decimal(str(pair_data.get("lowest_ask", "0")))
            last = Decimal(str(pair_data.get("last_price", "0")))

            if bid == 0 and ask == 0:
                if last == 0:
                    return None
                bid = last
                ask = last

            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last

            return PriceQuote(
                source="quidax",
                timestamp=int(time.time() * 1000),
                bid=bid,
                ask=ask,
                mid=mid,
            )
        except Exception as e:
            logger.error("quidax_price_fetch_failed", error=str(e))
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

    async def place_market_order(
        self,
        side: str,
        amount: Decimal,
    ) -> tuple[bool, Decimal, Decimal, str | None]:
        """
        Place a market order with up to 5 retries on network/5xx errors.

        Args:
            side: "buy" or "sell"
            amount: Order amount in CNGN

        Returns:
            (success, executed_cngn, avg_price_usdt, error)
        """
        client = await self._get_client()
        payload = {"market": self.market, "side": side, "ord_type": "market", "volume": str(amount)}
        last_error: str | None = None

        for attempt, delay in enumerate([0, 2, 4, 8, 16, 32]):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await client.post(f"{self.base_url}/users/me/orders", json=payload)
                if 400 <= response.status_code < 500:
                    error = str(response.json().get("message", "Bad request"))
                    return False, Decimal("0"), Decimal("0"), error
                response.raise_for_status()
                data = response.json().get("data", {})
                executed_cngn = Decimal(str(data.get("executed_volume", {}).get("amount", "0")))
                avg_price = Decimal(str(data.get("avg_price", {}).get("amount", "0")))
                logger.debug("market_order_placed", side=side, amount=float(amount), attempt=attempt)
                return True, executed_cngn, avg_price, None
            except Exception as e:
                last_error = str(e)
                logger.warning("market_order_attempt_failed", side=side, attempt=attempt, error=last_error)

        db = await get_db()
        await db.insert_alert(
            severity="critical",
            category="cex",
            message=f"Quidax {self.name} market order failed after 5 retries: {last_error}",
        )
        return False, Decimal("0"), Decimal("0"), last_error

    async def sync_order_ladder(self, reference_price: Decimal) -> None:
        """
        Sync order ladder to current reference price.

        Places buy and sell limit orders at fixed NGN offsets from the current rate.

        Args:
            reference_price: Current NGN/USDT rate (e.g. 1600 = 1 USDT buys 1600 NGN/cNGN)
        """
        if self.paused or not self.params.ladder_enabled:
            logger.info("quidax_ladder_skipped", paused=self.paused, enabled=self.params.ladder_enabled)
            return

        await self.cancel_all_orders()

        orders_placed = 0

        for offset in self.params.ladder_offsets_ngn:
            # Sell orders: cNGN is more expensive (fewer NGN per USDT from buyer's perspective)
            # price = 1 / (rate - offset) USDT per cNGN
            if self.params.order_size_cngn > 0:
                sell_ngn_rate = reference_price - offset
                if sell_ngn_rate > 0:
                    sell_price = Decimal("1") / sell_ngn_rate
                    try:
                        await self.place_order("sell", sell_price, self.params.order_size_cngn)
                        orders_placed += 1
                    except Exception as e:
                        logger.warning("place_sell_order_failed", offset=offset, error=str(e))

            # Buy orders: cNGN is cheaper (more NGN per USDT from seller's perspective)
            # price = 1 / (rate + offset) USDT per cNGN; convert USDT budget to cNGN volume
            if self.params.order_size_usdt > 0:
                buy_ngn_rate = reference_price + offset
                buy_price = Decimal("1") / buy_ngn_rate
                cngn_amount = self.params.order_size_usdt / buy_price
                try:
                    await self.place_order("buy", buy_price, cngn_amount)
                    orders_placed += 1
                except Exception as e:
                    logger.warning("place_buy_order_failed", offset=offset, error=str(e))

        logger.info(
            "order_ladder_synced",
            reference_price_ngn=float(reference_price),
            offsets=self.params.ladder_offsets_ngn,
            orders_placed=orders_placed,
        )

    async def get_deposit_address(self, currency: str) -> dict:
        """
        Get or create a deposit address for a currency on Quidax.

        Args:
            currency: Currency ticker (e.g. "cngn", "usdt")

        Returns:
            API response with deposit address details
        """
        open_api_url = "https://openapi.quidax.io/exchange-open-api/api/v1"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{open_api_url}/users/me/wallets/{currency}/addresses",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            return response.json()

    async def handle_webhook(self, event: dict) -> None:
        """Handle Quidax webhook events."""
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
        elif event_type == "wallet.address.generated":
            currency = event["data"]["currency"].lower()
            address = event["data"]["address"]
            self._deposit_addresses[currency] = address
            db = await get_db()
            await db.set_system_state(
                f"quidax_{self._funding_role}_deposit_{currency}", address
            )
            logger.info("quidax_deposit_address_received", role=self._funding_role, currency=currency)
