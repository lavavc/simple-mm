"""Shared route execution for arbitrage pipelines."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import structlog

from engine.arb.execution.preflight import _handle_preflight_error
from engine.types import coerce_decimal
from engine.arb.routing.route_registry import TradeRoute
from engine.arb.routing.router import SelectedRoute
from engine.venues.base import is_dex_execution_venue


logger = structlog.get_logger()


async def execute_route(engine: Any, route_def: TradeRoute, route: SelectedRoute, opp_id: str) -> None:
    """Execute a routed arbitrage trade (CEX-DEX or DEX-DEX)."""
    engine._arb_executing = True
    arbitrage_store = engine.arbitrage_store
    buy_trade: Any = None
    sell_tx_hash: str | None = None
    sell_cngn_amount: Decimal | None = None
    buy_venue_name = route_def.buy_leg.venue
    sell_venue_name = route_def.sell_leg.venue
    buy_is_cex = route_def.buy_leg.leg_type == "api"
    sell_is_cex = route_def.sell_leg.leg_type == "api"
    c = route.candidate
    direction = c.direction
    size_usd = route.adjusted_size_usd
    try:
        loop = asyncio.get_running_loop()
        slippage_bps = c.signal.get("optimal_arb", {}).get("slippage_tolerance_bps", 10)
        min_out_usd = size_usd * (1 - Decimal(str(slippage_bps)) / 10000)
        prices = c.signal.get("prices", {})
        _buy_p = prices.get(buy_venue_name)
        _sell_p = prices.get(sell_venue_name)
        if _buy_p is None or _sell_p is None:
            logger.warning(
                "arb_signal_missing_venue_price",
                direction=direction,
                buy_venue=buy_venue_name,
                sell_venue=sell_venue_name,
                available_prices=list(prices.keys()),
            )
            await engine.history.record_failed(
                opp_id, route, status="abandoned", reason="Signal missing venue price",
            )
            return
        buy_signal_price = Decimal(str(_buy_p))
        sell_signal_price = Decimal(str(_sell_p))
        net_spread_bps = c.signal["optimal_arb"].get("net_spread_bps", 0)
        await engine.history.record_routed(opp_id, route)

        if not sell_is_cex:
            from engine.arb.execution.executor import _clean_revert

            sell_venue = engine.venues[sell_venue_name]
            if not is_dex_execution_venue(sell_venue):
                raise TypeError(f"{sell_venue_name} is not a DEX execution venue")
            sim_min_out_raw = int(min_out_usd * Decimal(10 ** sell_venue.stable_decimals))

            if buy_is_cex:
                from engine.arb.detection.cex_dex import estimate_cex_buy_cngn

                quidax_depth = c.signal.get("depth", {}).get(buy_venue_name)
                sell_cngn_amount = estimate_cex_buy_cngn(quidax_depth, size_usd)
                if sell_cngn_amount <= 0:
                    logger.warning(
                        "cex_dex_preflight_missing_depth_or_zero_estimate",
                        direction=direction,
                        size_usd=float(size_usd),
                        sell_venue=sell_venue_name,
                    )
                    await engine.history.record_failed(
                        opp_id,
                        route,
                        status="sell_quote_unavailable",
                        reason="Missing Quidax depth or zero cNGN estimate",
                    )
                    return
            else:
                from engine.arb.detection.dex_dex import estimate_dex_dex_trade

                sell_estimate = estimate_dex_dex_trade(direction, size_usd)
                if not sell_estimate:
                    logger.warning(
                        "dex_dex_pool_cache_cold_at_execution",
                        direction=direction,
                        size_usd=float(size_usd),
                    )
                    await arbitrage_store.update_dex_arbitrage_execution_state(
                        opp_id,
                        status="abandoned",
                        reason="Pool cache cold at execution",
                    )
                    await engine.history.record_failed(
                        opp_id,
                        route,
                        status="pool_cache_cold",
                        reason="Pool cache cold at execution",
                    )
                    return
                sell_cngn_amount = Decimal(str(sell_estimate["cngn_transferred"]))

            cngn_estimate_raw = int(sell_cngn_amount * Decimal(10 ** sell_venue.cngn_decimals))
            sell_err = await loop.run_in_executor(
                None,
                sell_venue.simulate_swap,
                sell_venue.cngn_address,
                cngn_estimate_raw,
                sim_min_out_raw,
            )
            if sell_err:
                clean_sell_err = _clean_revert(sell_err)
                sell_price_usd = coerce_decimal(c.signal.get("prices", {}).get(sell_venue_name))
                _handle_preflight_error(
                    engine,
                    sell_venue_name,
                    clean_sell_err,
                    "arb_sell_preflight_failed",
                    direction=direction,
                    size_usd=float(size_usd),
                    sell_cngn_est=float(sell_cngn_amount),
                    sell_price_usd=float(sell_price_usd) if sell_price_usd is not None else None,
                    wallet_asset="cngn",
                )
                if not buy_is_cex:
                    await arbitrage_store.update_dex_arbitrage_execution_state(
                        opp_id,
                        status="abandoned",
                        reason=clean_sell_err,
                    )
                await engine.history.record_failed(
                    opp_id,
                    route,
                    status="sell_preflight_failed",
                    reason=clean_sell_err,
                )
                return

        if not buy_is_cex:
            from engine.arb.execution.executor import _clean_revert

            buy_venue = engine.venues[buy_venue_name]
            if not is_dex_execution_venue(buy_venue):
                raise TypeError(f"{buy_venue_name} is not a DEX execution venue")
            buy_amount_raw = int(size_usd * Decimal(10 ** buy_venue.stable_decimals))
            buy_err = await loop.run_in_executor(
                None, buy_venue.simulate_swap, buy_venue.stable_address, buy_amount_raw, 0
            )
            if buy_err:
                clean_buy_err = _clean_revert(buy_err)
                _handle_preflight_error(
                    engine,
                    buy_venue_name,
                    clean_buy_err,
                    "dex_dex_buy_preflight_failed",
                    direction=direction,
                    size_usd=float(size_usd),
                    wallet_asset="stable",
                    wallet_symbol=getattr(getattr(buy_venue, "config", None), "token0_symbol", None)
                    if getattr(getattr(buy_venue, "config", None), "invert_price", False)
                    else getattr(getattr(buy_venue, "config", None), "token1_symbol", None),
                    required_amount=float(size_usd),
                )
                await arbitrage_store.update_dex_arbitrage_execution_state(
                    opp_id,
                    status="abandoned",
                    reason=clean_buy_err,
                )
                await engine.history.record_failed(
                    opp_id,
                    route,
                    status="buy_preflight_failed",
                    reason=clean_buy_err,
                )
                return

        if buy_is_cex or sell_is_cex:
            net_spread_bps = c.signal.get("optimal_arb", {}).get("net_spread_bps", 0)
            from engine.types import ArbitrageOpportunity as ArbOpp

            await arbitrage_store.insert_arbitrage_opportunity(ArbOpp(
                id=opp_id,
                timestamp=int(time.time() * 1000),
                buy_venue=buy_venue_name,
                sell_venue=sell_venue_name,
                buy_price=buy_signal_price,
                sell_price=sell_signal_price,
                gross_spread_bps=net_spread_bps,
                net_spread_bps=net_spread_bps,
                recommended_size_usd=size_usd,
                expected_profit_usd=route.expected_profit_usd,
                status="executing",
            ))
        else:
            await arbitrage_store.update_dex_arbitrage_execution_state(opp_id, status="executing")

        engine.inventory.record_trade_start(opp_id, size_usd, buy_venue_name, sell_venue_name)

        if buy_is_cex:
            buy_trade = await engine.executor.execute_cex_buy(buy_venue_name, size_usd, buy_signal_price, opp_id)
        else:
            buy_trade = await engine.executor.execute_dex_buy(buy_venue_name, size_usd, opp_id)

        if not buy_trade or buy_trade.status == "failed":
            err = (buy_trade.error if buy_trade else None) or "buy failed"
            logger.error("arb_buy_failed", direction=direction, error=err)
            engine.inventory.record_trade_failure(opp_id, err)
            if buy_is_cex or sell_is_cex:
                await arbitrage_store.update_arbitrage_opportunity(opp_id, status="abandoned", reason=err)
            else:
                await arbitrage_store.expire_old_dex_arbitrage_opportunities(0)
            await engine.history.record_failed(opp_id, route, status="buy_failed", reason=err)
            return

        if not (buy_is_cex or sell_is_cex):
            await arbitrage_store.update_dex_arbitrage_execution_state(
                opp_id,
                status="buy_filled",
                buy_tx_hash=buy_trade.tx_hash,
                buy_amount_cngn=buy_trade.amount,
                executed_size_usd=float(size_usd),
            )

        if sell_is_cex:
            sell_trade = await engine.executor.execute_cex_sell(
                sell_venue_name,
                buy_trade.amount,
                sell_signal_price,
                opp_id,
            )
        else:
            assert sell_cngn_amount is not None
            sell_trade = await engine.executor.execute_dex_sell(
                sell_venue_name,
                sell_cngn_amount,
                min_out_usd,
                opp_id,
            )

        if not sell_trade or sell_trade.status == "failed":
            sell_tx_hash = sell_trade.tx_hash if sell_trade else None
            raise RuntimeError((sell_trade.error if sell_trade else None) or "sell failed")

        if buy_is_cex or sell_is_cex:
            actual_buy_price = buy_trade.price or buy_signal_price
            actual_sell_price = sell_trade.price or sell_signal_price
            actual_profit = sell_trade.amount * actual_sell_price - buy_trade.amount * actual_buy_price
            await arbitrage_store.update_arbitrage_opportunity(
                opp_id,
                status="completed",
                actual_profit_usd=float(actual_profit),
            )
            broadcast_type = "arb_executed"
        else:
            cngn_price = Decimal(str(c.signal.get("prices", {}).get(sell_venue_name, "0")))
            actual_profit = sell_trade.amount * (sell_trade.price or cngn_price) - size_usd
            await arbitrage_store.update_dex_arbitrage_execution_state(
                opp_id,
                status="completed",
                buy_tx_hash=buy_trade.tx_hash,
                sell_tx_hash=sell_trade.tx_hash,
                actual_profit_usd=float(actual_profit),
            )
            broadcast_type = "dex_arb_executed"

        engine.inventory.record_trade_complete(opp_id, size_usd, actual_profit, Decimal("0"))
        await engine.history.record_executed(
            opp_id,
            route,
            actual_profit_usd=actual_profit,
            buy_tx_hash=buy_trade.tx_hash,
            sell_tx_hash=sell_trade.tx_hash,
        )
        engine.broadcast({"type": broadcast_type, "data": {
            "id": opp_id,
            "direction": direction,
            "profit_usd": float(actual_profit),
        }})
        logger.info("arb_executed", opp_id=opp_id, direction=direction, profit_usd=float(actual_profit))

    except Exception as e:
        err = str(e)
        if buy_trade and buy_trade.status != "failed":
            buy_tx = buy_trade.tx_hash or ""
            if buy_is_cex or sell_is_cex:
                await arbitrage_store.update_arbitrage_opportunity(
                    opp_id,
                    status="half_open",
                    reason=f"HALF_OPEN:{buy_tx}:{e}",
                    buy_amount_cngn=float(buy_trade.amount),
                    buy_tx_hash=buy_tx or None,
                )
                alert_msg = (
                    f"Half-open CEX-DEX arb {opp_id} ({direction}): "
                    f"buy on {buy_venue_name} ok (tx {buy_tx}), sell failed: {err}. "
                    f"Recover: /recover {opp_id}"
                )
            else:
                sell_venue = engine.venues.get(sell_venue_name)
                if sell_venue is not None and is_dex_execution_venue(sell_venue):
                    sell_account = sell_venue.trade_account.address
                else:
                    sell_account = "unknown"
                await arbitrage_store.update_dex_arbitrage_execution_state(
                    opp_id,
                    status="half_open",
                    buy_tx_hash=buy_tx or None,
                    reason=err,
                )
                alert_msg = (
                    f"Half-open DEX-DEX arb {opp_id} ({direction}): "
                    f"buy on {buy_venue_name} ok (tx {buy_tx}), "
                    f"sell on {sell_venue_name} failed: {err}. "
                    f"Sell account: {sell_account}. "
                    f"Recover: /recover {opp_id}"
                )
            logger.error("arb_half_open", direction=direction, buy_tx=buy_tx, sell_error=err)
            engine.inventory.trip_circuit_breaker(f"Half-open arb: {opp_id}")
            engine.inventory.record_trade_failure(opp_id, f"HALF_OPEN:{buy_tx}:{err}")
            await engine.history.record_failed(
                opp_id,
                route,
                status="half_open",
                reason=err,
                executed_size_usd=size_usd,
                buy_tx_hash=buy_tx or None,
                sell_tx_hash=sell_tx_hash,
            )
            engine.broadcast({"type": "alert", "severity": "critical", "message": alert_msg})
        else:
            logger.error("arb_execution_error", opp_id=opp_id, error=err)
            engine.inventory.record_trade_failure(opp_id, err)
            if buy_is_cex or sell_is_cex:
                await arbitrage_store.update_arbitrage_opportunity(opp_id, status="abandoned", reason=err)
            else:
                await arbitrage_store.update_dex_arbitrage_execution_state(opp_id, status="abandoned", reason=err)
            await engine.history.record_failed(opp_id, route, status="execution_error", reason=err)
    finally:
        engine._arb_executing = False
