import json
from decimal import Decimal
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock, call

import pytest

from engine.types import CexParams
from engine.venues.cex.quidax import QuidaxAdapter
from engine.venues.cex.quidax_orders import normalize_order_summary, order_market_matches


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


class _FakeSystemStateStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get_system_state(self, key: str) -> str | None:
        return self.values.get(key)

    async def set_system_state(self, key: str, value: object) -> None:
        self.values[key] = value if isinstance(value, str) else json.dumps(value)


def _make_adapter(
    params: CexParams | None = None,
    *,
    system_state_store: _FakeSystemStateStore | None = None,
    alert_store: object | None = None,
    broadcast: object | None = None,
) -> QuidaxAdapter:
    return QuidaxAdapter(
        api_key="test-key",
        params=params,
        alert_store=alert_store or SimpleNamespace(insert_alert=AsyncMock()),
        system_state_store=system_state_store,
        broadcast=broadcast,
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
            spread_offset_ngn=3,
            ladder_step_ngn=2,
            ladder_levels_per_side=2,
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
            spread_offset_ngn=3,
            ladder_step_ngn=1,
            ladder_levels_per_side=1,
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
            spread_offset_ngn=3,
            ladder_step_ngn=1,
            ladder_levels_per_side=1,
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
async def test_sync_order_ladder_broadcasts_snapshot_before_refusing_stacking():
    broadcasts: list[dict[str, object]] = []
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            spread_offset_ngn=50,
            ladder_step_ngn=1,
            ladder_levels_per_side=1,
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        ),
        broadcast=broadcasts.append,
    )
    adapter.get_open_orders = AsyncMock(
        side_effect=[
            [
                {"id": "old-1", "side": "buy", "price": "1300.00", "volume": "1.53"},
                {"id": "old-2", "side": "sell", "price": "1500.00", "volume": "1.42"},
            ],
            [
                {"id": "old-1", "side": "buy", "price": "1300.00", "volume": "1.53"},
            ],
        ]
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.place_order = AsyncMock()

    with pytest.raises(RuntimeError, match="prior open orders remain: 1 still open"):
        await adapter.sync_order_ladder(Decimal("1400"))

    assert broadcasts and broadcasts[-1]["type"] == "quidax_open_orders"
    payload = broadcasts[-1]["data"]
    assert payload["count"] == 1
    assert all("id" not in order for order in payload["orders"])


@pytest.mark.asyncio
async def test_sync_order_ladder_refuses_to_stack_when_open_orders_remain():
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            spread_offset_ngn=50,
            ladder_step_ngn=1,
            ladder_levels_per_side=1,
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
    broadcasts: list[dict[str, object]] = []
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            spread_offset_ngn=50,
            ladder_step_ngn=1,
            ladder_levels_per_side=1,
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
    adapter._broadcast = broadcasts.append

    await adapter.sync_order_ladder(Decimal("1395.56"))

    adapter.cancel_all_orders.assert_not_awaited()
    adapter.place_order.assert_not_awaited()
    assert broadcasts and broadcasts[-1]["type"] == "quidax_open_orders"
    payload = broadcasts[-1]["data"]
    assert payload["count"] == 2
    assert all("id" not in order for order in payload["orders"])


@pytest.mark.asyncio
async def test_sync_order_ladder_skips_during_requote_cooldown():
    broadcasts: list[dict[str, object]] = []
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            spread_offset_ngn=50,
            ladder_step_ngn=1,
            ladder_levels_per_side=1,
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
    adapter._broadcast = broadcasts.append

    await adapter.sync_order_ladder(Decimal("1395.56"))

    adapter.cancel_all_orders.assert_not_awaited()
    adapter.place_order.assert_not_awaited()
    assert broadcasts and broadcasts[-1]["type"] == "quidax_open_orders"
    payload = broadcasts[-1]["data"]
    assert payload["count"] == 2
    assert all("id" not in order for order in payload["orders"])


@pytest.mark.asyncio
async def test_sync_order_ladder_broadcasts_warning_when_anchor_move_requotes():
    alerts = SimpleNamespace(insert_alert=AsyncMock(return_value=1))
    broadcasts: list[dict[str, object]] = []
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            spread_offset_ngn=50,
            ladder_step_ngn=1,
            ladder_levels_per_side=1,
            anchor_requote_threshold_bps=10,
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        ),
        alert_store=alerts,
        broadcast=broadcasts.append,
    )
    adapter.get_open_orders = AsyncMock(
        side_effect=[
            [
                {"id": "a", "side": "buy", "price": "1300.00", "volume": "1.53"},
                {"id": "b", "side": "sell", "price": "1500.00", "volume": "1.42"},
            ],
            [],
            [
                {
                    "id": "new-buy",
                    "market": {"id": "usdtcngn"},
                    "side": "buy",
                    "status": "wait",
                    "price": "1345.00",
                    "volume": {"amount": "1.48"},
                    "remaining_volume": {"amount": "1.48"},
                    "executed_volume": {"amount": "0"},
                    "created_at": 1712521000000,
                },
                {
                    "id": "new-sell",
                    "market": {"id": "usdtcngn"},
                    "side": "sell",
                    "status": "wait",
                    "price": "1445.00",
                    "volume": {"amount": "1.42"},
                    "remaining_volume": {"amount": "1.42"},
                    "executed_volume": {"amount": "0"},
                    "created_at": 1712521001000,
                },
            ],
        ]
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.place_order = AsyncMock()

    await adapter.sync_order_ladder(Decimal("1395.56"))

    alerts.insert_alert.assert_awaited_once()
    alert_kwargs = alerts.insert_alert.await_args.kwargs
    assert alert_kwargs["severity"] == "warning"
    assert alert_kwargs["category"] == "cex"
    assert "anchor moved" in alert_kwargs["message"]
    assert "1395.56" in alert_kwargs["message"]
    assert broadcasts[0] == {
        "type": "quidax_open_orders",
        "data": {"venue": "quidax", "market": "usdtcngn", "count": 0, "orders": []},
    }
    assert broadcasts[1]["type"] == "quidax_open_orders"
    assert broadcasts[1]["data"]["count"] == 2
    assert all("id" not in order for order in broadcasts[1]["data"]["orders"])
    assert broadcasts[2] == {
        "type": "alert",
        "severity": "warning",
        "message": alert_kwargs["message"],
        "dedupe_key": alert_kwargs["dedupe_key"],
        "cooldown_s": 30,
    }


@pytest.mark.asyncio
async def test_sync_order_ladder_broadcasts_empty_snapshot_before_requote_failure():
    broadcasts: list[dict[str, object]] = []
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            spread_offset_ngn=50,
            ladder_step_ngn=1,
            ladder_levels_per_side=1,
            anchor_requote_threshold_bps=10,
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        ),
        broadcast=broadcasts.append,
    )
    adapter.get_open_orders = AsyncMock(
        side_effect=[
            [
                {"id": "a", "side": "buy", "price": "1300.00", "volume": "1.53"},
                {"id": "b", "side": "sell", "price": "1500.00", "volume": "1.42"},
            ],
            [],
        ]
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.place_order = AsyncMock(side_effect=ValueError("rejected"))

    with pytest.raises(RuntimeError, match="buy@1350.00:rejected; sell@1450.00:rejected"):
        await adapter.sync_order_ladder(Decimal("1400"))

    assert broadcasts and broadcasts[0]["type"] == "quidax_open_orders"
    payload = broadcasts[0]["data"]
    assert payload["count"] == 0
    assert payload["orders"] == []


@pytest.mark.asyncio
async def test_sync_order_ladder_broadcasts_sanitized_open_orders_snapshot():
    broadcasts: list[dict[str, object]] = []
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            spread_offset_ngn=3,
            ladder_step_ngn=2,
            ladder_levels_per_side=1,
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        ),
        broadcast=broadcasts.append,
    )
    adapter.get_open_orders = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "id": "buy-1",
                    "market": {"id": "usdtcngn"},
                    "side": "buy",
                    "status": "wait",
                    "price": "1397.00",
                    "volume": {"amount": "1.43"},
                    "remaining_volume": {"amount": "1.43"},
                    "executed_volume": {"amount": "0"},
                    "created_at": 1712520000000,
                },
                {
                    "id": "sell-1",
                    "market": {"id": "usdtcngn"},
                    "side": "sell",
                    "status": "wait",
                    "price": "1403.00",
                    "volume": {"amount": "1.42"},
                    "remaining_volume": {"amount": "1.42"},
                    "executed_volume": {"amount": "0"},
                    "created_at": 1712520001000,
                },
            ],
        ]
    )
    adapter.cancel_all_orders = AsyncMock()
    adapter.place_order = AsyncMock()

    await adapter.sync_order_ladder(Decimal("1400"))

    assert broadcasts and broadcasts[-1]["type"] == "quidax_open_orders"
    payload = broadcasts[-1]["data"]
    assert payload["venue"] == "quidax"
    assert payload["count"] == 2
    assert all("id" not in order for order in payload["orders"])
    assert [order["side"] for order in payload["orders"]] == ["sell", "buy"]


