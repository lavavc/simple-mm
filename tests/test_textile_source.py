"""Textile REST order-book price/liquidity math, without hitting the network."""

import asyncio
from decimal import Decimal

from engine.market.venue_prices import TextilePriceSource


def _run(source: TextilePriceSource):
    return asyncio.run(source.fetch_price())


def _ray(x: str) -> str:
    return str(int(Decimal(x) * TextilePriceSource.RAY))


def test_bid_ask_liquidity_from_both_books(monkeypatch):
    s = TextilePriceSource()
    s._key = "tx_test_dummy"

    async def fake_book(client, sell_token, buy_token):
        if sell_token == s._cngn:  # sell cNGN -> USDT: bestRate is USDT per cNGN
            return {"bestRateRay": _ray("0.000715"), "availableBuyAmount": str(38_000 * 10**18)}
        # sell USDT -> cNGN: bestRate is cNGN per USDT
        return {"bestRateRay": _ray("1398"), "availableSellAmount": str(27_000 * 10**18)}

    monkeypatch.setattr(s, "_book", fake_book)
    q = _run(s)

    assert q is not None
    assert q.bid == Decimal("0.000715")           # best sell, USD per cNGN
    assert q.ask == s.RAY / Decimal(_ray("1398"))  # best buy inverted
    assert q.mid == (q.bid + q.ask) / Decimal("2")
    # both legs are USDT amounts: 38k receivable + 27k spendable
    assert s.liquidity_usd == Decimal("38000") + Decimal("27000")


def test_none_when_a_side_is_empty(monkeypatch):
    s = TextilePriceSource()
    s._key = "tx_test_dummy"

    async def fake_book(client, sell_token, buy_token):
        if sell_token == s._usdt:
            return None
        return {"bestRateRay": _ray("0.000715"), "availableBuyAmount": str(10**18)}

    monkeypatch.setattr(s, "_book", fake_book)
    assert _run(s) is None


def test_none_without_api_key(monkeypatch):
    s = TextilePriceSource()
    s._key = ""
    assert _run(s) is None


def test_zero_best_rate_is_rejected():
    # a "0" bestRateRay must not slip past the guard (would divide-by-zero on the ask)
    payload = {"data": {"hasLiquidity": True, "bestRateRay": "0", "availableBuyAmount": "1"}}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    async def run():
        s = TextilePriceSource()

        class _Client:
            async def get(self, *a, **k):
                return _Resp()

        return await s._book(_Client(), s._usdt, s._cngn)

    assert asyncio.run(run()) is None
