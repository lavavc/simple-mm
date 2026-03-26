"""Tests for CEX-DEX preflight gate and _clean_revert helper."""

import pytest
import tempfile
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from eth_abi import encode

from engine.api.schemas import ArbitrageParams, TxResult, PriceQuote, OrderBookDepth, OrderBookLevel
from engine.core.arbitrage.engine import ArbitrageEngine
from engine.core.arbitrage.router import RouteCandidate, SelectedRoute
from engine.core.arbitrage.executor import _clean_revert, _classify_preflight_error
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
    """DEX venue double for CEX-DEX sell-leg tests."""

    def __init__(self, name, sim_result=None, swap_ok=True):
        self.name = name
        self.stable_address = "0xstable"
        self.cngn_address = "0xcngn"
        self.stable_decimals = 6
        self.cngn_decimals = 6
        self.trade_account = SimpleNamespace(address="0x23DF63FAKE0000000000000000000000002e14E4")
        self._sim_result = sim_result
        self._swap_ok = swap_ok
        self.swap_calls = []

    def simulate_swap(self, token_in, amount_in, min_out):
        return self._sim_result

    async def swap(self, token_in, amount_in, min_out):
        self.swap_calls.append((token_in, amount_in, min_out))
        if self._swap_ok:
            return TxResult(hash="0xselltx", status="confirmed", output_raw=amount_in)
        return TxResult(hash="", status="failed", error="execution reverted: SWAP_FAILED")

    async def get_current_price(self):
        return PriceQuote(source=self.name, timestamp=0,
                          bid=Decimal("0.00061"), ask=Decimal("0.00061"), mid=Decimal("0.00061"))


class FakeCexVenue:
    """CEX venue double for CEX-DEX buy-leg tests."""

    def __init__(self, buy_ok=True):
        self.buy_calls = []
        self._buy_ok = buy_ok

    async def place_market_order(self, side, amount):
        self.buy_calls.append((side, amount))
        if self._buy_ok:
            return True, amount, Decimal("0.00061"), None
        return False, amount, Decimal("0"), "order rejected"


def _cex_dex_route(direction="QUIDAX_TO_UNI_BASE", size=Decimal("500")):
    """Build a SelectedRoute for the QUIDAX_TO_UNI_BASE direction."""
    depth = OrderBookDepth(
        venue="quidax",
        pair="cNGN/USDT",
        timestamp=1700000000000,
        bids=[OrderBookLevel(price=Decimal("1650"), amount=Decimal("1000"))],
        asks=[OrderBookLevel(price=Decimal("1600"), amount=Decimal("1000"))],
    )
    candidate = RouteCandidate(
        direction=direction,
        pipeline="cex_dex",
        buy_venue="quidax",
        sell_venue="uni-base",
        optimal_size_usd=size,
        expected_profit_usd=Decimal("1.50"),
        gas_usd=Decimal("0.05"),
        signal={
            "optimal_arb": {
                "slippage_tolerance_bps": 10,
                "net_spread_bps": 30,
            },
            "prices": {"quidax": "0.00061", "uni-base": "0.00071"},
            "depth": depth,
        },
    )
    return SelectedRoute(
        candidate=candidate,
        adjusted_size_usd=size,
        net_profit_usd=Decimal("1.45"),
        expected_profit_usd=Decimal("1.50"),
    )


def _cex_dex_route_no_depth(direction="QUIDAX_TO_UNI_BASE", size=Decimal("500")):
    route = _cex_dex_route(direction=direction, size=size)
    route.candidate.signal.pop("depth", None)
    return route


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
        execute_cex_dex_enabled=True,
    )
    engine._inventory_seeded = True

    async def _fake_get_db():
        return test_db

    return engine, alerts, _fake_get_db


# =============================================================================
# Issue 2: CEX-DEX preflight gate tests
# =============================================================================

