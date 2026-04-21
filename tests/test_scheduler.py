"""Scheduler invariants: bootstrap gate ordering and WS health fallback.

The LP rebalance lifecycle is tested separately in test_lp_e2e.py.
These tests pin the non-obvious coordination logic: the DEX arb curve
must not fire until pool state AND gas prices are both seeded, and the
WS listener provides pool-state updates so the periodic recalc is only
a fallback when a connection is down.
"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.scheduler import TradingScheduler, SchedulerConfig
from engine.types import CexParams
from tests.fakes import FakeDexAdapter


# =============================================================================
# Helpers
# =============================================================================


class MockDB:
    def __init__(self):
        self._prices = [Decimal("0.000606")] * 20
        self._prices_by_source: dict = {}
        self.insert_action = AsyncMock()
        self.update_venue_config = AsyncMock()
        self.insert_price_snapshot = AsyncMock()
        self.insert_position = AsyncMock()
        self.insert_alert = AsyncMock()
        self.set_system_state = AsyncMock()

    async def get_recent_prices(self, limit=100):
        return self._prices[:limit]

    async def get_recent_prices_for_source(self, source, limit=100):
        return self._prices_by_source.get(source, self._prices)[:limit]


def _build_scheduler(
    venues: dict,
    broadcasts: list,
    db: MockDB,
    *,
    arbitrage_engine: Any = None,
) -> TradingScheduler:
    return TradingScheduler(
        price_aggregator=MagicMock(),
        venues=venues,
        config=SchedulerConfig(),
        broadcast=broadcasts.append,
        blended_calculator=None,
        arbitrage_engine=arbitrage_engine,
        account_manager=None,
        token_contracts={},
        portfolio_exposure_calculator=None,
        lp_managers=venues,
        system_state_store=SimpleNamespace(set_system_state=db.set_system_state),
        price_store=SimpleNamespace(
            get_recent_prices=db.get_recent_prices,
            get_recent_prices_for_source=db.get_recent_prices_for_source,
            insert_price_snapshot=db.insert_price_snapshot,
        ),
        position_store=SimpleNamespace(insert_position=db.insert_position),
        alert_store=SimpleNamespace(insert_alert=db.insert_alert),
        venue_config_store=SimpleNamespace(update_venue_config=db.update_venue_config),
        action_store=SimpleNamespace(insert_action=db.insert_action),
    )


# =============================================================================
# Bootstrap gate: pool state → gas → first DEX-DEX update
# =============================================================================


class TestBootstrapGate:
    @pytest.mark.asyncio
    async def test_bootstrap_runs_pool_seed_then_gas_then_arb_in_order(self):
        """seed pool state → update gas → first on_dex_dex_update, in that order.

        If this ordering breaks, arb detection fires before the pool cache is
        seeded, producing stale-cache errors on every startup.
        """
        broadcasts = []
        db = MockDB()
        arbitrage_engine = MagicMock()
        arbitrage_engine.on_dex_dex_update = AsyncMock()
        sched = _build_scheduler({}, broadcasts, db, arbitrage_engine=arbitrage_engine)
        call_order = []
        sched.context.arbitrage_engine.on_dex_dex_update.side_effect = lambda: call_order.append("arb")
        sched.arbitrage_jobs._update_gas_oracle = AsyncMock(side_effect=lambda: call_order.append("gas"))

        with patch("engine.market.pool_state.seed_dex_pool_states", AsyncMock()) as seed_mock, \
             patch("engine.market.gas_oracle.gas_usd_base", return_value=Decimal("1")), \
             patch("engine.market.gas_oracle.gas_usd_bsc", return_value=Decimal("1")):
            await sched._bootstrap_dex_arb_curve()

        seed_mock.assert_awaited_once()
        assert call_order == ["gas", "arb"]
        assert sched.state.dex_bootstrap_pending is False

    @pytest.mark.asyncio
    async def test_bootstrap_defers_arb_when_gas_missing(self):
        """If gas oracle has no prices after seeding, on_dex_dex_update must not fire.

        dex_bootstrap_pending=True signals the gas oracle job to retry once
        prices become available, preventing zero-gas trades.
        """
        broadcasts = []
        db = MockDB()
        arbitrage_engine = MagicMock()
        arbitrage_engine.on_dex_dex_update = AsyncMock()
        sched = _build_scheduler({}, broadcasts, db, arbitrage_engine=arbitrage_engine)
        sched.arbitrage_jobs._update_gas_oracle = AsyncMock()

        with patch("engine.market.pool_state.seed_dex_pool_states", AsyncMock()), \
             patch("engine.market.gas_oracle.gas_usd_base", return_value=None), \
             patch("engine.market.gas_oracle.gas_usd_bsc", return_value=None):
            await sched._bootstrap_dex_arb_curve()

        sched.context.arbitrage_engine.on_dex_dex_update.assert_not_awaited()
        assert sched.state.dex_bootstrap_pending is True

    @pytest.mark.asyncio
    async def test_gas_failure_without_cached_values_broadcasts_critical(self):
        """Gas oracle fetch failure with no cached fallback → critical alert.

        Trading is blocked until gas prices recover, so operators must be alerted.
        A failure with stale-but-present cached values is non-critical (just logged).
        """
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({}, broadcasts, db)
        sched.market_jobs._schedule_dex_bootstrap = MagicMock()

        with patch("engine.market.gas_oracle.update", AsyncMock(side_effect=RuntimeError("rpc down"))), \
             patch("engine.market.gas_oracle.gas_usd_base", return_value=None), \
             patch("engine.market.gas_oracle.gas_usd_bsc", return_value=None):
            await sched._update_gas_oracle()

        assert any(b.get("severity") == "critical" for b in broadcasts), \
            "critical alert must be broadcast when gas oracle fails with no cached fallback"

    @pytest.mark.asyncio
    async def test_gas_failure_with_cached_values_does_not_alert(self):
        """Gas oracle fetch failure with stale cached values → no alert, just log.

        Operators do not need to be paged if trading can continue on cached gas prices.
        """
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({}, broadcasts, db)
        sched.market_jobs._schedule_dex_bootstrap = MagicMock()

        with patch("engine.market.gas_oracle.update", AsyncMock(side_effect=RuntimeError("timeout"))), \
             patch("engine.market.gas_oracle.gas_usd_base", return_value=Decimal("0.003")), \
             patch("engine.market.gas_oracle.gas_usd_bsc", return_value=Decimal("0.005")):
            await sched._update_gas_oracle()

        assert broadcasts == [], "no alert must be broadcast when cached gas prices are available"


# =============================================================================
# WS health check: periodic recalc only fires when a connection is down
# =============================================================================


class TestWsHealthGate:
    @pytest.mark.asyncio
    async def test_skips_dex_recalc_when_both_ws_healthy(self):
        """With both Base and BSC WS connections active, the periodic job must be a no-op.

        The WS listener receives pool state inline on every swap event, so the
        periodic recalc is redundant when both connections are healthy.
        """
        broadcasts = []
        db = MockDB()
        arbitrage_engine = MagicMock()
        arbitrage_engine.on_dex_dex_update = AsyncMock()
        sched = _build_scheduler({}, broadcasts, db, arbitrage_engine=arbitrage_engine)
        sched.ws_listener.active_connections = {"base", "bsc"}

        await sched._stream_dex_arb_curve()

        sched.context.arbitrage_engine.on_dex_dex_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_dex_recalc_when_one_ws_down(self):
        """With only one WS connection, the periodic fallback must fire the arb recalc."""
        broadcasts = []
        db = MockDB()
        arbitrage_engine = MagicMock()
        arbitrage_engine.on_dex_dex_update = AsyncMock()
        sched = _build_scheduler({}, broadcasts, db, arbitrage_engine=arbitrage_engine)
        sched.ws_listener.active_connections = {"base"}  # bsc down

        await sched._stream_dex_arb_curve()

        sched.context.arbitrage_engine.on_dex_dex_update.assert_awaited_once()


class TestCexSyncFallback:
    @pytest.mark.asyncio
    async def test_sync_cex_orders_falls_back_to_main_quidax_when_lp_is_missing(self):
        """The ladder should still sync when only the main Quidax venue exists."""
        quidax = SimpleNamespace(
            paused=False,
            params=CexParams(anchor_source="quidax"),
            sync_order_ladder=AsyncMock(),
        )
        sched = _build_scheduler({"quidax": quidax}, [], MockDB())
        sched.market_jobs.get_reference_price_ngn = AsyncMock(return_value=Decimal("1600"))

        await sched.market_jobs.sync_cex_orders()

        sched.market_jobs.get_reference_price_ngn.assert_awaited_once_with(anchor_source="quidax")
        quidax.sync_order_ladder.assert_awaited_once_with(Decimal("1600"))
