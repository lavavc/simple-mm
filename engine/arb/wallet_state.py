"""Wallet and inventory synchronization helpers for the arbitrage engine."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import structlog

from engine.types import TradeInventorySnapshot
from engine.venues.base import TradeInventoryVenue, is_dex_execution_venue

logger = structlog.get_logger()


def fetch_venue_wallet_snapshot(
    engine: Any,
    venue_name: str,
) -> tuple[str, TradeInventorySnapshot] | None:
    """Read a venue trade wallet's live stable/cNGN balances."""
    venue = engine.venues.get(venue_name)
    if venue is None or not is_dex_execution_venue(venue):
        return None

    observed_at = int(time.time() * 1000)
    try:
        stable_raw = venue.stable_token.functions.balanceOf(venue.trade_account.address).call()
        cngn_raw = venue.cngn_token.functions.balanceOf(venue.trade_account.address).call()
        stable_amount = Decimal(stable_raw) / Decimal(10 ** venue.stable_decimals)
        cngn_amount = Decimal(cngn_raw) / Decimal(10 ** venue.cngn_decimals)
        return venue_name, TradeInventorySnapshot(
            stable_amount, cngn_amount, stable_amount, cngn_amount, observed_at,
        )
    except Exception as e:
        logger.warning("wallet_snapshot_refresh_failed", venue=venue_name, error=str(e))
        return None


async def refresh_inventory_for_venues(engine: Any, *venue_names: str) -> None:
    """Refresh live stable/cNGN inventory for the given venues."""
    names = sorted(set(venue_names) & {"quidax", "uni-base", "uni-bsc"})
    loop = asyncio.get_running_loop()

    async def fetch(name: str) -> tuple[str, TradeInventorySnapshot] | None:
        venue = engine.venues.get(name)
        if venue is None:
            return None
        if isinstance(venue, TradeInventoryVenue):
            return name, await venue.get_trade_inventory()
        return await loop.run_in_executor(None, engine._fetch_venue_wallet_snapshot, name)

    snapshots = await asyncio.gather(*(fetch(name) for name in names), return_exceptions=True)
    for name, snapshot in zip(names, snapshots):
        if isinstance(snapshot, BaseException) or snapshot is None:
            engine.inventory.invalidate_snapshots(name)
            logger.warning("arb_inventory_refresh_failed", venue=name, error=str(snapshot))
            continue
        venue_name, balances = snapshot
        engine.inventory.set_account_snapshot(venue_name, balances)



async def seed_account_inventory(engine: Any, *, ensure_approvals: bool = True) -> None:
    """Seed wallet balances, and optionally ensure trade approvals for execution paths."""
    tradeable = {
        name: venue for name, venue in engine.venues.items()
        if is_dex_execution_venue(venue)
    }

    loop = asyncio.get_running_loop()

    def _read_balances(name: str, venue: Any) -> tuple[str, Decimal | None, Decimal | None]:
        try:
            raw_s = venue.stable_token.functions.balanceOf(venue.trade_account.address).call()
            stable = Decimal(raw_s) / Decimal(10 ** venue.stable_decimals)
        except Exception as e:
            logger.warning("account_stable_seed_failed", venue=name, error=str(e))
            stable = None
        try:
            raw_c = venue.cngn_token.functions.balanceOf(venue.trade_account.address).call()
            cngn = Decimal(raw_c) / Decimal(10 ** venue.cngn_decimals)
        except Exception as e:
            logger.warning("account_cngn_seed_failed", venue=name, error=str(e))
            cngn = None
        return name, stable, cngn

    observed_at = int(time.time() * 1000)
    balance_results = await asyncio.gather(
        *(loop.run_in_executor(None, _read_balances, name, venue) for name, venue in tradeable.items()),
        return_exceptions=True,
    )

    stable_balances: dict[str, Decimal] = {}
    cngn_balances: dict[str, Decimal] = {}
    for result in balance_results:
        if isinstance(result, BaseException):
            logger.warning("account_balance_seed_task_failed", error=str(result))
            continue
        name, stable, cngn = result
        if stable is not None:
            stable_balances[name] = stable
        if cngn is not None:
            cngn_balances[name] = cngn

    if stable_balances:
        engine.inventory.initialize_account_stable(stable_balances)
    if cngn_balances:
        engine.inventory.initialize_account_cngn(cngn_balances)
    for name in stable_balances.keys() & cngn_balances.keys():
        stable, cngn = stable_balances[name], cngn_balances[name]
        engine.inventory.set_account_snapshot(name, TradeInventorySnapshot(
            stable, cngn, stable, cngn, observed_at,
        ))
    engine._inventory_seeded = True

    if ensure_approvals:
        approvals_ok = True
        for name, venue in tradeable.items():
            try:
                await venue.ensure_trade_approvals()
            except Exception as e:
                approvals_ok = False
                logger.warning("trade_approval_failed", venue=name, error=str(e))
        engine._trade_approvals_seeded = approvals_ok
