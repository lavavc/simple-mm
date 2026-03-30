"""Scheduler tests using FakeDexAdapter + mocked DB."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from engine.api.schemas import DexParams
from engine.venues.dex.shared import PositionState
from engine.core.scheduler import TradingScheduler, SchedulerConfig
from tests.fakes import FakeDexAdapter


# =============================================================================
# Helpers
# =============================================================================


def _make_position(
    in_range: bool = True,
    current_price: float = 0.000606,
    price_lower: float = 0.0005,
    price_upper: float = 0.0007,
    token_id: int = 42,
) -> PositionState:
    return PositionState(
        token_id=token_id,
        liquidity=1_000_000,
        tick_lower=-1000,
        tick_upper=1000,
        tokens_owed_0=0,
        tokens_owed_1=0,
        price_lower=Decimal(str(price_lower)),
        price_upper=Decimal(str(price_upper)),
        current_price=Decimal(str(current_price)),
        in_range=in_range,
    )


class MockDB:
    """Minimal DB double for scheduler tests."""

    def __init__(self, prices=None):
        self._prices = prices if prices is not None else [Decimal("0.000606")] * 20
        self.insert_action = AsyncMock()

    async def get_recent_prices(self, limit=100):
        return self._prices[:limit]


def _build_scheduler(venues: dict, broadcasts: list, db: MockDB) -> TradingScheduler:
    """Build a minimal TradingScheduler without calling __init__ or start()."""
    sched = TradingScheduler.__new__(TradingScheduler)
    sched._trading_enabled = True
    sched.venues = venues
    sched.broadcast = broadcasts.append
    sched.config = SchedulerConfig()
    sched.price_aggregator = MagicMock()
    sched.blended_calculator = None
    sched.arbitrage_engine = None
    sched.account_manager = None
    sched.token_contracts = {}
    sched.quidax_lp = None
    sched._started = False
    sched._dex_bootstrap_pending = True
    sched._dex_bootstrap_task = None
    sched._db = db  # store for patching
    sched.ws_listener = MagicMock(active_connections=set())
    return sched


# =============================================================================
# _check_dex_rebalance
# =============================================================================


class TestCheckDexRebalance:

    @pytest.mark.asyncio
    async def test_position_in_range_no_rebalance(self, fake_dex_adapter):
        """Position in range: rebalance should NOT be triggered."""
        pos = _make_position(in_range=True)
        fake_dex_adapter._positions = [pos]

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            await sched._check_dex_rebalance()

        # No mint happened (no rebalance triggered)
        assert len(fake_dex_adapter.minted) == 0

    @pytest.mark.asyncio
    async def test_out_of_range_below_threshold_no_rebalance(self, fake_dex_adapter):
        """Out of range but only 0.5% past boundary — below 2% threshold."""
        pos = _make_position(
            in_range=False,
            current_price=0.000603,  # slightly below price_lower=0.0005 ? No...
            price_lower=0.000605,
            price_upper=0.000700,
        )
        # Distance = (0.000605 - 0.000603) / 0.000605 * 100 ≈ 0.33% < 2%
        fake_dex_adapter._positions = [pos]

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            await sched._check_dex_rebalance()

        assert len(fake_dex_adapter.minted) == 0

    @pytest.mark.asyncio
    async def test_out_of_range_beyond_threshold_triggers_rebalance(self, fake_dex_adapter):
        """Out of range > 2% past boundary triggers rebalance."""
        pos = _make_position(
            in_range=False,
            current_price=0.0004,   # well below price_lower=0.0005
            price_lower=0.0005,
            price_upper=0.0007,
        )
        # Distance = (0.0005 - 0.0004) / 0.0005 * 100 = 20% > 2% threshold
        fake_dex_adapter._positions = [pos]

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            await sched._check_dex_rebalance()

        # Rebalance triggered: old position removed, new one minted
        assert len(fake_dex_adapter._positions) == 1  # removed old, added new
        assert len(fake_dex_adapter.minted) == 1

    @pytest.mark.asyncio
    async def test_no_positions_skipped(self, fake_dex_adapter):
        fake_dex_adapter._positions = []

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            await sched._check_dex_rebalance()

        assert len(fake_dex_adapter.minted) == 0

    @pytest.mark.asyncio
    async def test_paused_venue_skipped(self, fake_dex_adapter):
        """Paused venue must not trigger rebalance."""
        fake_dex_adapter.paused = True
        pos = _make_position(
            in_range=False, current_price=0.0004,
            price_lower=0.0005, price_upper=0.0007,
        )
        fake_dex_adapter._positions = [pos]

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            await sched._check_dex_rebalance()

        assert len(fake_dex_adapter.minted) == 0


# =============================================================================
# _rebalance_dex_position
# =============================================================================


class TestRebalanceDexPosition:

    @pytest.mark.asyncio
    async def test_remove_fails_broadcasts_error(self, fake_dex_adapter):
        """If remove_position fails, broadcast an error alert and return False."""
        adapter = FakeDexAdapter(remove_fails=True)
        pos = _make_position()

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            result = await sched._rebalance_dex_position(adapter, pos.token_id, pos)

        assert result is False
        alert_msgs = [b.get("message", "") for b in broadcasts if b.get("type") == "alert"]
        assert any("removal failed" in m or "failed" in m.lower() for m in alert_msgs)

    @pytest.mark.asyncio
    async def test_trade_account_sufficient_transfers_tokens(self, fake_dex_adapter):
        """Ample trade account: tokens should be transferred before reminting."""
        # LP wallet has 0 tokens, trade wallet has plenty
        adapter = FakeDexAdapter(
            token0_bal=Decimal("0"),
            token1_bal=Decimal("0"),
            trade0_bal=Decimal("500000"),
            trade1_bal=Decimal("600"),
        )
        adapter.params = DexParams(
            deploy_token0=Decimal("100000"),
            deploy_token1=Decimal("100"),
            rebalance_threshold_percent=Decimal("2.0"),
        )
        pos = _make_position(token_id=99)
        adapter._positions = [pos]

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            result = await sched._rebalance_dex_position(adapter, pos.token_id, pos)

        assert result is True
        assert len(adapter.transfers) > 0

    @pytest.mark.asyncio
    async def test_insufficient_trade_warns_and_remints(self, fake_dex_adapter):
        """Trade account can't cover shortfall — warning broadcast, remint still attempted."""
        adapter = FakeDexAdapter(
            token0_bal=Decimal("0"),
            token1_bal=Decimal("0"),
            trade0_bal=Decimal("10"),  # far less than deploy_token0
            trade1_bal=Decimal("1"),
        )
        adapter.params = DexParams(
            deploy_token0=Decimal("100000"),
            deploy_token1=Decimal("100"),
            rebalance_threshold_percent=Decimal("2.0"),
        )
        pos = _make_position(token_id=77)
        adapter._positions = [pos]

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            await sched._rebalance_dex_position(adapter, pos.token_id, pos)

        warning_msgs = [b.get("message", "") for b in broadcasts if b.get("severity") == "warning"]
        assert any("treasury refill" in m or "need" in m for m in warning_msgs)

    @pytest.mark.asyncio
    async def test_recovery_price_passed_to_create(self, fake_dex_adapter):
        """recovery_price = current position price is passed to _create_dex_position."""
        pos = _make_position(in_range=False, current_price=0.0004, price_lower=0.0005, price_upper=0.0007)
        fake_dex_adapter._positions = [pos]

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        create_calls = []
        original_create = sched._create_dex_position

        async def fake_create(venue, recovery_price=None):
            create_calls.append(recovery_price)
            return True

        sched._create_dex_position = fake_create

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            await sched._rebalance_dex_position(fake_dex_adapter, pos.token_id, pos)

        assert len(create_calls) == 1
        assert create_calls[0] == float(pos.current_price)


