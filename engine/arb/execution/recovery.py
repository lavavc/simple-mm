"""Recovery flows for half-open arbitrage opportunities."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import structlog

from engine.arb.routing.route_registry import ROUTES, ROUTES_BY_DIRECTION
from engine.venues.base import is_dex_execution_venue


logger = structlog.get_logger()


async def recover_dex_half_open(engine: Any, opp_id: str) -> dict[str, Any]:
    """Recover a half-open DEX-DEX arb."""
    if engine._arb_executing:
        raise ValueError("execution in progress")
    engine._arb_executing = True
    try:
        return await _recover_dex_half_open_inner(engine, opp_id)
    finally:
        engine._arb_executing = False


async def _recover_dex_half_open_inner(engine: Any, opp_id: str) -> dict[str, Any]:
    """Inner implementation of recover_dex_half_open, called with _arb_executing held."""
    arbitrage_store = engine.arbitrage_store
    opp = await arbitrage_store.get_dex_arbitrage_opportunity(opp_id)
    if opp is None:
        raise ValueError(f"Unknown DEX arbitrage opportunity: {opp_id}")
    if opp.status not in ("buy_filled", "half_open"):
        raise ValueError(f"Opportunity {opp_id} is not recoverable from status {opp.status}")

    direction = opp.direction
    route_def = ROUTES_BY_DIRECTION[direction]
    buy_venue_name = route_def.buy_leg.venue
    sell_venue_name = route_def.sell_leg.venue
    buy_venue = engine.venues[buy_venue_name]
    sell_venue = engine.venues[sell_venue_name]
    from engine.arb.execution.executor import _clean_revert

    loop = asyncio.get_running_loop()

    sell_cngn = opp.buy_amount_cngn
    if not sell_cngn:
        raise ValueError(
            f"Cannot recover {opp_id}: buy_amount_cngn not recorded "
            f"(old record — check buy tx {opp.buy_tx_hash} manually)"
        )

    can_retry_sell = False
    if is_dex_execution_venue(sell_venue):
        sell_amount_raw = int(sell_cngn * Decimal(10 ** sell_venue.cngn_decimals))
        sell_sim_err = await loop.run_in_executor(
            None, sell_venue.simulate_swap, sell_venue.cngn_address, sell_amount_raw, 0
        )
        can_retry_sell = sell_sim_err is None

    if can_retry_sell:
        logger.info(
            "dex_dex_recovery_retrying_sell",
            opp_id=opp_id,
            sell_venue=sell_venue_name,
            amount_cngn=float(sell_cngn),
        )
        sell_trade = await engine.executor.execute_dex_sell(sell_venue_name, sell_cngn, Decimal("0"), opp_id)
        if sell_trade and sell_trade.status != "failed":
            cost_basis = opp.executed_size_usd if opp.executed_size_usd is not None else opp.optimal_size_usd
            actual_profit = sell_trade.amount * (sell_trade.price or Decimal("0")) - cost_basis
            await arbitrage_store.update_dex_arbitrage_execution_state(
                opp_id,
                status="completed",
                sell_tx_hash=sell_trade.tx_hash,
                reason="Recovered: retried sell leg",
                actual_profit_usd=float(actual_profit),
            )
            engine.inventory.record_trade_complete(opp_id, cost_basis, actual_profit, Decimal("0"))
            await engine.history.record_executed_raw(
                opp_id=opp_id,
                pipeline="dex_dex",
                direction=opp.direction,
                buy_venue=buy_venue_name,
                sell_venue=sell_venue_name,
                optimal_size_usd=opp.optimal_size_usd,
                routed_size_usd=cost_basis,
                executed_size_usd=cost_basis,
                expected_profit_usd=opp.expected_profit_usd,
                net_spread_bps=opp.net_spread_bps,
                actual_profit_usd=actual_profit,
                reason="Recovered: retried sell leg",
                buy_tx_hash=opp.buy_tx_hash,
                sell_tx_hash=sell_trade.tx_hash,
            )
            logger.info(
                "dex_dex_recovery_completed",
                opp_id=opp_id,
                method="retry_sell",
                sell_tx_hash=sell_trade.tx_hash,
                profit_usd=float(actual_profit),
            )
            return {
                "status": "completed",
                "method": "retry_sell",
                "opp_id": opp_id,
                "sell_tx_hash": sell_trade.tx_hash,
                "profit_usd": float(actual_profit),
            }
        logger.warning(
            "dex_dex_recovery_sell_retry_failed",
            opp_id=opp_id,
            error=sell_trade.error if sell_trade else "unknown",
        )

    logger.warning(
        "dex_dex_recovery_reversing_buy",
        opp_id=opp_id,
        buy_venue=buy_venue_name,
        cngn_to_reverse=float(sell_cngn),
    )
    reverse_trade = await engine.executor.execute_dex_sell(buy_venue_name, sell_cngn, Decimal("0"), opp_id)

    if not reverse_trade or reverse_trade.status == "failed":
        err = _clean_revert((reverse_trade.error if reverse_trade else None) or "reverse sell failed")
        recovery_reason = f"RECOVERY_FAILED:{err}"
        cost_basis = opp.executed_size_usd if opp.executed_size_usd is not None else opp.optimal_size_usd
        await arbitrage_store.update_dex_arbitrage_execution_state(
            opp_id,
            status="half_open",
            reason=recovery_reason,
        )
        await engine.history.record_failed_raw(
            opp_id=opp_id,
            pipeline="dex_dex",
            direction=opp.direction,
            buy_venue=buy_venue_name,
            sell_venue=sell_venue_name,
            status="half_open",
            optimal_size_usd=opp.optimal_size_usd,
            routed_size_usd=cost_basis,
            executed_size_usd=cost_basis,
            expected_profit_usd=opp.expected_profit_usd,
            net_spread_bps=opp.net_spread_bps,
            reason=recovery_reason,
            buy_tx_hash=opp.buy_tx_hash,
            sell_tx_hash=opp.sell_tx_hash,
        )
        engine.inventory.trip_circuit_breaker(f"DEX-DEX recovery reversal failed: {opp_id}")
        engine.inventory.record_trade_failure(opp_id, f"RECOVERY_REVERSAL:{err}")
        raise ValueError(err)

    cost_basis = opp.executed_size_usd if opp.executed_size_usd is not None else opp.optimal_size_usd
    actual_loss = reverse_trade.amount * (reverse_trade.price or Decimal("0")) - cost_basis
    await arbitrage_store.update_dex_arbitrage_execution_state(
        opp_id,
        status="completed",
        sell_tx_hash=reverse_trade.tx_hash,
        reason="Recovered: reversed buy leg",
        actual_profit_usd=float(actual_loss),
    )
    engine.inventory.record_trade_complete(opp_id, cost_basis, actual_loss, Decimal("0"))
    await engine.history.record_executed_raw(
        opp_id=opp_id,
        pipeline="dex_dex",
        direction=opp.direction,
        buy_venue=buy_venue_name,
        sell_venue=sell_venue_name,
        optimal_size_usd=opp.optimal_size_usd,
        routed_size_usd=cost_basis,
        executed_size_usd=cost_basis,
        expected_profit_usd=opp.expected_profit_usd,
        net_spread_bps=opp.net_spread_bps,
        actual_profit_usd=actual_loss,
        reason="Recovered: reversed buy leg",
        buy_tx_hash=opp.buy_tx_hash,
        sell_tx_hash=reverse_trade.tx_hash,
    )
    logger.info(
        "dex_dex_recovery_completed",
        opp_id=opp_id,
        method="reverse_buy",
        sell_tx_hash=reverse_trade.tx_hash,
        profit_usd=float(actual_loss),
    )
    return {
        "status": "completed",
        "method": "reverse_buy",
        "opp_id": opp_id,
        "sell_tx_hash": reverse_trade.tx_hash,
        "profit_usd": float(actual_loss),
    }


async def recover_cex_half_open(engine: Any, opp_id: str) -> dict[str, Any]:
    """Recover a half-open CEX-DEX arb."""
    if engine._arb_executing:
        raise ValueError("execution in progress")
    engine._arb_executing = True
    try:
        arbitrage_store = engine.arbitrage_store
        opp = await arbitrage_store.get_arbitrage_opportunity(opp_id)
        if opp is None:
            raise ValueError(f"Unknown CEX-DEX arbitrage opportunity: {opp_id}")
        if opp.status != "half_open":
            raise ValueError(f"Opportunity {opp_id} is not recoverable from status {opp.status}")
        buy_venue_name = opp.buy_venue
        sell_venue_name = opp.sell_venue
        cex_route_def = next(
            (r for r in ROUTES if r.buy_leg.venue == buy_venue_name and r.sell_leg.venue == sell_venue_name),
            None,
        )
        if cex_route_def is None:
            raise ValueError(f"Unknown CEX route: {buy_venue_name} → {sell_venue_name}")
        cex_direction = cex_route_def.direction
        buy_is_cex = cex_route_def.buy_leg.leg_type == "api"
        buy_amount_cngn = opp.buy_amount_cngn
        if not buy_amount_cngn:
            raise ValueError(
                f"Cannot recover {opp_id}: buy_amount_cngn not recorded "
                f"(old record — check buy tx {opp.buy_tx_hash} manually)"
            )

        return await _reverse_cex_recovery(engine, arbitrage_store, opp_id, opp, cex_direction, buy_is_cex, buy_amount_cngn)
    finally:
        engine._arb_executing = False


async def _reverse_cex_recovery(
    engine: Any,
    arbitrage_store: Any,
    opp_id: str,
    opp: Any,
    cex_direction: str,
    buy_is_cex: bool,
    buy_amount_cngn: Decimal,
) -> dict[str, Any]:
    """Execute a reversal trade to recover a half-open CEX arb and record the outcome."""
    buy_venue_name = opp.buy_venue
    if buy_is_cex:
        method = "reverse_cex_buy"
        logger.warning(
            "cex_dex_recovery_reversing_cex_buy",
            opp_id=opp_id,
            buy_venue=buy_venue_name,
            amount_cngn=float(buy_amount_cngn),
        )
        reverse_trade = await engine.executor.execute_cex_sell(
            buy_venue_name,
            buy_amount_cngn,
            opp.buy_price,
            opp_id,
        )
        fallback_price = opp.buy_price
    else:
        method = "reverse_dex_buy"
        logger.warning(
            "cex_dex_recovery_reversing_dex_buy",
            opp_id=opp_id,
            buy_venue=buy_venue_name,
            amount_cngn=float(buy_amount_cngn),
        )
        reverse_trade = await engine.executor.execute_dex_sell(
            buy_venue_name,
            buy_amount_cngn,
            Decimal("0"),
            opp_id,
        )
        fallback_price = Decimal("0")

    if not reverse_trade or reverse_trade.status == "failed":
        err = (reverse_trade.error if reverse_trade else None) or "reverse sell failed"
        recovery_reason = f"RECOVERY_FAILED:{err}"
        await arbitrage_store.update_arbitrage_opportunity(opp_id, status="half_open", reason=recovery_reason)
        await engine.history.record_failed_raw(
            opp_id=opp_id,
            pipeline="cex_dex",
            direction=cex_direction,
            buy_venue=opp.buy_venue,
            sell_venue=opp.sell_venue,
            status="half_open",
            optimal_size_usd=opp.recommended_size_usd,
            routed_size_usd=opp.recommended_size_usd,
            executed_size_usd=opp.recommended_size_usd,
            expected_profit_usd=opp.expected_profit_usd,
            net_spread_bps=opp.net_spread_bps,
            reason=recovery_reason,
            buy_tx_hash=opp.buy_tx_hash,
        )
        engine.broadcast({"type": "alert", "severity": "critical",
                          "message": (
                              f"CEX-DEX recovery reversal failed for {opp_id}: {err}. "
                              "Manual intervention required."
                          )})
        raise ValueError(err)

    reason = f"Recovered: reversed {'CEX' if buy_is_cex else 'DEX'} buy leg"
    actual_loss = reverse_trade.amount * (reverse_trade.price or fallback_price) - opp.recommended_size_usd
    await arbitrage_store.update_arbitrage_opportunity(
        opp_id,
        status="completed",
        actual_profit_usd=float(actual_loss),
        reason=reason,
    )
    engine.inventory.record_trade_complete(opp_id, opp.recommended_size_usd, actual_loss, Decimal("0"))
    await engine.history.record_executed_raw(
        opp_id=opp_id,
        pipeline="cex_dex",
        direction=cex_direction,
        buy_venue=opp.buy_venue,
        sell_venue=opp.sell_venue,
        optimal_size_usd=opp.recommended_size_usd,
        routed_size_usd=opp.recommended_size_usd,
        executed_size_usd=opp.recommended_size_usd,
        expected_profit_usd=opp.expected_profit_usd,
        net_spread_bps=opp.net_spread_bps,
        actual_profit_usd=actual_loss,
        reason=reason,
        buy_tx_hash=opp.buy_tx_hash,
        sell_tx_hash=reverse_trade.tx_hash if not buy_is_cex else None,
    )
    logger.info("cex_dex_recovery_completed", opp_id=opp_id, method=method, profit_usd=float(actual_loss))
    return {"status": "completed", "method": method, "opp_id": opp_id, "profit_usd": float(actual_loss)}