@pytest.mark.asyncio
async def test_sync_order_ladder_persists_last_ladder_anchor_price():
    state_store = _FakeSystemStateStore()
    adapter = _make_adapter(
        CexParams(
            ladder_enabled=True,
            spread_offset_ngn=3,
            ladder_step_ngn=2,
            ladder_levels_per_side=1,
            order_size_cngn=Decimal("2000"),
            order_size_usdt=Decimal("10"),
        ),
        system_state_store=state_store,
    )
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.cancel_all_orders = AsyncMock()
    adapter.place_order = AsyncMock()

    await adapter.sync_order_ladder(Decimal("1400"))

    persisted = json.loads(state_store.values["quidax:last_ladder_anchor_price_ngn"])
    assert persisted["reference_price_ngn"] == "1400"
    assert int(persisted["updated_at_ms"]) > 0


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


@pytest.mark.asyncio
async def test_place_order_tracks_created_order_id_in_system_state():
    state_store = _FakeSystemStateStore()
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": {"id": "ord-1"}})
    adapter._get_client = AsyncMock(return_value=fake_client)

    await adapter.place_order("buy", Decimal("1397.98"), Decimal("1.43"))

    tracked = await adapter._get_tracked_open_order_rows()

    assert [order["id"] for order in tracked] == ["ord-1"]
    assert state_store.values["quidax:tracked_open_orders"]


