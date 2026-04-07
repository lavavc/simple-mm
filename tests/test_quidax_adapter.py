from decimal import Decimal
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock, call

import pytest

from engine.api.schemas import CexParams
from engine.venues.cex.quidax import QuidaxAdapter


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.last_json: dict | None = None
        self.get_payloads: list[dict] = [payload]
        self.get_calls: list[dict | None] = []

    async def post(self, *_args, **kwargs) -> _FakeResponse:
        self.last_json = kwargs.get("json")
        return _FakeResponse(self.payload, status_code=self.status_code)

    async def get(self, *_args, **kwargs) -> _FakeResponse:
        self.get_calls.append(kwargs.get("params"))
        payload = self.get_payloads.pop(0) if self.get_payloads else self.payload
        return _FakeResponse(payload, status_code=self.status_code)


def _make_adapter(params: CexParams | None = None) -> QuidaxAdapter:
    return QuidaxAdapter(
        api_key="test-key",
        params=params,
        alert_store=SimpleNamespace(insert_alert=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_place_order_raises_on_quidax_error_payload():
    adapter = _make_adapter()
    adapter._get_client = AsyncMock(return_value=_FakeClient({"status": "error", "message": "bad market"}))

    with pytest.raises(ValueError, match="bad market"):
        await adapter.place_order("sell", Decimal("1403"), Decimal("10"))


@pytest.mark.asyncio
async def test_sync_order_ladder_uses_usdtcngn_order_semantics_and_balances_to_smaller_notional():
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            ladder_offsets_ngn=[3, 5],
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        )
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.place_order = AsyncMock()

    await adapter.sync_order_ladder(Decimal("1400"))

    balanced_usdt_size = Decimal("1.42")
    expected_calls = [
        call("buy", Decimal("1397.00"), Decimal("1.43")),
        call("sell", Decimal("1403"), balanced_usdt_size),
        call("buy", Decimal("1395.00"), Decimal("1.43")),
        call("sell", Decimal("1405"), balanced_usdt_size),
    ]
    assert adapter.place_order.await_args_list == expected_calls


@pytest.mark.asyncio
async def test_sync_order_ladder_caps_cngn_side_when_usdt_side_is_smaller():
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            ladder_offsets_ngn=[3],
            order_size_cngn=Decimal("20000"),
            order_size_usdt=Decimal("2"),
        )
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.place_order = AsyncMock()

    await adapter.sync_order_ladder(Decimal("1400"))

    balanced_cngn_size = Decimal("2") * Decimal("1400")
    expected_calls = [
        call("buy", Decimal("1397.00"), Decimal("2.00")),
        call("sell", Decimal("1403"), Decimal("2")),
    ]
    assert adapter.place_order.await_args_list == expected_calls


@pytest.mark.asyncio
async def test_place_order_rounds_price_and_volume_for_usdtcngn():
    adapter = _make_adapter()
    fake_client = _FakeClient({"status": "success", "data": {"id": "1"}})
    adapter._get_client = AsyncMock(return_value=fake_client)

    response = await adapter.place_order("buy", Decimal("1397.987"), Decimal("1.438"))

    assert response["status"] == "success"
    posted = fake_client.last_json
    assert posted["price"] == "1397.98"
    assert posted["volume"] == "1.43"


@pytest.mark.asyncio
async def test_sync_order_ladder_raises_when_no_orders_are_accepted():
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            ladder_offsets_ngn=[3],
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        )
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.place_order = AsyncMock(side_effect=ValueError("rejected"))

    with pytest.raises(RuntimeError, match="buy@1397.00:rejected; sell@1403.00:rejected"):
        await adapter.sync_order_ladder(Decimal("1400"))


@pytest.mark.asyncio
async def test_sync_order_ladder_refuses_to_stack_when_open_orders_remain():
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            ladder_offsets_ngn=[50],
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        )
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.get_open_orders = AsyncMock(return_value=[{"id": "old-1"}])
    adapter.place_order = AsyncMock()

    with pytest.raises(RuntimeError, match="prior open orders remain: 1 still open"):
        await adapter.sync_order_ladder(Decimal("1400"))

    adapter.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_order_ladder_skips_requote_when_existing_orders_are_within_threshold():
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            ladder_offsets_ngn=[50],
            anchor_requote_threshold_bps=10,
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        )
    )
    adapter.get_open_orders = AsyncMock(
        return_value=[
            {"id": "a", "side": "buy", "price": "1345.60", "volume": "1.48"},
            {"id": "b", "side": "sell", "price": "1445.60", "volume": "1.42"},
        ]
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.place_order = AsyncMock()

    await adapter.sync_order_ladder(Decimal("1395.56"))

    adapter.cancel_all_orders.assert_not_awaited()
    adapter.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_order_ladder_skips_during_requote_cooldown():
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            ladder_offsets_ngn=[50],
            anchor_requote_threshold_bps=1,
            anchor_requote_cooldown_seconds=30,
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        )
    )
    adapter.get_open_orders = AsyncMock(
        return_value=[
            {"id": "a", "side": "buy", "price": "1300.00", "volume": "1.53"},
            {"id": "b", "side": "sell", "price": "1500.00", "volume": "1.42"},
        ]
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.place_order = AsyncMock()
    adapter._last_ladder_requote_at = time.time()

    await adapter.sync_order_ladder(Decimal("1395.56"))

    adapter.cancel_all_orders.assert_not_awaited()
    adapter.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_open_orders_falls_back_and_filters_open_orders_client_side():
    adapter = _make_adapter()
    fake_client = _FakeClient({"status": "success", "data": []})
    fake_client.get_payloads = [
        {"status": "success", "data": []},
        {
            "status": "success",
            "data": [
                {"id": "done-1", "market": {"id": "usdtcngn"}, "status": "done", "volume": {"amount": "0"}},
                {
                    "id": "open-1",
                    "market": {"id": "usdtcngn"},
                    "status": "wait",
                    "volume": {"amount": "1.43"},
                    "executed_volume": {"amount": "0"},
                },
            ],
        },
    ]
    adapter._get_client = AsyncMock(return_value=fake_client)

    orders = await adapter.get_open_orders()

    assert [order["id"] for order in orders] == ["open-1"]
    assert fake_client.get_calls == [
        {"market": "usdtcngn", "state": "wait"},
        {"market": "usdtcngn"},
    ]


def test_order_market_matches_normalizes_market_formats():
    adapter = _make_adapter()

    assert adapter._order_market_matches({"market": "USDT/CNGN"})
    assert adapter._order_market_matches({"market": "usdt_cngn"})
    assert adapter._order_market_matches({"market": {"id": "USDT/CNGN"}})
    assert adapter._order_market_matches({"market": {"base_unit": "USDT", "quote_unit": "CNGN"}})
