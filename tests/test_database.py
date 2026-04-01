"""Tests for async SQLite database operations."""

import pytest
import time
from decimal import Decimal

from engine.db.database import Database
from engine.api.schemas import (
    PriceQuote, Position, Alert, ArbitrageOpportunity, DexArbOpportunity,
    ArbitrageHistoryEvent, ArbitrageHistoryWalletSnapshot,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def db(tmp_path):
    """Create an in-memory database for testing."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def sample_quote():
    return PriceQuote(
        source="quidax",
        timestamp=int(time.time() * 1000),
        bid=Decimal("0.000696"),
        ask=Decimal("0.000698"),
        mid=Decimal("0.000697"),
    )


@pytest.fixture
def sample_position():
    return Position(
        venue="uni-base",
        pair="cNGN/USDC",
        timestamp=int(time.time() * 1000),
        balances={"cngn": Decimal("10000"), "usdc": Decimal("50")},
    )


# =============================================================================
# Connection
# =============================================================================


class TestConnection:
    """Test database connection lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_creates_tables(self, db):
        """Tables should be created on connect."""
        cursor = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row["name"] for row in await cursor.fetchall()}

        assert "system_state" in tables
        assert "price_snapshots" in tables
        assert "positions" in tables
        assert "actions" in tables
        assert "venue_config" in tables
        assert "alerts" in tables
    @pytest.mark.asyncio
    async def test_connect_idempotent(self, tmp_path):
        """Calling connect twice should not error."""
        db = Database(str(tmp_path / "test2.db"))
        await db.connect()
        await db.connect()  # Should not raise
        await db.close()


# =============================================================================
# System state
# =============================================================================


class TestSystemState:
    """Test key-value system state store."""

    @pytest.mark.asyncio
    async def test_set_and_get_state(self, db):
        await db.set_system_state("trading_enabled", "true")
        val = await db.get_system_state("trading_enabled")
        assert val == "true"

    @pytest.mark.asyncio
    async def test_get_missing_state_returns_none(self, db):
        val = await db.get_system_state("nonexistent")
        assert val is None

    @pytest.mark.asyncio
    async def test_update_state(self, db):
        await db.set_system_state("key", "v1")
        await db.set_system_state("key", "v2")
        val = await db.get_system_state("key")
        assert val == "v2"


# =============================================================================
# Price snapshots
# =============================================================================


class TestPriceSnapshots:
    """Test price snapshot CRUD."""

    @pytest.mark.asyncio
    async def test_insert_and_retrieve(self, db, sample_quote):
        await db.insert_price_snapshot(sample_quote)
        history = await db.get_price_history(limit=10)

        assert len(history) == 1
        assert history[0]["source"] == "quidax"
        assert abs(history[0]["mid"] - 0.000697) < 0.0001

    @pytest.mark.asyncio
    async def test_insert_multiple_sources(self, db):
        for source, mid in [("quidax", "0.000697"), ("bybit_p2p", "1437")]:
            q = PriceQuote(
                source=source,
                timestamp=int(time.time() * 1000),
                bid=Decimal(mid), ask=Decimal(mid), mid=Decimal(mid),
            )
            await db.insert_price_snapshot(q)

        history = await db.get_price_history(limit=10)
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_get_recent_prices(self, db):
        """get_recent_prices returns mid values in chronological order."""
        for i in range(5):
            q = PriceQuote(
                source="quidax",
                timestamp=1000 + i,
                bid=Decimal("0.000690") + Decimal(str(i)) * Decimal("0.000001"),
                ask=Decimal("0.000690") + Decimal(str(i)) * Decimal("0.000001"),
                mid=Decimal("0.000690") + Decimal(str(i)) * Decimal("0.000001"),
            )
            await db.insert_price_snapshot(q)

        prices = await db.get_recent_prices(limit=5)
        assert len(prices) == 5
        # Should be in chronological order (ascending)
        for i in range(len(prices) - 1):
            assert prices[i] <= prices[i + 1]

    @pytest.mark.asyncio
    async def test_price_history_with_time_filter(self, db):
        """Should filter by timestamp range."""
        now_ms = int(time.time() * 1000)
        for offset in [0, 10000, 20000]:
            q = PriceQuote(
                source="quidax",
                timestamp=now_ms - offset,
                bid=Decimal("0.000697"), ask=Decimal("0.000697"), mid=Decimal("0.000697"),
            )
            await db.insert_price_snapshot(q)

        # Only last 15 seconds
        history = await db.get_price_history(from_ts=now_ms - 15000, limit=10)
        assert len(history) == 2  # 0ms and 10000ms ago

    @pytest.mark.asyncio
    async def test_snapshots_in_window(self, db):
        """get_price_snapshots_in_window filters by time and source."""
        now_ms = int(time.time() * 1000)
        for source in ["quidax", "bybit_p2p"]:
            q = PriceQuote(
                source=source, timestamp=now_ms,
                bid=Decimal("0.0007"), ask=Decimal("0.0007"), mid=Decimal("0.0007"),
            )
            await db.insert_price_snapshot(q)

        snaps = await db.get_price_snapshots_in_window(
            from_ts=now_ms - 1000, to_ts=now_ms + 1000, source="quidax",
        )
        assert len(snaps) == 1
        assert snaps[0]["source"] == "quidax"


