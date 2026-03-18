"""Arbitrage engine: orchestrates CEX-DEX and DEX-DEX detection signals into execution."""

import asyncio
import time
import uuid
from decimal import Decimal
from typing import Any, Callable, Optional

import structlog

from engine.api.schemas import ArbitrageParams, ArbitrageStatus, DexArbOpportunity
from engine.core.arbitrage.executor import ArbitrageExecutor
from engine.core.arbitrage.inventory import InventoryTracker
from engine.core.arbitrage.router import RouteCandidate, SelectedRoute, select_route
from engine.db import get_db
from engine.venues.base import VenueAdapter

logger = structlog.get_logger()

# Direction → (buy_venue, buy_is_cex, sell_venue, sell_is_cex)
_CEX_DEX_DIRECTIONS = {
    "QUIDAX_TO_UNI_BSC":  ("quidax",    True,  "uni-bsc",  False),
    "UNI_BSC_TO_QUIDAX":  ("uni-bsc",   False, "quidax",   True),
    "QUIDAX_TO_UNI_BASE": ("quidax",    True,  "uni-base", False),
    "UNI_BASE_TO_QUIDAX": ("uni-base",  False, "quidax",   True),
}

# Direction → (buy_venue, sell_venue)
_DEX_DEX_DIRECTIONS = {
    "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE": ("uni-bsc", "uni-base"),
    "UNI_BASE_TO_UNI_BSC_DELTA_BALANCE": ("uni-base", "uni-bsc"),
}


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
        self.executor = ArbitrageExecutor(venues, execute_cex_dex_enabled or execute_dex_dex_enabled)

        self._enabled = True
        self._arb_executing = False
        self._inventory_seeded = False
        self._cex_curve_task: Optional[asyncio.Task] = None
        self._dex_curve_task: Optional[asyncio.Task] = None

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
        self.executor.execution_enabled = self.execute_cex_dex_enabled or self.execute_dex_dex_enabled
        logger.info("execution_cex_dex_enabled")

    def disable_execute_cex_dex(self):
        self.execute_cex_dex_enabled = False
        self.executor.execution_enabled = self.execute_cex_dex_enabled or self.execute_dex_dex_enabled
        logger.info("execution_cex_dex_disabled")

    def enable_execute_dex_dex(self):
        self.execute_dex_dex_enabled = True
        self.executor.execution_enabled = self.execute_cex_dex_enabled or self.execute_dex_dex_enabled
        logger.info("execution_dex_dex_enabled")

    def disable_execute_dex_dex(self):
        self.execute_dex_dex_enabled = False
        self.executor.execution_enabled = self.execute_cex_dex_enabled or self.execute_dex_dex_enabled
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

        self._reconcile_stables(balances)
        loop = asyncio.get_running_loop()
        signal = await loop.run_in_executor(None, find_optimal_arb, depth)
        val = await loop.run_in_executor(None, portfolio_value, depth, balances) if balances else {}

        broadcast_data = signal or {}
        broadcast_data["portfolio_value"] = val
        self.broadcast({"type": "quidax_dex_optimal_arb", "data": broadcast_data})

        if signal and self._enabled and self.execute_cex_dex_enabled and not self._arb_executing:
            candidates = []
            for arb in signal.get("all_arbs", []):
                direction = arb.get("direction")
                if direction not in _CEX_DEX_DIRECTIONS:
                    continue
                buy_venue, _, sell_venue, _ = _CEX_DEX_DIRECTIONS[direction]
                candidates.append(RouteCandidate(
                    direction=direction,
                    pipeline="cex_dex",
                    buy_venue=buy_venue,
                    sell_venue=sell_venue,
                    optimal_size_usd=Decimal(str(arb["optimal_size_usd"])),
                    expected_profit_usd=Decimal(str(arb["expected_profit_usd"])),
                    estimated_gas_usd=Decimal(str(arb.get("estimated_gas_usd", 0.005))),
                    signal={"prices": signal["prices"], "optimal_arb": arb},
                ))
            route = select_route(candidates, self.inventory)
            if route:
                opp_id = f"cex-dex-{uuid.uuid4()}"
                asyncio.create_task(self._execute_cex_dex(route, opp_id))

        if not self._cex_curve_task or self._cex_curve_task.done():
            self._cex_curve_task = asyncio.create_task(self._broadcast_cex_curve(depth))

    async def _broadcast_cex_curve(self, depth) -> None:
        """Background: compute full CEX-DEX curve and broadcast."""
        from engine.core.arbitrage.cex_dex import compute_arb_curve
        try:
            loop = asyncio.get_running_loop()
            curve = await loop.run_in_executor(None, compute_arb_curve, depth)
            if curve:
                self.broadcast({"type": "quidax_dex_arb_curve", "data": curve})
        except Exception as e:
            logger.error("cex_dex_curve_compute_failed", error=str(e))

    async def _execute_cex_dex(self, route: SelectedRoute, opp_id: str) -> None:
        """Execute a CEX-DEX arbitrage."""
        self._arb_executing = True
        try:
            c = route.candidate
            direction = c.direction
            buy_venue_name, buy_is_cex, sell_venue_name, sell_is_cex = _CEX_DEX_DIRECTIONS[direction]
            size_usd = route.adjusted_size_usd
            slippage_bps = c.signal["optimal_arb"].get("slippage_tolerance_bps", 10)
            min_out_usd = size_usd * (1 - Decimal(str(slippage_bps)) / 10000)
            quidax_price = Decimal(str(c.signal["prices"]["quidax"]))

            self.inventory.record_trade_start(opp_id, size_usd, buy_venue_name, sell_venue_name)

            buy_trade = (
                await self.executor.execute_cex_buy(buy_venue_name, size_usd, quidax_price, opp_id)
                if buy_is_cex else
                await self.executor.execute_dex_buy(buy_venue_name, size_usd, opp_id)
            )

            if not buy_trade or buy_trade.status == "failed":
                err = (buy_trade.error if buy_trade else None) or "buy failed"
                logger.error("cex_dex_buy_failed", direction=direction, error=err)
                self.inventory.record_trade_failure(opp_id, err)
                return

            sell_trade = (
                await self.executor.execute_cex_sell(sell_venue_name, buy_trade.amount, quidax_price, opp_id)
                if sell_is_cex else
                await self.executor.execute_dex_sell(sell_venue_name, buy_trade.amount, min_out_usd, opp_id)
            )

            if not sell_trade or sell_trade.status == "failed":
                err = (sell_trade.error if sell_trade else None) or "sell failed"
                buy_tx = buy_trade.tx_hash or ""
                logger.error("cex_dex_half_open", direction=direction, buy_tx=buy_tx, sell_error=err)
                self.inventory.record_trade_failure(opp_id, f"HALF_OPEN:{buy_tx}:{err}")
                self.broadcast({"type": "alert", "severity": "critical",
                               "message": f"Half-open CEX-DEX arb {opp_id}: buy {buy_tx} ok, sell failed: {err}"})
                return

            actual_profit = (
                sell_trade.amount * (sell_trade.price or quidax_price)
                - buy_trade.amount * (buy_trade.price or quidax_price)
            )
            self.inventory.record_trade_complete(opp_id, size_usd, actual_profit, Decimal("0"))
            self.broadcast({"type": "arb_executed", "data": {
                "id": opp_id, "direction": direction, "profit_usd": float(actual_profit),
            }})
            logger.info("cex_dex_arb_executed", opp_id=opp_id, direction=direction,
                        profit_usd=float(actual_profit))

        except Exception as e:
            logger.error("cex_dex_execution_error", opp_id=opp_id, error=str(e))
            self.inventory.record_trade_failure(opp_id, str(e))
        finally:
            self._arb_executing = False

    # ------------------------------------------------------------------
    # DEX-DEX pipeline
    # ------------------------------------------------------------------

    async def on_dex_dex_update(self) -> None:
        """
        Entry point for DEX-DEX arb. Called by scheduler (timer) and listener (swap events).
        Computes optimal arb, broadcasts, records opportunity, optionally executes.
        Spawns background curve task.
        """
        from engine.core.arbitrage.dex_dex import find_optimal_dex_arb
        from engine.core.arbitrage.pool_state import seed_pool_states

        if not self._inventory_seeded:
            await self._seed_account_inventory()

        loop = asyncio.get_running_loop()
        fast = await loop.run_in_executor(None, find_optimal_dex_arb)
        if fast is None:
            asyncio.create_task(seed_pool_states())
            return

        opp_id = await self._record_dex_opportunity(fast)

        if self._enabled and self.execute_dex_dex_enabled and not self._arb_executing:
            optimal = fast.get("optimal_arb", {})
            direction = optimal.get("direction")
            if direction in _DEX_DEX_DIRECTIONS and optimal.get("expected_profit_usd", 0) > 0:
                buy_venue, sell_venue = _DEX_DEX_DIRECTIONS[direction]
                candidate = RouteCandidate(
                    direction=direction,
                    pipeline="dex_dex",
                    buy_venue=buy_venue,
                    sell_venue=sell_venue,
                    optimal_size_usd=Decimal(str(optimal["optimal_size_usd"])),
                    expected_profit_usd=Decimal(str(optimal["expected_profit_usd"])),
                    estimated_gas_usd=Decimal(str(optimal.get("estimated_gas_usd", 0.008))),
                    signal=fast,
                )
                route = select_route([candidate], self.inventory)
                if route:
                    asyncio.create_task(self._execute_dex_dex(route, opp_id))

        if not self._dex_curve_task or self._dex_curve_task.done():
            self._dex_curve_task = asyncio.create_task(self._broadcast_dex_curve())

    async def _record_dex_opportunity(self, fast: dict) -> str:
        """Persist the DEX-DEX opportunity to DB and broadcast it. Returns opp_id."""
        optimal = fast.get("optimal_arb", {})
        if optimal.get("expected_profit_usd", -1) <= 0:
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
                estimated_gas_usd=optimal.get("estimated_gas_usd"),
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
                for key in ("uni-bsc", "uni-base", "assetchain"):
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

    async def _execute_dex_dex(self, route: SelectedRoute, opp_id: str) -> None:
        """Execute a DEX-DEX delta-balance arbitrage."""
        self._arb_executing = True
        try:
            c = route.candidate
            optimal = c.signal["optimal_arb"]
            direction = c.direction
            buy_venue_name, sell_venue_name = _DEX_DEX_DIRECTIONS[direction]
            size_usd = route.adjusted_size_usd
            slippage_bps = optimal.get("slippage_tolerance_bps", 10)
            min_out_usd = size_usd * (1 - Decimal(str(slippage_bps)) / 10000)

            self.inventory.record_trade_start(opp_id, size_usd, buy_venue_name, sell_venue_name)

            db = await get_db()
            await db._conn.execute(
                "UPDATE dex_arbitrage_opportunities SET status = 'executing' WHERE id = ?",
                (opp_id,)
            )
            await db._conn.commit()

            buy_trade = await self.executor.execute_dex_buy(buy_venue_name, size_usd, opp_id)

            if not buy_trade or buy_trade.status == "failed":
                err = (buy_trade.error if buy_trade else None) or "buy failed"
                logger.error("dex_dex_buy_failed", direction=direction, error=err)
                self.inventory.record_trade_failure(opp_id, err)
                await db.expire_old_dex_arbitrage_opportunities(0)  # mark abandoned via expiry
                return

            sell_trade = await self.executor.execute_dex_sell(
                sell_venue_name, buy_trade.amount, min_out_usd, opp_id
            )

            if not sell_trade or sell_trade.status == "failed":
                err = (sell_trade.error if sell_trade else None) or "sell failed"
                buy_tx = buy_trade.tx_hash or ""
                logger.error("dex_dex_half_open", direction=direction, buy_tx=buy_tx, sell_error=err)
                self.inventory.record_trade_failure(opp_id, f"HALF_OPEN:{buy_tx}:{err}")
                self.broadcast({"type": "alert", "severity": "critical",
                               "message": f"Half-open DEX-DEX arb {opp_id}: buy {buy_tx} ok, sell failed: {err}"})
                return

            actual_profit = sell_trade.amount * (sell_trade.price or Decimal("0.0006")) - size_usd
            self.inventory.record_trade_complete(opp_id, size_usd, actual_profit, Decimal("0"))
            self.broadcast({"type": "dex_arb_executed", "data": {
                "id": opp_id, "direction": direction, "profit_usd": float(actual_profit),
            }})
            logger.info("dex_dex_arb_executed", opp_id=opp_id, direction=direction,
                        profit_usd=float(actual_profit))

        except Exception as e:
            logger.error("dex_dex_execution_error", opp_id=opp_id, error=str(e))
            self.inventory.record_trade_failure(opp_id, str(e))
        finally:
            self._arb_executing = False

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

    def update_portfolio_snapshot(self, cngn_value_usd: Decimal, total_usd: Decimal):
        self.inventory.update_portfolio_snapshot(cngn_value_usd, total_usd)

    def _reconcile_stables(self, balances: list) -> None:
        """Refresh per-account stablecoin from the scheduler's periodic balance fetch."""
        venue_stables: dict[str, Decimal] = {}
        for b in balances:
            role = getattr(b, "role", "")
            tb = getattr(b, "token_balances", {})
            if role in ("uni-bsc-trade", "trade_uni_bsc"):
                venue_stables["uni-bsc"] = Decimal(str(tb.get("USDT", 0)))
            elif role in ("uni-base-trade", "trade_uni_base"):
                venue_stables["uni-base"] = Decimal(str(tb.get("USDC", 0)))
            elif role == "quidax-exchange":
                venue_stables["quidax"] = Decimal(str(tb.get("USDT", 0)))
        if venue_stables:
            self.inventory.reconcile_stables(venue_stables)

    async def _seed_account_inventory(self):
        """Read trade-account stablecoin balances and pre-approve routers at first run."""
        from engine.venues.dex.base import BaseDexAdapter
        balances: dict[str, Decimal] = {}
        for name, venue in self.venues.items():
            if isinstance(venue, BaseDexAdapter):
                try:
                    raw = venue.stable_token.functions.balanceOf(venue.trade_account.address).call()
                    balances[name] = Decimal(raw) / Decimal(10 ** venue.stable_decimals)
                except Exception as e:
                    logger.warning("account_stable_seed_failed", venue=name, error=str(e))
                try:
                    await venue.ensure_trade_approvals()
                except Exception as e:
                    logger.warning("trade_approval_failed", venue=name, error=str(e))
        if balances:
            self.inventory.initialize_account_stable(balances)
        self._inventory_seeded = True
