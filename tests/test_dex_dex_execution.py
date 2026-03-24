"""Tests for DEX-DEX execution path: half-open prevention, detection, and recovery.

Covers the three behaviours that half-open trades exposed:
  1. Pre-execution simulation gate — sell-side preflight fails → buy never starts
  2. Half-open detection — buy ok, sell fails → DB status, circuit breaker, alert
  3. Recovery — retry sell (path 1) and reverse buy (path 2)
"""

import time
import pytest
import tempfile
import os
from decimal import Decimal
from unittest.mock import patch

from engine.api.schemas import ArbitrageParams, DexArbOpportunity, PriceQuote, TxResult
from engine.core.arbitrage.engine import ArbitrageEngine
from engine.core.arbitrage.router import RouteCandidate, SelectedRoute
from engine.db.database import Database


# =============================================================================
# Helpers
# =============================================================================

def _params():
    return ArbitrageParams(
        max_daily_volume_usd=Decimal("50000"),
        max_daily_loss_usd=Decimal("500"),
        max_inventory_imbalance_usd=Decimal("10000"),
        max_consecutive_failures=3,
        max_single_trade_usd=Decimal("1000"),
    )


class FakeV4Venue:
    """Minimal venue double for DEX-DEX execution tests."""

    def __init__(self, name, sim_result=None, swap_ok=True):
        self.name = name
        self.stable_address = "0xstable"
        self.cngn_address = "0xcngn"
        self.stable_decimals = 6
        self.cngn_decimals = 6
        self._sim_result = sim_result   # None = passes, str = error message
        self._swap_ok = swap_ok
        self.swap_calls = []
        self.sim_calls = []

    def simulate_swap(self, token_in, amount_in, min_out):
        self.sim_calls.append((token_in, amount_in, min_out))
        return self._sim_result

    async def swap(self, token_in, amount_in, min_out):
        self.swap_calls.append((token_in, amount_in, min_out))
        if self._swap_ok:
            return TxResult(hash="0xbuytx", status="confirmed", output_raw=amount_in)
        return TxResult(hash="", status="failed", error="execution reverted: TRANSFER_FROM_FAILED: 0x08c379a0")

    async def get_current_price(self):
        return PriceQuote(source=self.name, timestamp=0,
                          bid=Decimal("0.00061"), ask=Decimal("0.00061"), mid=Decimal("0.00061"))


def _route(direction="UNI_BASE_TO_UNI_BSC_DELTA_BALANCE", size=Decimal("500")):
    candidate = RouteCandidate(
        direction=direction,
        pipeline="dex_dex",
        buy_venue="uni-base",
        sell_venue="uni-bsc",
        optimal_size_usd=size,
        expected_profit_usd=Decimal("1.20"),
        gas_usd=Decimal("0.08"),
        signal={
            "optimal_arb": {
                "slippage_tolerance_bps": 10,
                "cngn_transferred": 800000.0,
            },
            "prices": {"uni-bsc": "0.00061", "uni-base": "0.00061"},
        },
    )
    return SelectedRoute(
        candidate=candidate,
        adjusted_size_usd=size,
        net_profit_usd=Decimal("1.12"),
    )


def _make_opp(opp_id, status="buy_filled", buy_amount_cngn=Decimal("798000")):
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
        buy_tx_hash="0xbuytx",
        buy_amount_cngn=buy_amount_cngn,
    )


@pytest.fixture
async def test_db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


def _make_engine(venues, test_db):
    alerts = []
    engine = ArbitrageEngine(
        venues=venues,
        params=_params(),
        broadcast=lambda e: alerts.append(e),
        execute_dex_dex_enabled=True,
    )
    engine._inventory_seeded = True  # skip seed network calls

    async def _fake_get_db():
        return test_db

    return engine, alerts, _fake_get_db


# =============================================================================
# 1. Pre-execution simulation gate
# =============================================================================