# =============================================================================
# Alerts
# =============================================================================


class TestAlerts:
    """Test alert CRUD."""

    @pytest.mark.asyncio
    async def test_insert_and_retrieve_alert(self, db):
        alert_id = await db.insert_alert(
            severity="warning",
            category="refill",
            message="Low ETH balance on uni-base-lp",
        )
        assert alert_id > 0

        alerts = await db.get_alerts(limit=10)
        assert len(alerts) == 1
        assert alerts[0].severity == "warning"
        assert alerts[0].message == "Low ETH balance on uni-base-lp"

    @pytest.mark.asyncio
    async def test_acknowledge_alert(self, db):
        alert_id = await db.insert_alert(
            severity="critical",
            category="test",
            message="Test alert",
        )

        await db.acknowledge_alert(alert_id)
        alerts = await db.get_alerts(limit=10)
        assert alerts[0].acknowledged is True


# =============================================================================
# Actions
# =============================================================================


class TestDexArbOpportunities:
    """Test DEX arbitrage opportunity persistence and stats aggregation."""

    def _sample_opp(self, opp_id="dex-arb-1", status="detected"):
        return DexArbOpportunity(
            id=opp_id,
            timestamp=int(time.time() * 1000),
            direction="UNI_BASE_TO_UNI_BSC_DELTA_BALANCE",
            optimal_size_usd=Decimal("500"),
            expected_profit_usd=Decimal("1.20"),
            cngn_transferred=Decimal("800000"),
            expected_usd_out=Decimal("501.20"),
            status=status,
            net_spread_bps=24,
            gas_usd=Decimal("0.08"),
        )

    @pytest.mark.asyncio
    async def test_insert_and_read_back(self, db):
        opp = self._sample_opp()
        await db.insert_dex_arbitrage_opportunity(opp)
        result = await db.get_dex_arbitrage_opportunity(opp.id)
        assert result is not None
        assert result.direction == opp.direction
        assert result.optimal_size_usd == opp.optimal_size_usd
        assert result.cngn_transferred == opp.cngn_transferred
        assert result.gas_usd == opp.gas_usd
        assert result.buy_amount_cngn is None  # not set until buy fills

    @pytest.mark.asyncio
    async def test_execution_state_update_writes_profit_and_buy_amount(self, db):
        opp = self._sample_opp()
        await db.insert_dex_arbitrage_opportunity(opp)

        await db.update_dex_arbitrage_execution_state(
            opp.id,
            status="buy_filled",
            buy_tx_hash="0xabc",
            buy_amount_cngn=Decimal("798000"),
        )
        mid = await db.get_dex_arbitrage_opportunity(opp.id)
        assert mid.status == "buy_filled"
        assert mid.buy_tx_hash == "0xabc"
        assert mid.buy_amount_cngn == Decimal("798000")
        assert mid.actual_profit_usd is None  # not yet completed

        await db.update_dex_arbitrage_execution_state(
            opp.id,
            status="completed",
            sell_tx_hash="0xdef",
            actual_profit_usd=1.15,
        )
        done = await db.get_dex_arbitrage_opportunity(opp.id)
        assert done.status == "completed"
        assert done.sell_tx_hash == "0xdef"
        assert done.actual_profit_usd == Decimal("1.15")
        assert done.buy_amount_cngn == Decimal("798000")  # preserved across updates

    @pytest.mark.asyncio
    async def test_daily_stats_aggregate_profit_across_both_pipelines(self, db):
        """get_arbitrage_stats drives the dashboard's daily P&L view.

        The engine runs two independent arb pipelines — CEX-DEX and DEX-DEX —
        each writing to a separate table. Both must contribute to the profit total.
        A detected-but-not-executed opportunity should count toward total detected
        but must not inflate the execution count or profit.
        """
        # CEX-DEX trade: detected and completed, $1.50 profit
        cex_dex_opp = ArbitrageOpportunity(
            id="cex-1", timestamp=int(time.time() * 1000),
            buy_venue="quidax", sell_venue="uni-base",
            buy_price=Decimal("0.000605"), sell_price=Decimal("0.000615"),
            gross_spread_bps=17, net_spread_bps=7,
            recommended_size_usd=Decimal("500"), expected_profit_usd=Decimal("1.50"),
            status="completed", actual_profit_usd=Decimal("1.50"),
        )
        await db.insert_arbitrage_opportunity(cex_dex_opp)

        # DEX-DEX trade: detected and completed, $2.00 profit
        dex_dex_opp = self._sample_opp("dex-1")
        await db.insert_dex_arbitrage_opportunity(dex_dex_opp)
        await db.update_dex_arbitrage_execution_state("dex-1", status="completed", actual_profit_usd=2.00)

        # DEX-DEX opportunity that was detected but never executed — should not add to profit
        dex_dex_stale = self._sample_opp("dex-2")
        await db.insert_dex_arbitrage_opportunity(dex_dex_stale)

        stats = await db.get_arbitrage_stats(0)

        assert stats["opportunities_detected"] == 3
        assert stats["opportunities_executed"] == 2
        assert stats["total_profit_usd"] == Decimal("3.50")


