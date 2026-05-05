"""Tests for ExecutableFairPrice (Tier 2) and signed swap flow ring buffer."""

import time
from collections import deque
from decimal import Decimal

import pytest

from engine.types import PriceQuote
from engine.market.price_aggregation import NormalizedPrice
from engine.market.pool_state import _SWAP_FLOW_RING, get_swap_flow_imbalance
from engine.market.fair_price import MarketFairPrice, ExecutableFairPrice, ExecutablePriceCalculator


# =============================================================================
# Helpers
# =============================================================================


def _market(price: float) -> MarketFairPrice:
    return MarketFairPrice(
        price=Decimal(str(price)),
        weights={"quidax": Decimal("1.0")},
        confidence=0.7,
        timestamp=int(time.time() * 1000),
    )


def _np(venue: str, price: float, spread_bps: float = 10.0) -> NormalizedPrice:
    mid = Decimal(str(price))
    half = mid * Decimal(str(spread_bps / 10_000 / 2))
    return NormalizedPrice(
        venue=venue,
        cngn_usd=mid,
        raw_quote=PriceQuote(
            source=venue,
            timestamp=int(time.time() * 1000),
            bid=mid - half,
            ask=mid + half,
            mid=mid,
        ),
        basis="cNGN/USDC",
        timestamp=int(time.time() * 1000),
    )


# =============================================================================
# TestGetSwapFlowImbalance
# =============================================================================


class TestGetSwapFlowImbalance:
    def test_empty_ring_returns_none(self):
        """No swaps recorded → returns None."""
        addr = "0xtest_pool_empty"
        # Ensure no entry exists for this address
        _SWAP_FLOW_RING.pop(addr, None)
        result = get_swap_flow_imbalance(addr)
        assert result is None

    def test_all_buys_returns_plus_one(self):
        """3 buy swaps (positive signed USD volume) → imbalance = +1.0."""
        addr = "0xtest_pool_buy"
        _SWAP_FLOW_RING[addr] = deque([(100.0, 1.0), (200.0, 2.0), (50.0, 3.0)], maxlen=20)
        result = get_swap_flow_imbalance(addr)
        assert result == pytest.approx(1.0)

    def test_all_sells_returns_minus_one(self):
        """3 sell swaps (negative signed USD volume) → imbalance = -1.0."""
        addr = "0xtest_pool_sell"
        _SWAP_FLOW_RING[addr] = deque([(-100.0, 1.0), (-200.0, 2.0), (-50.0, 3.0)], maxlen=20)
        result = get_swap_flow_imbalance(addr)
        assert result == pytest.approx(-1.0)

    def test_equal_buy_sell_returns_zero(self):
        """Equal buy/sell USD volume → imbalance = 0.0."""
        addr = "0xtest_pool_balanced"
        _SWAP_FLOW_RING[addr] = deque([(150.0, 1.0), (-150.0, 2.0)], maxlen=20)
        result = get_swap_flow_imbalance(addr)
        assert result == pytest.approx(0.0)


# =============================================================================
# TestExecutablePriceCalculator
# =============================================================================


class TestExecutablePriceCalculator:
    def test_zero_imbalance_returns_market_price(self):
        """Combined imbalance = 0 → executable price ≈ market_price."""
        calc = ExecutablePriceCalculator(pool_addresses={})
        mp = _market(0.000700)
        normalized = {"quidax": _np("quidax", 0.000700, spread_bps=10.0)}
        result = calc.compute(mp, normalized, cex_imbalance=0.0)
        assert float(result.price) == pytest.approx(0.000700, rel=1e-9)

    def test_positive_imbalance_raises_price(self):
        """Combined imbalance > 0 → executable price > market_price."""
        calc = ExecutablePriceCalculator(pool_addresses={})
        mp = _market(0.000700)
        normalized = {"quidax": _np("quidax", 0.000700, spread_bps=10.0)}
        result = calc.compute(mp, normalized, cex_imbalance=0.5)
        assert float(result.price) > 0.000700

    def test_negative_imbalance_lowers_price(self):
        """Combined imbalance < 0 → executable price < market_price."""
        calc = ExecutablePriceCalculator(pool_addresses={})
        mp = _market(0.000700)
        normalized = {"quidax": _np("quidax", 0.000700, spread_bps=10.0)}
        result = calc.compute(mp, normalized, cex_imbalance=-0.5)
        assert float(result.price) < 0.000700

    def test_cex_only_no_dex_data(self):
        """cex_imbalance provided, no DEX pool addresses → uses cex_imbalance alone."""
        calc = ExecutablePriceCalculator(pool_addresses={})
        mp = _market(0.000700)
        normalized = {"quidax": _np("quidax", 0.000700, spread_bps=10.0)}
        # No DEX entries in pool_addresses, so imbalance_dex will be empty
        result = calc.compute(mp, normalized, cex_imbalance=1.0)
        # imbalance_signal should equal the cex_imbalance (clamped)
        assert float(result.imbalance_signal) == pytest.approx(1.0)
        assert result.imbalance_cex == Decimal("1.0")
        assert result.imbalance_dex == {}

    def test_max_shift_bounded(self):
        """imbalance=1.0 → price shift ≤ spread/2 of market_price."""
        spread_bps = 10.0
        calc = ExecutablePriceCalculator(pool_addresses={})
        mp = _market(0.000700)
        normalized = {"quidax": _np("quidax", 0.000700, spread_bps=spread_bps)}
        result = calc.compute(mp, normalized, cex_imbalance=1.0)

        # Maximum shift = half_spread * market_price = (spread/2) * price
        half_spread = (spread_bps / 10_000) / 2
        max_shift = half_spread * 0.000700
        actual_shift = abs(float(result.price) - 0.000700)
        assert actual_shift <= max_shift * 1.0001  # allow tiny float rounding
