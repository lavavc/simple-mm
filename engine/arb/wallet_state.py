"""Wallet and inventory synchronization helpers for the arbitrage engine."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import structlog


logger = structlog.get_logger()


def reconcile_balances(engine: Any, balances: list[Any]) -> None:
    """Refresh per-account stablecoin and cNGN from periodic balance fetches."""
    venue_stables: dict[str, Decimal] = {}
    venue_cngn: dict[str, Decimal] = {}
    for b in balances:
        role = getattr(b, "role", "")
        tb = getattr(b, "token_balances", {})
        if role == "uni-bsc-trade":
            venue_stables["uni-bsc"] = Decimal(str(tb.get("USDT", 0)))
            venue_cngn["uni-bsc"] = Decimal(str(tb.get("cNGN", 0)))
        elif role == "uni-base-trade":
            venue_stables["uni-base"] = Decimal(str(tb.get("USDC", 0)))
            venue_cngn["uni-base"] = Decimal(str(tb.get("cNGN", 0)))
        elif role == "quidax-exchange":
            venue_stables["quidax"] = Decimal(str(tb.get("USDT", 0)))
            venue_cngn["quidax"] = Decimal(str(tb.get("cNGN", 0)))
    if venue_stables:
        engine.inventory.reconcile_stables(venue_stables)
    if venue_cngn:
        engine.inventory.reconcile_cngn(venue_cngn)


def fetch_venue_wallet_snapshot(
    engine: Any,
    venue_name: str,
) -> tuple[str, Decimal, Decimal] | None:
    """Read a venue trade wallet's live stable/cNGN balances."""
    venue = engine.venues.get(venue_name)
    if not venue:
        return None

    required_attrs = ("stable_token", "cngn_token", "trade_account", "stable_decimals", "cngn_decimals")
    if not all(hasattr(venue, attr) for attr in required_attrs):
        return None

    try:
        stable_raw = venue.stable_token.functions.balanceOf(venue.trade_account.address).call()
        cngn_raw = venue.cngn_token.functions.balanceOf(venue.trade_account.address).call()
        stable_amount = Decimal(stable_raw) / Decimal(10 ** venue.stable_decimals)
        cngn_amount = Decimal(cngn_raw) / Decimal(10 ** venue.cngn_decimals)
        return venue_name, stable_amount, cngn_amount
    except Exception as e:
        logger.warning("wallet_snapshot_refresh_failed", venue=venue_name, error=str(e))
        return None


async def refresh_inventory_for_venues(engine: Any, *venue_names: str) -> None:
    """Refresh live stable/cNGN inventory for the given venues."""
    names = sorted({name for name in venue_names if name in engine.venues})
    if not names:
        return

    loop = asyncio.get_running_loop()
    snapshots = await asyncio.gather(
        *(loop.run_in_executor(None, engine._fetch_venue_wallet_snapshot, name) for name in names),
        return_exceptions=True,
    )

    venue_stables: dict[str, Decimal] = {}
    venue_cngn: dict[str, Decimal] = {}
    for snapshot in snapshots:
        if isinstance(snapshot, BaseException):
            logger.warning("wallet_snapshot_refresh_task_failed", error=str(snapshot))
            continue
        if snapshot is None:
            continue

        venue_name, stable_amount, cngn_amount = snapshot
        venue_stables[venue_name] = stable_amount
        venue_cngn[venue_name] = cngn_amount

    if venue_stables:
        engine.inventory.reconcile_stables(venue_stables)
    if venue_cngn:
        engine.inventory.reconcile_cngn(venue_cngn)

    if venue_stables or venue_cngn:
        logger.info(
            "wallet_inventory_refreshed",
            venues=names,
            stable_balances={k: float(v) for k, v in venue_stables.items()},
            cngn_balances={k: float(v) for k, v in venue_cngn.items()},
        )


async def seed_account_inventory(engine: Any, *, ensure_approvals: bool = True) -> None:
    """Seed wallet balances, and optionally ensure trade approvals for execution paths."""
    tradeable = {
        name: venue for name, venue in engine.venues.items()
        if all(hasattr(venue, attr) for attr in (
            "stable_token", "cngn_token", "trade_account",
            "stable_decimals", "cngn_decimals", "ensure_trade_approvals",
        ))
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
