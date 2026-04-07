"""Quidax CEX adapter for order ladder management."""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Callable, Optional, cast

import httpx
import structlog

from engine.api.schemas import (
    Position,
    PriceQuote,
    CexParams,
    OrderBookDepth,
    OrderBookLevel,
    VenueOrderSummary,
)
from engine.db.backend import AlertStoreProtocol, SystemStateStoreProtocol
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class LadderOrderTarget:
    side: str
    price: Decimal
    volume: Decimal


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
        market: str = "usdtcngn",
        base_url: str | None = None,
        order_api_base_url: str | None = None,
        order_user_id: str = "me",
        name: str = "quidax",
        funding_role: str = "quidax-trade-fund",
        alert_store: AlertStoreProtocol | None = None,
        system_state_store: SystemStateStoreProtocol | None = None,
        broadcast: Callable[[dict[str, Any]], Any] | None = None,
    ):
        """
        Initialize Quidax adapter.

        Args:
            api_key: Quidax secret key (used as Bearer token)
            params: Order ladder parameters
            market: Trading pair (lowercase, no underscore, e.g., "cngnusdt")
            base_url: Override public/openapi base URL (useful for testing)
            order_api_base_url: Override private order API base URL (useful for testing)
            order_user_id: Quidax user identifier for private order endpoints (`me` or sub-account id)
            name: Adapter name (used in logs and venue registry)
            funding_role: Account role for auto-funding ("quidax-trade-fund" | "quidax-lp")
        """
        self.name = name
        self.api_key = api_key
        self.params = params or CexParams()
        self.market = market
        self.base_url = (base_url or "https://openapi.quidax.io/exchange-open-api/api/v1/").rstrip("/")
        self.order_api_base_url = (order_api_base_url or "https://app.quidax.io/api/v1").rstrip("/")
        self.order_user_id = order_user_id or "me"
        self._funding_role = funding_role
        self._client: Optional[httpx.AsyncClient] = None
        self._last_balances: dict[str, Decimal] = {}
        self._last_ladder_requote_at: float = 0
        self._tracked_open_orders: list[dict[str, Any]] = []
        self._tracked_open_orders_loaded = False
        self.enabled = True
        self.paused = False
        if alert_store is None:
            raise ValueError("QuidaxAdapter requires an alert store")
        self.alert_store = alert_store
        self.system_state_store = system_state_store
        self.broadcast = broadcast

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with auth headers."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _resolve_balanced_order_sizes(self, reference_price: Decimal) -> tuple[Decimal, Decimal]:
        """Balance both ladder sides to the same notional when both are enabled.

        Returns:
            (effective_order_size_cngn, effective_order_size_usdt)
        """
        order_size_cngn = self.params.order_size_cngn
        order_size_usdt = self.params.order_size_usdt
        if reference_price <= 0 or order_size_cngn <= 0 or order_size_usdt <= 0:
            return order_size_cngn, order_size_usdt

        usdt_side_cngn_equiv = order_size_usdt * reference_price
        target_notional_cngn = min(order_size_cngn, usdt_side_cngn_equiv)
        effective_order_size_cngn = target_notional_cngn
        effective_order_size_usdt = target_notional_cngn / reference_price

        if (
            effective_order_size_cngn != order_size_cngn
            or effective_order_size_usdt != order_size_usdt
        ):
            logger.info(
                "quidax_ladder_sizes_balanced",
                reference_price_ngn=float(reference_price),
                configured_order_size_cngn=float(order_size_cngn),
                configured_order_size_usdt=float(order_size_usdt),
                effective_order_size_cngn=float(effective_order_size_cngn),
                effective_order_size_usdt=float(effective_order_size_usdt),
            )

        return effective_order_size_cngn, effective_order_size_usdt

    def _format_limit_order(self, side: str, price: Decimal, amount: Decimal) -> tuple[Decimal, Decimal]:
        """Apply Quidax market precision rules before submitting a limit order."""
        if self.market == "usdtcngn":
            price_precision = Decimal("0.01")
            volume_precision = Decimal("0.01")
            rounded_price = price.quantize(
                price_precision,
                rounding=ROUND_DOWN if side == "buy" else ROUND_UP,
            )
            rounded_amount = amount.quantize(volume_precision, rounding=ROUND_DOWN)
            return rounded_price, rounded_amount
        return price, amount

    def _decimal_from_order_value(self, value: Any) -> Decimal:
        if isinstance(value, dict):
            for key in ("amount", "value"):
                nested = value.get(key)
                if nested is not None:
                    return Decimal(str(nested))
            return Decimal("0")
        if value is None:
            return Decimal("0")
        return Decimal(str(value))

    def _tracked_orders_state_key(self) -> str:
        return f"{self.name}:tracked_open_orders"

    async def _ensure_tracked_open_orders_loaded(self) -> None:
        if self._tracked_open_orders_loaded:
            return

        self._tracked_open_orders_loaded = True
        if self.system_state_store is None:
            return

        try:
            raw = await self.system_state_store.get_system_state(self._tracked_orders_state_key())
        except Exception as exc:
            logger.warning("quidax_tracked_orders_load_failed", venue=self.name, error=str(exc))
            return

        if not raw:
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("quidax_tracked_orders_decode_failed", venue=self.name, error=str(exc))
            return

        if not isinstance(payload, list):
            logger.warning(
                "quidax_tracked_orders_unexpected_shape",
                venue=self.name,
                payload_type=type(payload).__name__,
            )
            return

        self._tracked_open_orders = [
            item
            for item in payload
            if isinstance(item, dict) and item.get("id")
        ]

    async def _persist_tracked_open_orders(self) -> None:
        if self.system_state_store is None:
            return

        try:
            await self.system_state_store.set_system_state(
                self._tracked_orders_state_key(),
                self._tracked_open_orders,
            )
        except Exception as exc:
            logger.warning("quidax_tracked_orders_persist_failed", venue=self.name, error=str(exc))

    async def _broadcast_orders_changed(self) -> None:
        if self.broadcast is None:
            return

        try:
            result = self.broadcast({"type": "venue_orders", "data": {"venue": self.name}})
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning("quidax_orders_broadcast_failed", venue=self.name, error=str(exc))

    def _tracked_order_to_row(self, tracked: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(tracked.get("id", "")),
            "market": {"id": tracked.get("market") or self.market},
            "side": tracked.get("side"),
            "status": tracked.get("status") or "wait",
            "price": tracked.get("price", "0"),
            "volume": tracked.get("volume", "0"),
            "remaining_volume": tracked.get("remaining_volume") or tracked.get("volume", "0"),
            "executed_volume": tracked.get("executed_volume", "0"),
            "created_at": tracked.get("created_at"),
            "_tracked_local": True,
        }

    async def _get_tracked_open_order_rows(self) -> list[dict[str, Any]]:
        await self._ensure_tracked_open_orders_loaded()
        removed_pending_ids: set[str] = set()
        status_updates: dict[str, str] = {}

        for tracked in self._tracked_open_orders:
            order_id = str(tracked.get("id", ""))
            if not order_id or str(tracked.get("status") or "").lower() != "pending_cancel":
                continue

            resolution, order = await self._fetch_order_by_id(order_id)
            if resolution == "missing":
                removed_pending_ids.add(order_id)
                continue

            if resolution == "found" and isinstance(order, dict):
                if self._is_order_terminal(order):
                    removed_pending_ids.add(order_id)
                    continue

                remote_status = str(order.get("state") or order.get("status") or "").lower()
                if remote_status and remote_status != str(tracked.get("status") or "").lower():
                    status_updates[order_id] = remote_status

        changed = False
        if removed_pending_ids:
            self._tracked_open_orders = [
                order for order in self._tracked_open_orders if str(order.get("id", "")) not in removed_pending_ids
            ]
            changed = True

        if status_updates:
            for tracked in self._tracked_open_orders:
                order_id = str(tracked.get("id", ""))
                if order_id in status_updates:
                    tracked["status"] = status_updates[order_id]
                    changed = True

        if changed:
            await self._persist_tracked_open_orders()
            await self._broadcast_orders_changed()

        rows = [self._tracked_order_to_row(order) for order in self._tracked_open_orders]
        return [
            row
            for row in rows
            if self._order_market_matches(row) and self._is_order_open(row)
        ]

    async def _track_open_order(
        self,
        order_id: str,
        *,
        side: str,
        price: Decimal,
        volume: Decimal,
        created_at: Any = None,
    ) -> None:
        await self._ensure_tracked_open_orders_loaded()
        record = {
            "id": order_id,
            "market": self.market,
            "side": side,
            "status": "wait",
            "price": str(price),
            "volume": str(volume),
            "remaining_volume": str(volume),
            "executed_volume": "0",
            "created_at": created_at if created_at is not None else int(time.time() * 1000),
        }
        existing_index = next(
            (index for index, current in enumerate(self._tracked_open_orders) if current.get("id") == order_id),
            None,
        )
        if existing_index is None:
            self._tracked_open_orders.append(record)
        else:
            self._tracked_open_orders[existing_index] = record
        await self._persist_tracked_open_orders()
        await self._broadcast_orders_changed()

    async def _remove_tracked_open_order(self, order_id: str) -> None:
        await self._ensure_tracked_open_orders_loaded()
        original_count = len(self._tracked_open_orders)
        self._tracked_open_orders = [
            order for order in self._tracked_open_orders if str(order.get("id", "")) != order_id
        ]
        if len(self._tracked_open_orders) != original_count:
            await self._persist_tracked_open_orders()
            await self._broadcast_orders_changed()

    async def seed_orders_ws_state(self) -> None:
        """Seed the websocket retained state for this venue's active orders.

        The frontend uses a lightweight `venue_orders` event to invalidate the
        orders query. Because retained websocket events live only in memory,
        a backend restart can leave the socket quiet until the next order
        create/cancel. Broadcasting once at startup restores that retained
        event so newly opened dashboards refetch immediately.
        """
        await self._ensure_tracked_open_orders_loaded()
        await self._broadcast_orders_changed()

    def _order_collection_endpoints(self) -> list[str]:
        endpoints = [
            f"{self.order_api_base_url}/users/{self.order_user_id}/orders",
            f"{self.order_api_base_url}/users/me/orders",
            f"{self.base_url}/users/{self.order_user_id}/orders",
            f"{self.base_url}/users/me/orders",
        ]
        deduped: list[str] = []
        for endpoint in endpoints:
            if endpoint not in deduped:
                deduped.append(endpoint)
        return deduped

    def _order_item_endpoints(self, order_id: str) -> list[str]:
        endpoints = [
            f"{self.order_api_base_url}/users/{self.order_user_id}/orders/{order_id}",
            f"{self.order_api_base_url}/users/me/orders/{order_id}",
            f"{self.base_url}/users/{self.order_user_id}/orders/{order_id}",
            f"{self.base_url}/users/me/orders/{order_id}",
        ]
        deduped: list[str] = []
        for endpoint in endpoints:
            if endpoint not in deduped:
                deduped.append(endpoint)
        return deduped

    def _extract_order_rows(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data", [])
        if isinstance(data, dict):
            data = data.get("items") or data.get("orders") or []
        if not isinstance(data, list):
            logger.warning("quidax_orders_unexpected_shape", payload_type=type(data).__name__)
            return []
        return cast(list[dict[str, Any]], data)

    def _normalize_market_id(self, market: Any) -> str:
        if isinstance(market, dict):
            market_id = market.get("id")
            if market_id:
                return "".join(ch for ch in str(market_id).lower() if ch.isalnum())
            base = market.get("base_unit")
            quote = market.get("quote_unit")
            if base and quote:
                return "".join(ch for ch in f"{base}{quote}".lower() if ch.isalnum())
            return ""
        return "".join(ch for ch in str(market).lower() if ch.isalnum())

    def _order_market_matches(self, order: dict[str, Any]) -> bool:
        market = order.get("market")
        if market is None:
            return True
        return self._normalize_market_id(market) == self._normalize_market_id(self.market)

    def _is_order_terminal(self, order: dict[str, Any]) -> bool:
        status = str(order.get("state") or order.get("status") or "").lower()
        return status in {"done", "cancel", "cancelled", "canceled", "filled", "failed", "rejected"}

    def _is_order_open(self, order: dict[str, Any]) -> bool:
        status = str(order.get("state") or order.get("status") or "").lower()
        if status and status not in {"wait", "confirm", "pending_cancel"}:
            return False

        remaining = self._decimal_from_order_value(order.get("remaining_volume"))
        if remaining <= 0:
            remaining = self._decimal_from_order_value(order.get("volume"))
        if remaining > 0:
            return True

        origin = self._decimal_from_order_value(order.get("origin_volume"))
        executed = self._decimal_from_order_value(order.get("executed_volume"))
        if origin > executed:
            return True

        return status in {"wait", "confirm", "pending_cancel"}

    def _cancel_response_is_pending(self, result: dict[str, Any]) -> bool:
        data = result.get("data")
        if not isinstance(data, dict):
            return False
        nested_status = str(data.get("state") or data.get("status") or "").lower()
        return nested_status == "pending_cancel"

    def _is_missing_order_error(self, exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        if response is not None and getattr(response, "status_code", None) == 404:
            return True
        return "404" in str(exc)

    async def _fetch_order_by_id(self, order_id: str) -> tuple[str, dict[str, Any] | None]:
        client = await self._get_client()
        saw_missing = False
        saw_error = False

        for endpoint in self._order_item_endpoints(order_id):
            try:
                response = await client.get(endpoint)
                response.raise_for_status()
                payload = cast(dict[str, Any], response.json())
                data = payload.get("data")
                if isinstance(data, dict):
                    return "found", data
            except Exception as exc:
                if self._is_missing_order_error(exc):
                    saw_missing = True
                    continue
                saw_error = True
                logger.warning(
                    "quidax_fetch_order_by_id_failed",
                    endpoint=endpoint,
                    order_id=order_id,
                    error=str(exc),
                )

        if saw_missing and not saw_error:
            return "missing", None
        return "unknown", None

    async def _update_tracked_open_order(self, order_id: str, **fields: Any) -> bool:
        await self._ensure_tracked_open_orders_loaded()
        updated = False
        for order in self._tracked_open_orders:
            if str(order.get("id", "")) != order_id:
                continue
            for key, value in fields.items():
                if value is not None:
                    order[key] = value
                    updated = True
            break

        if updated:
            await self._persist_tracked_open_orders()
            await self._broadcast_orders_changed()
        return updated

    async def _reconcile_tracked_open_orders_from_rows(self, rows: list[dict[str, Any]]) -> set[str]:
        await self._ensure_tracked_open_orders_loaded()
        if not self._tracked_open_orders or not rows:
            return set()

        rows_by_id = {
            str(row.get("id", "")): row
            for row in rows
            if isinstance(row, dict) and row.get("id")
        }
        removed_ids: set[str] = set()
        status_updates: dict[str, str] = {}

        for order in self._tracked_open_orders:
            order_id = str(order.get("id", ""))
            if not order_id:
                continue
            row = rows_by_id.get(order_id)
            if row is None:
                continue
            if self._is_order_terminal(row):
                removed_ids.add(order_id)
                continue

            status = str(row.get("state") or row.get("status") or "").lower()
            if status and status != str(order.get("status") or "").lower():
                status_updates[order_id] = status

        changed = False
        if removed_ids:
            self._tracked_open_orders = [
                order for order in self._tracked_open_orders if str(order.get("id", "")) not in removed_ids
            ]
            changed = True

        if status_updates:
            for order in self._tracked_open_orders:
                order_id = str(order.get("id", ""))
                if order_id in status_updates:
                    order["status"] = status_updates[order_id]
                    changed = True

        if changed:
            await self._persist_tracked_open_orders()
            await self._broadcast_orders_changed()

        return removed_ids

    async def _fetch_orders_payload(self, params: dict[str, Any] | None) -> dict[str, Any]:
        last_error: Exception | None = None
        for endpoint in self._order_collection_endpoints():
            try:
                payload = await self._fetch_orders_payload_direct(endpoint, params)
                return payload
            except Exception as exc:
                last_error = exc
                logger.warning("quidax_fetch_orders_failed", endpoint=endpoint, error=str(exc))
        if last_error is not None:
            raise last_error
        return {"status": "error", "message": "No Quidax order endpoint available", "data": []}

    async def _fetch_orders_payload_direct(
        self,
        endpoint: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.get(endpoint, params=params)
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        payload.setdefault("_endpoint", endpoint)
        return payload

    def _build_desired_ladder_orders(self, reference_price: Decimal) -> list[LadderOrderTarget]:
        effective_order_size_cngn, effective_order_size_usdt = self._resolve_balanced_order_sizes(
            reference_price
        )
        targets: list[LadderOrderTarget] = []
        for offset in self.params.resolved_ladder_offsets_ngn:
            if effective_order_size_cngn > 0:
                sell_ngn_rate = reference_price - offset
                if sell_ngn_rate > 0:
                    sell_usdt_volume = effective_order_size_cngn / sell_ngn_rate
                    price, volume = self._format_limit_order("buy", sell_ngn_rate, sell_usdt_volume)
                    targets.append(LadderOrderTarget(side="buy", price=price, volume=volume))
            if effective_order_size_usdt > 0:
                buy_ngn_rate = reference_price + offset
                price, volume = self._format_limit_order("sell", buy_ngn_rate, effective_order_size_usdt)
                targets.append(LadderOrderTarget(side="sell", price=price, volume=volume))
        return targets

    def _extract_open_order_target(self, order: dict[str, Any]) -> LadderOrderTarget | None:
        side = str(order.get("side", "")).lower()
        if side not in {"buy", "sell"}:
            return None

        price = self._decimal_from_order_value(order.get("price"))
        if price <= 0:
            return None

        remaining_volume = order.get("remaining_volume")
        if remaining_volume is None:
            remaining_volume = order.get("volume")
        volume = self._decimal_from_order_value(remaining_volume)
        if volume <= 0:
            return None

        return LadderOrderTarget(side=side, price=price, volume=volume)

    def _requires_requote(
        self,
        existing_orders: list[dict[str, Any]],
        desired_orders: list[LadderOrderTarget],
    ) -> bool:
        current_targets = [
            target
            for order in existing_orders
            if (target := self._extract_open_order_target(order)) is not None
        ]
        if len(current_targets) != len(desired_orders):
            return True

        threshold_bps = Decimal(str(self.params.anchor_requote_threshold_bps))
        volume_tolerance = Decimal("0.01")
        current_targets.sort(key=lambda order: (order.side, order.price, order.volume))
        desired_orders = sorted(desired_orders, key=lambda order: (order.side, order.price, order.volume))

        for current, desired in zip(current_targets, desired_orders):
            if current.side != desired.side:
                return True
            if desired.price <= 0:
                return True
            price_drift_bps = (abs(current.price - desired.price) / desired.price) * Decimal("10000")
            if price_drift_bps > threshold_bps:
                return True
            if abs(current.volume - desired.volume) > volume_tolerance:
                return True

        return False

    def _coerce_timestamp_ms(self, value: Any) -> int | None:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            numeric = int(value)
            return numeric if numeric > 10_000_000_000 else numeric * 1000

        text = str(value).strip()
        if not text:
            return None

        if text.isdigit():
            numeric = int(text)
            return numeric if numeric > 10_000_000_000 else numeric * 1000

        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return None

    def _normalize_order_summary(self, order: dict[str, Any]) -> VenueOrderSummary | None:
        side = str(order.get("side", "")).lower()
        if side not in {"buy", "sell"}:
            return None

        price = self._decimal_from_order_value(order.get("price"))
        if price <= 0:
            return None

        volume = self._decimal_from_order_value(order.get("volume"))
        origin_volume = self._decimal_from_order_value(order.get("origin_volume"))
        remaining_volume = self._decimal_from_order_value(order.get("remaining_volume"))
        executed_volume = self._decimal_from_order_value(order.get("executed_volume"))

        if volume <= 0:
            volume = origin_volume
        if remaining_volume <= 0:
            base_volume = origin_volume if origin_volume > 0 else volume
            remaining_volume = base_volume - executed_volume if base_volume > executed_volume else base_volume
        if remaining_volume < 0:
            remaining_volume = Decimal("0")
        if volume <= 0:
            volume = origin_volume if origin_volume > 0 else remaining_volume + executed_volume

        return VenueOrderSummary(
            id=str(order.get("id", "")),
            market=self._normalize_market_id(order.get("market")) or self.market,
            side=side,
            status=str(order.get("state") or order.get("status") or "").lower() or None,
            price=price,
            volume=volume,
            remaining_volume=remaining_volume,
            executed_volume=executed_volume,
            notional=price * (remaining_volume if remaining_volume > 0 else volume),
            created_at=self._coerce_timestamp_ms(
                order.get("created_at")
                or order.get("created_at_i")
                or order.get("timestamp")
            ),
        )

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

    async def get_order_book_depth(self, limit: int = 50) -> Optional[OrderBookDepth]:
        """Fetch Level 2 Order Book depth (Bids and Asks with volume)."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.base_url}/markets/{self.market}/depth",
                    params={"limit": limit},
                    headers={"accept": "application/json"},
                )
                response.raise_for_status()
                data = response.json()

            if data.get("status") != "success":
                return None

            depth_data = data.get("data", {})
            
            bids = [
                OrderBookLevel(price=Decimal(str(p)), amount=Decimal(str(v)))
                for p, v in depth_data.get("bids", [])
                if Decimal(str(p)) > 0
            ]
            asks = [
                OrderBookLevel(price=Decimal(str(p)), amount=Decimal(str(v)))
                for p, v in depth_data.get("asks", [])
                if Decimal(str(p)) > 0
            ]

            return OrderBookDepth(
                venue=self.name,
                pair="CNGN/USDT",
                timestamp=depth_data.get("timestamp", int(time.time() * 1000)),
                bids=bids,
                asks=asks,
            )
        except Exception as e:
            logger.error("quidax_depth_fetch_failed", error=str(e))
            return None

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

    async def get_open_orders(self) -> list[dict[str, Any]]:
        """Get all open orders."""
        attempts = (
            {"market": self.market, "state": "wait"},
            {"market": self.market},
            {},
        )
        fallback_rows: list[dict[str, Any]] = []

        for params in attempts:
            payload = await self._fetch_orders_payload(params)
            rows = self._extract_order_rows(payload)
            await self._reconcile_tracked_open_orders_from_rows(rows)
            if rows:
                fallback_rows = rows
            open_rows = [
                order
                for order in rows
                if self._order_market_matches(order) and self._is_order_open(order)
            ]
            if open_rows:
                tracked_rows = await self._get_tracked_open_order_rows()
                tracked_by_id = {str(order.get("id", "")): order for order in tracked_rows}
                for order in open_rows:
                    tracked_by_id.pop(str(order.get("id", "")), None)
                open_rows.extend(tracked_by_id.values())
                if params != attempts[0]:
                    logger.info("quidax_open_orders_fallback_used", params=params, count=len(open_rows))
                return open_rows

        tracked_rows = await self._get_tracked_open_order_rows()
        if tracked_rows:
            logger.warning(
                "quidax_open_orders_api_empty_using_tracked_fallback",
                count=len(tracked_rows),
            )
            return tracked_rows

        if fallback_rows:
            logger.warning("quidax_open_orders_found_no_open_matches", rows=len(fallback_rows))
        return []

    async def get_orders_debug(self) -> dict[str, Any]:
        attempts = (
            {"market": self.market, "state": "wait"},
            {"market": self.market},
            {},
        )
        results: list[dict[str, Any]] = []

        for params in attempts:
            endpoint_results: list[dict[str, Any]] = []
            payload: dict[str, Any] | None = None
            for endpoint in self._order_collection_endpoints():
                try:
                    current_payload = await self._fetch_orders_payload_direct(endpoint, params)
                    endpoint_results.append(
                        {"endpoint": endpoint, "ok": True, "payload": current_payload}
                    )
                    if payload is None:
                        payload = current_payload
                except Exception as exc:
                    endpoint_results.append(
                        {"endpoint": endpoint, "ok": False, "error": str(exc)}
                    )

            payload = payload or {"status": "error", "message": "No payload returned", "data": []}
            rows = self._extract_order_rows(payload)
            open_rows = [
                order
                for order in rows
                if self._order_market_matches(order) and self._is_order_open(order)
            ]
            results.append(
                {
                    "params": params,
                    "payload": payload,
                    "endpoint_results": endpoint_results,
                    "row_count": len(rows),
                    "rows": rows,
                    "open_match_count": len(open_rows),
                    "open_matches": open_rows,
                }
            )

        return {"market": self.market, "attempts": results}

    async def get_open_order_summaries(self) -> list[VenueOrderSummary]:
        orders = await self.get_open_orders()
        summaries: list[VenueOrderSummary] = []
        for order in orders:
            if (summary := self._normalize_order_summary(order)) is not None:
                summaries.append(summary)
        summaries.sort(key=lambda order: order.created_at or 0, reverse=True)
        return summaries

    async def cancel_all_orders(self) -> int:
        """
        Cancel all open orders.

        Returns:
            Number of orders cancelled
        """
        orders = await self.get_open_orders()
        client = await self._get_client()

        terminal_cancelled_ids: set[str] = set()
        pending_cancel_ids: set[str] = set()
        for order in orders:
            order_id = str(order.get("id", ""))
            if not order_id:
                logger.warning("cancel_order_missing_id", order=order)
                continue
            try:
                cancelled_this_order = False
                for endpoint in self._order_item_endpoints(order_id):
                    cancel_endpoint = f"{endpoint}/cancel"
                    try:
                        response = await client.post(cancel_endpoint)
                        response.raise_for_status()
                        result = cast(dict[str, Any], response.json())
                        if result.get("status") == "success":
                            if self._cancel_response_is_pending(result):
                                pending_cancel_ids.add(order_id)
                                await self._update_tracked_open_order(order_id, status="pending_cancel")
                                logger.info(
                                    "cancel_order_pending",
                                    endpoint=cancel_endpoint,
                                    order_id=order_id,
                                )
                            else:
                                terminal_cancelled_ids.add(order_id)
                                await self._remove_tracked_open_order(order_id)
                            cancelled_this_order = True
                            break
                        logger.warning(
                            "cancel_order_rejected",
                            endpoint=cancel_endpoint,
                            order_id=order_id,
                            detail=result.get("message") or result.get("errors") or "unknown",
                        )
                    except Exception as endpoint_exc:
                        logger.warning(
                            "cancel_order_endpoint_failed",
                            endpoint=cancel_endpoint,
                            order_id=order_id,
                            error=str(endpoint_exc),
                        )
                if not cancelled_this_order:
                    logger.warning("cancel_order_failed_all_endpoints", order_id=order_id)
            except Exception as e:
                logger.warning("cancel_order_failed", order_id=order_id, error=str(e))

        remaining = orders
        for _ in range(3):
            if not remaining:
                break
            await asyncio.sleep(0.5)
            remaining = await self.get_open_orders()

        remaining_ids = {str(order.get("id", "")) for order in remaining if order.get("id")}
        completed_pending_ids = {order_id for order_id in pending_cancel_ids if order_id not in remaining_ids}
        for order_id in completed_pending_ids:
            await self._remove_tracked_open_order(order_id)

        cancelled = len(terminal_cancelled_ids | completed_pending_ids)
        logger.info(
            "cancelled_orders",
            count=cancelled,
            total=len(orders),
            pending=len(pending_cancel_ids - completed_pending_ids),
            remaining=len(remaining),
        )
        return cancelled

    async def place_order(
        self,
        side: str,
        price: Decimal,
        amount: Decimal,
    ) -> dict[str, Any]:
        """
        Place a limit order.

        Args:
            side: "buy" or "sell"
            price: Limit price in cNGN per USDT for the configured Quidax market
            amount: Order volume in the market's base asset (USDT for usdtcngn)

        Returns:
            Order result from API
        """
        client = await self._get_client()
        formatted_price, formatted_amount = self._format_limit_order(side, price, amount)
        payload = {
            "market": self.market,
            "side": side,
            "ord_type": "limit",
            "price": str(formatted_price),
            "volume": str(formatted_amount),
        }
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        for endpoint in self._order_collection_endpoints():
            try:
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                result = cast(dict[str, Any], response.json())
                if result.get("status") == "success" and result.get("data"):
                    break
                detail = result.get("message") or result.get("errors") or "Unknown Quidax error"
                raise ValueError(str(detail))
            except Exception as exc:
                last_error = exc
                logger.warning("place_order_endpoint_failed", endpoint=endpoint, error=str(exc))
                result = None
        if result is None:
            raise last_error or ValueError("Unknown Quidax error")

        logger.debug(
            "order_placed",
            side=side,
            price=float(formatted_price),
            amount=float(formatted_amount),
            success=bool(result.get("data")),
        )

        order_data = result.get("data")
        if isinstance(order_data, dict):
            order_id = str(order_data.get("id", "")).strip()
            if order_id:
                tracked_price = self._decimal_from_order_value(order_data.get("price"))
                tracked_volume = self._decimal_from_order_value(order_data.get("volume"))
                await self._track_open_order(
                    order_id,
                    side=str(order_data.get("side") or side).lower(),
                    price=tracked_price if tracked_price > 0 else formatted_price,
                    volume=tracked_volume if tracked_volume > 0 else formatted_amount,
                    created_at=order_data.get("created_at") or int(time.time() * 1000),
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
                response = None
                resp_json: dict[str, Any] = {}
                for endpoint in self._order_collection_endpoints():
                    try:
                        response = await client.post(endpoint, json=payload)
                        try:
                            resp_json = response.json()
                        except Exception:
                            resp_json = {}

                        if 400 <= response.status_code < 500:
                            detail = resp_json.get("message", response.text or "")
                            error_msg = f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"
                            last_error = error_msg
                            logger.warning(
                                "market_order_endpoint_rejected",
                                endpoint=endpoint,
                                side=side,
                                attempt=attempt,
                                error=error_msg,
                            )
                            continue

                        response.raise_for_status()
                        break
                    except Exception as endpoint_exc:
                        last_error = str(endpoint_exc)
                        logger.warning(
                            "market_order_endpoint_failed",
                            endpoint=endpoint,
                            side=side,
                            attempt=attempt,
                            error=last_error,
                        )
                        response = None
                        resp_json = {}
                        continue

                if response is None:
                    raise RuntimeError(last_error or "All Quidax market order endpoints failed")
                
                if resp_json.get("status") != "success":
                    error = str(resp_json.get("message", "Unknown Quidax Error"))
                    return False, Decimal("0"), Decimal("0"), error
                    
                data = resp_json.get("data", {})
                
                # Safely extract amounts handling both string and dict formats from Quidax
                vol_data = data.get("executed_volume", "0")
                executed_cngn = Decimal(str(vol_data.get("amount", "0") if isinstance(vol_data, dict) else vol_data))
                    
                price_data = data.get("avg_price", "0")
                avg_price = Decimal(str(price_data.get("amount", "0") if isinstance(price_data, dict) else price_data))
                    
                logger.debug("market_order_placed", side=side, amount=float(amount), attempt=attempt)
                return True, executed_cngn, avg_price, None
                
            except Exception as e:
                last_error = str(e)
                logger.warning("market_order_attempt_failed", side=side, attempt=attempt, error=last_error)

        await self.alert_store.insert_alert(
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

        desired_orders = self._build_desired_ladder_orders(reference_price)
        existing_orders = await self.get_open_orders()
        if existing_orders and not self._requires_requote(existing_orders, desired_orders):
            logger.info(
                "quidax_ladder_requote_skipped",
                reference_price_ngn=float(reference_price),
                existing_orders=len(existing_orders),
                threshold_bps=self.params.anchor_requote_threshold_bps,
            )
            return

        if existing_orders:
            cooldown_seconds = max(0, int(self.params.anchor_requote_cooldown_seconds))
            if cooldown_seconds > 0 and self._last_ladder_requote_at > 0:
                elapsed = time.time() - self._last_ladder_requote_at
                if elapsed < cooldown_seconds:
                    logger.info(
                        "quidax_ladder_requote_cooldown_skipped",
                        reference_price_ngn=float(reference_price),
                        cooldown_seconds=cooldown_seconds,
                        elapsed_seconds=elapsed,
                    )
                    return
            await self.cancel_all_orders()
            remaining_orders = await self.get_open_orders()
            if remaining_orders:
                remaining_ids = [str(order.get("id", "unknown")) for order in remaining_orders[:10]]
                raise RuntimeError(
                    "Refusing to place a new Quidax ladder while prior open orders remain: "
                    f"{len(remaining_orders)} still open ({', '.join(remaining_ids)})"
                )

        orders_attempted = 0
        orders_placed = 0
        order_errors: list[str] = []

        for target in desired_orders:
            try:
                orders_attempted += 1
                await self.place_order(target.side, target.price, target.volume)
                orders_placed += 1
            except Exception as e:
                order_errors.append(f"{target.side}@{target.price}:{e}")
                logger.warning("place_ladder_order_failed", side=target.side, price=float(target.price), error=str(e))

        if orders_attempted > 0 and orders_placed == 0:
            raise RuntimeError(
                "Quidax ladder sync attempted "
                f"{orders_attempted} orders but none were accepted: {'; '.join(order_errors)}"
            )

        logger.info(
            "order_ladder_synced",
            reference_price_ngn=float(reference_price),
            offsets=self.params.resolved_ladder_offsets_ngn,
            orders_attempted=orders_attempted,
            orders_placed=orders_placed,
        )
        if orders_placed > 0:
            self._last_ladder_requote_at = time.time()


    async def handle_webhook(self, event: dict[str, Any]) -> None:
        """Handle Quidax webhook events."""
        event_type = event.get("event")

        if event_type in {"order.filled", "order.cancel", "order.cancelled", "order.canceled"}:
            order = event.get("data", {})
            order_id = str(order.get("id", "")).strip()
            if order_id:
                await self._remove_tracked_open_order(order_id)
            logger.info(
                "quidax_order_webhook",
                event_type=event_type,
                order_id=order.get("id"),
                side=order.get("side"),
                price=order.get("price"),
                volume=order.get("executed_volume"),
            )
