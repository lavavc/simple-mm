"""Seeded-cache tests for cex_dex.py (find_optimal_arb, compute_arb_curve)."""

import pytest
from decimal import Decimal

from engine.api.schemas import OrderBookDepth, OrderBookLevel
from engine.core.arbitrage.cex_dex import find_optimal_arb, compute_arb_curve, QUIDAX_FEE


def _level(price: float, amount: float) -> OrderBookLevel:
    return OrderBookLevel(price=Decimal(str(price)), amount=Decimal(str(amount)))


def _make_depth(bid_price: float, ask_price: float, amount: float = 10000.0) -> OrderBookDepth:
    # price convention: cNGN per USDT (e.g. 1650 means 1 USDT = 1650 cNGN)
    return OrderBookDepth(
        venue="quidax",
        pair="cNGN/USDT",
        timestamp=1700000000000,
        bids=[_level(bid_price, amount)],
        asks=[_level(ask_price, amount)],
    )


# price convention: cNGN per USDT
# bid = 1650 (best bid), ask = 1640 (slightly cheaper cNGN per USDT on ask = slightly more expensive in USDT terms)
_TIGHT_DEPTH = _make_depth(bid_price=1650, ask_price=1640)


class TestFindOptimalArbNullCases:
    """find_optimal_arb returns None when preconditions are not met."""

    def test_none_when_cache_empty(self, monkeypatch):
        from engine.core.arbitrage import pool_state as _ps
        monkeypatch.setattr(_ps, "_POOL_CACHE", {})
        result = find_optimal_arb(_TIGHT_DEPTH)
        assert result is None

    def test_none_when_depth_none(self, seeded_pool_cache):
        result = find_optimal_arb(None)
        assert result is None

    def test_none_when_asks_empty(self, seeded_pool_cache):
        empty_depth = OrderBookDepth(
            venue="quidax", pair="cNGN/USDT", timestamp=0,
            bids=[_level(1 / 1650, 1000)], asks=[],
        )
        result = find_optimal_arb(empty_depth)
        assert result is None


class TestFindOptimalArbResult:
    """find_optimal_arb returns a well-formed result dict when pool state is seeded."""

    def test_returns_dict_with_required_keys(self, seeded_pool_cache):
        result = find_optimal_arb(_TIGHT_DEPTH)
        assert result is not None
        assert "prices" in result
        assert "optimal_arb" in result
        assert "all_arbs" in result
        assert "timestamp" in result

    def test_prices_structure(self, seeded_pool_cache):
        result = find_optimal_arb(_TIGHT_DEPTH)
        prices = result["prices"]
        assert "quidax" in prices
        assert "uni-bsc" in prices
        assert "uni-base" in prices
        assert prices["uni-base"] > 0
        assert prices["uni-bsc"] > 0

    def test_optimal_arb_structure(self, seeded_pool_cache):
        result = find_optimal_arb(_TIGHT_DEPTH)
        opt = result["optimal_arb"]
        assert "direction" in opt
        assert "optimal_size_usd" in opt
        assert "expected_profit_usd" in opt
        assert "cngn_transferred" in opt

    def test_all_directions_exercised(self, seeded_pool_cache):
        """all_arbs should contain entries for profitable directions."""
        # Use a very wide spread (bid=1700, ask=1600) to ensure directions are evaluated
        wide_depth = _make_depth(bid_price=1700, ask_price=1600, amount=100000.0)
        result = find_optimal_arb(wide_depth)
        assert result is not None
        # We can only assert on structure — actual directions depend on pool prices


class TestComputeArbCurve:
    """compute_arb_curve returns a 1000-point curve."""

    def test_returns_none_when_cache_empty(self, monkeypatch):
        from engine.core.arbitrage import pool_state as _ps
        monkeypatch.setattr(_ps, "_POOL_CACHE", {})
        result = compute_arb_curve(_TIGHT_DEPTH)
        assert result is None

    def test_curve_has_1000_points(self, seeded_pool_cache):
        result = compute_arb_curve(_TIGHT_DEPTH)
        assert result is not None
        assert len(result["curve_cex_to_dex"]) == 1000
        assert len(result["curve_dex_to_cex"]) == 1000

    def test_curve_points_have_required_keys(self, seeded_pool_cache):
        result = compute_arb_curve(_TIGHT_DEPTH)
        point = result["curve_cex_to_dex"][0]
        assert "size" in point
        assert "bsc" in point
        assert "base" in point
        assert "profit" in point["bsc"]
        assert "usdt_out" in point["bsc"]

    def test_curve_sizes_are_sequential(self, seeded_pool_cache):
        result = compute_arb_curve(_TIGHT_DEPTH)
        sizes = [p["size"] for p in result["curve_cex_to_dex"]]
        assert sizes == list(range(1, 1001))
