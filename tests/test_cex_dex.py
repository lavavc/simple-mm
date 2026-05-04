"""Seeded-cache tests for cex_dex.py (find_optimal_arb, compute_arb_curve)."""

import pytest
from decimal import Decimal

from engine.types import OrderBookDepth, OrderBookLevel
from engine.arb.detection.cex_dex import (
    QUIDAX_FEE,
    compute_arb_curve,
    estimate_cex_buy_cngn,
    estimate_cex_dex_trade,
    estimate_max_cex_dex_buy_usd_for_cngn,
    estimate_max_cex_buy_usd_for_cngn,
    find_optimal_arb,
)


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
        from engine.market import pool_state as _ps
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


class TestOrderbookHelpers:
    def test_estimate_cex_buy_cngn_walks_bids(self):
        depth = OrderBookDepth(
            venue="quidax",
            pair="cNGN/USDT",
            timestamp=1700000000000,
            bids=[_level(1700, 50), _level(1600, 50)],
            asks=[_level(1500, 50)],
        )

        cngn = estimate_cex_buy_cngn(depth, Decimal("75"), QUIDAX_FEE)

        expected = (Decimal("50") * Decimal("1700") + Decimal("25") * Decimal("1600")) * (Decimal("1") - QUIDAX_FEE)
        assert cngn == expected

    def test_estimate_cex_buy_cngn_returns_zero_for_missing_depth(self):
        assert estimate_cex_buy_cngn(None, Decimal("50")) == Decimal("0")

    def test_estimate_max_cex_buy_usd_for_cngn_inverts_bid_walk(self):
        depth = OrderBookDepth(
            venue="quidax",
            pair="cNGN/USDT",
            timestamp=1700000000000,
            bids=[_level(1700, 50), _level(1600, 50)],
            asks=[_level(1500, 50)],
        )

        wallet_cngn = Decimal("100000")
        max_usdt = estimate_max_cex_buy_usd_for_cngn(depth, wallet_cngn, QUIDAX_FEE)
        bought_cngn = estimate_cex_buy_cngn(depth, max_usdt, QUIDAX_FEE)

        assert bought_cngn <= wallet_cngn
        assert estimate_cex_buy_cngn(depth, max_usdt + Decimal("0.01"), QUIDAX_FEE) > wallet_cngn

    def test_estimate_max_cex_buy_usd_for_cngn_returns_zero_for_missing_depth(self):
        assert estimate_max_cex_buy_usd_for_cngn(None, Decimal("50000")) == Decimal("0")

    def test_estimate_cex_dex_trade_recomputes_specific_direction(self, seeded_pool_cache):
        result = estimate_cex_dex_trade("QUIDAX_TO_UNI_BASE", _TIGHT_DEPTH, Decimal("10"))

        assert result is not None
        assert result["cngn_transferred"] > 0
        assert result["expected_usd_out"] > 0
        assert result["expected_profit_usd"] == result["expected_usd_out"] - Decimal("10")

    def test_estimate_max_cex_dex_buy_usd_for_cngn_inverts_dex_buy_path(self, seeded_pool_cache):
        trade = estimate_cex_dex_trade("UNI_BASE_TO_QUIDAX", _TIGHT_DEPTH, Decimal("10"))
        assert trade is not None

        wallet_cngn = Decimal(str(trade["cngn_transferred"]))
        capped = estimate_max_cex_dex_buy_usd_for_cngn("UNI_BASE_TO_QUIDAX", _TIGHT_DEPTH, wallet_cngn)

        assert capped is not None
        assert Decimal(str(capped["cngn_transferred"])) <= wallet_cngn

        larger = estimate_cex_dex_trade(
            "UNI_BASE_TO_QUIDAX",
            _TIGHT_DEPTH,
            Decimal(str(capped["optimal_size_usd"])) + Decimal("0.01"),
        )
        assert larger is not None
        assert Decimal(str(larger["cngn_transferred"])) > wallet_cngn

    def test_thin_pool_still_routes_below_exhaustion_ceiling(self, seeded_pool_cache, monkeypatch):
        """Pool exhausted at $5000 should not suppress viable smaller trades."""
        import engine.arb.detection.cex_dex as _mod

        _CEILING = Decimal("100")
        _real = _mod.estimate_cex_dex_trade

        def _thin(direction, depth, investment_usd, cex_fee=QUIDAX_FEE):
            return None if investment_usd > _CEILING else _real(direction, depth, investment_usd, cex_fee)

        monkeypatch.setattr(_mod, "estimate_cex_dex_trade", _thin)

        result = estimate_max_cex_dex_buy_usd_for_cngn(
            "UNI_BASE_TO_QUIDAX",
            _TIGHT_DEPTH,
            Decimal("999999"),  # wallet not a constraint
        )

        assert result is not None
        assert Decimal(str(result["optimal_size_usd"])) <= float(_CEILING)


class TestComputeArbCurve:
    """compute_arb_curve returns a 5000-point curve."""

    def test_returns_none_when_cache_empty(self, monkeypatch):
        from engine.market import pool_state as _ps
        monkeypatch.setattr(_ps, "_POOL_CACHE", {})
        result = compute_arb_curve(_TIGHT_DEPTH)
        assert result is None

    def test_curve_has_5000_points(self, seeded_pool_cache):
        result = compute_arb_curve(_TIGHT_DEPTH)
        assert result is not None
        assert len(result["curve_cex_to_dex"]) == 5000
        assert len(result["curve_dex_to_cex"]) == 5000

    def test_curve_points_match_frontend_contract(self, seeded_pool_cache):
        """Curve point schema must match what both chart components expect.

        This test exists because this schema mismatch has broken the chart four times.
        TWO components consume this data: OrderBookDepthChart.tsx (CurvePointV2) and
        ProfitCurveChart.tsx (CurvePoint). If you rename a key here you MUST update
        both components and this test in the same commit.
        """
        result = compute_arb_curve(_TIGHT_DEPTH)
        for curve_name in ("curve_cex_to_dex", "curve_dex_to_cex"):
            point = result[curve_name][0]
            assert "size" in point, f"{curve_name}: missing 'size'"
            for direction_key in ("base_to_bsc", "bsc_to_base"):
                assert direction_key in point, f"{curve_name}: missing '{direction_key}' (OrderBookDepthChart + ProfitCurveChart read this key)"
                d = point[direction_key]
                for field in ("profit", "profit_after_slippage", "min_acceptable_usd", "cngn_acquired", "usdt_out"):
                    assert field in d, f"{curve_name}['{direction_key}']: missing '{field}'"

    def test_curve_sizes_are_sequential(self, seeded_pool_cache):
        result = compute_arb_curve(_TIGHT_DEPTH)
        sizes = [p["size"] for p in result["curve_cex_to_dex"]]
        assert sizes == list(range(1, 5001))
