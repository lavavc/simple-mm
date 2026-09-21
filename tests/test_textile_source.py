"""Textile v2 RFQ-preview price/liquidity math, without hitting the network."""

import asyncio
from decimal import Decimal

from engine.market.venue_prices import TextilePriceSource


def _run(source: TextilePriceSource):
    return asyncio.run(source.fetch_price())


def _ray(x: str) -> str:
    return str(int(Decimal(x) * TextilePriceSource.RAY))


def test_bid_ask_liquidity_from_both_previews(monkeypatch):
    s = TextilePriceSource()
    s._key = "tx_test_dummy"

    async def fake_preview(client, sell_token, buy_token, sell_amount):
        if sell_token == s._cngn:  # sell cNGN -> USDT: rateRay USD/cNGN, depth in cNGN (6dp)
            return {"rateRay": _ray("0.000715"), "availableSellAmount": str(100_000_000 * 10**6)}
        # sell USDT -> cNGN: rateRay is also USD/cNGN (no inversion in v2), depth in USDT (18dp)
        return {"rateRay": _ray("0.000720"), "availableSellAmount": str(20_000 * 10**18)}

    monkeypatch.setattr(s, "_preview", fake_preview)
    q = _run(s)

    assert q is not None
    assert q.bid == Decimal("0.000715")            # sell rate, USD per cNGN
    assert q.ask == Decimal("0.000720")            # buy rate, USD per cNGN (no inversion)
    assert q.mid == (q.bid + q.ask) / Decimal("2")
    # cNGN depth valued at bid + USDT depth: 100M * 0.000715 + 20k
    assert s.liquidity_usd == Decimal("100000000") * Decimal("0.000715") + Decimal("20000")


def test_none_when_a_side_is_empty(monkeypatch):
    s = TextilePriceSource()
    s._key = "tx_test_dummy"

    async def fake_preview(client, sell_token, buy_token, sell_amount):
        if sell_token == s._usdt:
            return None
        return {"rateRay": _ray("0.000715"), "availableSellAmount": str(10**6)}

    monkeypatch.setattr(s, "_preview", fake_preview)
    assert _run(s) is None


def test_none_without_api_key():
    s = TextilePriceSource()
    s._key = ""
    assert _run(s) is None


def test_no_quote_and_zero_rate_are_rejected():
    """_preview must reject a no_quote result and a zero rateRay (would divide/mis-price)."""

    async def preview(payload):
        s = TextilePriceSource()

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        class _Client:
            async def post(self, *a, **k):
                return _Resp()

        return await s._preview(_Client(), s._cngn, s._usdt, "1")

    assert asyncio.run(preview({"data": {"status": "no_quote", "reason": "no_valid_quote"}})) is None
    assert asyncio.run(preview({"data": {"status": "preview", "rateRay": "0"}})) is None
    ok = asyncio.run(preview({"data": {"status": "preview", "rateRay": "123", "availableSellAmount": "1"}}))
    assert ok is not None and ok["rateRay"] == "123"
