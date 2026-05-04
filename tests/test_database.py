"""Non-obvious DB persistence invariants.

Basic CRUD (insert/get/update) is not tested here — that is aiosqlite's job.
These tests pin behaviour that would silently break the dashboard or recovery
flows if the underlying queries changed.
"""

import time
from decimal import Decimal

import pytest

from engine.db.connection import SQLiteConnectionManager
from engine.db.repository import DatabaseRepository
from engine.types import (
    ArbitrageHistoryEvent,
    ArbitrageHistoryWalletSnapshot,
    ArbitrageOpportunity,
    DexArbOpportunity,
    PriceQuote,
)


@pytest.fixture
async def db(tmp_path):
    repo = DatabaseRepository(SQLiteConnectionManager(str(tmp_path / "test.db")))
    await repo.connect()
    yield repo
    await repo.close()


def _make_history_event(
    opp_id: str,
    event_type: str = "routed",
    status: str = "routed",
    pipeline: str = "cex_dex",
    timestamp: int = 1_000_000,
    reason: str | None = None,
    actual_profit_usd: Decimal | None = None,
    executed_size_usd: Decimal | None = None,
    buy_tx_hash: str | None = None,
    sell_tx_hash: str | None = None,
) -> ArbitrageHistoryEvent:
    return ArbitrageHistoryEvent(
        opportunity_id=opp_id,
        pipeline=pipeline,
        event_type=event_type,
        timestamp=timestamp,
        direction="QUIDAX_TO_UNI_BSC",
        buy_venue="quidax",
        sell_venue="uni-bsc",
        status=status,
        optimal_size_usd=Decimal("500"),
        routed_size_usd=Decimal("400"),
        executed_size_usd=executed_size_usd,
        expected_profit_usd=Decimal("5"),
        actual_profit_usd=actual_profit_usd,
        net_profit_usd=Decimal("4"),
        net_spread_bps=80,
        reason=reason,
        buy_tx_hash=buy_tx_hash,
        sell_tx_hash=sell_tx_hash,
    )


# =============================================================================
# Price source filtering
# =============================================================================


class TestPriceSourceFiltering:
    @pytest.mark.asyncio
    async def test_get_recent_prices_for_source_returns_only_requested_source(self, db):
        """Source filter must exclude other venues — LP tick range calculation depends on it.

        If filtering is broken, uni-bsc prices would contaminate the uni-base
        price history used for tick range computation.
        """
        now = int(time.time() * 1000)
        for source, mid in [
            ("uni-base_pool", "0.000601"),
            ("quidax", "0.000700"),
            ("uni-base_pool", "0.000602"),
            ("uni-bsc_pool", "0.000603"),
            ("uni-base_pool", "0.000604"),
        ]:
            await db.prices.insert_price_snapshot(
                PriceQuote(source=source, timestamp=now, bid=Decimal(mid), ask=Decimal(mid), mid=Decimal(mid))
            )
            now += 1

        prices = await db.prices.get_recent_prices_for_source("uni-base_pool", limit=10)
        assert prices == [Decimal("0.000601"), Decimal("0.000602"), Decimal("0.000604")]


# =============================================================================
# Daily stats aggregation
# =============================================================================


class TestArbitrageStats:
    @pytest.mark.asyncio
    async def test_daily_stats_aggregate_profit_across_both_pipelines(self, db):
        """get_arbitrage_stats drives the dashboard's daily P&L view.

        Both CEX-DEX and DEX-DEX pipelines write to arb_attempts with different
        pipeline values. A detected-but-not-executed opportunity must count toward
        total_detected but must not inflate executed count or profit.
        """
        cex_opp = ArbitrageOpportunity(
            id="cex-1", timestamp=int(time.time() * 1000),
            buy_venue="quidax", sell_venue="uni-base",
            direction="QUIDAX_TO_UNI_BASE",
            buy_price=Decimal("0.000605"), sell_price=Decimal("0.000615"),
            gross_spread_bps=17, net_spread_bps=7,
            recommended_size_usd=Decimal("500"), expected_profit_usd=Decimal("1.50"),
            status="completed", actual_profit_usd=Decimal("1.50"),
        )
        await db.arbitrage.insert_arbitrage_opportunity(cex_opp)

        dex_opp = DexArbOpportunity(
            id="dex-1", timestamp=int(time.time() * 1000),
            direction="UNI_BASE_TO_UNI_BSC_DELTA_BALANCE",
            optimal_size_usd=Decimal("500"), expected_profit_usd=Decimal("1.20"),
            cngn_transferred=Decimal("800000"), expected_usd_out=Decimal("501.20"),
            status="detected", net_spread_bps=24,
        )
        await db.arbitrage.insert_dex_arbitrage_opportunity(dex_opp)
        await db.arbitrage.update_dex_arbitrage_execution_state("dex-1", status="completed", actual_profit_usd=2.00)

        stale = DexArbOpportunity(
            id="dex-2", timestamp=int(time.time() * 1000),
            direction="UNI_BASE_TO_UNI_BSC_DELTA_BALANCE",
            optimal_size_usd=Decimal("500"), expected_profit_usd=Decimal("1.20"),
            cngn_transferred=Decimal("800000"), expected_usd_out=Decimal("501.20"),
            status="detected", net_spread_bps=24,
        )
        await db.arbitrage.insert_dex_arbitrage_opportunity(stale)

        stats = await db.arbitrage.get_arbitrage_stats(0)
        assert stats["opportunities_detected"] == 3
        assert stats["opportunities_executed"] == 2
        assert stats["total_profit_usd"] == Decimal("3.50")


