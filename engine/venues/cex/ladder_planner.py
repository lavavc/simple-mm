"""Pure helpers for ladder order planning and requote comparison."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from engine.venues.cex.order_values import decimal_from_order_value


@dataclass(frozen=True, slots=True)
class LadderOrderTarget:
    side: str
    price: Decimal
    volume: Decimal


def build_ladder_order_targets(
    *,
    reference_price: Decimal,
    offsets: list[int],
    order_size_cngn: Decimal,
    order_size_usdt: Decimal,
    format_limit_order: Callable[[str, Decimal, Decimal], tuple[Decimal, Decimal]],
) -> list[LadderOrderTarget]:
    """Build the desired per-side ladder orders around a reference price."""
    targets: list[LadderOrderTarget] = []
    for offset in offsets:
        if order_size_cngn > 0:
            buy_ngn_rate = reference_price - offset
            if buy_ngn_rate > 0:
                buy_usdt_volume = order_size_cngn / buy_ngn_rate
                price, volume = format_limit_order("buy", buy_ngn_rate, buy_usdt_volume)
                targets.append(LadderOrderTarget(side="buy", price=price, volume=volume))
        if order_size_usdt > 0:
            sell_ngn_rate = reference_price + offset
            price, volume = format_limit_order("sell", sell_ngn_rate, order_size_usdt)
            targets.append(LadderOrderTarget(side="sell", price=price, volume=volume))
    return targets


def extract_open_order_target(order: dict[str, Any]) -> LadderOrderTarget | None:
    """Convert an exchange order payload into a comparable ladder target."""
    side = str(order.get("side", "")).lower()
    if side not in {"buy", "sell"}:
        return None

    price = decimal_from_order_value(order.get("price"))
    if price <= 0:
        return None

    remaining_volume = order.get("remaining_volume")
    if remaining_volume is None:
        remaining_volume = order.get("volume")
    volume = decimal_from_order_value(remaining_volume)
    if volume <= 0:
        return None

    return LadderOrderTarget(side=side, price=price, volume=volume)


def requires_requote(
    *,
    existing_orders: list[dict[str, Any]],
    desired_orders: list[LadderOrderTarget],
    threshold_bps: Decimal,
    volume_tolerance: Decimal = Decimal("0.01"),
) -> bool:
    """Return whether the current open ladder should be cancelled and replaced."""
    current_targets = [
        target
        for order in existing_orders
        if (target := extract_open_order_target(order)) is not None
    ]
    if len(current_targets) != len(desired_orders):
        return True

    current_targets.sort(key=lambda order: (order.side, order.price, order.volume))
    desired_targets = sorted(desired_orders, key=lambda order: (order.side, order.price, order.volume))

    for current, desired in zip(current_targets, desired_targets):
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
