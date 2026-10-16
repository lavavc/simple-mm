"""Quidax API transport helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, cast

import httpx
import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class QuidaxWalletBalance:
    available: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.available + self.locked


class QuidaxApiClient:
    """Wrap Quidax endpoint discovery and transport fallbacks."""

    def __init__(
        self,
        *,
        market: str,
        base_url: str,
        order_api_base_url: str,
        order_user_id: str,
        client_getter: Callable[[], Awaitable[httpx.AsyncClient]],
    ) -> None:
        self.market = market
        self.base_url = base_url.rstrip("/")
        self.order_api_base_url = order_api_base_url.rstrip("/")
        self.order_user_id = order_user_id or "me"
        self._client_getter = client_getter

    async def get_wallet_balance(self, currency: str) -> Decimal:
        return (await self.get_wallet_inventory(currency)).available

    async def get_wallet_inventory(self, currency: str) -> QuidaxWalletBalance:
        client = await self._client_getter()
        response = await client.get(f"{self.base_url}/users/{self.order_user_id}/wallets/{currency}")
        response.raise_for_status()
        data = response.json()["data"]
        available = Decimal(str(data["balance"]))
        locked = Decimal(str(data["locked"]))
        if any(not amount.is_finite() or amount < 0 for amount in (available, locked)):
            raise ValueError(f"Invalid Quidax {currency} wallet balance")
        return QuidaxWalletBalance(available=available, locked=locked)

    async def get_order_book_payload(self, limit: int) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}/markets/{self.market}/depth",
                params={"limit": limit},
                headers={"accept": "application/json"},
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    async def get_market_summary_payload(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}/markets/summary/",
                headers={"accept": "application/json"},
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def order_collection_endpoints(self) -> list[str]:
        endpoints = [
            f"{self.base_url}/users/{self.order_user_id}/orders",
            f"{self.order_api_base_url}/users/{self.order_user_id}/orders",
        ]
        deduped: list[str] = []
        for endpoint in endpoints:
            if endpoint not in deduped:
                deduped.append(endpoint)
        return deduped

    def order_item_endpoints(self, order_id: str) -> list[str]:
        endpoints = [
            f"{self.base_url}/users/{self.order_user_id}/orders/{order_id}",
            f"{self.order_api_base_url}/users/{self.order_user_id}/orders/{order_id}",
        ]
        deduped: list[str] = []
        for endpoint in endpoints:
            if endpoint not in deduped:
                deduped.append(endpoint)
        return deduped

    async def fetch_orders_payload(self, params: dict[str, Any] | None) -> dict[str, Any]:
        last_error: Exception | None = None
        for endpoint in self.order_collection_endpoints():
            try:
                return await self.fetch_orders_payload_direct(endpoint, params)
            except Exception as exc:
                last_error = exc
                logger.warning("quidax_fetch_orders_failed", endpoint=endpoint, error=str(exc))
        if last_error is not None:
            raise last_error
        return {"status": "error", "message": "No Quidax order endpoint available", "data": []}

    async def fetch_orders_payload_direct(
        self,
        endpoint: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        client = await self._client_getter()
        response = await client.get(endpoint, params=params)
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        payload.setdefault("_endpoint", endpoint)
        return payload

    @staticmethod
    def is_missing_order_error(exc: Exception) -> bool:
        if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 404:
            return False
        try:
            payload = exc.response.json()
        except ValueError:
            return False
        data = payload.get("data") if isinstance(payload, dict) else None
        return isinstance(data, dict) and str(data.get("code")) == "110043"

    async def fetch_order_by_id(self, order_id: str) -> tuple[str, dict[str, Any] | None]:
        client = await self._client_getter()
        for endpoint in self.order_item_endpoints(order_id):
            try:
                response = await client.get(endpoint)
                response.raise_for_status()
                payload = cast(dict[str, Any], response.json())
                data = payload.get("data")
                if isinstance(data, dict):
                    return "found", data
            except Exception as exc:
                if self.is_missing_order_error(exc):
                    return "missing", None
                logger.warning(
                    "quidax_fetch_order_by_id_failed",
                    endpoint=endpoint,
                    order_id=order_id,
                    error=str(exc),
                )

        return "unknown", None