class TestCexDexPreflightGate:
    @pytest.mark.asyncio
    async def test_missing_depth_aborts_before_cex_buy(self, test_db):
        """If Quidax depth is missing, preflight must abort instead of simulating a zero sell."""
        sell_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        cex_venue = FakeCexVenue(buy_ok=True)
        venues = {"quidax": cex_venue, "uni-base": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_cex_dex(_cex_dex_route_no_depth(), "opp-cex-preflight-missing-depth")

        assert cex_venue.buy_calls == [], "CEX buy must not be called when Quidax depth is missing"
        assert not engine._arb_executing

    @pytest.mark.asyncio
    async def test_sell_preflight_fails_cex_buy_never_placed(self, test_db):
        """If DEX sell preflight fails, CEX buy must never be placed."""
        sell_venue = FakeV4Venue("uni-base", sim_result="execution reverted: TRANSFER_FROM_FAILED")
        cex_venue = FakeCexVenue(buy_ok=True)
        venues = {"quidax": cex_venue, "uni-base": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_cex_dex(_cex_dex_route(), "opp-cex-preflight-1")

        assert cex_venue.buy_calls == [], "CEX buy must not be called when sell preflight fails"
        assert not engine._arb_executing

    @pytest.mark.asyncio
    async def test_sell_preflight_alert_includes_trade_and_wallet_context(self, test_db):
        """Unknown preflight alerts should include route size and wallet inventory for debugging."""
        sell_venue = FakeV4Venue("uni-base", sim_result="execution reverted: TRANSFER_FROM_FAILED")
        cex_venue = FakeCexVenue(buy_ok=True)
        venues = {"quidax": cex_venue, "uni-base": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        engine.inventory.reconcile_cngn({"uni-base": Decimal("26999")})
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_cex_dex(_cex_dex_route(size=Decimal("500")), "opp-cex-preflight-debug")

        message = next(a["message"] for a in alerts if a.get("type") == "alert")
        assert "Trade size: $500.00" in message
        assert "Estimated sell:" in message
        assert "Wallet: 0x23DF...2e14E4 | 26,999.00 cNGN | ~$19.17" in message
        assert "Shortfall:" in message

    @pytest.mark.asyncio
    async def test_sell_preflight_passes_cex_buy_is_attempted(self, test_db):
        """If DEX sell preflight passes, CEX buy must be attempted."""
        sell_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        cex_venue = FakeCexVenue(buy_ok=True)
        venues = {"quidax": cex_venue, "uni-base": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_cex_dex(_cex_dex_route(), "opp-cex-preflight-2")

        assert len(cex_venue.buy_calls) == 1, "CEX buy must be called when sell preflight passes"
        assert not engine._arb_executing

    @pytest.mark.asyncio
    async def test_execution_persists_route_expected_profit(self, test_db):
        """The DB record should use the capped/recomputed route profit, not the stale detection value."""
        sell_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        cex_venue = FakeCexVenue(buy_ok=True)
        venues = {"quidax": cex_venue, "uni-base": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        route = _cex_dex_route()
        route.expected_profit_usd = Decimal("0.42")
        route.candidate.expected_profit_usd = Decimal("1.50")

        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_cex_dex(route, "opp-cex-profit-persist")

        opp = await test_db.get_arbitrage_opportunity("opp-cex-profit-persist")
        assert opp is not None
        assert opp.expected_profit_usd == Decimal("0.42")

    @pytest.mark.asyncio
    async def test_execution_persists_zero_route_expected_profit(self, test_db):
        """A recomputed expected profit of zero is still authoritative and must not fall back."""
        sell_venue = FakeV4Venue("uni-base", sim_result=None, swap_ok=True)
        cex_venue = FakeCexVenue(buy_ok=True)
        venues = {"quidax": cex_venue, "uni-base": sell_venue}

        engine, alerts, fake_get_db = _make_engine(venues, test_db)
        route = _cex_dex_route()
        route.expected_profit_usd = Decimal("0")
        route.candidate.expected_profit_usd = Decimal("1.50")

        with patch("engine.core.arbitrage.engine.get_db", fake_get_db):
            await engine._execute_cex_dex(route, "opp-cex-profit-zero")

        opp = await test_db.get_arbitrage_opportunity("opp-cex-profit-zero")
        assert opp is not None
        assert opp.expected_profit_usd == Decimal("0")


# =============================================================================
# Issue 6: _clean_revert tests
# =============================================================================

class TestCleanRevert:
    def test_none_input_returns_none(self):
        assert _clean_revert(None) is None

    def test_empty_string_returns_empty(self):
        assert _clean_revert("") == ""

    def test_normal_string_unchanged(self):
        msg = "something went wrong"
        assert _clean_revert(msg) == msg

    def test_strips_trailing_hex(self):
        err = "execution reverted: TRANSFER_FROM_FAILED: 0x08c379a0000000deadbeef"
        result = _clean_revert(err)
        assert "0x08c379a0" not in result
        assert "TRANSFER_FROM_FAILED" in result

    def test_decodes_abi_encoded_error(self):
        """Raw 0x08c379a0 + ABI-encoded Error(string) should decode to human-readable message."""
        payload = "0x08c379a0" + encode(["string"], ["TRANSFER_FROM_FAILED"]).hex()
        result = _clean_revert(payload)
        assert result == "execution reverted: TRANSFER_FROM_FAILED"

    def test_decodes_abi_encoded_error_other_message(self):
        payload = "0x08c379a0" + encode(["string"], ["INSUFFICIENT_LIQUIDITY"]).hex()
        result = _clean_revert(payload)
        assert result == "execution reverted: INSUFFICIENT_LIQUIDITY"


# =============================================================================
# _classify_preflight_error unit tests
# =============================================================================

class TestClassifyPreflightError:
    def test_balance_transfer_exceeds(self):
        assert _classify_preflight_error("execution reverted: ERC20: transfer amount exceeds balance") == "balance"

    def test_balance_insufficient(self):
        assert _classify_preflight_error("execution reverted: insufficient balance") == "balance"

    def test_rpc_timeout(self):
        assert _classify_preflight_error("Read timed out. (connect timeout=10)") == "rpc"

    def test_rpc_connection_error(self):
        assert _classify_preflight_error("ConnectionError: HTTPSConnectionPool host='rpc.example.com'") == "rpc"

    def test_rpc_max_retries(self):
        assert _classify_preflight_error("Max retries exceeded with url: /") == "rpc"

    def test_permit2_expired(self):
        assert _classify_preflight_error("execution reverted: AllowanceExpired") == "permit2"

    def test_permit2_insufficient(self):
        assert _classify_preflight_error("execution reverted: InsufficientAllowance") == "permit2"

    def test_pool_paused_lok(self):
        assert _classify_preflight_error("execution reverted: LOK") == "pool_paused"

    def test_pool_not_initialized(self):
        assert _classify_preflight_error("execution reverted: PoolNotInitialized") == "pool_paused"

    def test_unknown_revert(self):
        assert _classify_preflight_error("execution reverted: SOME_UNKNOWN_ERROR") == "unknown"

    def test_none_returns_unknown(self):
        assert _classify_preflight_error(None) == "unknown"


# =============================================================================
# _handle_preflight_error engine integration tests
# =============================================================================

class TestHandlePreflightError:
    """Test that _handle_preflight_error takes the right action for each category."""

    def _make_engine_for_preflight(self):
        from engine.core.arbitrage.engine import ArbitrageEngine
        alerts = []
        engine = ArbitrageEngine(
            venues={},
            params=_params(),
            broadcast=lambda e: alerts.append(e),
        )
        engine._inventory_seeded = True
        return engine, alerts

    def _cngn(self, engine, venue):
        return engine.inventory._state.per_account_cngn.get(venue, Decimal("0"))

    def _stable(self, engine, venue):
        return engine.inventory._state.per_account_stable.get(venue, Decimal("0"))

    def _breaker(self, engine):
        return engine.inventory.get_status_dict()["circuit_breaker_active"]

    def test_balance_zeroes_inventory_and_broadcasts_warning(self):
        from engine.core.arbitrage.engine import _handle_preflight_error
        engine, alerts = self._make_engine_for_preflight()
        engine.inventory.reconcile_cngn({"uni-base": Decimal("500")})
        _handle_preflight_error(engine, "uni-base",
                                "execution reverted: ERC20: transfer amount exceeds balance",
                                "test_preflight")
        assert self._cngn(engine, "uni-base") == Decimal("0")
        assert any(a.get("severity") == "warning" and "uni-base" in a.get("message", "") for a in alerts)

    def test_stable_balance_zeroes_stable_only_and_broadcasts_warning(self):
        from engine.core.arbitrage.engine import _handle_preflight_error
        engine, alerts = self._make_engine_for_preflight()
        engine.inventory.reconcile_cngn({"uni-base": Decimal("500")})
        engine.inventory.reconcile_stables({"uni-base": Decimal("167.07")})
        _handle_preflight_error(
            engine,
            "uni-base",
            "execution reverted: ERC20: transfer amount exceeds balance",
            "dex_dex_buy_preflight_failed",
            wallet_asset="stable",
            wallet_symbol="USDC",
            required_amount=float(Decimal("250")),
        )
        assert self._stable(engine, "uni-base") == Decimal("0")
        assert self._cngn(engine, "uni-base") == Decimal("500")
        assert any(
            a.get("severity") == "warning" and "USDC balance on uni-base" in a.get("message", "")
            for a in alerts
        )

    def test_rpc_does_not_zero_inventory_and_broadcasts_warning(self):
        from engine.core.arbitrage.engine import _handle_preflight_error
        engine, alerts = self._make_engine_for_preflight()
        engine.inventory.reconcile_cngn({"uni-bsc": Decimal("500")})
        _handle_preflight_error(engine, "uni-bsc", "Read timed out.", "test_preflight")
        assert self._cngn(engine, "uni-bsc") == Decimal("500"), "RPC error must not zero inventory"
        assert any(a.get("severity") == "warning" and "uni-bsc" in a.get("message", "") for a in alerts)

    def test_permit2_does_not_zero_inventory_and_broadcasts_critical(self):
        from engine.core.arbitrage.engine import _handle_preflight_error
        engine, alerts = self._make_engine_for_preflight()
        engine.inventory.reconcile_cngn({"uni-base": Decimal("500")})
        _handle_preflight_error(engine, "uni-base",
                                "execution reverted: AllowanceExpired", "test_preflight")
        assert self._cngn(engine, "uni-base") == Decimal("500"), "Permit2 error must not zero inventory"
        assert any(a.get("severity") == "critical" for a in alerts)

    def test_pool_paused_trips_circuit_breaker_and_does_not_zero_inventory(self):
        from engine.core.arbitrage.engine import _handle_preflight_error
        engine, alerts = self._make_engine_for_preflight()
        engine.inventory.reconcile_cngn({"uni-bsc": Decimal("500")})
        _handle_preflight_error(engine, "uni-bsc",
                                "execution reverted: LOK", "test_preflight")
        assert self._cngn(engine, "uni-bsc") == Decimal("500"), "Pool paused must not zero inventory"
        assert self._breaker(engine) is True
        assert any(a.get("severity") == "critical" and "uni-bsc" in a.get("message", "") for a in alerts)

    def test_unknown_does_not_zero_inventory_and_does_not_trip_breaker(self):
        from engine.core.arbitrage.engine import _handle_preflight_error
        engine, alerts = self._make_engine_for_preflight()
        engine.inventory.reconcile_cngn({"uni-base": Decimal("500")})
        _handle_preflight_error(engine, "uni-base",
                                "execution reverted: SOME_WEIRD_ERROR", "test_preflight")
        assert self._cngn(engine, "uni-base") == Decimal("500"), "Unknown error must not zero inventory"
        assert self._breaker(engine) is False
        assert any(a.get("type") == "alert" for a in alerts)

    def test_buy_preflight_context_uses_stable_wallet(self):
        from engine.core.arbitrage.engine import _handle_preflight_error
        engine, alerts = self._make_engine_for_preflight()
        engine.venues["uni-base"] = SimpleNamespace(
            trade_account=SimpleNamespace(address="0x23DF63FAKE0000000000000000000000002e14E4"),
        )
        engine.inventory.reconcile_cngn({"uni-base": Decimal("99999")})
        engine.inventory.reconcile_stables({"uni-base": Decimal("167.07")})

        _handle_preflight_error(
            engine,
            "uni-base",
            "execution reverted: SOME_WEIRD_ERROR",
            "dex_dex_buy_preflight_failed",
            direction="UNI_BSC_TO_UNI_BASE_DELTA_BALANCE",
            size_usd=float(Decimal("250")),
            wallet_asset="stable",
            wallet_symbol="USDC",
            required_amount=float(Decimal("250")),
        )

        message = next(a["message"] for a in alerts if a.get("type") == "alert")
        assert "Wallet: 0x23DF...2e14E4 | 167.07 USDC | ~$167.07" in message
        assert "Shortfall: 82.93 USDC" in message

    def test_message_changes_when_trade_size_changes(self):
        from engine.core.arbitrage.engine import _handle_preflight_error
        engine, alerts = self._make_engine_for_preflight()
        engine.inventory.reconcile_cngn({"uni-base": Decimal("26999")})

        _handle_preflight_error(
            engine,
            "uni-base",
            "execution reverted: SOME_WEIRD_ERROR",
            "cex_dex_sell_preflight_failed",
            direction="QUIDAX_TO_UNI_BASE",
            size_usd=float(Decimal("500")),
            sell_cngn_est=float(Decimal("700000")),
            wallet_asset="cngn",
        )
        _handle_preflight_error(
            engine,
            "uni-base",
            "execution reverted: SOME_WEIRD_ERROR",
            "cex_dex_sell_preflight_failed",
            direction="QUIDAX_TO_UNI_BASE",
            size_usd=float(Decimal("650")),
            sell_cngn_est=float(Decimal("910000")),
            wallet_asset="cngn",
        )

        alert_events = [a for a in alerts if a.get("type") == "alert"]
        assert len(alert_events) == 2
        assert alert_events[0]["message"] != alert_events[1]["message"]
