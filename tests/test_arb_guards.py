"""Guards that fire between detection and execution.

These tests cover behaviours that sit *between* routing and execution:
the _arb_executing flag that serialises concurrent signals, and the
preflight error classification that determines which recovery action fires.

The asymmetric preflight invariant (from CLAUDE.md):
  balance  → zeros inventory for the venue; circuit breaker NOT tripped
  rpc      → no inventory mutation; circuit breaker NOT tripped
  permit2  → no inventory mutation; circuit breaker NOT tripped (critical alert)
  pool_paused → trips circuit breaker
  unknown  → no inventory mutation; circuit breaker NOT tripped
"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from engine.arb.engine import ArbitrageEngine
from engine.arb.routing.route_registry import ROUTES_BY_DIRECTION
from engine.arb.routing.router import RouteCandidate, SelectedRoute
from engine.db.connection import SQLiteConnectionManager
from engine.db.repository import DatabaseRepository
from engine.types import ArbitrageParams, DexArbOpportunity, PriceQuote, TxResult
import engine.arb.detection.dex_dex as _dex_dex_module


# =============================================================================
# Shared helpers (follow the same pattern as test_dex_dex_execution.py)
# =============================================================================


def _params():
    return ArbitrageParams(
        max_daily_volume_usd=Decimal("50000"),
        max_daily_loss_usd=Decimal("500"),
        max_inventory_imbalance_usd=Decimal("10000"),
        max_consecutive_failures=3,
        max_single_trade_usd=Decimal("1000"),
    )


class _FakeV4Venue:
    """Minimal venue double with configurable simulate_swap result."""

    def __init__(self, name: str, sim_result: str | None = None, swap_ok: bool = True):
        self.name = name
        self.stable_address = "0xstable"
        self.cngn_address = "0xcngn"
        self.stable_decimals = 6
        self.cngn_decimals = 6
        self.trade_account = SimpleNamespace(address="0xFAKE000000000000000000000000000000000001")
        self.stable_token = SimpleNamespace(
            functions=SimpleNamespace(balanceOf=lambda _: SimpleNamespace(call=lambda: 0))
        )
        self.cngn_token = SimpleNamespace(
            functions=SimpleNamespace(balanceOf=lambda _: SimpleNamespace(call=lambda: 0))
        )
        self._sim_result = sim_result
        self._swap_ok = swap_ok
        self.sim_calls: list = []

    def simulate_swap(self, token_in, amount_in, min_out):
        self.sim_calls.append((token_in, amount_in, min_out))
        return self._sim_result

    async def swap(self, token_in, amount_in, min_out):
        if self._swap_ok:
            return TxResult(hash="0xtx", status="confirmed", output_raw=amount_in)
        return TxResult(hash="", status="failed", error="execution reverted")

    async def get_current_price(self):
        return PriceQuote(source=self.name, timestamp=0,
                          bid=Decimal("0.00061"), ask=Decimal("0.00061"), mid=Decimal("0.00061"))

    async def ensure_trade_approvals(self) -> None:
        return None


def _route(direction="UNI_BASE_TO_UNI_BSC_DELTA_BALANCE", size=Decimal("100")):
    candidate = RouteCandidate(
        direction=direction,
        buy_venue="uni-base",
        sell_venue="uni-bsc",
        optimal_size_usd=size,
        expected_profit_usd=Decimal("0.50"),
        gas_usd=Decimal("0.05"),
        signal={
            "optimal_arb": {"slippage_tolerance_bps": 10, "cngn_transferred": 160000.0},
            "prices": {"uni-bsc": "0.00061", "uni-base": "0.00061"},
        },
    )
    return SelectedRoute(
        candidate=candidate,
        adjusted_size_usd=size,
        net_profit_usd=Decimal("0.45"),
        expected_profit_usd=Decimal("0.45"),
    )


def _make_opp(opp_id: str, status: str = "detected") -> DexArbOpportunity:
    return DexArbOpportunity(
        id=opp_id,
        timestamp=0,
        direction="UNI_BASE_TO_UNI_BSC_DELTA_BALANCE",
        optimal_size_usd=Decimal("100"),
        expected_profit_usd=Decimal("0.50"),
        cngn_transferred=Decimal("160000"),
        expected_usd_out=Decimal("100.50"),
        status=status,
        net_spread_bps=50,
    )


@pytest.fixture
async def test_db(tmp_path):
    db = DatabaseRepository(SQLiteConnectionManager(str(tmp_path / "test.db")))
    await db.connect()
    yield db
    await db.close()


def _make_engine(venues: dict, db: DatabaseRepository):
    alerts: list = []
    engine = ArbitrageEngine(
        venues=venues,
        params=_params(),
        broadcast=lambda e: alerts.append(e),
        execute_dex_dex_enabled=True,
        arbitrage_store=db.arbitrage,
        history_store=db.history,
        price_store=db.prices,
    )
    engine._inventory_seeded = True
    engine._trade_approvals_seeded = True
    return engine, alerts


# =============================================================================
# 1. Race condition serialisation
# =============================================================================


class TestArbExecutingFlag:
    @pytest.mark.asyncio
    async def test_second_signal_dropped_while_arb_executing(self, test_db, monkeypatch):
        """While _arb_executing is True, a new DEX-DEX update must not start execution.

        This is the serialisation invariant: only one arb runs at a time.
        A profitable signal that arrives mid-execution is silently dropped —
        no DB write, no execute_route call.
        """
        buy_venue = _FakeV4Venue("uni-base")
        sell_venue = _FakeV4Venue("uni-bsc")
        engine, _ = _make_engine({"uni-base": buy_venue, "uni-bsc": sell_venue}, test_db)

        profitable_signal = {
            "optimal_arb": {
                "direction": "UNI_BASE_TO_UNI_BSC_DELTA_BALANCE",
                "optimal_size_usd": 100.0,
                "expected_profit_usd": 0.50,
                "cngn_transferred": 160000.0,
                "expected_usd_out": 100.50,
                "gas_usd": 0.05,
                "net_spread_bps": 50,
            },
            "prices": {"uni-base": "0.00061", "uni-bsc": "0.00061"},
        }

        monkeypatch.setattr(_dex_dex_module, "find_optimal_dex_arb", lambda: profitable_signal)
        monkeypatch.setattr(_dex_dex_module, "estimate_dex_dex_trade", lambda d, s: {"cngn_transferred": 160000.0})

        tasks_created: list = []

        def _fake_create_task(coro):
            # Record the coroutine name but immediately close it to avoid warnings
            tasks_created.append(coro.__qualname__ if hasattr(coro, "__qualname__") else str(coro))
            coro.close()
            return SimpleNamespace(done=lambda: True)

        engine._arb_executing = True

        with patch("asyncio.create_task", side_effect=_fake_create_task):
            await engine.on_dex_dex_update()

        execute_route_calls = [name for name in tasks_created if "execute_route" in name or "_execute_route" in name]
        assert execute_route_calls == [], (
            f"_execute_route must not be called while _arb_executing=True, "
            f"but create_task was called with: {tasks_created}"
        )

    @pytest.mark.asyncio
    async def test_arb_executing_flag_cleared_on_exception(self, test_db, monkeypatch):
        """_arb_executing must be False in the finally block, even when sell fails.

        If the flag were not cleared, a sell failure would permanently block
        all future arb attempts.
        """
        monkeypatch.setattr(_dex_dex_module, "estimate_dex_dex_trade",
                            lambda d, s: {"cngn_transferred": 160000.0})
        buy_venue = _FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        sell_venue = _FakeV4Venue("uni-bsc", sim_result=None, swap_ok=False)  # sell swap fails
        engine, _ = _make_engine({"uni-base": buy_venue, "uni-bsc": sell_venue}, test_db)

        opp_id = "opp-flag-clear"
        await test_db.arbitrage.insert_dex_arbitrage_opportunity(_make_opp(opp_id))

        assert not engine._arb_executing
        route_def = ROUTES_BY_DIRECTION[_route().candidate.direction]
        await engine._execute_route(route_def, _route(), opp_id)

        # Regardless of sell failure → half_open, flag must be cleared
        assert not engine._arb_executing, "_arb_executing must be False after execution completes"


# =============================================================================
# 2. Preflight error classification and inventory effects
# =============================================================================


class TestPreflightInventoryEffects:
    """The error classification in _handle_preflight_error is asymmetric by design.

    Only "balance" reverts zero inventory. Only "pool_paused" trips the
    circuit breaker. RPC, permit2, and unknown errors leave both intact.
    """

    @pytest.mark.asyncio
    async def test_balance_error_zeros_cngn_inventory_for_sell_venue(self, test_db, monkeypatch):
        """A balance-revert on the sell preflight → cNGN inventory zeroed for that venue.

        No circuit breaker, no other venue affected.
        """
        monkeypatch.setattr(_dex_dex_module, "estimate_dex_dex_trade",
                            lambda d, s: {"cngn_transferred": 160000.0})
        sell_venue = _FakeV4Venue(
            "uni-bsc",
            sim_result="execution reverted: insufficient balance",
        )
        buy_venue = _FakeV4Venue("uni-base")
        engine, _ = _make_engine({"uni-base": buy_venue, "uni-bsc": sell_venue}, test_db)

        # Seed some cNGN inventory for the sell venue
        engine.inventory.reconcile_cngn({"uni-bsc": Decimal("50000"), "uni-base": Decimal("50000")})
        assert engine.inventory.state.per_account_cngn["uni-bsc"] == Decimal("50000")

        opp_id = "opp-balance-err"
        await test_db.arbitrage.insert_dex_arbitrage_opportunity(_make_opp(opp_id))
        await engine._execute_route(ROUTES_BY_DIRECTION[_route().candidate.direction], _route(), opp_id)

        # sell venue inventory zeroed, buy venue unaffected
        assert engine.inventory.state.per_account_cngn["uni-bsc"] == Decimal("0"), \
            "balance revert must zero cNGN inventory for the sell venue"
        assert engine.inventory.state.per_account_cngn["uni-base"] == Decimal("50000"), \
            "buy venue cNGN inventory must not be affected"
        # Circuit breaker must NOT trip on a balance revert
        assert not engine.inventory.state.circuit_breaker_active, \
            "circuit breaker must not trip on a balance revert"

    @pytest.mark.asyncio
    async def test_rpc_error_leaves_inventory_intact(self, test_db, monkeypatch):
        """An RPC failure on the sell preflight → inventory unchanged, no circuit breaker."""
        monkeypatch.setattr(_dex_dex_module, "estimate_dex_dex_trade",
                            lambda d, s: {"cngn_transferred": 160000.0})
        sell_venue = _FakeV4Venue(
            "uni-bsc",
            sim_result="execution reverted: timeout waiting for transaction",
        )
        buy_venue = _FakeV4Venue("uni-base")
        engine, _ = _make_engine({"uni-base": buy_venue, "uni-bsc": sell_venue}, test_db)

        engine.inventory.reconcile_cngn({"uni-bsc": Decimal("50000")})

        opp_id = "opp-rpc-err"
        await test_db.arbitrage.insert_dex_arbitrage_opportunity(_make_opp(opp_id))
        await engine._execute_route(ROUTES_BY_DIRECTION[_route().candidate.direction], _route(), opp_id)

        assert engine.inventory.state.per_account_cngn["uni-bsc"] == Decimal("50000"), \
            "rpc error must leave cNGN inventory unchanged"
        assert not engine.inventory.state.circuit_breaker_active, \
            "circuit breaker must not trip on an rpc error"

    @pytest.mark.asyncio
    async def test_pool_paused_trips_circuit_breaker(self, test_db, monkeypatch):
        """A pool_paused revert → circuit breaker trips immediately.

        This is the only preflight category that kills all future arb attempts
        until the operator resets the breaker.
        """
        monkeypatch.setattr(_dex_dex_module, "estimate_dex_dex_trade",
                            lambda d, s: {"cngn_transferred": 160000.0})
        sell_venue = _FakeV4Venue(
            "uni-bsc",
            sim_result="execution reverted: PoolNotInitialized()",
        )
        buy_venue = _FakeV4Venue("uni-base")
        engine, alerts = _make_engine({"uni-base": buy_venue, "uni-bsc": sell_venue}, test_db)

        opp_id = "opp-pool-paused"
        await test_db.arbitrage.insert_dex_arbitrage_opportunity(_make_opp(opp_id))
        await engine._execute_route(ROUTES_BY_DIRECTION[_route().candidate.direction], _route(), opp_id)

        assert engine.inventory.state.circuit_breaker_active, \
            "circuit breaker must trip on a pool_paused revert"
        critical_alerts = [a for a in alerts if a.get("severity") == "critical"]
        assert critical_alerts, "a critical alert must be broadcast when the circuit breaker trips"