class TestPreflightGate:
    @pytest.mark.asyncio
    async def test_sell_preflight_fail_prevents_buy(self):
        """If sell-side simulation fails, the buy leg must never execute."""
        buy_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        sell_venue = FakeV4Venue("uni-bsc", sim_result="execution reverted: TRANSFER_FROM_FAILED", swap_ok=False)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, None)
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_dex_dex(_route(), "opp-preflight-1")

        assert buy_venue.swap_calls == [], "buy swap must not be called when sell preflight fails"
        assert not engine._arb_executing

    @pytest.mark.asyncio
    async def test_buy_preflight_fail_prevents_buy(self):
        """If buy-side simulation fails, the buy leg must not execute."""
        buy_venue = FakeV4Venue("uni-base", sim_result="execution reverted: INSUFFICIENT_BALANCE")
        sell_venue = FakeV4Venue("uni-bsc", sim_result=None)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, None)
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_dex_dex(_route(), "opp-preflight-2")

        assert buy_venue.swap_calls == []
        assert not engine._arb_executing

    @pytest.mark.asyncio
    async def test_both_preflights_pass_proceeds_to_buy(self, test_db):
        """If both simulations pass, the buy should be attempted."""
        buy_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        sell_venue = FakeV4Venue("uni-bsc", sim_result=None, swap_ok=True)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        await test_db.insert_dex_arbitrage_opportunity(_make_opp("opp-preflight-3", status="detected"))
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_dex_dex(_route(), "opp-preflight-3")

        assert len(buy_venue.swap_calls) == 1
        opp = await test_db.get_dex_arbitrage_opportunity("opp-preflight-3")
        assert opp.status == "completed"
        assert opp.actual_profit_usd is not None
        assert opp.sell_tx_hash is not None
        # Preflight happened (amount is from pool estimate, may be 0 in test with cold cache).
        assert len(sell_venue.sim_calls) == 1
        assert sell_venue.sim_calls[0][0] == sell_venue.cngn_address
        # Live sell must use buy_trade.amount (actual cNGN received), not the pre-buy estimate.
        # The mock returns output_raw = stable_amount_in = 500 * 10^6, so buy_trade.amount = 500 cNGN.
        assert sell_venue.swap_calls[0] == (sell_venue.cngn_address, 500000000, 499500000)


# =============================================================================
# 2. Half-open detection
# =============================================================================

class TestHalfOpenDetection:
    @pytest.mark.asyncio
    async def test_half_open_recorded_in_db(self, test_db):
        """When sell fails after buy succeeds, DB status must be half_open."""
        buy_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        sell_venue = FakeV4Venue("uni-bsc", sim_result=None, swap_ok=False)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        opp_id = "opp-halfopen-1"
        await test_db.insert_dex_arbitrage_opportunity(_make_opp(opp_id, status="detected"))
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_dex_dex(_route(), opp_id)

        opp = await test_db.get_dex_arbitrage_opportunity(opp_id)
        assert opp.status == "half_open"
        assert opp.buy_tx_hash is not None

    @pytest.mark.asyncio
    async def test_half_open_trips_circuit_breaker(self, test_db):
        """A half-open trade must activate the circuit breaker."""
        buy_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        sell_venue = FakeV4Venue("uni-bsc", sim_result=None, swap_ok=False)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        opp_id = "opp-halfopen-2"
        await test_db.insert_dex_arbitrage_opportunity(_make_opp(opp_id, status="detected"))
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_dex_dex(_route(), opp_id)

        assert engine.inventory._state.circuit_breaker_active

    @pytest.mark.asyncio
    async def test_half_open_broadcasts_critical_alert(self, test_db):
        """A half-open trade must broadcast a critical alert containing the opp_id and /recover."""
        buy_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        sell_venue = FakeV4Venue("uni-bsc", sim_result=None, swap_ok=False)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        opp_id = "opp-halfopen-3"
        await test_db.insert_dex_arbitrage_opportunity(_make_opp(opp_id, status="detected"))
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_dex_dex(_route(), opp_id)

        critical = [a for a in alerts if a.get("severity") == "critical"]
        assert len(critical) == 1
        msg = critical[0]["message"]
        assert opp_id in msg
        assert "/recover" in msg
        assert "uni-bsc" in msg

    @pytest.mark.asyncio
    async def test_half_open_error_is_human_readable(self, test_db):
        """The sell error in the alert and DB should not contain raw hex revert data."""
        buy_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        sell_venue = FakeV4Venue("uni-bsc", sim_result=None, swap_ok=False)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        opp_id = "opp-halfopen-4"
        await test_db.insert_dex_arbitrage_opportunity(_make_opp(opp_id, status="detected"))
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_dex_dex(_route(), opp_id)

        opp = await test_db.get_dex_arbitrage_opportunity(opp_id)
        assert opp.reason is not None
        assert "0x08c379a0" not in opp.reason