class TestActions:
    """Test action logging."""

    @pytest.mark.asyncio
    async def test_insert_action(self, db):
        await db.insert_action(
            venue="quidax",
            action_type="order_placed",
            triggered_by="scheduler",
            status="completed",
            direction="buy",
            price=0.000697,
        )

        actions = await db.get_actions(limit=10)
        assert len(actions) == 1
        assert actions[0]["venue"] == "quidax"
        assert actions[0]["action_type"] == "order_placed"
        assert actions[0]["status"] == "completed"


# =============================================================================
# Arbitrage history
# =============================================================================


def _make_history_event(
    opp_id: str,
    event_type: str = "routed",
    status: str = "routed",
    pipeline: str = "cex_dex",
    direction: str = "QUIDAX_TO_UNI_BSC",
    timestamp: int = 1_000_000,
    reason: str | None = None,
    actual_profit_usd: Decimal | None = None,
    executed_size_usd: Decimal | None = None,
    buy_tx_hash: str | None = None,
    sell_tx_hash: str | None = None,
    buy_wallet: ArbitrageHistoryWalletSnapshot | None = None,
    sell_wallet: ArbitrageHistoryWalletSnapshot | None = None,
) -> ArbitrageHistoryEvent:
    return ArbitrageHistoryEvent(
        opportunity_id=opp_id,
        pipeline=pipeline,
        event_type=event_type,
        timestamp=timestamp,
        direction=direction,
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
        buy_wallet=buy_wallet,
        sell_wallet=sell_wallet,
        buy_tx_hash=buy_tx_hash,
        sell_tx_hash=sell_tx_hash,
    )


