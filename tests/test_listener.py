"""Tests for wallet activity matching in the DEX WS listener."""

import asyncio

import pytest

from engine.arb.listener import (
    ERC20_TRANSFER_TOPIC,
    build_wallet_transfer_filters,
    matching_wallet_venues,
)
from engine.types import WalletActivitySubscription


_WALLET = "0x74b479868e3B8a21BDE4bb09F85177aCF9976A2d"
_TOKEN = "0x55d398326f99059fF775485246999027B3197955"
_RPC_SECRET = "fixture-secret-that-must-not-be-logged"
_SECRET_URL = f"wss://example.invalid/v2/{_RPC_SECRET}"


class _RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.records.append(("warning", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.records.append(("error", event, fields))

    def info(self, event: str, **fields: object) -> None:
        self.records.append(("info", event, fields))

    def debug(self, event: str, **fields: object) -> None:
        self.records.append(("debug", event, fields))


def _topic(address: str) -> str:
    normalized = address.lower().removeprefix("0x")
    return "0x" + ("0" * 24) + normalized


def test_matching_wallet_venues_detects_incoming_transfer():
    log = {
        "address": _TOKEN,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            _topic("0x1111111111111111111111111111111111111111"),
            _topic(_WALLET),
        ],
    }
    subs = [
        WalletActivitySubscription(
            venue_name="uni-bsc",
            wallet_address=_WALLET,
            token_address=_TOKEN,
            )
    ]

    assert matching_wallet_venues(log, subs) == {"uni-bsc"}


def test_matching_wallet_venues_ignores_unrelated_transfer():
    log = {
        "address": _TOKEN,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            _topic("0x1111111111111111111111111111111111111111"),
            _topic("0x2222222222222222222222222222222222222222"),
        ],
    }
    subs = [
        WalletActivitySubscription(
            venue_name="uni-bsc",
            wallet_address=_WALLET,
            token_address=_TOKEN,
            )
    ]

    assert matching_wallet_venues(log, subs) == set()


def test_build_wallet_transfer_filters_targets_wallet_topics():
    subs = [
        WalletActivitySubscription(
            venue_name="uni-bsc",
            wallet_address=_WALLET,
            token_address=_TOKEN,
            )
    ]

    filters = build_wallet_transfer_filters(subs)

    assert len(filters) == 2
    outgoing_filter, outgoing_meta = filters[0]
    incoming_filter, incoming_meta = filters[1]

    assert outgoing_filter == {
        "address": _TOKEN.lower(),
        "topics": [ERC20_TRANSFER_TOPIC, [_topic(_WALLET)]],
    }
    assert outgoing_meta["kind"] == "wallet_transfer"
    assert outgoing_meta["direction"] == "outgoing"

    assert incoming_filter == {
        "address": _TOKEN.lower(),
        "topics": [ERC20_TRANSFER_TOPIC, None, [_topic(_WALLET)]],
    }
    assert incoming_meta["kind"] == "wallet_transfer"
    assert incoming_meta["direction"] == "incoming"


class _FakeWebSocket:
    def __init__(self):
        self.recv_calls = 0
        self.ping_calls = 0

    async def recv(self):
        self.recv_calls += 1
        if self.recv_calls == 1:
            await asyncio.sleep(0.05)
        return '{"method":"eth_subscription","params":{"subscription":"1","result":{}}}'

    async def ping(self):
        self.ping_calls += 1
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut


@pytest.mark.asyncio
async def test_recv_with_keepalive_pings_on_idle(monkeypatch):
    from engine.arb.listener import ArbitrageWebSocketListener
    import engine.arb.listener as _listener

    monkeypatch.setattr(_listener, "_WSS_IDLE_RECV_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(_listener, "_WSS_PING_TIMEOUT_SECONDS", 0.01)

    listener = ArbitrageWebSocketListener(broadcast=lambda _: None)
    listener._running = True
    ws = _FakeWebSocket()

    msg = await listener._recv_with_keepalive(ws, "base")

    assert "eth_subscription" in msg
    assert ws.ping_calls == 1


@pytest.mark.asyncio
async def test_refresh_pool_state_triggers_market_update(monkeypatch):
    from engine.arb.listener import ArbitrageWebSocketListener
    import engine.arb.listener as _listener

    listener = ArbitrageWebSocketListener(broadcast=lambda _: None)
    triggered: list[str] = []
    monkeypatch.setattr(listener, "_trigger_market_update", lambda chain: triggered.append(chain))

    async def _ok_refresh(_pool_config):
        return True

    monkeypatch.setattr(_listener, "update_single_v4_pool_state", _ok_refresh)

    await listener._refresh_pool_state("base", type("PoolCfg", (), {"pool_address": "0xpool"})())

    assert triggered == ["base"]


@pytest.mark.asyncio
async def test_refresh_pool_state_failure_redacts_endpoint_credential(monkeypatch):
    from engine.arb.listener import ArbitrageWebSocketListener
    import engine.arb.listener as _listener

    recording = _RecordingLogger()
    listener = ArbitrageWebSocketListener(broadcast=lambda _: None)
    monkeypatch.setattr(_listener, "logger", recording)

    async def _failed_refresh(_pool_config):
        raise RuntimeError(f"RPC request failed at {_SECRET_URL}")

    monkeypatch.setattr(_listener, "update_single_v4_pool_state", _failed_refresh)

    await listener._refresh_pool_state(
        "base",
        type("PoolCfg", (), {"pool_address": "0xpool"})(),
    )

    _, event, fields = recording.records[-1]
    assert event == "wss_pool_state_refresh_failed"
    assert _RPC_SECRET not in repr(fields)
    assert "[REDACTED]" in str(fields["error"])


@pytest.mark.asyncio
async def test_wss_connection_failure_redacts_endpoint_credential(monkeypatch):
    from engine.arb.listener import ArbitrageWebSocketListener
    import engine.arb.listener as _listener

    recording = _RecordingLogger()
    listener = ArbitrageWebSocketListener(broadcast=lambda _: None)
    listener._running = True
    monkeypatch.setattr(_listener, "logger", recording)

    class _FailingConnection:
        async def __aenter__(self):
            raise RuntimeError(f"WSS request failed at {_SECRET_URL}")

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        _listener.websockets,
        "connect",
        lambda _url: _FailingConnection(),
    )

    async def _stop_after_failure(_seconds: float) -> None:
        listener._running = False

    monkeypatch.setattr(_listener.asyncio, "sleep", _stop_after_failure)
    pool_config = type(
        "PoolCfg",
        (),
        {"pool_address": "0xpool", "pool_manager": "0xmanager"},
    )()

    await listener._listen_to_chain("base", _SECRET_URL, pool_config, [])

    error_records = [
        fields
        for _, event, fields in recording.records
        if event == "wss_connection_error"
    ]
    assert len(error_records) == 1
    assert _RPC_SECRET not in repr(error_records[0])
    assert "[REDACTED]" in str(error_records[0]["error"])
