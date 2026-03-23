"""Tests for CEX-DEX preflight gate and _clean_revert helper."""

import pytest
import tempfile
import os
from decimal import Decimal
from unittest.mock import patch

from eth_abi import encode

from engine.api.schemas import ArbitrageParams, TxResult, PriceQuote
from engine.core.arbitrage.engine import ArbitrageEngine
from engine.core.arbitrage.router import RouteCandidate, SelectedRoute
from engine.core.arbitrage.executor import _clean_revert
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
            "prices": {"quidax": "0.00061"},
        },
    )
    return SelectedRoute(candidate=candidate, adjusted_size_usd=size, net_profit_usd=Decimal("1.45"))


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
