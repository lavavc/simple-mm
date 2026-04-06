"""Scheduler tests using FakeDexAdapter + mocked DB."""

import asyncio
import pytest
from typing import Any
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from engine.api.schemas import TxResult
from engine.venues.dex.shared import PositionState
from engine.scheduler import TradingScheduler, SchedulerConfig
from engine.venues.dex.lp_v4 import LPBalanceSwapResult
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

    def __init__(self, prices=None, prices_by_source=None):
        self._prices = prices if prices is not None else [Decimal("0.000606")] * 20
        self._prices_by_source = prices_by_source or {}
        self.recent_price_queries: list[tuple[str, int]] = []
        self.insert_action = AsyncMock()
        self.update_venue_config = AsyncMock()
        self.insert_price_snapshot = AsyncMock()
        self.insert_position = AsyncMock()
        self.insert_alert = AsyncMock()
        self.set_system_state = AsyncMock()

    async def get_recent_prices(self, limit=100):
        return self._prices[:limit]

    async def get_recent_prices_for_source(self, source, limit=100):
        self.recent_price_queries.append((source, limit))
        return self._prices_by_source.get(source, self._prices)[:limit]


def _build_scheduler(
    venues: dict,
    broadcasts: list,
    db: MockDB,
    *,
    price_aggregator: MagicMock | None = None,
    blended_calculator: Any = None,
    arbitrage_engine: Any = None,
    account_manager: Any = None,
    token_contracts: dict | None = None,
    portfolio_exposure_calculator: Any = None,
    quidax_lp: Any = None,
) -> TradingScheduler:
    """Build a minimal TradingScheduler through the real constructor."""
    return TradingScheduler(
        price_aggregator=price_aggregator or MagicMock(),
        venues=venues,
        config=SchedulerConfig(),
        broadcast=broadcasts.append,
        blended_calculator=blended_calculator,
        arbitrage_engine=arbitrage_engine,
        account_manager=account_manager,
        token_contracts=token_contracts or {},
        portfolio_exposure_calculator=portfolio_exposure_calculator,
        quidax_lp=quidax_lp,
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


def _make_ws_tracking_dex(name: str, chain_name: str) -> FakeDexAdapter:
    venue = FakeDexAdapter(name=name)
    venue.config.chain_name = chain_name
    venue.trade_account = SimpleNamespace(address=f"0x{name.replace('-', '')}")
    venue.stable_address = f"0x{name.replace('-', '')}stable"
    venue.cngn_address = f"0x{name.replace('-', '')}cngn"
    return venue


class TestSchedulerConstruction:

    def test_start_registers_jobs_from_context_dependencies(self):
        broadcasts = []
        db = MockDB()
        arb_engine = SimpleNamespace(on_dex_dex_update=AsyncMock())
        account_manager = MagicMock()
        blended_calculator = MagicMock()
        venues = {
            "uni-base": _make_ws_tracking_dex("uni-base", "base"),
            "uni-bsc": _make_ws_tracking_dex("uni-bsc", "bsc"),
            "quidax": MagicMock(),
            "quidax-lp": MagicMock(),
            "blockradar": MagicMock(),
        }
        sched = _build_scheduler(
            venues,
            broadcasts,
            db,
            arbitrage_engine=arb_engine,
            account_manager=account_manager,
            blended_calculator=blended_calculator,
        )

        def fake_create_task(coro):
            coro.close()
            return MagicMock()

        with patch("asyncio.create_task", side_effect=fake_create_task) as create_task_mock, patch.object(
            sched.scheduler, "start"
        ) as scheduler_start_mock:
            sched.start()

        job_ids = {job.id for job in sched.scheduler.get_jobs()}
        assert "balance_check" in job_ids
        assert "auto_fund_quidax_arb" in job_ids
        assert "auto_fund_quidax_lp" in job_ids
        assert "portfolio_delta" in job_ids
        assert "blockradar_rate_sync" in job_ids
        assert "dex_arb_curve_stream" in job_ids
        assert "quidax_depth_stream" in job_ids
        assert sched.ws_listener.on_dex_event is arb_engine.on_dex_dex_update
        assert sched.ws_listener.on_wallet_event == sched._handle_wallet_activity
        assert len(sched.ws_listener.wallet_subscriptions["base"]) == 2
        assert len(sched.ws_listener.wallet_subscriptions["bsc"]) == 2
        assert sched.state.started is True
        assert create_task_mock.call_count == 2
        scheduler_start_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_pause_resume_use_context_backed_system_state_store(self):
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({}, broadcasts, db)

        await sched.pause()
        await sched.resume()

        assert sched.trading_enabled is True
        assert broadcasts == [
            {"type": "system", "status": "paused"},
            {"type": "system", "status": "running"},
        ]
        db.set_system_state.assert_any_await("trading_enabled", "false")
        db.set_system_state.assert_any_await("trading_enabled", "true")


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

        await sched._check_dex_rebalance()

        # Rebalance triggered: old position removed, new one minted
        assert len(fake_dex_adapter._positions) == 1  # removed old, added new
        assert len(fake_dex_adapter.minted) == 1

    @pytest.mark.asyncio
    async def test_no_positions_no_funds_skipped(self, fake_dex_adapter):
        """No positions and no LP wallet balance: no mint attempted."""
        fake_dex_adapter._positions = []
        fake_dex_adapter._token0_bal = Decimal("0")
        fake_dex_adapter._token1_bal = Decimal("0")

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        await sched._check_dex_rebalance()

        assert len(fake_dex_adapter.minted) == 0

    @pytest.mark.asyncio
    async def test_no_positions_with_funds_triggers_initial_mint(self, fake_dex_adapter):
        """No positions but LP wallet has balance: initial position is created automatically."""
        fake_dex_adapter._positions = []
        # Default token0/token1 balances are non-zero

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        await sched._check_dex_rebalance()

        assert len(fake_dex_adapter.minted) == 1
        assert db.recent_price_queries == [("uni-base_pool", 100)]

    @pytest.mark.asyncio
    async def test_pause_while_job_waits_on_lock_skips_auto_lp_management(self, fake_dex_adapter):
        """A queued LP job must not remint after trading is paused."""
        fake_dex_adapter._positions = []

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        venue_lock = sched.lp_rebalancer._get_venue_lock(fake_dex_adapter.name)
        await venue_lock.acquire()
        try:
            check_task = asyncio.create_task(sched._check_dex_rebalance())
            await asyncio.sleep(0)
            await sched.pause()
        finally:
            venue_lock.release()

        await check_task

        assert len(fake_dex_adapter.minted) == 0
        assert db.recent_price_queries == []

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

        await sched._check_dex_rebalance()

        assert len(fake_dex_adapter.minted) == 0

    @pytest.mark.asyncio
    async def test_multiple_positions_halt_auto_management(self, fake_dex_adapter):
        fake_dex_adapter._positions = [_make_position(token_id=41), _make_position(token_id=42)]

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        await sched._check_dex_rebalance()

        assert len(fake_dex_adapter.minted) == 0
        assert any("multiple LP positions" in b.get("message", "") for b in broadcasts if b.get("type") == "alert")
        assert db.insert_action.await_args.kwargs["action_type"] == "lp_management_halted"
        assert db.insert_action.await_args.kwargs["idempotency_key"] == "lp_management_halted:uni-base:41,42"

    @pytest.mark.asyncio
    async def test_multiple_position_incident_broadcasts_once_until_resolved(self, fake_dex_adapter):
        fake_dex_adapter._positions = [_make_position(token_id=41), _make_position(token_id=42)]

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        await sched._check_dex_rebalance()
        await sched._check_dex_rebalance()

        alert_messages = [b.get("message", "") for b in broadcasts if b.get("type") == "alert"]
        assert len(alert_messages) == 1
        assert db.insert_action.await_count == 2
        assert db.insert_action.await_args_list[0].kwargs["idempotency_key"] == "lp_management_halted:uni-base:41,42"
        assert db.insert_action.await_args_list[1].kwargs["idempotency_key"] == "lp_management_halted:uni-base:41,42"

        fake_dex_adapter._positions = [_make_position(token_id=41)]
        await sched._check_dex_rebalance()

        fake_dex_adapter._positions = [_make_position(token_id=41), _make_position(token_id=42)]
        await sched._check_dex_rebalance()

        alert_messages = [b.get("message", "") for b in broadcasts if b.get("type") == "alert"]
        assert len(alert_messages) == 2

    @pytest.mark.asyncio
    async def test_missing_live_position_state_logs_warning_and_skips(self, fake_dex_adapter):
        fake_dex_adapter._positions = [_make_position(token_id=41)]
        fake_dex_adapter.get_position_state = MagicMock(return_value=None)

        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.lp.rebalancer.logger.warning") as warning_mock:
            await sched._check_dex_rebalance()

        assert len(fake_dex_adapter.minted) == 0
        warning_mock.assert_any_call(
            "lp_rebalance_skipped",
            venue="uni-base",
            token_id=41,
            owned_token_ids=[41],
            reason="position_state_unavailable",
        )


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

        result = await sched.lp_rebalancer.rebalance(adapter, pos.token_id, pos)

        assert result is False
        alert_msgs = [b.get("message", "") for b in broadcasts if b.get("type") == "alert"]
        assert any("removal failed" in m or "failed" in m.lower() for m in alert_msgs)

    @pytest.mark.asyncio
    async def test_successful_rebalance_remints(self, fake_dex_adapter):
        """Successful remove is followed by remint using full LP wallet balance."""
        pos = _make_position(token_id=99)
        fake_dex_adapter._positions = [pos]

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        result = await sched.lp_rebalancer.rebalance(fake_dex_adapter, pos.token_id, pos)

        assert result is True
        assert len(fake_dex_adapter.minted) == 1

    @pytest.mark.asyncio
    async def test_recovery_price_passed_to_create(self, fake_dex_adapter):
        """recovery_price = current position price is passed to create_position."""
        pos = _make_position(in_range=False, current_price=0.0004, price_lower=0.0005, price_upper=0.0007)
        fake_dex_adapter._positions = [pos]

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        create_calls = []

        async def fake_create(venue, recovery_price=None, triggered_by="auto:range_exit_rebalance"):
            create_calls.append((recovery_price, triggered_by))
            return True

        sched.lp_rebalancer._create_position_locked = fake_create

        await sched.lp_rebalancer.rebalance(fake_dex_adapter, pos.token_id, pos)

        assert len(create_calls) == 1
        assert create_calls[0] == (float(pos.current_price), "auto:range_exit_rebalance")


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

        result = await sched.lp_rebalancer.create_position(fake_dex_adapter)

        assert result is False
        assert len(fake_dex_adapter.minted) == 0

    @pytest.mark.asyncio
    async def test_successful_mint_calls_insert_action(self, fake_dex_adapter):
        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        result = await sched.lp_rebalancer.create_position(fake_dex_adapter)

        assert result is True
        db.insert_action.assert_called_once()
        _, kwargs = db.insert_action.call_args
        assert kwargs.get("status") == "confirmed" or db.insert_action.call_args[0][2] == "confirmed"
        assert db.recent_price_queries == [("uni-base_pool", 100)]

    @pytest.mark.asyncio
    async def test_mint_failure_calls_insert_action_failed(self, fake_dex_adapter):
        """Failed mint records insert_action with status=failed."""
        adapter = FakeDexAdapter(mint_fails=True)

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": adapter}, broadcasts, db)

        result = await sched.lp_rebalancer.create_position(adapter)

        assert result is False
        db.insert_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_mint_broadcasts_position_created(self, fake_dex_adapter):
        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        await sched.lp_rebalancer.create_position(fake_dex_adapter)

        action_broadcasts = [b for b in broadcasts if b.get("type") == "action"]
        assert any(b.get("data", {}).get("action") == "position_created" for b in action_broadcasts)

    @pytest.mark.asyncio
    async def test_failed_prepare_lp_balance_records_failed_swap_and_skips_mint(self, fake_dex_adapter):
        fake_dex_adapter.prepare_lp_balance = AsyncMock(
            return_value=LPBalanceSwapResult(
                direction="token0_to_token1",
                token_in=fake_dex_adapter.config.token0_address,
                token_out=fake_dex_adapter.config.token1_address,
                amount_in_raw=100_000,
                min_out_raw=99_000,
                tx_result=TxResult(hash="", status="failed", error="swap reverted"),
            )
        )

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        result = await sched.lp_rebalancer.create_position(fake_dex_adapter)

        assert result is False
        assert len(fake_dex_adapter.minted) == 0
        db.insert_action.assert_awaited_once()
        assert db.insert_action.await_args.kwargs["action_type"] == "lp_ratio_swap"
        assert db.insert_action.await_args.kwargs["status"] == "failed"

    @pytest.mark.asyncio
    async def test_successful_prepare_lp_balance_records_swap_action(self, fake_dex_adapter):
        fake_dex_adapter.prepare_lp_balance = AsyncMock(
            return_value=LPBalanceSwapResult(
                direction="token0_to_token1",
                token_in=fake_dex_adapter.config.token0_address,
                token_out=fake_dex_adapter.config.token1_address,
                amount_in_raw=200_000,
                min_out_raw=198_000,
                tx_result=TxResult(hash="0xswap", status="confirmed", output_raw=199_000),
            )
        )

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        result = await sched.lp_rebalancer.create_position(fake_dex_adapter)

        assert result is True
        assert db.insert_action.await_count == 2
        first_call = db.insert_action.await_args_list[0].kwargs
        second_call = db.insert_action.await_args_list[1].kwargs
        assert first_call["action_type"] == "lp_ratio_swap"
        assert first_call["status"] == "confirmed"
        assert second_call["action_type"] == "mint_position"

    @pytest.mark.asyncio
    async def test_recovery_price_passed_through(self, fake_dex_adapter):
        """recovery_price is forwarded to strategy.calculate_tick_range."""
        from unittest.mock import patch

        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        with patch("engine.lp.strategy.calculate_tick_range", return_value=(-1000, 1000)) as mock_range:
            await sched.lp_rebalancer.create_position(fake_dex_adapter, recovery_price=0.000400)

        call_kwargs = mock_range.call_args
        assert call_kwargs.kwargs.get("recovery_price") == 0.000400

    @pytest.mark.asyncio
    async def test_recovery_rebalance_persists_params(self, fake_dex_adapter):
        """After a rebalance that adjusts downside_skew, update_venue_config is called."""
        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        await sched.lp_rebalancer.create_position(fake_dex_adapter, recovery_price=0.000800)

        db.update_venue_config.assert_called_once()
        venue_arg, params_arg = db.update_venue_config.call_args[0]
        assert venue_arg == "uni-base"
        assert "downside_skew" in params_arg

    @pytest.mark.asyncio
    async def test_non_recovery_create_does_not_persist_params(self, fake_dex_adapter):
        """create_position without recovery_price must not call update_venue_config."""
        broadcasts = []
        db = MockDB(prices=[Decimal("0.000606")] * 20)
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        await sched.lp_rebalancer.create_position(fake_dex_adapter, recovery_price=None)

        db.update_venue_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_bsc_create_uses_bsc_pool_history(self, fake_dex_adapter):
        fake_dex_adapter.name = "uni-bsc"
        broadcasts = []
        db = MockDB(
            prices=[Decimal("0.000606")] * 20,
            prices_by_source={"uni-bsc_pool": [Decimal("0.000501")] * 20},
        )
        sched = _build_scheduler({"uni-bsc": fake_dex_adapter}, broadcasts, db)

        result = await sched.lp_rebalancer.create_position(fake_dex_adapter)

        assert result is True
        assert db.recent_price_queries == [("uni-bsc_pool", 100)]


class TestPortfolioDelta:

    @pytest.mark.asyncio
    async def test_portfolio_delta_uses_portfolio_exposure_calculator(self):
        broadcasts = []
        db = MockDB()
        exposure = SimpleNamespace(
            total_cngn=Decimal("1000"),
            total_usdt=Decimal("50"),
            total_usdc=Decimal("25"),
            total_usd_value=Decimal("75.7"),
            delta_ratio=Decimal("0.009247027741083223"),
            target_delta=Decimal("0.5"),
            sources=[],
        )
        portfolio_calculator = SimpleNamespace(calculate=AsyncMock(return_value=exposure))
        arb_engine = SimpleNamespace(
            update_portfolio_snapshot=MagicMock(),
            on_dex_dex_update=AsyncMock(),
        )
        sched = _build_scheduler(
            {},
            broadcasts,
            db,
            blended_calculator=MagicMock(),
            arbitrage_engine=arb_engine,
            portfolio_exposure_calculator=portfolio_calculator,
        )

        await sched._check_portfolio_delta()

        portfolio_calculator.calculate.assert_awaited_once()
        arb_engine.update_portfolio_snapshot.assert_called_once()
        payload = next(item for item in broadcasts if item.get("type") == "portfolio_delta")
        assert payload["data"]["total_usd_value"] == float(exposure.total_usd_value)


class TestLpLifecycleLocking:

    @pytest.mark.asyncio
    async def test_same_venue_withdraws_serialize(self, fake_dex_adapter):
        fake_dex_adapter._positions = [_make_position(token_id=77)]
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        active_calls = 0
        max_active = 0
        removed: list[int] = []

        async def slow_remove(token_id: int, recipient: str | None = None):
            nonlocal active_calls, max_active
            active_calls += 1
            max_active = max(max_active, active_calls)
            await asyncio.sleep(0.02)
            removed.append(token_id)
            fake_dex_adapter._positions = [p for p in fake_dex_adapter._positions if p.token_id != token_id]
            active_calls -= 1
            return TxResult(hash="0xremove", status="confirmed")

        fake_dex_adapter.remove_position = slow_remove

        await asyncio.gather(
            sched.lp_rebalancer.withdraw_positions(fake_dex_adapter, recipient="0xabc"),
            sched.lp_rebalancer.withdraw_positions(fake_dex_adapter, recipient="0xabc"),
        )

        assert removed == [77]
        assert max_active == 1

    @pytest.mark.asyncio
    async def test_different_venues_do_not_block_each_other(self):
        base = FakeDexAdapter(name="uni-base", position=_make_position(token_id=1))
        bsc = FakeDexAdapter(name="uni-bsc", position=_make_position(token_id=2))
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": base, "uni-bsc": bsc}, broadcasts, db)

        active_calls = 0
        max_active = 0

        async def slow_remove_factory(adapter: FakeDexAdapter):
            async def slow_remove(token_id: int, recipient: str | None = None):
                nonlocal active_calls, max_active
                active_calls += 1
                max_active = max(max_active, active_calls)
                await asyncio.sleep(0.02)
                adapter._positions = [p for p in adapter._positions if p.token_id != token_id]
                active_calls -= 1
                return TxResult(hash=f"0x{adapter.name}", status="confirmed")
            return slow_remove

        base.remove_position = await slow_remove_factory(base)
        bsc.remove_position = await slow_remove_factory(bsc)

        await asyncio.gather(
            sched.lp_rebalancer.withdraw_positions(base),
            sched.lp_rebalancer.withdraw_positions(bsc),
        )

        assert max_active == 2

    @pytest.mark.asyncio
    async def test_manual_withdraw_persists_action(self, fake_dex_adapter):
        fake_dex_adapter._positions = [_make_position(token_id=88)]
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({"uni-base": fake_dex_adapter}, broadcasts, db)

        results = await sched.lp_rebalancer.withdraw_positions(
            fake_dex_adapter,
            recipient="0xabc",
        )

        assert results[0]["token_id"] == 88
        db.insert_action.assert_awaited_once()
        kwargs = db.insert_action.await_args.kwargs
        assert kwargs["action_type"] == "manual_withdraw"
        assert kwargs["triggered_by"] == "manual:withdraw"
        assert kwargs["metadata"]["recipient"] == "0xabc"


class TestDexArbCurveStream:

    @pytest.mark.asyncio
    async def test_bootstrap_runs_initial_dex_recalc(self):
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
        sched.arbitrage_jobs._update_gas_oracle.assert_awaited_once()
        sched.context.arbitrage_engine.on_dex_dex_update.assert_awaited_once()
        assert call_order == ["gas", "arb"]
        assert sched.state.dex_bootstrap_pending is False

    @pytest.mark.asyncio
    async def test_bootstrap_waits_when_gas_missing(self):
        broadcasts = []
        db = MockDB()
        arbitrage_engine = MagicMock()
        arbitrage_engine.on_dex_dex_update = AsyncMock()
        sched = _build_scheduler({}, broadcasts, db, arbitrage_engine=arbitrage_engine)
        sched.arbitrage_jobs._update_gas_oracle = AsyncMock()

        with patch("engine.market.pool_state.seed_dex_pool_states", AsyncMock()) as seed_mock, \
             patch("engine.market.gas_oracle.gas_usd_base", return_value=None), \
             patch("engine.market.gas_oracle.gas_usd_bsc", return_value=None):
            await sched._bootstrap_dex_arb_curve()

        seed_mock.assert_awaited_once()
        sched.arbitrage_jobs._update_gas_oracle.assert_awaited_once()
        sched.context.arbitrage_engine.on_dex_dex_update.assert_not_awaited()
        assert sched.state.dex_bootstrap_pending is True

    @pytest.mark.asyncio
    async def test_gas_update_schedules_pending_bootstrap(self):
        broadcasts = []
        db = MockDB()
        sched = _build_scheduler({}, broadcasts, db)
        sched.market_jobs._schedule_dex_bootstrap = MagicMock()

        with patch("engine.market.gas_oracle.update", AsyncMock()):
            await sched._update_gas_oracle()

        sched.market_jobs._schedule_dex_bootstrap.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_dex_recalc_when_ws_healthy(self):
        broadcasts = []
        db = MockDB()
        arbitrage_engine = MagicMock()
        arbitrage_engine.on_dex_dex_update = AsyncMock()
        sched = _build_scheduler({}, broadcasts, db, arbitrage_engine=arbitrage_engine)
        sched.ws_listener.active_connections = {"base", "bsc"}

        await sched._stream_dex_arb_curve()

        sched.context.arbitrage_engine.on_dex_dex_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_dex_recalc_when_ws_unhealthy(self):
        broadcasts = []
        db = MockDB()
        arbitrage_engine = MagicMock()
        arbitrage_engine.on_dex_dex_update = AsyncMock()
        sched = _build_scheduler({}, broadcasts, db, arbitrage_engine=arbitrage_engine)
        sched.ws_listener.active_connections = {"base"}

        await sched._stream_dex_arb_curve()

        sched.context.arbitrage_engine.on_dex_dex_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wallet_activity_broadcasts_account_balances(self):
        broadcasts = []
        db = MockDB()
        arbitrage_engine = MagicMock()
        arbitrage_engine.on_wallet_activity = AsyncMock()
        account_manager = MagicMock()
        account_manager.check_all_balances = AsyncMock(return_value=[
            SimpleNamespace(
                role="uni-bsc-trade",
                address="0xabc",
                chain_id=56,
                native_balance=Decimal("0.1"),
                native_symbol="BNB",
                token_balances={"USDT": Decimal("4"), "cNGN": Decimal("5")},
                needs_refill=False,
                refill_reasons=[],
            )
        ])
        sched = _build_scheduler(
            {},
            broadcasts,
            db,
            arbitrage_engine=arbitrage_engine,
            account_manager=account_manager,
            token_contracts={"USDT": "0x123"},
        )

        await sched._handle_wallet_activity(["uni-bsc"])

        sched.context.arbitrage_engine.on_wallet_activity.assert_awaited_once_with(["uni-bsc"])
        sched.context.account_manager.check_all_balances.assert_awaited_once_with({"USDT": "0x123"})
        assert broadcasts[-1]["type"] == "account_balances"
        assert broadcasts[-1]["data"][0]["role"] == "uni-bsc-trade"
        assert broadcasts[-1]["data"][0]["refill_reasons"] == []