@pytest.mark.asyncio
async def test_get_open_orders_uses_tracked_orders_when_api_returns_empty():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "buy",
                "status": "wait",
                "price": "1345.10",
                "volume": "1.48",
                "remaining_volume": "1.48",
                "executed_volume": "0",
                "created_at": 1712520000000,
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    adapter._get_client = AsyncMock(return_value=_FakeClient({"status": "success", "data": []}))

    orders = await adapter.get_open_orders()

    assert [order["id"] for order in orders] == ["ord-1"]


@pytest.mark.asyncio
async def test_get_open_orders_reconciles_terminal_rows_and_clears_tracked_orders():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "buy",
                "status": "wait",
                "price": "1345.10",
                "volume": "1.48",
                "remaining_volume": "1.48",
                "executed_volume": "0",
                "created_at": 1712520000000,
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": []})
    fake_client.get_payloads = [
        {"status": "success", "data": []},
        {"status": "success", "data": [{"id": "ord-1", "market": {"id": "usdtcngn"}, "status": "cancel"}]},
        {"status": "success", "data": []},
    ]
    adapter._get_client = AsyncMock(return_value=fake_client)

    orders = await adapter.get_open_orders()
    remaining = await adapter._get_tracked_open_order_rows()

    assert orders == []
    assert remaining == []


