"""Tests for arbitrage opportunity detection."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from engine.api.schemas import ArbitrageParams, ArbitrageOpportunity
from engine.core.arbitrage.detector import ArbitrageDetector
from engine.core.price_aggregation import (
    PriceNormalizer,
    NormalizedPrice,
    USDT_NGN_VENUES,
    CNGN_USD_VENUES,
)
from engine.core.venue_prices import VenuePrice
from engine.api.schemas import PriceQuote


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def default_params():
    """Default arb params: 150 bps min gross, 50 bps min net."""
    return ArbitrageParams()


@pytest.fixture
def relaxed_params():
    """Relaxed params for testing: 50 bps min gross, 10 bps min net."""
    return ArbitrageParams(
        min_spread_bps=50,
        min_net_profit_bps=10,
        dex_swap_fee_bps=30,
        dex_slippage_bps=10,
        cex_taker_fee_bps=15,
        max_single_trade_usd=Decimal("500"),
    )


def _make_quote(source: str, mid: Decimal) -> PriceQuote:
    return PriceQuote(
        source=source, timestamp=1700000000000,
        bid=mid * Decimal("0.999"),
        ask=mid * Decimal("1.001"),
        mid=mid,
    )


# =============================================================================
# Fee estimation
# =============================================================================


class TestFeeEstimation:
    """Test fee calculation for venue pairs."""

    def test_dex_to_dex_fees(self, default_params):
        """Both sides DEX: 2 × (swap_fee + slippage)."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
        )
        fees = detector._estimate_fees("aerodrome", "pancakeswap")
        expected = 2 * (default_params.dex_swap_fee_bps + default_params.dex_slippage_bps)
        assert fees == expected

    def test_dex_to_cex_fees(self, default_params):
        """One side DEX, one side CEX."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
        )
        fees = detector._estimate_fees("aerodrome", "bybit")
        expected = (
            default_params.dex_swap_fee_bps + default_params.dex_slippage_bps
            + default_params.cex_taker_fee_bps
        )
        assert fees == expected

    def test_fair_value_has_no_fees(self, default_params):
        """Trading against fair_value reference has no execution fees."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
        )
        fees = detector._estimate_fees("fair_value", "aerodrome")
        # Only the DEX side
        expected = default_params.dex_swap_fee_bps + default_params.dex_slippage_bps
        assert fees == expected


# =============================================================================
# Opportunity checking
# =============================================================================


