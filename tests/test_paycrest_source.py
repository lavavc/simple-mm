"""Paycrest top-of-book selection: bid = best sell (offramp), ask = best buy
(onramp), with dust providers excluded by the balance floor."""
from decimal import Decimal

from engine.market.venue_prices import PaycrestPriceSource


def _row(side: str, rate: str, balance_usd: str) -> dict:
    return {"side": side, "rate": rate, "balanceUsd": balance_usd}


def test_best_bid_ask_picks_best_sell_and_buy():
    book = [
        _row("sell", "1392.0", "5000"),
        _row("sell", "1390.0", "5000"),   # worse sell
        _row("buy", "1406.0", "5000"),
        _row("buy", "1408.0", "5000"),    # worse buy
    ]
    bid, ask = PaycrestPriceSource._best_bid_ask(book, Decimal("100"))
    assert bid == Decimal("1392.0")   # highest sell = best rate to sell into
    assert ask == Decimal("1406.0")   # lowest buy = best rate to buy at
    assert bid < ask                  # positive spread


def test_dust_offer_below_floor_is_ignored():
    book = [
        _row("sell", "9999.0", "5"),     # dust: absurd rate, tiny balance → ignored
        _row("sell", "1392.0", "5000"),
        _row("buy", "1406.0", "5000"),
        _row("buy", "1.0", "10"),        # dust on the buy side → ignored
    ]
    bid, ask = PaycrestPriceSource._best_bid_ask(book, Decimal("100"))
    assert bid == Decimal("1392.0")
    assert ask == Decimal("1406.0")


def test_returns_none_when_a_side_is_empty():
    book = [_row("sell", "1392.0", "5000")]  # no buy side
    assert PaycrestPriceSource._best_bid_ask(book, Decimal("100")) is None


def test_malformed_rows_are_skipped_not_fatal():
    book = [
        {"side": "sell"},                       # missing rate
        _row("sell", "not-a-number", "5000"),   # bad rate
        _row("sell", "1392.0", "5000"),
        _row("buy", "1406.0", "5000"),
    ]
    bid, ask = PaycrestPriceSource._best_bid_ask(book, Decimal("100"))
    assert (bid, ask) == (Decimal("1392.0"), Decimal("1406.0"))