@pytest.mark.asyncio
async def test_cancel_all_orders_prunes_missing_tracked_orders_before_counting_cancels():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "sell",
                "status": "wait",
                "price": "1445.11",
                "volume": "1.43",
                "remaining_volume": "1.43",
                "executed_volume": "0",
                "created_at": 1712520000000,
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": []})

    async def _post(url: str, *_args, **_kwargs) -> _FakeResponse:
        if url.endswith("/cancel"):
            return _FakeResponse({"status": "success", "data": {"status": "cancel"}})
        return _FakeResponse({"status": "success", "data": {"id": "ord-1"}})

    async def _get(url: str, *_args, **kwargs) -> _FakeResponse:
        fake_client.get_calls.append(kwargs.get("params"))
        if "/orders/ord-1" in url:
            return _FakeResponse({}, status_code=404)
        return _FakeResponse({"status": "success", "data": []})

    fake_client.post = _post  # type: ignore[method-assign]
    fake_client.get = _get  # type: ignore[method-assign]
    adapter._get_client = AsyncMock(return_value=fake_client)

    cancelled = await adapter.cancel_all_orders()
    remaining = await adapter._get_tracked_open_order_rows()

    assert cancelled == 0
    assert remaining == []


@pytest.mark.asyncio
async def test_cancel_all_orders_keeps_terminally_acknowledged_orders_tracked_until_settled():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "sell",
                "status": "wait",
                "price": "1445.11",
                "volume": "1.43",
                "remaining_volume": "1.43",
                "executed_volume": "0",
                "created_at": 1712520000000,
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": []})

    async def _post(url: str, *_args, **_kwargs) -> _FakeResponse:
        if url.endswith("/cancel"):
            return _FakeResponse({"status": "success", "data": {"status": "cancel"}})
        return _FakeResponse({"status": "success", "data": {"id": "ord-1"}})

    async def _get(url: str, *_args, **kwargs) -> _FakeResponse:
        fake_client.get_calls.append(kwargs.get("params"))
        if "/orders/ord-1" in url:
            return _FakeResponse({"status": "success", "data": []})
        return _FakeResponse({"status": "success", "data": []})

    fake_client.post = _post  # type: ignore[method-assign]
    fake_client.get = _get  # type: ignore[method-assign]
    adapter._get_client = AsyncMock(return_value=fake_client)

    cancelled = await adapter.cancel_all_orders()
    remaining = await adapter._get_tracked_open_order_rows()

    assert cancelled == 0
    assert [order["id"] for order in remaining] == ["ord-1"]
    assert remaining[0]["status"] == "pending_cancel"


@pytest.mark.asyncio
async def test_cancel_all_orders_keeps_tracked_orders_when_cancel_is_pending():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "sell",
                "status": "wait",
                "price": "1445.11",
                "volume": "1.43",
                "remaining_volume": "1.43",
                "executed_volume": "0",
                "created_at": 1712520000000,
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": []})
    fake_client.get_payloads = [{"status": "success", "data": []}] * 4

    async def _post(url: str, *_args, **_kwargs) -> _FakeResponse:
        if url.endswith("/cancel"):
            return _FakeResponse({"status": "success", "data": {"status": "pending_cancel"}})
        return _FakeResponse({"status": "success", "data": {"id": "ord-1"}})

    fake_client.post = _post  # type: ignore[method-assign]
    adapter._get_client = AsyncMock(return_value=fake_client)

    cancelled = await adapter.cancel_all_orders()
    remaining = await adapter._get_tracked_open_order_rows()

    assert cancelled == 0
    assert [order["id"] for order in remaining] == ["ord-1"]