class TestCheckOpportunity:
    """Test the _check_opportunity method."""

    def test_profitable_opportunity(self, relaxed_params):
        """Large spread should produce an opportunity."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=relaxed_params,
        )
        # Buy at 0.000690, sell at 0.000750 → ~870 bps gross spread
        opp = detector._check_opportunity(
            "aerodrome", Decimal("0.000690"),
            "quidax", Decimal("0.000750"),
        )
        assert opp is not None
        assert opp.gross_spread_bps > 0
        assert opp.net_spread_bps > 0
        assert opp.expected_profit_usd > 0
        assert opp.buy_venue == "aerodrome"
        assert opp.sell_venue == "quidax"

    def test_too_small_spread_rejected(self, default_params):
        """Spread below min_spread_bps should return None."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
        )
        # 0.5% spread → 50 bps, below default 150 bps
        opp = detector._check_opportunity(
            "aerodrome", Decimal("0.000696"),
            "quidax", Decimal("0.0006995"),
        )
        assert opp is None

    def test_negative_spread_rejected(self, relaxed_params):
        """Selling cheaper than buying should return None."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=relaxed_params,
        )
        opp = detector._check_opportunity(
            "quidax", Decimal("0.000700"),
            "aerodrome", Decimal("0.000690"),
        )
        assert opp is None

    def test_zero_buy_price_rejected(self, relaxed_params):
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=relaxed_params,
        )
        opp = detector._check_opportunity(
            "quidax", Decimal("0"),
            "aerodrome", Decimal("0.000700"),
        )
        assert opp is None


# =============================================================================
# Recommended size
# =============================================================================


class TestRecommendedSize:
    """Test trade size calculation."""

    def test_large_spread_full_size(self, relaxed_params):
        """Large spread buffer should use full max size."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=relaxed_params,
        )
        # 200 bps net, well above min of 10 → full size
        size = detector._calculate_recommended_size(200)
        assert size == relaxed_params.max_single_trade_usd

    def test_small_spread_reduced_size(self, relaxed_params):
        """Spread close to minimum should reduce size."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=relaxed_params,
        )
        # 30 bps net, buffer of 20 (< 50) → 50% of max
        size = detector._calculate_recommended_size(30)
        assert size == relaxed_params.max_single_trade_usd * Decimal("0.5")


# =============================================================================
# Full detection flow (async)
# =============================================================================


class TestDetectOpportunities:
    """Test the full detect_opportunities async method."""

    @pytest.mark.asyncio
    async def test_detects_clear_opportunity(self, relaxed_params):
        """Should detect opportunity when large price divergence exists."""
        # Mock aggregator that returns divergent prices
        mock_agg = AsyncMock()
        mock_agg.fetch_all.return_value = {
            "aerodrome": VenuePrice(
                venue="aerodrome", pair="cNGN/USDC",
                quote=_make_quote("aerodrome_pool", Decimal("0.000680")),
            ),
            "quidax": VenuePrice(
                venue="quidax", pair="cNGN/USDT",
                quote=_make_quote("quidax", Decimal("0.000750")),
            ),
        }

        detector = ArbitrageDetector(
            price_aggregator=mock_agg,
            params=relaxed_params,
        )
        opps = await detector.detect_opportunities()

        assert len(opps) > 0
        # Should find buying at aerodrome and selling at quidax
        buy_at_aero = [o for o in opps if o.buy_venue == "aerodrome"]
        assert len(buy_at_aero) > 0

    @pytest.mark.asyncio
    async def test_no_opportunity_when_prices_similar(self, default_params):
        """Should find no opportunities when prices are tight."""
        mock_agg = AsyncMock()
        mock_agg.fetch_all.return_value = {
            "aerodrome": VenuePrice(
                venue="aerodrome", pair="cNGN/USDC",
                quote=_make_quote("aerodrome_pool", Decimal("0.000697")),
            ),
            "quidax": VenuePrice(
                venue="quidax", pair="cNGN/USDT",
                quote=_make_quote("quidax", Decimal("0.000698")),
            ),
        }

        detector = ArbitrageDetector(
            price_aggregator=mock_agg,
            params=default_params,
        )
        opps = await detector.detect_opportunities()
        assert len(opps) == 0

    @pytest.mark.asyncio
    async def test_insufficient_venues(self, default_params):
        """Should return empty list with fewer than 2 venues."""
        mock_agg = AsyncMock()
        mock_agg.fetch_all.return_value = {
            "quidax": VenuePrice(
                venue="quidax", pair="cNGN/USDT",
                quote=_make_quote("quidax", Decimal("0.000700")),
            ),
        }

        detector = ArbitrageDetector(
            price_aggregator=mock_agg,
            params=default_params,
        )
        opps = await detector.detect_opportunities()
        assert opps == []

    @pytest.mark.asyncio
    async def test_results_sorted_by_profit(self, relaxed_params):
        """Opportunities should be sorted by expected_profit_usd descending."""
        mock_agg = AsyncMock()
        mock_agg.fetch_all.return_value = {
            "aerodrome": VenuePrice(
                venue="aerodrome", pair="cNGN/USDC",
                quote=_make_quote("aerodrome_pool", Decimal("0.000650")),
            ),
            "quidax": VenuePrice(
                venue="quidax", pair="cNGN/USDT",
                quote=_make_quote("quidax", Decimal("0.000750")),
            ),
            "pancakeswap": VenuePrice(
                venue="pancakeswap", pair="cNGN/USDT",
                quote=_make_quote("pancakeswap_pool", Decimal("0.000700")),
            ),
        }

        detector = ArbitrageDetector(
            price_aggregator=mock_agg,
            params=relaxed_params,
        )
        opps = await detector.detect_opportunities()

        if len(opps) >= 2:
            for i in range(len(opps) - 1):
                assert opps[i].expected_profit_usd >= opps[i + 1].expected_profit_usd
