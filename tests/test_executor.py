"""Tests for arbitrage executor (Phase 1: detection-only stubs)."""

import pytest
from decimal import Decimal

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

