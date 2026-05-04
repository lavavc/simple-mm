"""Seeded-cache tests for dex_dex.py (find_optimal_dex_arb)."""

import pytest
from decimal import Decimal

from engine.arb.detection.dex_dex import (
    estimate_dex_dex_trade,
    estimate_max_dex_buy_usd_for_cngn,
    find_optimal_dex_arb,
)


class TestFindOptimalDexArbNullCases:
    """find_optimal_dex_arb returns None when pool state is missing or thin."""

    def test_none_when_cache_empty(self, monkeypatch):
        from engine.market import pool_state as _ps
        monkeypatch.setattr(_ps, "_POOL_CACHE", {})
        result = find_optimal_dex_arb()
        assert result is None

    def test_none_when_fee_missing(self, monkeypatch):
        """Pool with fee=None blocks execution."""
        from engine.market import pool_state as _ps
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
        from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
        import time

        fake_cache = {
            UNISWAP_BASE_POOL_READ_CONFIG.pool_address: {
                "tick": 0, "liquidity": Decimal(10**18), "fee": None,
                "sqrt_p": Decimal(10**25), "timestamp": time.time(),
            },
            UNISWAP_BSC_POOL_READ_CONFIG.pool_address: {
                "tick": 0, "liquidity": Decimal(10**18), "fee": Decimal("0.0005"),
                "sqrt_p": Decimal(10**20), "timestamp": time.time(),
            },
        }
        monkeypatch.setattr(_ps, "_POOL_CACHE", fake_cache)
        result = find_optimal_dex_arb()
        assert result is None


class TestFindOptimalDexArbResult:
    """find_optimal_dex_arb returns a well-formed result dict."""

    def test_returns_dict_with_required_keys(self, seeded_pool_cache):
        result = find_optimal_dex_arb()
        assert result is not None
        assert "prices" in result
        assert "optimal_arb" in result
        assert "stats" in result
        assert "timestamp" in result

    def test_optimal_arb_has_direction(self, seeded_pool_cache):
        result = find_optimal_dex_arb()
        opt = result["optimal_arb"]
        assert opt["direction"] in (
            "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE",
            "UNI_BASE_TO_UNI_BSC_DELTA_BALANCE",
        )

    def test_prices_both_positive(self, seeded_pool_cache):
        result = find_optimal_dex_arb()
        assert result["prices"]["uni-bsc"] > 0
        assert result["prices"]["uni-base"] > 0

    def test_stats_include_pool_liquidity(self, seeded_pool_cache):
        result = find_optimal_dex_arb()
        stats = result["stats"]
        assert "uni_bsc_liquidity_cngn_raw" in stats
        assert "uni_base_liquidity_cngn_raw" in stats

    def test_spread_bps_computed(self, seeded_pool_cache):
        result = find_optimal_dex_arb()
        assert "net_spread_bps" in result["optimal_arb"]

    def test_estimate_trade_scales_with_routed_size(self, seeded_pool_cache):
        smaller = estimate_dex_dex_trade("UNI_BSC_TO_UNI_BASE_DELTA_BALANCE", Decimal("100"))
        larger = estimate_dex_dex_trade("UNI_BSC_TO_UNI_BASE_DELTA_BALANCE", Decimal("500"))
        assert smaller is not None
        assert larger is not None
        assert smaller["cngn_transferred"] < larger["cngn_transferred"]
        assert smaller["expected_usd_out"] < larger["expected_usd_out"]

    def test_exact_wallet_cap_inverts_trade_size(self, seeded_pool_cache):
        trade = estimate_dex_dex_trade("UNI_BSC_TO_UNI_BASE_DELTA_BALANCE", Decimal("250"))
        assert trade is not None

        capped = estimate_max_dex_buy_usd_for_cngn(
            "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE",
            Decimal(str(trade["cngn_transferred"])),
        )

        assert capped is not None
        assert abs(Decimal(str(capped["optimal_size_usd"])) - Decimal("250")) <= Decimal("0.05")
        assert Decimal(str(capped["cngn_transferred"])) <= Decimal(str(trade["cngn_transferred"]))
