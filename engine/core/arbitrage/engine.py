"""Arbitrage engine: orchestrates CEX-DEX and DEX-DEX detection signals into execution."""

import asyncio
import time
import uuid
from decimal import Decimal
from typing import Any, Callable, Optional

import structlog

from engine.api.schemas import ArbitrageParams, ArbitrageStatus, DexArbOpportunity
from engine.core.arbitrage.executor import ArbitrageExecutor
from engine.core.arbitrage.history import ArbitrageHistoryRecorder
from engine.core.arbitrage.inventory import InventoryTracker
from engine.core.arbitrage.preflight import _coerce_decimal, _handle_preflight_error
from engine.core.arbitrage.recovery import (
    _recover_dex_half_open_inner as _recover_dex_half_open_inner_impl,
    recover_cex_half_open as _recover_cex_half_open_impl,
    recover_dex_half_open as _recover_dex_half_open_impl,
)
from engine.core.arbitrage.route_execution import execute_route as _execute_route_impl
from engine.core.arbitrage.route_registry import ROUTES, ROUTES_BY_DIRECTION, TradeRoute
from engine.core.arbitrage.router import RouteCandidate, SelectedRoute, select_route
from engine.core.arbitrage.wallet_state import (
    fetch_venue_wallet_snapshot as _fetch_venue_wallet_snapshot_impl,
    reconcile_balances as _reconcile_balances_impl,
    refresh_inventory_for_venues as _refresh_inventory_for_venues_impl,
    seed_account_inventory as _seed_account_inventory_impl,
)
from engine.db import get_db
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()