# =============================================================================
# _create_dex_position
# =============================================================================


class TestCreateDexPosition:

    @pytest.mark.asyncio
    async def test_fewer_than_10_prices_returns_false(self, fake_dex_adapter):
        """Fewer than 10 price history points: return False without attempting mint."""
        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 5)  # only 5 prices
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            result = await sched._create_dex_position(fake_dex_adapter)

        assert result is False
        assert len(fake_dex_adapter.minted) == 0

    @pytest.mark.asyncio
    async def test_successful_mint_calls_insert_action(self, fake_dex_adapter):
        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            result = await sched._create_dex_position(fake_dex_adapter)

        assert result is True
        db.insert_action.assert_called_once()
        _, kwargs = db.insert_action.call_args
        assert kwargs.get("status") == "confirmed" or db.insert_action.call_args[0][2] == "confirmed"

    @pytest.mark.asyncio
    async def test_mint_failure_calls_insert_action_failed(self, fake_dex_adapter):
        """Failed mint records insert_action with status=failed."""
        adapter = FakeDexAdapter(mint_fails=True)
        adapter.params = DexParams(
            deploy_token0=Decimal("100"),
            deploy_token1=Decimal("100"),
            rebalance_threshold_percent=Decimal("2.0"),
        )

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            result = await sched._create_dex_position(adapter)

        assert result is False
        db.insert_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_mint_broadcasts_position_created(self, fake_dex_adapter):
        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            await sched._create_dex_position(fake_dex_adapter)

        action_broadcasts = [b for b in broadcasts if b.get("type") == "action"]
        assert any(b.get("data", {}).get("action") == "position_created" for b in action_broadcasts)

    @pytest.mark.asyncio
    async def test_recovery_price_passed_through(self, fake_dex_adapter):
        """recovery_price is forwarded to calculate_tick_range."""
        prices_received = []
        original = fake_dex_adapter.calculate_tick_range

        def fake_range(prices, recovery_price=None):
            prices_received.append(recovery_price)
            return -1000, 1000

        fake_dex_adapter.calculate_tick_range = fake_range

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.core.scheduler.get_db", AsyncMock(return_value=db)):
            await sched._create_dex_position(fake_dex_adapter, recovery_price=0.000400)

        assert len(prices_received) == 1
        assert prices_received[0] == 0.000400