@pytest.mark.asyncio
async def test_get_open_orders_clears_pending_cancel_tracked_orders_when_order_is_missing():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "sell",
                "status": "pending_cancel",
                "price": "1445.11",
                "volume": "1.43",
                "remaining_volume": "1.43",
                "executed_volume": "0",
                "created_at": 1712520000000,
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": []})

    async def _get(url: str, *_args, **kwargs) -> _FakeResponse:
        fake_client.get_calls.append(kwargs.get("params"))
        if "/orders/ord-1" in url:
            return _FakeResponse({}, status_code=404)
        return _FakeResponse({"status": "success", "data": []})

    fake_client.get = _get  # type: ignore[method-assign]
    adapter._get_client = AsyncMock(return_value=fake_client)

    orders = await adapter.get_open_orders()
    remaining = await adapter._get_tracked_open_order_rows()

    assert orders == []
    assert remaining == []


@pytest.mark.asyncio
async def test_get_open_orders_marks_wait_tracked_orders_missing_once_before_pruning():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "sell",
                "status": "wait",
                "price": "1445.11",
                "volume": "1.43",
                "remaining_volume": "1.43",
                "executed_volume": "0",
                "created_at": 1712520000000,
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": []})

    async def _get(url: str, *_args, **kwargs) -> _FakeResponse:
        fake_client.get_calls.append(kwargs.get("params"))
        if "/orders/ord-1" in url:
            return _FakeResponse({}, status_code=404)
        return _FakeResponse({"status": "success", "data": []})

    fake_client.get = _get  # type: ignore[method-assign]
    adapter._get_client = AsyncMock(return_value=fake_client)

    first_orders = await adapter.get_open_orders()
    first_state = json.loads(state_store.values["quidax:tracked_open_orders"])
    second_orders = await adapter.get_open_orders()
    second_remaining = await adapter._get_tracked_open_order_rows()

    assert [order["id"] for order in first_orders] == ["ord-1"]
    assert [order["id"] for order in first_state] == ["ord-1"]
    assert first_state[0]["_missing_lookup_seen_once"] is True
    assert second_orders == []
    assert second_remaining == []


@pytest.mark.asyncio
async def test_get_open_orders_keeps_wait_tracked_orders_when_missing_timestamp_is_unknown():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "sell",
                "status": "wait",
                "price": "1445.11",
                "volume": "1.43",
                "remaining_volume": "1.43",
                "executed_volume": "0",
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": []})

    async def _get(url: str, *_args, **kwargs) -> _FakeResponse:
        fake_client.get_calls.append(kwargs.get("params"))
        if "/orders/ord-1" in url:
            return _FakeResponse({}, status_code=404)
        return _FakeResponse({"status": "success", "data": []})

    fake_client.get = _get  # type: ignore[method-assign]
    adapter._get_client = AsyncMock(return_value=fake_client)

    orders = await adapter.get_open_orders()
    tracked_state = json.loads(state_store.values["quidax:tracked_open_orders"])

    assert [order["id"] for order in orders] == ["ord-1"]
    assert [order["id"] for order in tracked_state] == ["ord-1"]
    assert tracked_state[0]["_missing_lookup_seen_once"] is True


@pytest.mark.asyncio
async def test_get_open_orders_keeps_pending_cancel_tracked_orders_when_lookup_is_inconclusive():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "sell",
                "status": "pending_cancel",
                "price": "1445.11",
                "volume": "1.43",
                "remaining_volume": "1.43",
                "executed_volume": "0",
                "created_at": 1712520000000,
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": []})
    failed_once = False

    async def _get(url: str, *_args, **kwargs) -> _FakeResponse:
        nonlocal failed_once
        fake_client.get_calls.append(kwargs.get("params"))
        if "/orders/ord-1" in url and "/users/me/" not in url and not failed_once:
            failed_once = True
            return _FakeResponse({}, status_code=404)
        if "/orders/ord-1" in url:
            return _FakeResponse({}, status_code=500)
        return _FakeResponse({"status": "success", "data": []})

    fake_client.get = _get  # type: ignore[method-assign]
    adapter._get_client = AsyncMock(return_value=fake_client)

    orders = await adapter.get_open_orders()
    remaining = await adapter._get_tracked_open_order_rows()

    assert [order["id"] for order in orders] == ["ord-1"]
    assert [order["id"] for order in remaining] == ["ord-1"]


