"""Mock Quidax client for local testing."""

import time
import uuid
from decimal import Decimal
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


class MockResponse:
    """Mock httpx.Response for testing."""

    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Mock error {self.status_code}",
                request=None,
                response=self,
            )


class MockQuidaxClient:
    """
    Mock Quidax API client for local testing.

    Simulates the Quidax API with in-memory state for:
    - Wallet balances
    - Open orders
    - Order placement/cancellation

    Usage:
        mock = MockQuidaxClient(
            initial_balances={"cngn": "1000000", "usdt": "1000"}
        )
        adapter = QuidaxAdapter(
            api_key="test",
            api_secret="test",
            http_client=mock,
        )
    """

    def __init__(
        self,
        initial_balances: dict[str, str] | None = None,
        simulate_latency: bool = False,
    ):
        """
        Initialize mock client.

        Args:
            initial_balances: Starting balances, e.g., {"cngn": "1000000", "usdt": "1000"}
            simulate_latency: If True, add artificial delays
        """
        self.balances = initial_balances or {
            "cngn": "500000",
            "usdt": "500",
            "usdc": "0",
            "ngn": "100000",
        }
        self.orders: dict[str, dict] = {}
        self.order_history: list[dict] = []
        self.simulate_latency = simulate_latency

        logger.info("mock_quidax_initialized", balances=self.balances)

    async def get(self, url: str, **kwargs) -> MockResponse:
        """Handle GET requests."""
        if self.simulate_latency:
            import asyncio
            await asyncio.sleep(0.1)

        # GET /users/me/wallets
        if "/wallets" in url:
            return self._get_wallets()

        # GET /users/me/orders
        if "/orders" in url and "params" in kwargs:
            params = kwargs.get("params", {})
            return self._get_orders(
                market=params.get("market"),
                state=params.get("state"),
            )

        return MockResponse({"status": "error", "message": "Unknown endpoint"}, 404)

    async def post(self, url: str, **kwargs) -> MockResponse:
        """Handle POST requests."""
        if self.simulate_latency:
            import asyncio
            await asyncio.sleep(0.1)

        # POST /users/me/orders (create order)
        if url.endswith("/orders") and "json" in kwargs:
            return self._create_order(kwargs["json"])

        # POST /users/me/orders/{id}/cancel
        if "/cancel" in url:
            order_id = url.split("/orders/")[1].split("/cancel")[0]
            return self._cancel_order(order_id)

        return MockResponse({"status": "error", "message": "Unknown endpoint"}, 404)

    def _get_wallets(self) -> MockResponse:
        """Return wallet balances."""
        wallets = [
            {
                "currency": currency.upper(),
                "balance": balance,
                "locked": "0",
                "staked": "0",
            }
            for currency, balance in self.balances.items()
        ]

        return MockResponse({
            "status": "success",
            "message": "Successful",
            "data": wallets,
        })

    def _get_orders(self, market: str | None, state: str | None) -> MockResponse:
        """Return filtered orders."""
        orders = list(self.orders.values())

        if market:
            orders = [o for o in orders if o["market"] == market]

        if state:
            # Map our internal states to Quidax states
            # "wait" = pending orders
            if state == "wait":
                orders = [o for o in orders if o["state"] == "wait"]

        return MockResponse({
            "status": "success",
            "message": "Successful",
            "data": orders,
        })

    def _create_order(self, body: dict) -> MockResponse:
        """Create a new order."""
        order_id = str(uuid.uuid4())[:8]
        now = time.time()

        market = body.get("market", "")
        side = body.get("side", "buy")
        ord_type = body.get("ord_type", "limit")
        price = body.get("price", "0")
        volume = body.get("volume", "0")

        # Validate required fields
        if not market or not volume:
            return MockResponse({
                "status": "error",
                "message": "Missing required fields",
            }, 400)

        if ord_type == "limit" and not price:
            return MockResponse({
                "status": "error",
                "message": "Price required for limit orders",
            }, 400)

        order = {
            "id": order_id,
            "market": market,
            "side": side,
            "ord_type": ord_type,
            "price": price,
            "volume": volume,
            "remaining_volume": volume,
            "executed_volume": "0",
            "state": "wait",
            "created_at": int(now * 1000),
            "updated_at": int(now * 1000),
        }

        self.orders[order_id] = order
        self.order_history.append({"action": "create", "order": order.copy()})

        logger.debug(
            "mock_order_created",
            order_id=order_id,
            side=side,
            price=price,
            volume=volume,
        )

        return MockResponse({
            "status": "success",
            "message": "Successful",
            "data": order,
        })

    def _cancel_order(self, order_id: str) -> MockResponse:
        """Cancel an order."""
        if order_id not in self.orders:
            return MockResponse({
                "status": "error",
                "message": "Order not found",
            }, 404)

        order = self.orders.pop(order_id)
        order["state"] = "cancel"
        self.order_history.append({"action": "cancel", "order": order.copy()})

        logger.debug("mock_order_cancelled", order_id=order_id)

        return MockResponse({
            "status": "success",
            "message": "Successful",
            "data": order,
        })

    # === Test helpers ===

    def simulate_fill(self, order_id: str, fill_percent: float = 100.0) -> bool:
        """
        Simulate an order being filled (for testing).

        Args:
            order_id: Order to fill
            fill_percent: Percentage to fill (0-100)

        Returns:
            True if order was found and filled
        """
        if order_id not in self.orders:
            return False

        order = self.orders[order_id]
        volume = Decimal(order["volume"])
        fill_amount = volume * Decimal(str(fill_percent)) / 100

        order["executed_volume"] = str(
            Decimal(order["executed_volume"]) + fill_amount
        )
        order["remaining_volume"] = str(
            Decimal(order["remaining_volume"]) - fill_amount
        )

        if Decimal(order["remaining_volume"]) <= 0:
            order["state"] = "done"
            self.orders.pop(order_id)

        # Update balances based on fill
        side = order["side"]
        price = Decimal(order["price"])

        if side == "buy":
            # Buying CNGN with USDT
            usdt_spent = fill_amount * price
            self.balances["usdt"] = str(
                Decimal(self.balances["usdt"]) - usdt_spent
            )
            self.balances["cngn"] = str(
                Decimal(self.balances["cngn"]) + fill_amount
            )
        else:
            # Selling CNGN for USDT
            usdt_received = fill_amount * price
            self.balances["cngn"] = str(
                Decimal(self.balances["cngn"]) - fill_amount
            )
            self.balances["usdt"] = str(
                Decimal(self.balances["usdt"]) + usdt_received
            )

        self.order_history.append({
            "action": "fill",
            "order_id": order_id,
            "fill_amount": str(fill_amount),
        })

        logger.info(
            "mock_order_filled",
            order_id=order_id,
            fill_amount=float(fill_amount),
            new_balances=self.balances,
        )

        return True

    def get_stats(self) -> dict:
        """Get mock client statistics for debugging."""
        return {
            "balances": self.balances,
            "open_orders": len(self.orders),
            "total_actions": len(self.order_history),
            "orders": list(self.orders.values()),
        }

    def reset(self, balances: dict[str, str] | None = None):
        """Reset state (useful between tests)."""
        self.balances = balances or {
            "cngn": "500000",
            "usdt": "500",
            "usdc": "0",
            "ngn": "100000",
        }
        self.orders.clear()
        self.order_history.clear()
        logger.info("mock_quidax_reset", balances=self.balances)