class TestArbitrageHistory:
    """Tests for arbitrage lifecycle history storage and retrieval."""

    @pytest.mark.asyncio
    async def test_upsert_inserts_event(self, db):
        event = _make_history_event("opp-1")
        await db.upsert_arbitrage_history_event(event)

        items = await db.get_arbitrage_history()
        assert len(items) == 1
        assert items[0].opportunity_id == "opp-1"
        assert items[0].latest_status == "routed"

    @pytest.mark.asyncio
    async def test_upsert_updates_on_conflict(self, db):
        """Second upsert for same (opp_id, event_type) refreshes the record."""
        await db.upsert_arbitrage_history_event(_make_history_event("opp-1", status="routed"))
        await db.upsert_arbitrage_history_event(
            _make_history_event("opp-1", status="routed", reason="updated")
        )

        items = await db.get_arbitrage_history()
        assert len(items) == 1
        assert items[0].reason == "updated"

    @pytest.mark.asyncio
    async def test_full_lifecycle_returns_correct_fields(self, db):
        """routed + executed events are grouped into one item with correct field sourcing."""
        await db.upsert_arbitrage_history_event(
            _make_history_event("opp-1", event_type="routed", status="routed", timestamp=1000)
        )
        await db.upsert_arbitrage_history_event(
            _make_history_event(
                "opp-1",
                event_type="executed",
                status="completed",
                timestamp=2000,
                actual_profit_usd=Decimal("4.50"),
                executed_size_usd=Decimal("400"),
                buy_tx_hash="0xabc",
                sell_tx_hash="0xdef",
            )
        )

        items = await db.get_arbitrage_history()
        assert len(items) == 1
        item = items[0]
        assert item.routed_at == 1000
        assert item.updated_at == 2000
        assert item.latest_event_type == "executed"
        assert item.latest_status == "completed"
        assert item.actual_profit_usd == Decimal("4.50")
        assert item.buy_tx_hash == "0xabc"
        assert item.sell_tx_hash == "0xdef"

    @pytest.mark.asyncio
    async def test_from_ts_includes_routed_event_before_window(self, db):
        """When from_ts is set, the 'routed' event timestamped before from_ts must still
        be returned so the item can be built correctly — only the opp selection is filtered."""
        await db.upsert_arbitrage_history_event(
            _make_history_event("opp-1", event_type="routed", status="routed", timestamp=100)
        )
        await db.upsert_arbitrage_history_event(
            _make_history_event(
                "opp-1",
                event_type="executed",
                status="completed",
                timestamp=300,
                actual_profit_usd=Decimal("3"),
                executed_size_usd=Decimal("400"),
            )
        )

        # from_ts=200 is after the routed event but before the executed event
        items = await db.get_arbitrage_history(from_ts=200)
        assert len(items) == 1
        item = items[0]
        # routed_at must come from the actual routed event, not the executed one
        assert item.routed_at == 100
        assert item.optimal_size_usd == Decimal("500")  # sourced from routed event
        assert item.actual_profit_usd == Decimal("3")

    @pytest.mark.asyncio
    async def test_from_ts_excludes_fully_old_opps(self, db):
        """An opp whose latest event is before from_ts must not appear."""
        await db.upsert_arbitrage_history_event(
            _make_history_event("opp-old", event_type="routed", status="routed", timestamp=50)
        )
        await db.upsert_arbitrage_history_event(
            _make_history_event("opp-new", event_type="routed", status="routed", timestamp=500)
        )

        items = await db.get_arbitrage_history(from_ts=200)
        assert len(items) == 1
        assert items[0].opportunity_id == "opp-new"

    @pytest.mark.asyncio
    async def test_to_ts_does_not_leak_later_terminal_events(self, db):
        """Grouped results should not pull in later events beyond the requested upper bound."""
        await db.upsert_arbitrage_history_event(
            _make_history_event("opp-1", event_type="routed", status="routed", timestamp=100)
        )
        await db.upsert_arbitrage_history_event(
            _make_history_event(
                "opp-1",
                event_type="executed",
                status="completed",
                timestamp=200,
                actual_profit_usd=Decimal("3"),
                executed_size_usd=Decimal("400"),
            )
        )
        await db.upsert_arbitrage_history_event(
            _make_history_event(
                "opp-1",
                event_type="failed",
                status="execution_error",
                timestamp=400,
                reason="late failure",
            )
        )

        items = await db.get_arbitrage_history(to_ts=250)
        assert len(items) == 1
        item = items[0]
        assert item.latest_status == "completed"
        assert item.updated_at == 200
        assert item.actual_profit_usd == Decimal("3")
        assert item.reason is None

    @pytest.mark.asyncio
    async def test_pipeline_and_to_ts_combined(self, db):
        """pipeline filter on the detail query must not drop lifecycle events for matched opps."""
        await db.upsert_arbitrage_history_event(
            _make_history_event("cex-1", pipeline="cex_dex", event_type="routed", status="routed", timestamp=100)
        )
        await db.upsert_arbitrage_history_event(
            _make_history_event(
                "cex-1", pipeline="cex_dex", event_type="executed", status="completed",
                timestamp=200, actual_profit_usd=Decimal("2"), executed_size_usd=Decimal("400"),
            )
        )
        await db.upsert_arbitrage_history_event(
            _make_history_event("cex-1", pipeline="cex_dex", event_type="failed", status="execution_error", timestamp=500)
        )
        # Unrelated opp on a different pipeline — must not appear.
        await db.upsert_arbitrage_history_event(
            _make_history_event("dex-1", pipeline="dex_dex", direction="UNI_BSC_TO_UNI_BASE_DELTA_BALANCE")
        )

        items = await db.get_arbitrage_history(pipeline="cex_dex", to_ts=300)
        assert len(items) == 1
        item = items[0]
        assert item.opportunity_id == "cex-1"
        assert item.latest_status == "completed"
        assert item.actual_profit_usd == Decimal("2")

    @pytest.mark.asyncio
    async def test_pipeline_filter(self, db):
        await db.upsert_arbitrage_history_event(
            _make_history_event("cex-1", pipeline="cex_dex")
        )
        await db.upsert_arbitrage_history_event(
            _make_history_event(
                "dex-1",
                pipeline="dex_dex",
                direction="UNI_BSC_TO_UNI_BASE_DELTA_BALANCE",
            )
        )

        cex_items = await db.get_arbitrage_history(pipeline="cex_dex")
        assert len(cex_items) == 1
        assert cex_items[0].opportunity_id == "cex-1"

        dex_items = await db.get_arbitrage_history(pipeline="dex_dex")
        assert len(dex_items) == 1
        assert dex_items[0].opportunity_id == "dex-1"

    @pytest.mark.asyncio
    async def test_limit(self, db):
        for i in range(5):
            await db.upsert_arbitrage_history_event(
                _make_history_event(f"opp-{i}", timestamp=i * 100)
            )

        items = await db.get_arbitrage_history(limit=3)
        assert len(items) == 3

    @pytest.mark.asyncio
    async def test_wallet_snapshot_roundtrip(self, db):
        wallet = ArbitrageHistoryWalletSnapshot(
            stable_symbol="USDT",
            stable_balance=Decimal("1000"),
            cngn_balance=Decimal("500000"),
        )
        await db.upsert_arbitrage_history_event(
            _make_history_event("opp-1", buy_wallet=wallet, sell_wallet=wallet)
        )

        items = await db.get_arbitrage_history()
        assert items[0].buy_wallet is not None
        assert items[0].buy_wallet.stable_symbol == "USDT"
        assert items[0].buy_wallet.stable_balance == Decimal("1000")
        assert items[0].buy_wallet.cngn_balance == Decimal("500000")