@pytest.mark.asyncio
async def test_cancel_all_orders_does_not_count_already_missing_tracked_orders_as_cancels():
    state_store = _FakeSystemStateStore()
    state_store.values["quidax:tracked_open_orders"] = json.dumps(
        [
            {
                "id": "ord-1",
                "market": "usdtcngn",
                "side": "sell",
                "status": "wait",
                "price": "1445.11",
                "volume": "1.43",
                "remaining_volume": "1.43",
                "executed_volume": "0",
                "created_at": 1712520000000,
            }
        ]
    )
    adapter = _make_adapter(system_state_store=state_store)
    fake_client = _FakeClient({"status": "success", "data": []})

    async def _post(url: str, *_args, **_kwargs) -> _FakeResponse:
        if url.endswith("/cancel"):
            return _FakeResponse({"status": "success", "data": {"status": "pending_cancel"}})
        return _FakeResponse({"status": "success", "data": {"id": "ord-1"}})

    async def _get(url: str, *_args, **kwargs) -> _FakeResponse:
        fake_client.get_calls.append(kwargs.get("params"))
        if "/orders/ord-1" in url:
            return _FakeResponse({}, status_code=404)
        return _FakeResponse({"status": "success", "data": []})

    fake_client.post = _post  # type: ignore[method-assign]
    fake_client.get = _get  # type: ignore[method-assign]
    adapter._get_client = AsyncMock(return_value=fake_client)

    cancelled = await adapter.cancel_all_orders()
    remaining = await adapter._get_tracked_open_order_rows()

    assert cancelled == 0
    assert remaining == []


def test_order_market_matches_normalizes_market_formats():
    market = "usdtcngn"

    assert order_market_matches({"market": "USDT/CNGN"}, market)
    assert order_market_matches({"market": "usdt_cngn"}, market)
    assert order_market_matches({"market": {"id": "USDT/CNGN"}}, market)
    assert order_market_matches({"market": {"base_unit": "USDT", "quote_unit": "CNGN"}}, market)


def test_normalize_order_summary_uses_origin_volume_when_volume_is_zero():
    summary = normalize_order_summary(
        {
            "id": "ord-1",
            "market": {"id": "usdtcngn"},
            "side": "sell",
            "status": "wait",
            "price": {"amount": "100"},
            "volume": {"amount": "0"},
            "origin_volume": {"amount": "2"},
            "executed_volume": {"amount": "0"},
        },
        market="usdtcngn",
    )

    assert summary is not None
    assert summary.volume == Decimal("2")
    assert summary.remaining_volume == Decimal("2")
    assert summary.notional == Decimal("200")


@pytest.mark.asyncio
async def test_get_position_does_not_expose_open_orders():
    adapter = _make_adapter()
    fake_client = _FakeClient({"status": "success", "data": {"balance": "0"}})
    adapter._get_client = AsyncMock(return_value=fake_client)
    adapter.get_open_order_summaries = AsyncMock(side_effect=AssertionError("should not be called"))  # type: ignore[method-assign]

    position = await adapter.get_position()

    assert "open_orders" not in position.model_dump(mode="json")


def test_order_collection_endpoints_include_docs_me_fallback():
    adapter = QuidaxAdapter(
        api_key="test-key",
        order_user_id="11423927",
        alert_store=SimpleNamespace(insert_alert=AsyncMock()),
    )

    assert adapter._api.order_collection_endpoints() == [
        "https://app.quidax.io/api/v1/users/11423927/orders",
        "https://app.quidax.io/api/v1/users/me/orders",
        "https://openapi.quidax.io/exchange-open-api/api/v1/users/11423927/orders",
        "https://openapi.quidax.io/exchange-open-api/api/v1/users/me/orders",
    ]
