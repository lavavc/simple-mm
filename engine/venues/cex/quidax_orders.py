"""Pure Quidax order payload helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from engine.types import VenueOrderSummary
from engine.venues.cex.order_values import coerce_timestamp_ms, decimal_from_order_value


def extract_order_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract list-like order rows from Quidax collection payloads."""
    data = payload.get("data", [])
    if isinstance(data, dict):
        data = data.get("items") or data.get("orders") or []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def normalize_market_id(market: Any) -> str:
    """Normalize market ids across string and object payload shapes."""
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


def order_market_matches(order: dict[str, Any], market: str) -> bool:
    """Return whether an order row belongs to the requested market."""
    order_market = order.get("market")
    if order_market is None:
        return True
    return normalize_market_id(order_market) == normalize_market_id(market)


def is_order_terminal(order: dict[str, Any]) -> bool:
    """Return whether an order is fully finished and should not be tracked."""
    status = str(order.get("state") or order.get("status") or "").lower()
    return status in {"done", "cancel", "cancelled", "canceled", "filled", "failed", "rejected"}


def is_order_open(order: dict[str, Any]) -> bool:
    """Return whether an order still has open size or open-state semantics."""
    status = str(order.get("state") or order.get("status") or "").lower()
    if status and status not in {"wait", "confirm", "pending_cancel"}:
        return False

    remaining = decimal_from_order_value(order.get("remaining_volume"))
    if remaining <= 0:
        remaining = decimal_from_order_value(order.get("volume"))
    if remaining > 0:
        return True

    origin = decimal_from_order_value(order.get("origin_volume"))
    executed = decimal_from_order_value(order.get("executed_volume"))
    if origin > executed:
        return True

    return status in {"wait", "confirm", "pending_cancel"}


def cancel_response_is_pending(result: dict[str, Any]) -> bool:
    """Return whether a cancel acknowledgement is still asynchronous."""
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    status = str(data.get("status") or data.get("state") or "").lower()
    return status == "pending_cancel"


def normalize_order_summary(order: dict[str, Any], *, market: str) -> VenueOrderSummary | None:
    """Normalize a raw Quidax order payload for API surfaces."""
    side = str(order.get("side", "")).lower()
    if side not in {"buy", "sell"}:
        return None

    price = decimal_from_order_value(order.get("price"))
    if price <= 0:
        return None

    volume = decimal_from_order_value(order.get("volume"))
    origin_volume = decimal_from_order_value(order.get("origin_volume"))
    remaining_volume = decimal_from_order_value(order.get("remaining_volume"))
    executed_volume = decimal_from_order_value(order.get("executed_volume"))

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
        market=normalize_market_id(order.get("market")) or market,
        side=side,
        status=str(order.get("state") or order.get("status") or "").lower() or None,
        price=price,
        volume=volume,
        remaining_volume=remaining_volume,
        executed_volume=executed_volume,
        notional=price * (remaining_volume if remaining_volume > 0 else volume),
        created_at=coerce_timestamp_ms(
            order.get("created_at")
            or order.get("created_at_i")
            or order.get("timestamp")
        ),
    )
