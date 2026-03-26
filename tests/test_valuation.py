"""Seeded-cache tests for valuation.py."""

import pytest
from decimal import Decimal
from types import SimpleNamespace

from engine.core.arbitrage.valuation import portfolio_value, cex_holdings_value, dex_holdings_value
from engine.api.schemas import OrderBookDepth, OrderBookLevel
from engine.core.arbitrage.cex_dex import QUIDAX_FEE
from engine.core.arbitrage.pool_state import swap_token0_for_token1


def _level(price: float, amount: float) -> OrderBookLevel:
    return OrderBookLevel(price=Decimal(str(price)), amount=Decimal(str(amount)))


def _make_depth(bid_price: float, ask_price: float, amount: float = 10000.0) -> OrderBookDepth:
    # price convention: cNGN per USDT (e.g. 1650 means 1 USDT = 1650 cNGN)
    return OrderBookDepth(
        venue="quidax", pair="cNGN/USDT", timestamp=0,
        bids=[_level(bid_price, amount)],
        asks=[_level(ask_price, amount)],
    )


def _make_balance(role: str, cngn: float = 0.0, usdt: float = 0.0, usdc: float = 0.0):
    return SimpleNamespace(
        role=role,
        token_balances={
            "cNGN": Decimal(str(cngn)),
            "USDT": Decimal(str(usdt)),
            "USDC": Decimal(str(usdc)),
        },
    )


# price convention: cNGN per USDT (1650 means 1 USDT = 1650 cNGN)
_DEPTH = _make_depth(bid_price=1650, ask_price=1640)


class TestCexHoldingsValue:
    def test_zero_cngn_returns_zero(self):
        # ask.price = 1640 cNGN/USDT, amount = 1000 USDT → 1,640,000 cNGN available
        asks = [_level(1640, 1000)]
        value = cex_holdings_value(asks, Decimal("0"), QUIDAX_FEE)
        assert value == Decimal("0")

    def test_positive_cngn_returns_positive_value(self):
        asks = [_level(1640, 1000)]  # 1,640,000 cNGN available
        value = cex_holdings_value(asks, Decimal("100000"), QUIDAX_FEE)
        assert value > Decimal("0")

    def test_output_matches_walk_orderbook_asks(self):
        """cex_holdings_value is a thin wrapper — output must match walk directly."""
        from engine.core.arbitrage.cex_dex import walk_orderbook_asks
        asks = [_level(1640, 1000)]
        expected, _ = walk_orderbook_asks(asks, Decimal("50000"), QUIDAX_FEE)
        actual = cex_holdings_value(asks, Decimal("50000"), QUIDAX_FEE)
        assert actual == expected


class TestDexHoldingsValue:
    def test_cngn_is_token0_uses_swap_t0_for_t1(self, seeded_pool_cache):
        from engine.core.arbitrage.pool_state import get_cached_pool_state, swap_token0_for_token1
        base_key = seeded_pool_cache["uni-base"]
        sqrt_p, liq, _, fee = get_cached_pool_state(base_key)

        value = dex_holdings_value(
            cngn_amount=Decimal("100000"),
            sqrt_p=sqrt_p, liquidity=liq, fee=fee,
            token0_decimals=6, token1_decimals=6,
            cngn_is_token0=True,
        )
        expected = swap_token0_for_token1(Decimal("100000"), sqrt_p, liq, fee, 6, 6)
        assert value == expected

    def test_cngn_is_token1_uses_swap_t1_for_t0(self, seeded_pool_cache):
        from engine.core.arbitrage.pool_state import get_cached_pool_state, swap_token1_for_token0
        bsc_key = seeded_pool_cache["uni-bsc"]
        sqrt_p, liq, _, fee = get_cached_pool_state(bsc_key)

        value = dex_holdings_value(
            cngn_amount=Decimal("100000"),
            sqrt_p=sqrt_p, liquidity=liq, fee=fee,
            token0_decimals=18, token1_decimals=6,
            cngn_is_token0=False,
        )
        expected = swap_token1_for_token0(Decimal("100000"), sqrt_p, liq, fee, 18, 6)
        assert value == expected


class TestPortfolioValue:
    def test_empty_balances_returns_zeros(self, seeded_pool_cache):
        result = portfolio_value(_DEPTH, [])
        assert result["quidax_cngn_usd"] == 0.0
        assert result["uni_bsc_cngn_usd"] == 0.0
        assert result["uni_base_cngn_usd"] == 0.0

    def test_quidax_cngn_valued(self, seeded_pool_cache):
        balances = [_make_balance("quidax-exchange", cngn=100000, usdt=500)]
        result = portfolio_value(_DEPTH, balances)
        assert result["quidax_cngn_usd"] > 0
        assert result["quidax_usdt"] == 500.0

    def test_bsc_cngn_valued(self, seeded_pool_cache):
        balances = [_make_balance("uni-bsc-trade", cngn=100000, usdt=100)]
        result = portfolio_value(_DEPTH, balances)
        assert result["uni_bsc_cngn_usd"] > 0

    def test_base_cngn_valued(self, seeded_pool_cache):
        balances = [_make_balance("uni-base-trade", cngn=100000, usdc=100)]
        result = portfolio_value(_DEPTH, balances)
        assert result["uni_base_cngn_usd"] > 0

    def test_missing_pool_state_graceful(self, monkeypatch):
        """When pool state is missing, DEX cNGN value is 0 (no crash)."""
        from engine.core.arbitrage import pool_state as _ps
        monkeypatch.setattr(_ps, "_POOL_CACHE", {})
        balances = [_make_balance("uni-base-trade", cngn=100000)]
        result = portfolio_value(_DEPTH, balances)
        assert result["uni_base_cngn_usd"] == 0.0

    def test_all_cngn_delta_ratio_near_one(self, seeded_pool_cache):
        """If all holdings are cNGN, cNGN USD values dominate stablecoin values."""
        # Large cNGN, tiny stablecoins
        balances = [
            _make_balance("quidax-exchange", cngn=1_000_000, usdt=1),
            _make_balance("uni-base-trade", cngn=1_000_000, usdc=1),
            _make_balance("uni-bsc-trade", cngn=1_000_000, usdt=1),
        ]
        result = portfolio_value(_DEPTH, balances)
        total_cngn_usd = result["quidax_cngn_usd"] + result["uni_bsc_cngn_usd"] + result["uni_base_cngn_usd"]
        total_stable = result["quidax_usdt"] + result["uni_bsc_usdt"] + result["uni_base_usdc"]
        if total_cngn_usd + total_stable > 0:
            delta_ratio = total_cngn_usd / (total_cngn_usd + total_stable)
            assert delta_ratio > 0.9  # overwhelmingly cNGN
