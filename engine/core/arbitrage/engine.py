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
        self.executor = ArbitrageExecutor(venues)

        self._enabled = True
        self._arb_executing = False
        self._inventory_seeded = False
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
        from engine.core.arbitrage.pool_state import seed_pool_states

        self._reconcile_balances(balances)
        loop = asyncio.get_running_loop()
        signal = await loop.run_in_executor(None, find_optimal_arb, depth)
        val = await loop.run_in_executor(None, portfolio_value, depth, balances) if balances else {}

        if signal is None:
            # Pool cache is cold — kick off a one-shot seed so the next depth
            # tick (2 s later) has pool state to work with.
            if not self._pool_seed_task or self._pool_seed_task.done():
                self._pool_seed_task = asyncio.create_task(seed_pool_states())

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
                gas_usd_raw = arb.get("gas_usd")
                if not gas_usd_raw:
                    logger.warning("cex_dex_candidate_skipped_missing_gas", direction=direction)
                    continue
                candidates.append(RouteCandidate(
                    direction=direction,
                    pipeline="cex_dex",
                    buy_venue=buy_venue,
                    sell_venue=sell_venue,
                    optimal_size_usd=Decimal(str(arb["optimal_size_usd"])),
                    expected_profit_usd=Decimal(str(arb["expected_profit_usd"])),
                    gas_usd=Decimal(str(gas_usd_raw)),
                    signal={"prices": signal["prices"], "optimal_arb": arb},
                ))
            route = select_route(candidates, self.inventory)
            if route:
                opp_id = f"cex-dex-{uuid.uuid4()}"
                asyncio.create_task(self._execute_cex_dex(route, opp_id))

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

    async def _execute_cex_dex(self, route: SelectedRoute, opp_id: str) -> None:
        """Execute a CEX-DEX arbitrage."""
        self._arb_executing = True
        db = await get_db()
        try:
            c = route.candidate
            direction = c.direction
            buy_venue_name, buy_is_cex, sell_venue_name, sell_is_cex = _CEX_DEX_DIRECTIONS[direction]
            size_usd = route.adjusted_size_usd
            slippage_bps = c.signal["optimal_arb"].get("slippage_tolerance_bps", 10)
            min_out_usd = size_usd * (1 - Decimal(str(slippage_bps)) / 10000)
            quidax_price = Decimal(str(c.signal["prices"]["quidax"]))
            net_spread_bps = c.signal["optimal_arb"].get("net_spread_bps", 0)

            # If the sell leg is a DEX, simulate it before placing the CEX buy.
            # A failed DEX sell after a confirmed CEX buy would leave us half-open with no recovery path.
            if not sell_is_cex:
                from engine.core.arbitrage.executor import _clean_revert
                loop = asyncio.get_running_loop()
                sell_venue = self.venues[sell_venue_name]
                cngn_estimate = int(size_usd / quidax_price * Decimal(10 ** sell_venue.cngn_decimals))
                sell_err = await loop.run_in_executor(
                    None, sell_venue.simulate_swap, sell_venue.cngn_address, cngn_estimate, 0
                )
                if sell_err:
                    self.inventory.reconcile_cngn({sell_venue_name: Decimal("0")})
                    logger.warning("cex_dex_sell_preflight_failed", direction=direction,
                                   sell_venue=sell_venue_name, error=_clean_revert(sell_err))
                    return

            from engine.api.schemas import ArbitrageOpportunity as ArbOpp
            await db.insert_arbitrage_opportunity(ArbOpp(
                id=opp_id,
                timestamp=int(time.time() * 1000),
                buy_venue=buy_venue_name,
                sell_venue=sell_venue_name,
                buy_price=quidax_price,
                sell_price=quidax_price,
                gross_spread_bps=net_spread_bps,
                net_spread_bps=net_spread_bps,
                recommended_size_usd=size_usd,
                expected_profit_usd=c.expected_profit_usd,
                status="executing",
            ))

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
                await db.update_arbitrage_opportunity(opp_id, status="abandoned", reason=err)
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
                await db.update_arbitrage_opportunity(opp_id, status="abandoned",
                                                      reason=f"HALF_OPEN:{buy_tx}:{err}")
                self.broadcast({"type": "alert", "severity": "critical",
                               "message": f"Half-open CEX-DEX arb {opp_id}: buy {buy_tx} ok, sell failed: {err}"})
                return

            actual_profit = (
                sell_trade.amount * (sell_trade.price or quidax_price)
                - buy_trade.amount * (buy_trade.price or quidax_price)
            )
            await db.update_arbitrage_opportunity(opp_id, status="completed",
                                                  actual_profit_usd=float(actual_profit))
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
                gas_usd_raw = optimal.get("gas_usd")
                if not gas_usd_raw:
                    logger.warning("dex_dex_candidate_skipped_missing_gas", direction=direction)
                    return
                candidate = RouteCandidate(
                    direction=direction,
                    pipeline="dex_dex",
                    buy_venue=buy_venue,
                    sell_venue=sell_venue,
                    optimal_size_usd=Decimal(str(optimal["optimal_size_usd"])),
                    expected_profit_usd=Decimal(str(optimal["expected_profit_usd"])),
                    gas_usd=Decimal(str(gas_usd_raw)),
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
        buy_trade = None
        direction = route.candidate.direction
        try:
            c = route.candidate
            optimal = c.signal["optimal_arb"]
            execution = route.execution_signal or optimal
            buy_venue_name, sell_venue_name = _DEX_DEX_DIRECTIONS[direction]
            size_usd = route.adjusted_size_usd
            slippage_bps = optimal.get("slippage_tolerance_bps", 10)
            min_out_usd = size_usd * (1 - Decimal(str(slippage_bps)) / 10000)

            # Simulate both legs via eth_call before executing either.
            # Buy (chain A) and sell (chain B) are on different chains — sell-side state is
            # independent of the buy, so the simulation is valid through execution.
            # Catches insufficient balance, missing approvals, and any other revert before
            # we commit to the buy leg.
            from engine.core.arbitrage.executor import _clean_revert
            loop = asyncio.get_running_loop()
            buy_venue = self.venues[buy_venue_name]
            sell_venue = self.venues[sell_venue_name]
            planned_sell_cngn = Decimal(str(execution.get("cngn_transferred", "0")))
            sell_amount_raw = int(planned_sell_cngn * Decimal(10 ** sell_venue.cngn_decimals))
            buy_amount_raw = int(size_usd * Decimal(10 ** buy_venue.stable_decimals))
            min_out_raw = int(min_out_usd * Decimal(10 ** sell_venue.stable_decimals))

            sell_err = await loop.run_in_executor(
                None, sell_venue.simulate_swap, sell_venue.cngn_address, sell_amount_raw, min_out_raw
            )
            if sell_err:
                # Mark sell-side cNGN as zero so the router rejects this route on every
                # subsequent tick until the scheduler's balance cycle restores the real value.
                self.inventory.reconcile_cngn({sell_venue_name: Decimal("0")})
                logger.warning(
                    "dex_dex_sell_preflight_failed",
                    direction=direction,
                    buy_venue=buy_venue_name,
                    sell_venue=sell_venue_name,
                    market_optimal_size_usd=float(c.optimal_size_usd),
                    executable_size_usd=float(size_usd),
                    planned_sell_cngn=float(planned_sell_cngn),
                    min_out_usd=float(min_out_usd),
                    error=_clean_revert(sell_err),
                )
                return

            buy_err = await loop.run_in_executor(
                None, buy_venue.simulate_swap, buy_venue.stable_address, buy_amount_raw, 0
            )
            if buy_err:
                logger.warning(
                    "dex_dex_buy_preflight_failed",
                    direction=direction,
                    buy_venue=buy_venue_name,
                    sell_venue=sell_venue_name,
                    market_optimal_size_usd=float(c.optimal_size_usd),
                    executable_size_usd=float(size_usd),
                    planned_sell_cngn=float(planned_sell_cngn),
                    error=_clean_revert(buy_err),
                )
                return

            self.inventory.record_trade_start(opp_id, size_usd, buy_venue_name, sell_venue_name)

            db = await get_db()
            await db._conn.execute(
                "UPDATE dex_arbitrage_opportunities SET status = 'executing' WHERE id = ?",
                (opp_id,)
            )
            await db._conn.commit()
            await db.update_dex_arbitrage_execution_state(
                opp_id,
                status="executing",
                planned_sell_cngn=planned_sell_cngn,
            )

            buy_trade = await self.executor.execute_dex_buy(buy_venue_name, size_usd, opp_id)

            if not buy_trade or buy_trade.status == "failed":
                err = (buy_trade.error if buy_trade else None) or "buy failed"
                logger.error("dex_dex_buy_failed", direction=direction, error=err)
                self.inventory.record_trade_failure(opp_id, err)
                await db.expire_old_dex_arbitrage_opportunities(0)  # mark abandoned via expiry
                return

            await db.update_dex_arbitrage_execution_state(
                opp_id,
                status="buy_filled",
                buy_tx_hash=buy_trade.tx_hash,
                buy_amount_cngn=buy_trade.amount,
            )

            # Sell the cNGN actually received from the buy, not the pre-buy estimate.
            sell_trade = await self.executor.execute_dex_sell(
                sell_venue_name, buy_trade.amount, min_out_usd, opp_id
            )

            if not sell_trade or sell_trade.status == "failed":
                err = (sell_trade.error if sell_trade else None) or "sell failed"
                buy_tx = buy_trade.tx_hash or ""
                sell_account = getattr(getattr(self.venues.get(sell_venue_name), "trade_account", None), "address", "unknown")
                logger.error("dex_dex_half_open", direction=direction, buy_tx=buy_tx, sell_error=err,
                             sell_venue=sell_venue_name, sell_account=sell_account)
                await db.update_dex_arbitrage_execution_state(
                    opp_id,
                    status="half_open",
                    buy_tx_hash=buy_tx or None,
                    sell_tx_hash=sell_trade.tx_hash if sell_trade else None,
                    reason=err,
                )
                self.inventory.trip_circuit_breaker(f"Half-open DEX-DEX arb: {opp_id}")
                self.inventory.record_trade_failure(opp_id, f"HALF_OPEN:{buy_tx}:{err}")
                self.broadcast({"type": "alert", "severity": "critical",
                               "message": (
                                   f"Half-open DEX-DEX arb {opp_id} ({direction}): "
                                   f"buy on {buy_venue_name} ok (tx {buy_tx}), "
                                   f"sell on {sell_venue_name} failed: {err}. "
                                   f"Sell account: {sell_account}. "
                                   f"Recover: /recover {opp_id}"
                               )})
                return

            cngn_price = Decimal(str(c.signal.get("prices", {}).get(sell_venue_name, "0")))
            actual_profit = sell_trade.amount * (sell_trade.price or cngn_price) - size_usd
            await db.update_dex_arbitrage_execution_state(
                opp_id,
                status="completed",
                buy_tx_hash=buy_trade.tx_hash,
                sell_tx_hash=sell_trade.tx_hash,
                actual_profit_usd=float(actual_profit),
            )
            self.inventory.record_trade_complete(opp_id, size_usd, actual_profit, Decimal("0"))
            self.broadcast({"type": "dex_arb_executed", "data": {
                "id": opp_id, "direction": direction, "profit_usd": float(actual_profit),
            }})
            logger.info("dex_dex_arb_executed", opp_id=opp_id, direction=direction,
                        profit_usd=float(actual_profit))

        except Exception as e:
            err = str(e)
            if buy_trade and buy_trade.status != "failed":
                buy_tx = buy_trade.tx_hash or ""
                buy_venue_name, sell_venue_name = _DEX_DEX_DIRECTIONS[direction]
                sell_account = getattr(getattr(self.venues.get(sell_venue_name), "trade_account", None), "address", "unknown")
                logger.error("dex_dex_half_open", direction=direction, buy_tx=buy_tx, sell_error=err,
                             sell_venue=sell_venue_name, sell_account=sell_account)
                db = await get_db()
                await db.update_dex_arbitrage_execution_state(
                    opp_id,
                    status="half_open",
                    buy_tx_hash=buy_tx or None,
                    reason=err,
                )
                self.inventory.trip_circuit_breaker(f"Half-open DEX-DEX arb: {opp_id}")
                self.inventory.record_trade_failure(opp_id, f"HALF_OPEN:{buy_tx}:{err}")
                self.broadcast({"type": "alert", "severity": "critical",
                               "message": (
                                   f"Half-open DEX-DEX arb {opp_id} ({direction}): "
                                   f"buy on {buy_venue_name} ok (tx {buy_tx}), "
                                   f"sell on {sell_venue_name} failed: {err}. "
                                   f"Sell account: {sell_account}. "
                                   f"Recover: /recover {opp_id}"
                               )})
            else:
                logger.error("dex_dex_execution_error", opp_id=opp_id, error=err)
                self.inventory.record_trade_failure(opp_id, err)
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

    def update_portfolio_snapshot(self, cngn_value_usd: Decimal, total_usd: Decimal, cngn_price_usd: Decimal = Decimal("0")):
        self.inventory.update_portfolio_snapshot(cngn_value_usd, total_usd, cngn_price_usd)

    async def recover_dex_half_open(self, opp_id: str) -> dict:
        """Recover a half-open DEX-DEX arb.

        Two paths, tried in order:
        1. Retry sell: simulate the original sell leg — if it passes now (e.g. cNGN was
           deposited externally), execute it.
        2. Reverse buy: sell the cNGN back on the buy-side chain to recover capital at a
           small loss (~2× pool fees). Used when the sell-side has no cNGN.
        """
        if self._arb_executing:
            raise ValueError("execution in progress")
        self._arb_executing = True
        try:
            return await self._recover_dex_half_open_inner(opp_id)
        finally:
            self._arb_executing = False

    async def _recover_dex_half_open_inner(self, opp_id: str) -> dict:
        """Inner implementation of recover_dex_half_open, called with _arb_executing held."""
        db = await get_db()
        opp = await db.get_dex_arbitrage_opportunity(opp_id)
        if opp is None:
            raise ValueError(f"Unknown DEX arbitrage opportunity: {opp_id}")
        if opp.status not in ("buy_filled", "half_open"):
            raise ValueError(f"Opportunity {opp_id} is not recoverable from status {opp.status}")

        direction = opp.direction
        buy_venue_name, sell_venue_name = _DEX_DEX_DIRECTIONS[direction]
        buy_venue = self.venues[buy_venue_name]
        sell_venue = self.venues[sell_venue_name]
        from engine.core.arbitrage.executor import _clean_revert
        loop = asyncio.get_running_loop()

        # Path 1: simulate the original sell — if it passes, retry it.
        can_retry_sell = False
        planned_sell_cngn = opp.planned_sell_cngn if opp.planned_sell_cngn is not None else opp.cngn_transferred
        if hasattr(sell_venue, "simulate_swap"):
            sell_amount_raw = int(planned_sell_cngn * Decimal(10 ** sell_venue.cngn_decimals))
            sell_sim_err = await loop.run_in_executor(
                None, sell_venue.simulate_swap, sell_venue.cngn_address, sell_amount_raw, 0
            )
            can_retry_sell = sell_sim_err is None

        if can_retry_sell:
            logger.info("dex_dex_recovery_retrying_sell", opp_id=opp_id, sell_venue=sell_venue_name,
                        amount_cngn=float(planned_sell_cngn))
            sell_trade = await self.executor.execute_dex_sell(sell_venue_name, planned_sell_cngn, Decimal("0"), opp_id)
            if sell_trade and sell_trade.status != "failed":
                actual_profit = sell_trade.amount * (sell_trade.price or Decimal("0")) - opp.optimal_size_usd
                await db.update_dex_arbitrage_execution_state(opp_id, status="completed",
                    sell_tx_hash=sell_trade.tx_hash, reason="Recovered: retried sell leg",
                    actual_profit_usd=float(actual_profit))
                self.inventory.record_trade_complete(opp_id, opp.optimal_size_usd, actual_profit, Decimal("0"))
                logger.info("dex_dex_recovery_completed", opp_id=opp_id, method="retry_sell",
                            sell_tx_hash=sell_trade.tx_hash, profit_usd=float(actual_profit))
                return {"status": "completed", "method": "retry_sell", "opp_id": opp_id,
                        "sell_tx_hash": sell_trade.tx_hash, "profit_usd": float(actual_profit)}
            # Simulation passed but execution failed — fall through to reversal
            logger.warning("dex_dex_recovery_sell_retry_failed", opp_id=opp_id,
                           error=sell_trade.error if sell_trade else "unknown")

        # Path 2: reverse the buy — sell cNGN back on the buy-side chain to recover capital.
        # Use the stored buy_amount_cngn (actual cNGN received in the buy leg), not the live
        # balance which may include pre-existing cNGN from LP activity or other trades.
        cngn_to_reverse = opp.buy_amount_cngn
        if not cngn_to_reverse:
            raise ValueError(
                f"Cannot reverse {opp_id}: buy_amount_cngn not recorded "
                f"(old record — check buy tx {opp.buy_tx_hash} manually)"
            )

        logger.warning("dex_dex_recovery_reversing_buy", opp_id=opp_id, buy_venue=buy_venue_name,
                       cngn_to_reverse=float(cngn_to_reverse))
        reverse_trade = await self.executor.execute_dex_sell(buy_venue_name, cngn_to_reverse, Decimal("0"), opp_id)

        if not reverse_trade or reverse_trade.status == "failed":
            err = _clean_revert((reverse_trade.error if reverse_trade else None) or "reverse sell failed")
            self.inventory.trip_circuit_breaker(f"DEX-DEX recovery reversal failed: {opp_id}")
            self.inventory.record_trade_failure(opp_id, f"RECOVERY_REVERSAL:{err}")
            raise ValueError(err)

        actual_loss = reverse_trade.amount * (reverse_trade.price or Decimal("0")) - opp.optimal_size_usd
        await db.update_dex_arbitrage_execution_state(opp_id, status="completed",
            sell_tx_hash=reverse_trade.tx_hash, reason="Recovered: reversed buy leg",
            actual_profit_usd=float(actual_loss))
        self.inventory.record_trade_complete(opp_id, opp.optimal_size_usd, actual_loss, Decimal("0"))
        logger.info("dex_dex_recovery_completed", opp_id=opp_id, method="reverse_buy",
                    sell_tx_hash=reverse_trade.tx_hash, profit_usd=float(actual_loss))
        return {"status": "completed", "method": "reverse_buy", "opp_id": opp_id,
                "sell_tx_hash": reverse_trade.tx_hash, "profit_usd": float(actual_loss)}

    def _reconcile_balances(self, balances: list) -> None:
        """Refresh per-account stablecoin and cNGN from the scheduler's periodic balance fetch."""
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
            self.inventory.reconcile_stables(venue_stables)
        if venue_cngn:
            self.inventory.reconcile_cngn(venue_cngn)

    async def _seed_account_inventory(self):
        """Read trade-account stablecoin and cNGN balances and pre-approve routers at first run."""
        stable_balances: dict[str, Decimal] = {}
        cngn_balances: dict[str, Decimal] = {}
        for name, venue in self.venues.items():
            if all(hasattr(venue, attr) for attr in ("stable_token", "cngn_token", "trade_account", "stable_decimals", "cngn_decimals", "ensure_trade_approvals")):
                try:
                    raw = venue.stable_token.functions.balanceOf(venue.trade_account.address).call()
                    stable_balances[name] = Decimal(raw) / Decimal(10 ** venue.stable_decimals)
                except Exception as e:
                    logger.warning("account_stable_seed_failed", venue=name, error=str(e))
                try:
                    raw = venue.cngn_token.functions.balanceOf(venue.trade_account.address).call()
                    cngn_balances[name] = Decimal(raw) / Decimal(10 ** venue.cngn_decimals)
                except Exception as e:
                    logger.warning("account_cngn_seed_failed", venue=name, error=str(e))
                try:
                    await venue.ensure_trade_approvals()
                except Exception as e:
                    logger.warning("trade_approval_failed", venue=name, error=str(e))
        if stable_balances:
            self.inventory.initialize_account_stable(stable_balances)
        if cngn_balances:
            self.inventory.initialize_account_cngn(cngn_balances)
        self._inventory_seeded = True