# =============================================================================
# 3. Recovery
# =============================================================================

class TestRecovery:
    @pytest.mark.asyncio
    async def test_recover_retries_sell_when_simulation_passes(self, test_db):
        """Path 1: if sell-side simulation now passes, retry the sell using buy_amount_cngn."""
        sell_venue = FakeV4Venue("uni-bsc", sim_result=None, swap_ok=True)
        buy_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        opp_id = "opp-recover-1"
        opp = _make_opp(opp_id, status="half_open", buy_amount_cngn=Decimal("798000"))
        await test_db.insert_dex_arbitrage_opportunity(opp)
        await test_db.update_dex_arbitrage_execution_state(
            opp_id, status="half_open", buy_amount_cngn=Decimal("798000"),
        )

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            result = await engine.recover_dex_half_open(opp_id)

        assert result["method"] == "retry_sell"
        assert result["status"] == "completed"
        # Recovery sells the actual cNGN received (buy_amount_cngn), not any pre-planned estimate.
        _, amount_in, _ = sell_venue.swap_calls[0]
        assert amount_in == int(Decimal("798000") * Decimal(10 ** sell_venue.cngn_decimals))
        done = await test_db.get_dex_arbitrage_opportunity(opp_id)
        assert done.status == "completed"
        assert done.actual_profit_usd is not None

    @pytest.mark.asyncio
    async def test_recover_reverses_buy_when_sell_unavailable(self, test_db):
        """Path 2: if sell-side simulation fails, reverse the buy using buy_amount_cngn."""
        sell_venue = FakeV4Venue("uni-bsc", sim_result="execution reverted: TRANSFER_FROM_FAILED")
        buy_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        opp_id = "opp-recover-2"
        opp = _make_opp(opp_id, status="half_open", buy_amount_cngn=Decimal("798000"))
        await test_db.insert_dex_arbitrage_opportunity(opp)
        await test_db.update_dex_arbitrage_execution_state(opp_id, status="half_open",
                                                           buy_amount_cngn=Decimal("798000"))

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            result = await engine.recover_dex_half_open(opp_id)

        assert result["method"] == "reverse_buy"
        assert result["status"] == "completed"
        # Confirm it used the stored amount, not a live balance fetch
        assert len(buy_venue.swap_calls) == 1
        _, amount_in, _ = buy_venue.swap_calls[0]
        assert amount_in == int(Decimal("798000") * Decimal(10 ** buy_venue.cngn_decimals))
        done = await test_db.get_dex_arbitrage_opportunity(opp_id)
        assert done.status == "completed"
        assert done.actual_profit_usd is not None

    @pytest.mark.asyncio
    async def test_recover_fails_without_buy_amount_cngn(self, test_db):
        """Recovery must raise for old records missing buy_amount_cngn."""
        sell_venue = FakeV4Venue("uni-bsc", sim_result="execution reverted: TRANSFER_FROM_FAILED")
        buy_venue = FakeV4Venue("uni-base", sim_result=None)
        venues = {"uni-base": buy_venue, "uni-bsc": sell_venue}

        opp_id = "opp-recover-3"
        opp = _make_opp(opp_id, status="half_open", buy_amount_cngn=None)
        await test_db.insert_dex_arbitrage_opportunity(opp)
        await test_db.update_dex_arbitrage_execution_state(opp_id, status="half_open")

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            with pytest.raises(ValueError, match="buy_amount_cngn not recorded"):
                await engine.recover_dex_half_open(opp_id)

    @pytest.mark.asyncio
    async def test_recover_rejects_non_recoverable_status(self, test_db):
        """Recovery must reject opportunities that are not in a recoverable state."""
        venues = {"uni-base": FakeV4Venue("uni-base"), "uni-bsc": FakeV4Venue("uni-bsc")}
        opp_id = "opp-recover-4"
        opp = _make_opp(opp_id, status="completed")
        await test_db.insert_dex_arbitrage_opportunity(opp)

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            with pytest.raises(ValueError, match="not recoverable"):
                await engine.recover_dex_half_open(opp_id)
