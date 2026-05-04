import json

import pytest

from engine.ws import ConnectionManager


class FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent_texts: list[str] = []

    async def accept(self):
        self.accepted = True

    async def send_text(self, payload: str):
        self.sent_texts.append(payload)


@pytest.mark.asyncio
async def test_retained_events_replayed_on_accept():
    manager = ConnectionManager()
    manager.broadcast({"type": "dex_arb_curve", "data": {"curve": [1, 2, 3]}})

    ws = FakeWebSocket()
    await manager.accept(ws)

    assert ws.accepted is True
    assert len(ws.sent_texts) == 1
    assert json.loads(ws.sent_texts[0]) == {"type": "dex_arb_curve", "data": {"curve": [1, 2, 3]}}


@pytest.mark.asyncio
async def test_non_retained_events_not_replayed_on_accept():
    manager = ConnectionManager()
    manager.broadcast({"type": "alert", "severity": "warning", "message": "hello"})

    ws = FakeWebSocket()
    await manager.accept(ws)

    assert ws.accepted is True
    assert ws.sent_texts == []


@pytest.mark.asyncio
async def test_quidax_open_orders_retention_is_namespaced_by_venue():
    manager = ConnectionManager()
    manager.broadcast(
        {
            "type": "quidax_open_orders",
            "data": {"venue": "quidax", "count": 1, "orders": []},
        }
    )
    manager.broadcast(
        {
            "type": "quidax_open_orders",
            "data": {"venue": "quidax-lp", "count": 2, "orders": []},
        }
    )

    ws = FakeWebSocket()
    await manager.accept(ws)

    assert ws.accepted is True
    assert len(ws.sent_texts) == 2
    payloads = [json.loads(text) for text in ws.sent_texts]
    assert {payload["data"]["venue"] for payload in payloads} == {"quidax", "quidax-lp"}