class ArbitrageEngine:
    """
    Orchestrates arbitrage detection signals into execution.

    Receives signals from the scheduler (which owns the detection polling):
    - on_cex_dex_depth(): handles CEX-DEX signals from Quidax order book
    - on_dex_dex_update(): handles DEX-DEX signals from V4 pool state

    Each method: computes signal → broadcasts → optionally executes → records.
    """

    def __init__(
        self,
        venues: dict[str, VenueAdapter],
        params: ArbitrageParams,
        broadcast: Callable[[dict], Any],
        execute_cex_dex_enabled: bool = False,
        execute_dex_dex_enabled: bool = False,
    ):
        self.venues = venues
        self.params = params
        self.broadcast = broadcast
        self.execute_cex_dex_enabled = execute_cex_dex_enabled
        self.execute_dex_dex_enabled = execute_dex_dex_enabled

        self.inventory = InventoryTracker(params)
        self.executor = ArbitrageExecutor(venues)
        self.history = ArbitrageHistoryRecorder(self.inventory, broadcast, db_getter=lambda: get_db())

        self._enabled = True
        self._arb_executing = False
        self._inventory_seeded = False
        self._trade_approvals_seeded = False
        self._cex_curve_task: Optional[asyncio.Task] = None
        self._dex_curve_task: Optional[asyncio.Task] = None
        self._pool_seed_task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True
        logger.info("arbitrage_engine_enabled")

    def disable(self):
        self._enabled = False
        logger.info("arbitrage_engine_disabled")

    def enable_execute_cex_dex(self):
        self.execute_cex_dex_enabled = True
        logger.info("execution_cex_dex_enabled")

    def disable_execute_cex_dex(self):
        self.execute_cex_dex_enabled = False
        logger.info("execution_cex_dex_disabled")

    def enable_execute_dex_dex(self):
        self.execute_dex_dex_enabled = True
        logger.info("execution_dex_dex_enabled")

    def disable_execute_dex_dex(self):
        self.execute_dex_dex_enabled = False
        logger.info("execution_dex_dex_disabled")

    # ------------------------------------------------------------------
    # CEX-DEX pipeline
    # ------------------------------------------------------------------

    async def on_cex_dex_depth(self, depth, balances: list) -> None:
        """
        Entry point for CEX-DEX arb. Called by scheduler on every Quidax depth update.
        Computes optimal arb + portfolio valuation, broadcasts both, optionally executes.
        """
        from engine.core.arbitrage.cex_dex import find_optimal_arb
        from engine.core.arbitrage.valuation import portfolio_value
        from engine.core.arbitrage.pool_state import seed_dex_pool_states

        self._reconcile_balances(balances)
        loop = asyncio.get_running_loop()
        signal = await loop.run_in_executor(None, find_optimal_arb, depth)
        val = await loop.run_in_executor(None, portfolio_value, depth, balances) if balances else {}

        if signal is None:
            # Pool cache is cold — kick off a one-shot seed so the next depth
            # tick (2 s later) has pool state to work with.
            if not self._pool_seed_task or self._pool_seed_task.done():
                self._pool_seed_task = asyncio.create_task(seed_dex_pool_states())

        broadcast_data = signal or {}
        broadcast_data["portfolio_value"] = val
        self.broadcast({"type": "quidax_dex_optimal_arb", "data": broadcast_data})

        if signal and self._enabled and self.execute_cex_dex_enabled and not self._arb_executing:
            candidates = []
            for arb in signal.get("all_arbs", []):
                direction = arb.get("direction")
                route_def = ROUTES_BY_DIRECTION.get(direction)
                if not route_def:
                    continue
                gas_usd_raw = arb.get("gas_usd")
                if not gas_usd_raw:
                    logger.warning("cex_dex_candidate_skipped_missing_gas", direction=direction)
                    continue
                candidates.append(RouteCandidate(
                    direction=direction,
                    buy_venue=route_def.buy_leg.venue,
                    sell_venue=route_def.sell_leg.venue,
                    optimal_size_usd=Decimal(str(arb["optimal_size_usd"])),
                    expected_profit_usd=Decimal(str(arb["expected_profit_usd"])),
                    gas_usd=Decimal(str(gas_usd_raw)),
                    signal={"prices": signal["prices"], "optimal_arb": arb, "depth": {depth.venue: depth}},
                ))
            route = select_route(candidates, self.inventory)
            if route:
                opp_id = f"cex-dex-{uuid.uuid4()}"
                asyncio.create_task(self._execute_route(ROUTES_BY_DIRECTION[route.candidate.direction], route, opp_id))

        if not self._cex_curve_task or self._cex_curve_task.done():
            self._cex_curve_task = asyncio.create_task(self._broadcast_cex_curve(depth, signal))

    async def _broadcast_cex_curve(self, depth, signal) -> None:
        """Background: compute full CEX-DEX curve and broadcast."""
        from engine.core.arbitrage.cex_dex import compute_arb_curve
        from engine.core.arbitrage.pool_state import seed_pool_states
        try:
            loop = asyncio.get_running_loop()
            curve = await loop.run_in_executor(None, compute_arb_curve, depth)
            if curve:
                curve["optimal_arb"] = signal.get("optimal_arb") if signal else None
                curve["all_arbs"] = signal.get("all_arbs", []) if signal else []
                self.broadcast({"type": "quidax_dex_arb_curve", "data": curve})
            else:
                # Pool cache miss — seed so the next curve attempt succeeds.
                if not self._pool_seed_task or self._pool_seed_task.done():
                    self._pool_seed_task = asyncio.create_task(seed_pool_states())
                logger.warning("cex_dex_curve_pool_cache_cold_seeding")
        except Exception as e:
            logger.error("cex_dex_curve_compute_failed", error=str(e))

    async def _execute_route(self, route_def: TradeRoute, route: SelectedRoute, opp_id: str) -> None:
        await _execute_route_impl(self, route_def, route, opp_id)

    # ------------------------------------------------------------------
    # DEX-DEX pipeline
    # ------------------------------------------------------------------

    async def on_dex_dex_update(self) -> None:
        """
        Entry point for DEX-DEX arb. Called by scheduler fallback and WS-driven signals.
        Computes optimal arb, broadcasts, records opportunity, optionally executes.
        Spawns background curve task.
        """
        from engine.core.arbitrage.dex_dex import find_optimal_dex_arb
        from engine.core.arbitrage.pool_state import seed_dex_pool_states

        if not self._inventory_seeded or not self._trade_approvals_seeded:
            await self._seed_account_inventory()

        loop = asyncio.get_running_loop()
        fast = await loop.run_in_executor(None, find_optimal_dex_arb)
        if fast is None:
            asyncio.create_task(seed_dex_pool_states())
            return

        opp_id = await self._record_dex_opportunity(fast)

        if self._enabled and self.execute_dex_dex_enabled and not self._arb_executing:
            optimal = fast.get("optimal_arb", {})
            direction = optimal.get("direction")
            route_def = ROUTES_BY_DIRECTION.get(direction)
            if route_def and optimal.get("expected_profit_usd", 0) > 0:
                gas_usd_raw = optimal.get("gas_usd")
                if not gas_usd_raw:
                    logger.warning("dex_dex_candidate_skipped_missing_gas", direction=direction)
                    return
                candidate = RouteCandidate(
                    direction=direction,
                    buy_venue=route_def.buy_leg.venue,
                    sell_venue=route_def.sell_leg.venue,
                    optimal_size_usd=Decimal(str(optimal["optimal_size_usd"])),
                    expected_profit_usd=Decimal(str(optimal["expected_profit_usd"])),
                    gas_usd=Decimal(str(gas_usd_raw)),
                    signal=fast,
                )
                route = select_route([candidate], self.inventory)
                if route:
                    asyncio.create_task(self._execute_route(route_def, route, opp_id))

        if not self._dex_curve_task or self._dex_curve_task.done():
            self._dex_curve_task = asyncio.create_task(self._broadcast_dex_curve())

    async def on_wallet_activity(self, venue_names: list[str]) -> None:
        """Refresh executable wallet inventory for affected DEX venues."""
        if not venue_names:
            return

        if not self._inventory_seeded:
            await self._seed_account_inventory(ensure_approvals=False)
            return

        await self._refresh_inventory_for_venues(*venue_names)

    async def _record_dex_opportunity(self, fast: dict) -> str:
        """Persist the DEX-DEX opportunity to DB and broadcast it. Returns opp_id."""
        from engine.config import settings
        optimal = fast.get("optimal_arb", {})
        if optimal.get("expected_profit_usd", -1) < settings.arbitrage_min_profit_usd:
            return f"dex-arb-{uuid.uuid4()}"

        db = await get_db()
        cutoff_ts = int(time.time() * 1000) - 60000
        await db.expire_old_dex_arbitrage_opportunities(cutoff_ts)

        direction = optimal["direction"]
        existing = await db._conn.execute(
            "SELECT id FROM dex_arbitrage_opportunities "
            "WHERE status IN ('detected', 'executing') AND direction = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (direction,)
        )
        row = await existing.fetchone()

        if row:
            opp_id = row["id"]
        else:
            opp_id = f"dex-arb-{uuid.uuid4()}"
            opportunity = DexArbOpportunity(
                id=opp_id,
                timestamp=int(time.time() * 1000),
                direction=direction,
                optimal_size_usd=optimal["optimal_size_usd"],
                expected_profit_usd=optimal["expected_profit_usd"],
                cngn_transferred=optimal["cngn_transferred"],
                expected_usd_out=optimal["expected_usd_out"],
                status="detected",
                net_spread_bps=optimal.get("net_spread_bps", 0),
                uni_bsc_price=fast.get("prices", {}).get("uni-bsc"),
                uni_base_price=fast.get("prices", {}).get("uni-base"),
                slippage_tolerance_bps=optimal.get("slippage_tolerance_bps"),
                uni_bsc_fee_bps=optimal.get("uni_bsc_fee_bps"),
                uni_base_fee_bps=optimal.get("uni_base_fee_bps"),
                gas_usd=optimal.get("gas_usd"),
            )
            await db.insert_dex_arbitrage_opportunity(opportunity)

        broadcast_data = {**optimal, "id": opp_id}
        self.broadcast({"type": "dex_arb_opportunity", "data": broadcast_data})
        return opp_id

    async def _broadcast_dex_curve(self) -> None:
        """Background: compute full DEX-DEX curve and broadcast."""
        from engine.core.arbitrage.dex_dex import generate_dex_profit_curve
        try:
            loop = asyncio.get_running_loop()
            curve_data = await loop.run_in_executor(None, generate_dex_profit_curve)
            if curve_data:
                # Persist pool prices to DB for history charts
                from engine.api.schemas import PriceQuote
                db = await get_db()
                now_ms = int(time.time() * 1000)
                for key in ("uni-bsc", "uni-base"):
                    price_val = curve_data.get("prices", {}).get(key)
                    if price_val is not None:
                        await db.insert_price_snapshot(PriceQuote(
                            source=f"{key}_pool",
                            timestamp=now_ms,
                            bid=price_val,
                            ask=price_val,
                            mid=price_val,
                        ))
                self.broadcast({"type": "dex_arb_curve", "data": curve_data})
        except Exception as e:
            logger.error("dex_curve_compute_failed", error=str(e))

    # ------------------------------------------------------------------
    # Status, params, risk
    # ------------------------------------------------------------------

    async def get_status(self) -> ArbitrageStatus:
        db = await get_db()
        now = int(time.time() * 1000)
        stats = await db.get_arbitrage_stats(now - 86400000)
        inv = self.inventory.get_status_dict()

        return ArbitrageStatus(
            enabled=self._enabled,
            execute_cex_dex=self.execute_cex_dex_enabled,
            execute_dex_dex=self.execute_dex_dex_enabled,
            last_scan_timestamp=None,
            opportunities_detected_24h=stats["opportunities_detected"],
            opportunities_executed_24h=stats["opportunities_executed"],
            total_profit_24h_usd=stats["total_profit_usd"],
            daily_volume_usd=inv["daily_volume_usd"],
            inventory_imbalance_usd=inv["cngn_imbalance_usd"],
            circuit_breaker_active=inv["circuit_breaker_active"],
            consecutive_failures=inv["consecutive_failures"],
            params=self.params,
            low_inventory_venues=inv["low_inventory_venues"],
        )

    def update_params(self, params: ArbitrageParams):
        self.params = params
        self.inventory.params = params
        logger.info("arbitrage_params_updated")

    def reset_circuit_breaker(self):
        self.inventory.reset_circuit_breaker()

    def update_portfolio_snapshot(self, cngn_value_usd: Decimal, total_usd: Decimal, cngn_price_usd: Decimal = Decimal("0")):
        self.inventory.update_portfolio_snapshot(cngn_value_usd, total_usd, cngn_price_usd)

    async def _get_db(self):
        return await get_db()

    async def recover_dex_half_open(self, opp_id: str) -> dict:
        return await _recover_dex_half_open_impl(self, opp_id)

    async def _recover_dex_half_open_inner(self, opp_id: str) -> dict:
        return await _recover_dex_half_open_inner_impl(self, opp_id)

    async def recover_cex_half_open(self, opp_id: str) -> dict:
        return await _recover_cex_half_open_impl(self, opp_id)

    def _reconcile_balances(self, balances: list) -> None:
        _reconcile_balances_impl(self, balances)

    def _fetch_venue_wallet_snapshot(self, venue_name: str) -> tuple[str, Decimal, Decimal] | None:
        return _fetch_venue_wallet_snapshot_impl(self, venue_name)

    async def _refresh_inventory_for_venues(self, *venue_names: str) -> None:
        await _refresh_inventory_for_venues_impl(self, *venue_names)

    async def _seed_account_inventory(self, *, ensure_approvals: bool = True):
        await _seed_account_inventory_impl(self, ensure_approvals=ensure_approvals)
