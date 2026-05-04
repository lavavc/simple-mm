"""Valuation invariants: token order, wrapper identity, graceful pool-state degradation.

The non-obvious invariant is that cNGN is token0 on Base but token1 on BSC — swapping
in the wrong direction inverts the price. These tests confirm the adapters hardcode
the correct order regardless of RPC-derived state, and that the valuation wrapper
delegates faithfully to the underlying swap math.
"""

from decimal import Decimal
from types import SimpleNamespace

from engine.arb.valuation import portfolio_value, cex_holdings_value, dex_holdings_value
from engine.types import OrderBookDepth, OrderBookLevel
from engine.arb.detection.cex_dex import QUIDAX_FEE
from engine.venues.dex.uniswap_base import UniswapBaseV4Adapter
from engine.venues.dex.uniswap_bsc import UniswapBscV4Adapter


def _level(price: float, amount: float) -> OrderBookLevel:
    return OrderBookLevel(price=Decimal(str(price)), amount=Decimal(str(amount)))


def _make_depth(bid_price: float, ask_price: float, amount: float = 10000.0) -> OrderBookDepth:
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


_DEPTH = _make_depth(bid_price=1650, ask_price=1640)


class TestCexHoldingsValue:
    def test_output_matches_walk_orderbook_asks(self):
        """cex_holdings_value is a thin wrapper — output must match walk_orderbook_asks directly.

        If this fails, the two code paths have diverged and portfolio valuation
        will show different figures than what the arb detector uses.
        """
        from engine.arb.detection.cex_dex import walk_orderbook_asks
        asks = [_level(1640, 1000)]
        expected, _ = walk_orderbook_asks(asks, Decimal("50000"), QUIDAX_FEE)
        actual = cex_holdings_value(asks, Decimal("50000"), QUIDAX_FEE)
        assert actual == expected


class TestDexHoldingsValue:
    def test_cngn_is_token0_uses_swap_t0_for_t1(self, seeded_pool_cache):
        """Base pool: cNGN is token0, so valuing cNGN → USDC must call swap_token0_for_token1."""
        from engine.market.pool_state import get_cached_pool_state, swap_token0_for_token1
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
        """BSC pool: cNGN is token1, so valuing cNGN → USDT must call swap_token1_for_token0."""
        from engine.market.pool_state import get_cached_pool_state, swap_token1_for_token0
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

    def test_uni_base_rpc_override_preserves_cngn_token_order(self, test_private_key):
        """cngn_is_token0=True must be hardcoded on Base, not derived from RPC state."""
        adapter = UniswapBaseV4Adapter(
            lp_private_key=test_private_key,
            trade_private_key=test_private_key,
            rpc_url="https://example.invalid",
        )
        assert adapter.config.cngn_is_token0 is True

    def test_uni_bsc_rpc_override_preserves_cngn_token_order(self, test_private_key):
        """cngn_is_token0=False must be hardcoded on BSC — using True would invert the price."""
        adapter = UniswapBscV4Adapter(
            lp_private_key=test_private_key,
            trade_private_key=test_private_key,
            rpc_url="https://example.invalid",
        )
        assert adapter.config.cngn_is_token0 is False


class TestPortfolioValue:
    def test_missing_pool_state_graceful(self, monkeypatch):
        """When pool state is missing (cache cold), DEX cNGN value must be 0, not an exception.

        This covers the startup window before the pool cache is seeded.
        """
        from engine.market import pool_state as _ps
        monkeypatch.setattr(_ps, "_POOL_CACHE", {})
        balances = [_make_balance("uni-base-trade", cngn=100000)]
        result = portfolio_value(_DEPTH, balances)
        assert result["uni_base_cngn_usd"] == 0.0