# =============================================================================
# Arbitrage history time-boundary behaviour
# =============================================================================


class TestArbitrageHistoryBoundaries:
    @pytest.mark.asyncio
    async def test_from_ts_includes_routed_event_before_window(self, db):
        """When from_ts is set, the 'routed' event timestamped before from_ts must still
        be returned so the item can be built correctly.

        Without this, the dashboard would show completed trades missing their
        entry timestamp and initial sizing.
        """
        await db.history.upsert_arbitrage_history_event(
            _make_history_event("opp-1", event_type="routed", status="routed", timestamp=100)
        )
        await db.history.upsert_arbitrage_history_event(
            _make_history_event(
                "opp-1", event_type="executed", status="completed",
                timestamp=300, actual_profit_usd=Decimal("3"), executed_size_usd=Decimal("400"),
            )
        )

        items = await db.history.get_arbitrage_history(from_ts=200)
        assert len(items) == 1
        assert items[0].routed_at == 100
        assert items[0].optimal_size_usd == Decimal("500")
        assert items[0].actual_profit_usd == Decimal("3")

    @pytest.mark.asyncio
    async def test_to_ts_does_not_leak_later_terminal_events(self, db):
        """Grouped results must not pull in events beyond the requested upper bound.

        A late failure event (e.g. execution_error at t=400) must not overwrite
        the completed status when the query is bounded to t=250.
        """
        await db.history.upsert_arbitrage_history_event(
            _make_history_event("opp-1", event_type="routed", status="routed", timestamp=100)
        )
        await db.history.upsert_arbitrage_history_event(
            _make_history_event(
                "opp-1", event_type="executed", status="completed",
                timestamp=200, actual_profit_usd=Decimal("3"), executed_size_usd=Decimal("400"),
            )
        )
        await db.history.upsert_arbitrage_history_event(
            _make_history_event("opp-1", event_type="failed", status="execution_error", timestamp=400)
        )

        items = await db.history.get_arbitrage_history(to_ts=250)
        assert len(items) == 1
        assert items[0].latest_status == "completed"
        assert items[0].actual_profit_usd == Decimal("3")
        assert items[0].reason is None

    @pytest.mark.asyncio
    async def test_pipeline_and_to_ts_filter_correctly_combined(self, db):
        """pipeline filter on the detail query must not drop lifecycle events for matched opps."""
        await db.history.upsert_arbitrage_history_event(
            _make_history_event("cex-1", pipeline="cex_dex", event_type="routed", status="routed", timestamp=100)
        )
        await db.history.upsert_arbitrage_history_event(
            _make_history_event(
                "cex-1", pipeline="cex_dex", event_type="executed", status="completed",
                timestamp=200, actual_profit_usd=Decimal("2"), executed_size_usd=Decimal("400"),
            )
        )
        await db.history.upsert_arbitrage_history_event(
            _make_history_event("cex-1", pipeline="cex_dex", event_type="failed", status="execution_error", timestamp=500)
        )
        # Different pipeline — must not appear
        await db.history.upsert_arbitrage_history_event(
            _make_history_event("dex-1", pipeline="dex_dex", event_type="routed", status="routed")
        )

        items = await db.history.get_arbitrage_history(pipeline="cex_dex", to_ts=300)
        assert len(items) == 1
        assert items[0].opportunity_id == "cex-1"
        assert items[0].latest_status == "completed"
        assert items[0].actual_profit_usd == Decimal("2")
