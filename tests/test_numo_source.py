"""Numo public order-book price and visible-depth math, without network calls."""

import asyncio
from decimal import Decimal

from engine.market.price_aggregation import FAIR_VALUE_EXCLUDED
from engine.market.venue_prices import NumoPriceSource


def _order(ui_price: str, size: str, desired: str, filled: str = "0", **extra):
    return {
        "desired_amount": desired,
        "filled_amount": filled,
        "limit_price": extra.get("limit_price", "999"),
        "price": extra.get("top_level_price", "999"),
        "spot_contract": {
            "ui_intent": {"price": ui_price, "size": size},
            "engine_order": {"price": extra.get("engine_price", "999")},
        },
    }


def _run_with_payload(monkeypatch, payload):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params):
            assert params == {"symbol": "USDCcNGN-SPOT"}
            return Response()

    monkeypatch.setattr("engine.market.venue_prices.httpx.AsyncClient", Client)
    source = NumoPriceSource()
    return source, asyncio.run(source.fetch_price())


def test_nested_ui_mid_is_normalized_and_top_level_prices_are_ignored(monkeypatch):
    payload = {
        "bids": [
            _order(
                "1372",
                "2",
                "2744",
                top_level_price="0.0001",
                limit_price="0.0001",
            )
        ],
        "asks": [_order("1368", "3", "4104", engine_price="0.5")],
    }

    source, quote = _run_with_payload(monkeypatch, payload)

    assert quote is not None
    assert quote.mid == Decimal("1") / Decimal("1370")
    assert quote.bid == Decimal("1") / Decimal("1372")
    assert quote.ask == Decimal("1") / Decimal("1368")
    assert source.liquidity_usd == Decimal("5")


def test_visible_depth_accounts_for_partial_fills(monkeypatch):
    payload = {
        "bids": [
            _order("1400", "10", "14000", "3500"),  # 75% of $10 remains
            _order("1402", "2", "2804", "2804"),    # fully filled
        ],
        "asks": [_order("1390", "4", "5560", "2780")],  # 50% of $4 remains
    }

    source, quote = _run_with_payload(monkeypatch, payload)

    assert quote is not None
    assert source.liquidity_usd == Decimal("9.5")


def test_empty_side_returns_none(monkeypatch):
    source, quote = _run_with_payload(
        monkeypatch,
        {"bids": [_order("1400", "1", "1400")], "asks": []},
    )
    assert quote is None
    assert source.liquidity_usd is None


def test_invalid_nested_price_returns_none(monkeypatch):
    _, quote = _run_with_payload(
        monkeypatch,
        {
            "bids": [_order("0", "1", "1400")],
            "asks": [_order("1390", "1", "1390")],
        },
    )
    assert quote is None


def test_cached_quote_avoids_second_request(monkeypatch):
    calls = 0
    payload = {
        "bids": [_order("1400", "1", "1400")],
        "asks": [_order("1390", "1", "1390")],
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params):
            nonlocal calls
            calls += 1
            return Response()

    monkeypatch.setattr("engine.market.venue_prices.httpx.AsyncClient", Client)
    source = NumoPriceSource()
    first = asyncio.run(source.fetch_price())
    second = asyncio.run(source.fetch_price())

    assert first is second
    assert calls == 1


def test_numo_is_display_only():
    assert "numo" in FAIR_VALUE_EXCLUDED
