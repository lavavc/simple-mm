"""Seeded-cache tests for dex_dex.py (find_optimal_dex_arb)."""

import pytest
from decimal import Decimal

from engine.core.arbitrage.dex_dex import (
    estimate_dex_dex_trade,
    find_optimal_dex_arb,
)


class TestFindOptimalDexArbNullCases:
    """find_optimal_dex_arb returns None when pool state is missing or thin."""

    def test_none_when_cache_empty(self, monkeypatch):
        from engine.core.arbitrage import pool_state as _ps
        monkeypatch.setattr(_ps, "_POOL_CACHE", {})
        result = find_optimal_dex_arb()
        assert result is None

    def test_none_when_fee_missing(self, monkeypatch):
        """Pool with fee=None blocks execution."""
        from engine.core.arbitrage import pool_state as _ps
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
        from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
        from engine.venues.dex.assetchain import ASSETCHAIN_POOL_READ_CONFIG
        import time

        fake_cache = {
            UNISWAP_BASE_POOL_READ_CONFIG.pool_address: {
                "tick": 0, "liquidity": Decimal(10**18), "fee": None,
                "sqrt_p": Decimal(10**25), "balance0": Decimal("10000"),
                "balance1": Decimal("10000"), "timestamp": time.time(),
            },
            UNISWAP_BSC_POOL_READ_CONFIG.pool_address: {
                "tick": 0, "liquidity": Decimal(10**18), "fee": Decimal("0.0005"),
                "sqrt_p": Decimal(10**20), "balance0": Decimal("10000"),
                "balance1": Decimal("10000"), "timestamp": time.time(),
            },
            ASSETCHAIN_POOL_READ_CONFIG.pool_address: {
                "tick": 0, "liquidity": Decimal(0), "fee": Decimal("0.0003"),
                "sqrt_p": Decimal(0), "balance0": None, "balance1": None,
                "timestamp": time.time(),
            },
        }
        monkeypatch.setattr(_ps, "_POOL_CACHE", fake_cache)
        result = find_optimal_dex_arb()
        assert result is None

    def test_none_when_pools_have_zero_liquidity(self, monkeypatch):
        """Zero in-range liquidity (no active LP) blocks execution."""
        from engine.core.arbitrage import pool_state as _ps
        from engine.venues.dex.uniswap_base import UNISWAP_BASE_POOL_READ_CONFIG
        from engine.venues.dex.uniswap_bsc import UNISWAP_BSC_POOL_READ_CONFIG
        from engine.venues.dex.assetchain import ASSETCHAIN_POOL_READ_CONFIG
        import math, time

        base_sqrt = Decimal(int(math.sqrt(0.000606) * 2**96))
        bsc_sqrt = Decimal(int(math.sqrt(1 / (0.000606 * 1e12)) * 2**96))

        fake_cache = {
            UNISWAP_BASE_POOL_READ_CONFIG.pool_address: {
                "tick": -276324, "liquidity": Decimal(0), "fee": Decimal("0.0005"),
                "sqrt_p": base_sqrt, "balance0": None, "balance1": None,
                "timestamp": time.time(),
            },
            UNISWAP_BSC_POOL_READ_CONFIG.pool_address: {
                "tick": -276324, "liquidity": Decimal(0), "fee": Decimal("0.0005"),
                "sqrt_p": bsc_sqrt, "balance0": None, "balance1": None,
                "timestamp": time.time(),
            },
            ASSETCHAIN_POOL_READ_CONFIG.pool_address: {
                "tick": 0, "liquidity": Decimal(0), "fee": Decimal("0.0003"),
                "sqrt_p": Decimal(0), "balance0": None, "balance1": None,
                "timestamp": time.time(),
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
