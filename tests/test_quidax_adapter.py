"""Non-obvious Quidax adapter invariants: ladder math, drift thresholds, safety guards.

Order-tracking state machine internals (pending_cancel lifecycle, missing-lookup
heuristics) are omitted, but when the API returns an empty book and our
local state knows about a previously placed order, get_open_orders() must return
that tracked order so sync_order_ladder() refuses to stack a new ladder on top.

The rest of the tests pin the invariants that, if broken, would silently place 
mis-sized orders, fail to requote when the market moves, or stack orders on top 
of open ones.
"""
import time
from decimal import Decimal
from types import SimpleNamespace
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

    async def post(self, *_args, **kwargs) -> _FakeResponse:
        self.last_json = kwargs.get("json")
        return _FakeResponse(self.payload, status_code=self.status_code)

    async def get(self, *_args, **kwargs) -> _FakeResponse:
        return _FakeResponse(self.payload, status_code=self.status_code)


def _make_adapter(
    params: CexParams | None = None,
    *,
    alert_store: object | None = None,
    broadcast: object | None = None,
) -> QuidaxAdapter:
    return QuidaxAdapter(
        api_key="test-key",
        params=params,
        alert_store=alert_store or SimpleNamespace(insert_alert=AsyncMock()),
        system_state_store=None,
        broadcast=broadcast,
    )


@pytest.mark.asyncio
async def test_place_order_raises_on_quidax_error_payload():
    """Quidax returns HTTP 200 with status=error — we must raise, not silently succeed."""
    adapter = _make_adapter()
    adapter._get_client = AsyncMock(return_value=_FakeClient({"status": "error", "message": "bad market"}))

    with pytest.raises(ValueError, match="bad market"):
        await adapter.place_order("sell", Decimal("1403"), Decimal("10"))


@pytest.mark.asyncio
async def test_sync_order_ladder_uses_usdtcngn_order_semantics_and_balances_to_smaller_notional():
    """Order sizing balances to whichever leg is smaller in notional terms.

    order_size_cngn=2000 at price ~1400 ≈ 1.43 USDT; order_size_usdt=10 is larger.
    So USDT side must be capped to 1.43 (not 10), and cNGN side computed from that.
    """
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
    """When USDT leg is smaller, cNGN side is capped to USDT-equivalent, not order_size_cngn."""
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

    expected_calls = [
        call("buy", Decimal("1397.00"), Decimal("2.00")),
        call("sell", Decimal("1403"), Decimal("2")),
    ]
    assert adapter.place_order.await_args_list == expected_calls


@pytest.mark.asyncio
async def test_sync_order_ladder_raises_when_no_orders_are_accepted():
    """If every order placement fails, the ladder must raise — not silently produce no orders.

    Silent failure here means the CEX book is empty while the system believes it has
    active orders, leading to unhedged DEX exposure.
    """
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
    """If cancel_all_orders leaves residual orders, place_order must not be called.

    Stacking new orders on top of un-cancelled ones doubles CEX exposure.
    """
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
    """Within threshold_bps, existing orders are still valid — no cancel/replace cycle."""
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
    """Even if anchor moved beyond threshold, cooldown window prevents thrashing."""
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
    """A requote triggered by anchor drift must alert operators via both alert_store and broadcast."""
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


def test_order_market_matches_normalizes_market_formats():
    """Quidax returns market in at least four different shapes — all must match 'usdtcngn'."""
    market = "usdtcngn"

    assert order_market_matches({"market": "USDT/CNGN"}, market)
    assert order_market_matches({"market": "usdt_cngn"}, market)
    assert order_market_matches({"market": {"id": "USDT/CNGN"}}, market)
    assert order_market_matches({"market": {"base_unit": "USDT", "quote_unit": "CNGN"}}, market)


def test_normalize_order_summary_uses_origin_volume_when_volume_is_zero():
    """When volume=0 (fully consumed), origin_volume is the correct remaining-volume proxy.

    Without this, a fully-filled order appears as size=0, distorting open-order tracking.
    """
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
async def test_sync_order_ladder_does_not_stack_when_api_empty_but_tracked_order_exists():
    """API returns an empty book, but _order_state holds a locally-tracked order.

    The tracked fallback in get_open_orders() must surface the order. With a mocked
    cancel_all_orders() that doesn't actually remove it, the post-cancel stacking
    guard fires and raises rather than placing a new ladder on top.

    If the tracked fallback breaks, get_open_orders() returns [] → the initial
    existing_orders check is skipped → place_order fires blindly → CEX exposure doubles.
    """
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
    adapter.place_order = AsyncMock()
    adapter.cancel_all_orders = AsyncMock()  # no-op: order stays in tracked state

    # Seed the tracked state directly — bypasses persistence (system_state_store=None).
    adapter._order_state._tracked_open_orders_loaded = True
    adapter._order_state._tracked_open_orders = [
        {
            "id": "tracked-1",
            "market": "usdtcngn",
            "side": "buy",
            "status": "wait",
            "price": "1345.56",
            "volume": "1.48",
            "remaining_volume": "1.48",
        }
    ]

    # API returns empty on all attempts — tracked fallback must kick in.
    adapter._api.fetch_orders_payload = AsyncMock(return_value={"status": "success", "data": []})
    # fetch_order_by_id confirms the tracked order is still open (not cancelled).
    adapter._api.fetch_order_by_id = AsyncMock(
        return_value=(
            "found",
            {
                "id": "tracked-1",
                "market": {"id": "usdtcngn"},
                "side": "buy",
                "state": "wait",
                "price": {"amount": "1345.56"},
                "volume": {"amount": "1.48"},
                "remaining_volume": {"amount": "1.48"},
                "executed_volume": {"amount": "0"},
            },
        )
    )

    # The stacking guard fires: cancel was mocked, tracked order is still returned,
    # so the post-cancel check raises rather than stacking a new ladder.
    with pytest.raises(RuntimeError, match="prior open orders remain"):
        await adapter.sync_order_ladder(Decimal("1395.56"))

    # Regardless of which guard fires, place_order must never be called.
    adapter.place_order.assert_not_awaited()
