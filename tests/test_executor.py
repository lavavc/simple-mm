"""Tests for arbitrage executor (Phase 1: detection-only stubs)."""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from engine.api.schemas import ArbitrageOpportunity
from engine.core.arbitrage.executor import ArbitrageExecutor


@pytest.fixture
def opportunity():
    return ArbitrageOpportunity(
        id="test-opp-1",
        timestamp=1700000000000,
        buy_venue="aerodrome",
        sell_venue="quidax",
        buy_price=Decimal("0.000690"),
        sell_price=Decimal("0.000750"),
        gross_spread_bps=870,
        net_spread_bps=800,
        recommended_size_usd=Decimal("500"),
        expected_profit_usd=Decimal("40"),
        status="detected",
    )


class TestExecutorDetectionMode:
    """Test executor in detection-only mode (Phase 1)."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, opportunity):
        executor = ArbitrageExecutor(
            venues={},
            execution_enabled=False,
        )
        success, profit, error = await executor.execute(opportunity)

        assert success is False
        assert profit is None
        assert "detection-only" in error.lower()

    @pytest.mark.asyncio
    async def test_not_implemented_when_enabled(self, opportunity):
        """Even when enabled, execution is not yet implemented."""
        executor = ArbitrageExecutor(
            venues={"aerodrome": MagicMock(), "quidax": MagicMock()},
            execution_enabled=True,
        )
        success, profit, error = await executor.execute(opportunity)

        assert success is False
        assert "not yet implemented" in error.lower()


class TestExecutorPhase2Stubs:
    """Test that Phase 2+ methods raise NotImplementedError."""

    @pytest.mark.asyncio
    async def test_dex_buy_not_implemented(self):
        executor = ArbitrageExecutor(venues={}, execution_enabled=True)
        with pytest.raises(NotImplementedError, match="Phase 2"):
            await executor.execute_dex_buy("aerodrome", Decimal("100"), 50)

    @pytest.mark.asyncio
    async def test_dex_sell_not_implemented(self):
        executor = ArbitrageExecutor(venues={}, execution_enabled=True)
        with pytest.raises(NotImplementedError, match="Phase 2"):
            await executor.execute_dex_sell("aerodrome", Decimal("1000"), Decimal("0.69"))

    @pytest.mark.asyncio
    async def test_cex_buy_not_implemented(self):
        executor = ArbitrageExecutor(venues={}, execution_enabled=True)
        with pytest.raises(NotImplementedError, match="Phase 3"):
            await executor.execute_cex_buy("quidax", Decimal("100"), Decimal("0.0007"))

    @pytest.mark.asyncio
    async def test_cex_sell_not_implemented(self):
        executor = ArbitrageExecutor(venues={}, execution_enabled=True)
        with pytest.raises(NotImplementedError, match="Phase 3"):
            await executor.execute_cex_sell("quidax", Decimal("1000"), Decimal("0.0007"))
