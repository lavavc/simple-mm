"""Order reconciliation must distinguish absent orders from unavailable APIs."""

from unittest.mock import AsyncMock

import httpx
import pytest

from engine.venues.cex.quidax_client import QuidaxApiClient


def make_api(client):
    return QuidaxApiClient(
        market="usdtcngn",
        base_url="https://openapi.example/api/v1",
        order_api_base_url="https://private.example/api/v1",
        order_user_id="lp-user",
        client_getter=AsyncMock(return_value=client),
    )


@pytest.mark.asyncio
async def test_confirmed_missing_order_does_not_fall_through_to_unauthorized_host():
    calls = []

    def respond(request):
        calls.append(request.url.host)
        if request.url.host == "private.example":
            return httpx.Response(401, json={"message": "Invalid secret key"})
        return httpx.Response(404, json={"status": "error", "data": {"code": "110043"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await make_api(client).fetch_order_by_id("old-order") == ("missing", None)
    assert calls == ["openapi.example"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [
    (401, {"message": "Invalid secret key"}),
    (404, {"data": {"code": "SUBACCOUNT_NOT_FOUND"}}),
    (404, {"message": "Not found"}),
    (500, {"data": {"code": "110043"}}),
    (200, {"status": "error", "data": []}),
])
async def test_ambiguous_failures_do_not_clear_tracked_orders(status, payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload),
    )) as client:
        assert await make_api(client).fetch_order_by_id("old-order") == ("unknown", None)


@pytest.mark.asyncio
async def test_live_order_on_fallback_is_preserved():
    order = {"id": "live-order", "state": "wait"}

    def respond(request):
        if request.url.host == "openapi.example":
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "success", "data": order})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await make_api(client).fetch_order_by_id("live-order") == ("found", order)


@pytest.mark.asyncio
async def test_authenticated_order_list_uses_documented_host_first():
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"status": "success", "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = make_api(client)
        assert (await api.fetch_orders_payload({"state": "wait"}))["data"] == []
        assert api.order_collection_endpoints()[0].startswith("https://openapi.example/")
    assert calls == ["openapi.example"]


@pytest.mark.asyncio
async def test_wallet_inventory_separates_available_from_total():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": {"balance": "10", "locked": "90"}}),
    )) as client:
        api = make_api(client)
        wallet = await api.get_wallet_inventory("usdt")
        assert wallet.available == 10
        assert wallet.total == 100
        assert await api.get_wallet_balance("usdt") == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {"balance": "10"},
    {"balance": "NaN", "locked": "0"},
    {"balance": "10", "locked": "-1"},
])
async def test_incomplete_or_invalid_wallet_inventory_fails(data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": data}),
    )) as client:
        with pytest.raises((KeyError, ValueError)):
            await make_api(client).get_wallet_inventory("usdt")
