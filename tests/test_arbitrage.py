"""Tests for arbitrage opportunity detection."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from engine.api.schemas import ArbitrageParams, ArbitrageOpportunity
from engine.core.arbitrage.detector import ArbitrageDetector, _optimal_cngn_amount
from engine.core.price_aggregation import PriceNormalizer, NormalizedPrice
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


def _make_dex_mock(chain_id: int, fee_bps: int = 30) -> MagicMock:
    """Minimal DEX adapter mock with chain_id and get_fee_bps."""
    mock = MagicMock()
    mock.config.chain_id = chain_id
    mock.get_fee_bps.return_value = fee_bps
    return mock


# =============================================================================
# Fee estimation
# =============================================================================


class TestFeeEstimation:
    """Test fee calculation for venue pairs."""

    def test_dex_to_dex_fees(self, default_params):
        """Cross-chain DEX pair: 2 × swap_fee + rebalance cost fallback."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
            dex_venues={
                "aerodrome": _make_dex_mock(chain_id=8453),
                "pancakeswap": _make_dex_mock(chain_id=56),
            },
        )
        fees = detector._estimate_fees("aerodrome", "pancakeswap")
        expected = 2 * default_params.dex_swap_fee_bps + default_params.cross_chain_rebalance_bps
        assert fees == expected

    def test_dex_to_cex_fees(self, default_params):
        """One side DEX, one side CEX."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
            dex_venues={"aerodrome": _make_dex_mock(chain_id=8453)},
        )
        fees = detector._estimate_fees("aerodrome", "bybit")
        expected = default_params.dex_swap_fee_bps + default_params.cex_taker_fee_bps
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

    def test_no_reserves_returns_max(self, relaxed_params):
        """Without reserve data, returns max_single_trade_usd."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=relaxed_params,
        )
        size = detector._calculate_recommended_size(Decimal("0.0007"), None, "", "")
        assert size == relaxed_params.max_single_trade_usd


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


# =============================================================================
# Cross-chain fee estimation
# =============================================================================


class TestCrossChainFeeEstimation:
    """Cross-chain DEX pairs should add inventory-weighted rebalancing cost."""

    def test_cross_chain_dex_pair_adds_rebalance_cost(self, default_params):
        """aerodrome (Base) ↔ pancakeswap (BSC) adds cross_chain_rebalance_bps."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
            dex_venues={
                "aerodrome": _make_dex_mock(chain_id=8453),
                "pancakeswap": _make_dex_mock(chain_id=56),
            },
        )
        fees = detector._estimate_fees("aerodrome", "pancakeswap")
        assert fees == 2 * default_params.dex_swap_fee_bps + default_params.cross_chain_rebalance_bps

    def test_same_chain_dex_pair_no_extra_cost(self, default_params):
        """Two DEX venues on the same chain should not add rebalance cost."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
            dex_venues={
                "aerodrome": _make_dex_mock(chain_id=8453),
                "pancakeswap": _make_dex_mock(chain_id=8453),  # same chain
            },
        )
        fees = detector._estimate_fees("aerodrome", "pancakeswap")
        assert fees == 2 * default_params.dex_swap_fee_bps

    def test_cost_scales_with_inventory_level(self, default_params):
        """Rebalance cost should scale with inventory drain via inventory_tracker."""
        from engine.core.arbitrage.inventory import InventoryTracker
        tracker = InventoryTracker(default_params)
        tracker.initialize_account_stable({"aerodrome": Decimal("5000")})
        tracker.update_account_inventory("aerodrome", Decimal("2500"), is_buy=True)

        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
            inventory_tracker=tracker,
            dex_venues={
                "aerodrome": _make_dex_mock(chain_id=8453),
                "pancakeswap": _make_dex_mock(chain_id=56),
            },
        )
        fees = detector._estimate_fees("aerodrome", "pancakeswap")
        # 50% drained → 5 bps rebalance cost
        assert fees == 2 * default_params.dex_swap_fee_bps + 5

