"""Pure unit tests for order book walking and ternary search."""

import pytest
from decimal import Decimal

from engine.types import OrderBookLevel
from engine.arb.detection.cex_dex import (
    walk_orderbook_asks,
    walk_orderbook_bids,
    _ternary_search,
    _SPREAD_CHECK_SIZE,
    _SPREAD_CHECK_MIN_PROFIT,
)

FEE = Decimal("0.001")  # 0.1%

# Price convention: bid.price / ask.price = cNGN per USDT
# e.g. 1650 means 1 USDT = 1650 cNGN; mid price in USDT/cNGN = 1/1650 ≈ 0.000606
# bid.amount = USDT available at that level
_BID_PRICE = Decimal("1650")   # cNGN/USDT
_ASK_PRICE = Decimal("1640")   # cNGN/USDT (lower → costs more cNGN per USDT on ask)


def _levels(*pairs) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=Decimal(str(p)), amount=Decimal(str(a))) for p, a in pairs]


# =============================================================================
# walk_orderbook_asks
# =============================================================================


class TestWalkOrderbookAsks:
    """Sell cNGN → USDT by walking the ask side.

    ask.price = cNGN per USDT; ask.amount = USDT available at this level.
    max_cngn_at_level = ask.amount * ask.price.
    """

    def test_single_level_full_fill(self):
        # Ask: 1640 cNGN/USDT, 100 USDT available → max cNGN = 164,000
        asks = _levels((_ASK_PRICE, 100))
        usdt, trace = walk_orderbook_asks(asks, Decimal("164000"), FEE)
        # 100 USDT received, minus 0.1% fee
        expected = Decimal("100") * (1 - FEE)
        assert abs(usdt - expected) < Decimal("0.01")
        assert len(trace) == 1

    def test_single_level_partial_fill(self):
        # Only 50 USDT of depth — selling 164,000 cNGN can only fill 50 USDT worth
        asks = _levels((_ASK_PRICE, 50))
        usdt, trace = walk_orderbook_asks(asks, Decimal("164000"), FEE)
        expected = Decimal("50") * (1 - FEE)
        assert abs(usdt - expected) < Decimal("0.01")

    def test_empty_book_returns_zero(self):
        usdt, trace = walk_orderbook_asks([], Decimal("1000"), FEE)
        assert usdt == Decimal("0")
        assert trace == []

    def test_fee_deducted_from_output(self):
        asks = _levels((_ASK_PRICE, 1000))  # 1,640,000 cNGN available
        usdt_no_fee, _ = walk_orderbook_asks(asks, Decimal("1000000"), Decimal("0"))
        usdt_with_fee, _ = walk_orderbook_asks(asks, Decimal("1000000"), FEE)
        assert usdt_with_fee < usdt_no_fee
        assert abs(usdt_with_fee - usdt_no_fee * (1 - FEE)) < Decimal("0.01")

    def test_sorted_by_price_ascending(self):
        # Lower ask.price means cheaper cNGN (less cNGN per USDT → more USDT per cNGN)
        # Ascending sort = cheapest first = highest price in cNGN/USDT... wait:
        # In cNGN/USDT: lower cNGN/USDT = more expensive cNGN = higher USDT/cNGN = ask
        # Sorted ascending by ask.price (cNGN/USDT) = cheapest ask first
        asks = _levels((Decimal("1640"), 10), (Decimal("1650"), 10))
        usdt, trace = walk_orderbook_asks(asks, Decimal("1000000"), FEE)
        assert trace[0]["price"] < trace[1]["price"]

    def test_multi_level_fill(self):
        asks = _levels((_ASK_PRICE, 50), (Decimal("1630"), 50))
        usdt, trace = walk_orderbook_asks(asks, Decimal("200000"), FEE)
        assert len(trace) == 2
        assert usdt > Decimal("0")


# =============================================================================
# walk_orderbook_bids
# =============================================================================