class TestDexArbCurveStream:

    @pytest.mark.asyncio
    async def test_bootstrap_runs_initial_dex_recalc(self):
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({}, broadcasts, db)
        sched.arbitrage_engine = MagicMock()
        call_order = []
        sched.arbitrage_engine.on_dex_dex_update = AsyncMock(side_effect=lambda: call_order.append("arb"))
        sched._update_gas_oracle = AsyncMock(side_effect=lambda: call_order.append("gas"))

        with patch("engine.core.arbitrage.pool_state.seed_dex_pool_states", AsyncMock()) as seed_mock, \
             patch("engine.core.gas_oracle.gas_usd_base", return_value=Decimal("1")), \
             patch("engine.core.gas_oracle.gas_usd_bsc", return_value=Decimal("1")):
            await sched._bootstrap_dex_arb_curve()

        seed_mock.assert_awaited_once()
        sched._update_gas_oracle.assert_awaited_once()
        sched.arbitrage_engine.on_dex_dex_update.assert_awaited_once()
        assert call_order == ["gas", "arb"]
        assert sched._dex_bootstrap_pending is False

    @pytest.mark.asyncio
    async def test_bootstrap_waits_when_gas_missing(self):
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({}, broadcasts, db)
        sched.arbitrage_engine = MagicMock()
        sched.arbitrage_engine.on_dex_dex_update = AsyncMock()
        sched._update_gas_oracle = AsyncMock()

        with patch("engine.core.arbitrage.pool_state.seed_dex_pool_states", AsyncMock()) as seed_mock, \
             patch("engine.core.gas_oracle.gas_usd_base", return_value=None), \
             patch("engine.core.gas_oracle.gas_usd_bsc", return_value=None):
            await sched._bootstrap_dex_arb_curve()

        seed_mock.assert_awaited_once()
        sched._update_gas_oracle.assert_awaited_once()
        sched.arbitrage_engine.on_dex_dex_update.assert_not_awaited()
        assert sched._dex_bootstrap_pending is True

    @pytest.mark.asyncio
    async def test_gas_update_schedules_pending_bootstrap(self):
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({}, broadcasts, db)
        sched._schedule_dex_bootstrap = MagicMock()

        with patch("engine.core.gas_oracle.update", AsyncMock()):
            await sched._update_gas_oracle()

        sched._schedule_dex_bootstrap.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_dex_recalc_when_ws_healthy(self):
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({}, broadcasts, db)
        sched.ws_listener.active_connections = {"base", "bsc"}
        sched.arbitrage_engine = MagicMock()
        sched.arbitrage_engine.on_dex_dex_update = AsyncMock()

        await sched._stream_dex_arb_curve()

        sched.arbitrage_engine.on_dex_dex_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_dex_recalc_when_ws_unhealthy(self):
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({}, broadcasts, db)
        sched.ws_listener.active_connections = {"base"}
        sched.arbitrage_engine = MagicMock()
        sched.arbitrage_engine.on_dex_dex_update = AsyncMock()

        await sched._stream_dex_arb_curve()

        sched.arbitrage_engine.on_dex_dex_update.assert_awaited_once()