# =============================================================================
# Optimal sizing
# =============================================================================


class TestOptimalSizing:
    """Test pool-depth-aware trade sizing."""

    def test_equal_price_pools_gives_zero(self):
        """Identical pools → no profitable trade."""
        delta = _optimal_cngn_amount(
            Decimal("1000000"), Decimal("700"),
            Decimal("1000000"), Decimal("700"),
        )
        assert delta == Decimal("0")

    def test_divergent_pools_gives_correct_delta(self):
        """Known reserves → verify formula output."""
        # Pool A: buy side (cheap cNGN) — more cNGN relative to stable
        # Pool B: sell side (expensive cNGN) — less cNGN relative to stable
        cngn_A, stable_A = Decimal("2000000"), Decimal("1000")
        cngn_B, stable_B = Decimal("1000000"), Decimal("1000")
        delta = _optimal_cngn_amount(cngn_A, stable_A, cngn_B, stable_B)
        # Manual: k_A=2e9, k_B=1e9, sqrt_kA=~44721, sqrt_kB=~31623
        # delta = (31623*2000000 - 44721*1000000) / (44721 + 31623) ≈ 247_000
        assert delta > 0
        expected = (
            (Decimal("1000000000").sqrt() * cngn_A - Decimal("2000000000").sqrt() * cngn_B)
            / (Decimal("2000000000").sqrt() + Decimal("1000000000").sqrt())
        )
        assert abs(delta - expected) < Decimal("1")

    def test_negative_clamped_to_zero(self):
        """Buy pool more expensive than sell pool → 0."""
        # Pool A: expensive cNGN (low cNGN, high stable)
        # Pool B: cheap cNGN (high cNGN, low stable)
        delta = _optimal_cngn_amount(
            Decimal("500000"), Decimal("1000"),
            Decimal("2000000"), Decimal("700"),
        )
        assert delta == Decimal("0")

    def test_formula_runs_uncapped(self, relaxed_params):
        """With deep divergent pools the formula returns a size larger than max_single_trade_usd."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=relaxed_params,
        )
        reserves = {
            "aerodrome": (Decimal("1e12"), Decimal("1e9")),
            "pancakeswap": (Decimal("1e9"), Decimal("1e12")),
        }
        size = detector._calculate_recommended_size(
            Decimal("0.0007"), reserves, "aerodrome", "pancakeswap",
        )
        assert size > relaxed_params.max_single_trade_usd

    def test_falls_back_when_no_reserves(self, relaxed_params):
        """No reserve data → returns max_single_trade_usd."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=relaxed_params,
        )
        size = detector._calculate_recommended_size(Decimal("0.0007"), None, "", "")
        assert size == relaxed_params.max_single_trade_usd

    def test_falls_back_when_one_side_missing(self, relaxed_params):
        """Only one DEX has reserves → returns max_single_trade_usd."""
        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=relaxed_params,
        )
        reserves = {"aerodrome": (Decimal("1000000"), Decimal("700"))}
        size = detector._calculate_recommended_size(
            Decimal("0.0007"), reserves, "aerodrome", "pancakeswap",
        )
        assert size == relaxed_params.max_single_trade_usd

    def test_full_stock_adds_zero_rebalance(self, default_params):
        """When buy-side is fully stocked, rebalance cost should be 0."""
        from engine.core.arbitrage.inventory import InventoryTracker
        tracker = InventoryTracker(default_params)
        tracker.initialize_account_stable({"aerodrome": Decimal("5000")})

        detector = ArbitrageDetector(
            price_aggregator=MagicMock(),
            params=default_params,
            inventory_tracker=tracker,
            dex_venues={
                "aerodrome": _make_dex_mock(chain_id=8453),
                "pancakeswap": _make_dex_mock(chain_id=56),
            },
        )
        fees = detector._estimate_fees("aerodrome", "pancakeswap")
        assert fees == 2 * default_params.dex_swap_fee_bps
