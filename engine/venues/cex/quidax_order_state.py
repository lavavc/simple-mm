"""Tracked Quidax open-order fallback state."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Awaitable, Callable

import structlog

from engine.db.backend import SystemStateStoreProtocol
from engine.venues.cex.quidax_orders import is_order_open, is_order_terminal, order_market_matches

logger = structlog.get_logger()


class QuidaxTrackedOrderState:
    """Persist and reconcile locally tracked Quidax open orders."""

    def __init__(
        self,
        *,
        venue_name: str,
        market: str,
        system_state_store: SystemStateStoreProtocol | None,
    ) -> None:
        self.venue_name = venue_name
        self.market = market
        self.system_state_store = system_state_store
        self._tracked_open_orders: list[dict[str, Any]] = []
        self._tracked_open_orders_loaded = False

    def tracked_orders_state_key(self) -> str:
        return f"{self.venue_name}:tracked_open_orders"

    async def ensure_loaded(self) -> None:
        if self._tracked_open_orders_loaded:
            return

        self._tracked_open_orders_loaded = True
        if self.system_state_store is None:
            return

        try:
            raw = await self.system_state_store.get_system_state(self.tracked_orders_state_key())
        except Exception as exc:
            logger.warning("quidax_tracked_orders_load_failed", venue=self.venue_name, error=str(exc))
            return

        if not raw:
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("quidax_tracked_orders_decode_failed", venue=self.venue_name, error=str(exc))
            return

        if not isinstance(payload, list):
            logger.warning(
                "quidax_tracked_orders_unexpected_shape",
                venue=self.venue_name,
                payload_type=type(payload).__name__,
            )
            return

        self._tracked_open_orders = [
            item
            for item in payload
            if isinstance(item, dict) and item.get("id")
        ]

    async def persist(self) -> None:
        if self.system_state_store is None:
            return

        try:
            await self.system_state_store.set_system_state(
                self.tracked_orders_state_key(),
                self._tracked_open_orders,
            )
        except Exception as exc:
            logger.warning("quidax_tracked_orders_persist_failed", venue=self.venue_name, error=str(exc))

    def tracked_order_to_row(self, tracked: dict[str, Any]) -> dict[str, Any]:
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

    async def get_open_order_rows(
        self,
        fetch_order_by_id: Callable[[str], Awaitable[tuple[str, dict[str, Any] | None]]],
    ) -> list[dict[str, Any]]:
        await self.ensure_loaded()
        removed_pending_ids: set[str] = set()
        status_updates: dict[str, str] = {}

        for tracked in self._tracked_open_orders:
            order_id = str(tracked.get("id", ""))
            if not order_id or str(tracked.get("status") or "").lower() != "pending_cancel":
                continue

            resolution, order = await fetch_order_by_id(order_id)
            if resolution == "missing":
                removed_pending_ids.add(order_id)
                continue

            if resolution == "found" and isinstance(order, dict):
                if is_order_terminal(order):
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
            await self.persist()

        rows = [self.tracked_order_to_row(order) for order in self._tracked_open_orders]
        return [
            row
            for row in rows
            if order_market_matches(row, self.market) and is_order_open(row)
        ]

    async def track_open_order(
        self,
        order_id: str,
        *,
        side: str,
        price: Decimal,
        volume: Decimal,
        created_at: Any = None,
    ) -> None:
        await self.ensure_loaded()
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
        await self.persist()

    async def remove_tracked_open_order(self, order_id: str) -> None:
        await self.ensure_loaded()
        original_count = len(self._tracked_open_orders)
        self._tracked_open_orders = [
            order for order in self._tracked_open_orders if str(order.get("id", "")) != order_id
        ]
        if len(self._tracked_open_orders) != original_count:
            await self.persist()

    async def update_tracked_open_order(self, order_id: str, **fields: Any) -> bool:
        await self.ensure_loaded()
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
            await self.persist()
        return updated

    async def reconcile_from_rows(self, rows: list[dict[str, Any]]) -> set[str]:
        await self.ensure_loaded()
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
            if is_order_terminal(row):
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
            await self.persist()

        return removed_ids