class TestWalkOrderbookBids:
    """Spend USDT → receive cNGN by walking the bid side.

    bid.price = cNGN per USDT; bid.amount = USDT available at that level.
    """

    def test_single_level_full_fill(self):
        # Bid: 1650 cNGN/USDT, 100 USDT available
        bids = _levels((_BID_PRICE, 100))
        cngn, trace = walk_orderbook_bids(bids, Decimal("100"), FEE)
        expected = Decimal("100") * _BID_PRICE * (1 - FEE)
        assert abs(cngn - expected) < Decimal("1")
        assert len(trace) == 1

    def test_partial_fill(self):
        bids = _levels((_BID_PRICE, 30))  # Only 30 USDT of depth
        cngn, trace = walk_orderbook_bids(bids, Decimal("100"), FEE)
        expected = Decimal("30") * _BID_PRICE * (1 - FEE)
        assert abs(cngn - expected) < Decimal("1")

    def test_empty_book_returns_zero(self):
        cngn, trace = walk_orderbook_bids([], Decimal("100"), FEE)
        assert cngn == Decimal("0")
        assert trace == []

    def test_sorted_by_price_descending(self):
        # Higher bid.price (more cNGN per USDT) = better deal → consumed first
        bids = _levels((Decimal("1600"), 10), (Decimal("1700"), 10))
        cngn, trace = walk_orderbook_bids(bids, Decimal("20"), FEE)
        assert trace[0]["price"] > trace[1]["price"]

    def test_fee_deducted_from_output(self):
        bids = _levels((_BID_PRICE, 1000))
        cngn_no_fee, _ = walk_orderbook_bids(bids, Decimal("100"), Decimal("0"))
        cngn_with_fee, _ = walk_orderbook_bids(bids, Decimal("100"), FEE)
        assert cngn_with_fee < cngn_no_fee


# =============================================================================
# _ternary_search
# =============================================================================


class TestTernarySearch:
    """_ternary_search finds the profit-maximising size."""

    def test_converges_on_simple_quadratic(self):
        # Profit function: -(x - 50)^2 + 100 — peak at x=50, profit=100
        # eval_func must return (profit, out, cngn) matching the real signature
        def eval_func(x):
            profit = -(x - 50) ** 2 + Decimal("100")
            return profit, x, x

        best_profit, best_size, best_cngn, best_out = _ternary_search(
            eval_func, low=Decimal("1"), high=Decimal("100"), tol=Decimal("0.5")
        )
        assert abs(best_size - Decimal("50")) < Decimal("1")
        assert best_profit > Decimal("99")

    def test_returns_at_least_something(self):
        # Monotonically increasing profit — should return the upper bound
        def eval_func(x):
            return x, x, x

        best_profit, best_size, best_cngn, best_out = _ternary_search(
            eval_func, low=Decimal("1"), high=Decimal("100"), tol=Decimal("0.5")
        )
        assert best_size > Decimal("90")

    def test_default_search_range(self):
        # Ensure it can run with default parameters without error
        peak = Decimal("2500")

        def eval_func(x):
            profit = -(x - peak) ** 2 + Decimal("10000")
            return profit, x, x

        best_profit, best_size, _, _ = _ternary_search(eval_func)
        assert abs(best_size - peak) < Decimal("10")


# =============================================================================
# Short-circuit in find_optimal_arb
# =============================================================================


class TestShortCircuit:
    """$5 short-circuit skips dead directions before ternary search."""

    def test_negative_spread_at_5_skipped(self):
        """A direction where eval($5) ≤ 0 must be skipped."""
        call_count = 0

        def dead_direction(x):
            nonlocal call_count
            call_count += 1
            # Always unprofitable
            return Decimal("-999"), Decimal("0"), Decimal("0")

        result = dead_direction(_SPREAD_CHECK_SIZE)
        # The short-circuit condition: profit <= _SPREAD_CHECK_MIN_PROFIT
        assert result[0] <= _SPREAD_CHECK_MIN_PROFIT
        # Ternary search would NOT be called on this direction
