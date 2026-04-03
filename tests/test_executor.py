"""Tests for ArbitrageExecutor leg methods (execute_dex_buy/sell, execute_cex_buy/sell)."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from engine.api.schemas import TxResult, PriceQuote
from engine.arb.execution.executor import ArbitrageExecutor
from tests.fakes import FakeCexAdapter, FakeDexAdapter


def _make_dex_venue(price=Decimal("0.000610"), swap_ok=True):
    venue = FakeDexAdapter()
    venue.stable_address = "0xusdc"
    venue.cngn_address = "0xcngn"
    venue.stable_decimals = 6
    venue.cngn_decimals = 6

    async def _get_price():
        return PriceQuote(source="test", timestamp=0, bid=price, ask=price, mid=price)

    async def _swap(token_in, amount_in, min_out):
        if swap_ok:
            return TxResult(hash="0xabc", status="confirmed", output_raw=amount_in)
        return TxResult(hash="", status="failed", error="swap failed")

    venue.get_current_price = _get_price
    venue.swap = _swap
    return venue


class TestExecuteDexBuy:
    @pytest.mark.asyncio
    async def test_success(self):
        venue = _make_dex_venue()
        executor = ArbitrageExecutor(venues={"uni-base": venue})
        trade = await executor.execute_dex_buy("uni-base", Decimal("500"), "opp-1")
        assert trade is not None
        assert trade.status == "confirmed"
        assert trade.side == "buy"

    @pytest.mark.asyncio
    async def test_swap_failure(self):
        venue = _make_dex_venue(swap_ok=False)
        executor = ArbitrageExecutor(venues={"uni-base": venue})
        trade = await executor.execute_dex_buy("uni-base", Decimal("500"), "opp-1")
        assert trade.status == "failed"

    @pytest.mark.asyncio
    async def test_no_price_quote(self):
        venue = _make_dex_venue()
        venue.get_current_price = AsyncMock(return_value=None)
        executor = ArbitrageExecutor(venues={"uni-base": venue})
        trade = await executor.execute_dex_buy("uni-base", Decimal("500"), "opp-1")
        assert trade.status == "failed"
        assert "price" in (trade.error or "").lower()


class TestExecuteDexSell:
    @pytest.mark.asyncio
    async def test_success(self):
        venue = _make_dex_venue()
        executor = ArbitrageExecutor(venues={"uni-base": venue})
        trade = await executor.execute_dex_sell("uni-base", Decimal("800000"), Decimal("490"), "opp-1")
        assert trade.status == "confirmed"
        assert trade.side == "sell"

    @pytest.mark.asyncio
    async def test_swap_failure(self):
        venue = _make_dex_venue(swap_ok=False)
        executor = ArbitrageExecutor(venues={"uni-base": venue})
        trade = await executor.execute_dex_sell("uni-base", Decimal("800000"), Decimal("490"), "opp-1")
        assert trade.status == "failed"


class TestExecuteCexBuy:
    @pytest.mark.asyncio
    async def test_success(self):
        cex = FakeCexAdapter(buy_success=True)
        executor = ArbitrageExecutor(venues={"quidax": cex})
        trade = await executor.execute_cex_buy("quidax", Decimal("500"), Decimal("0.000606"), "opp-1")
        assert trade.status == "submitted"
        assert trade.side == "buy"

    @pytest.mark.asyncio
    async def test_failure(self):
        cex = FakeCexAdapter(buy_success=False)
        executor = ArbitrageExecutor(venues={"quidax": cex})
        trade = await executor.execute_cex_buy("quidax", Decimal("500"), Decimal("0.000606"), "opp-1")
        assert trade.status == "failed"


class TestExecuteCexSell:
    @pytest.mark.asyncio
    async def test_success(self):
        cex = FakeCexAdapter(sell_success=True)
        executor = ArbitrageExecutor(venues={"quidax": cex})
        trade = await executor.execute_cex_sell("quidax", Decimal("800000"), Decimal("0.000610"), "opp-1")
        assert trade.status == "submitted"
        assert trade.side == "sell"

    @pytest.mark.asyncio
    async def test_failure(self):
        cex = FakeCexAdapter(sell_success=False)
        executor = ArbitrageExecutor(venues={"quidax": cex})
        trade = await executor.execute_cex_sell("quidax", Decimal("800000"), Decimal("0.000610"), "opp-1")
        assert trade.status == "failed"
