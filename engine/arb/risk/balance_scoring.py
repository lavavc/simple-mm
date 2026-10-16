"""Venue-local inventory preference, separate from monetary profit."""

from dataclasses import dataclass
from decimal import Decimal

from engine.arb.risk.inventory import InventoryTracker
from engine.types import TradeInventorySnapshot


@dataclass(frozen=True)
class AccountProjection:
    venue: str
    before_ratio: Decimal
    after_ratio: Decimal
    improvement_usd: Decimal


def _projection(
    venue: str,
    snapshot: TradeInventorySnapshot,
    price: Decimal,
    target: Decimal,
    stable_delta: Decimal,
    cngn_delta: Decimal,
    max_imbalance_usd: Decimal,
) -> AccountProjection | None:
    before_stable = snapshot.stable_total
    before_cngn = snapshot.cngn_total * price
    after_stable = before_stable + stable_delta
    after_cngn = before_cngn + cngn_delta * price
    before_total = before_stable + before_cngn
    after_total = after_stable + after_cngn
    if min(after_stable, after_cngn) < 0 or min(before_total, after_total) <= 0:
        return None
    before_ratio = before_cngn / before_total
    after_ratio = after_cngn / after_total
    before_deviation = abs(before_cngn - target * before_total)
    after_deviation = abs(after_cngn - target * after_total)
    if after_deviation > max_imbalance_usd and after_deviation > before_deviation:
        return None
    before_cost = before_total * (before_ratio - target) ** 2
    after_cost = after_total * (after_ratio - target) ** 2
    return AccountProjection(venue, before_ratio, after_ratio, before_cost - after_cost)


def project_route(
    inventory: InventoryTracker,
    buy_venue: str,
    sell_venue: str,
    prices: dict[str, Decimal],
    size_usd: Decimal,
    cngn: Decimal,
    usd_out: Decimal,
) -> tuple[AccountProjection, AccountProjection] | None:
    targets = {
        "quidax": inventory.params.quidax_target_cngn_ratio,
        "uni-base": inventory.params.uni_base_target_cngn_ratio,
        "uni-bsc": inventory.params.uni_bsc_target_cngn_ratio,
    }
    projections = []
    for venue, stable_delta, cngn_delta in (
        (buy_venue, -size_usd, cngn),
        (sell_venue, usd_out, -cngn),
    ):
        snapshot = inventory.fresh_snapshot(venue)
        price = prices.get(venue)
        if snapshot is None or price is None or not price.is_finite() or price <= 0:
            return None
        projection = _projection(
            venue,
            snapshot,
            price,
            targets[venue],
            stable_delta,
            cngn_delta,
            inventory.params.max_inventory_imbalance_usd,
        )
        if projection is None:
            return None
        projections.append(projection)
    return projections[0], projections[1]
