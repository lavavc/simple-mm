from types import SimpleNamespace

import pytest

import engine.bot.telegram as telegram


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text):
        self.calls.append((chat_id, text))


@pytest.mark.asyncio
async def test_forward_alert_suppresses_duplicates(monkeypatch):
    fake_bot = FakeBot()
    monkeypatch.setattr(telegram, "_app", SimpleNamespace(bot=fake_bot))
    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(telegram, "_recent_alerts", {})

    event = {
        "type": "alert",
        "severity": "warning",
        "message": "Repeated preflight alert",
        "dedupe_key": "preflight:unknown:uni-base",
        "cooldown_s": 60,
    }

    await telegram.forward_alert(event)
    await telegram.forward_alert(event)

    assert fake_bot.calls == [("12345", "⚠️ Repeated preflight alert")]


@pytest.mark.asyncio
async def test_forward_alert_allows_distinct_dedupe_keys(monkeypatch):
    fake_bot = FakeBot()
    monkeypatch.setattr(telegram, "_app", SimpleNamespace(bot=fake_bot))
    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(telegram, "_recent_alerts", {})

    event_one = {
        "type": "alert",
        "severity": "warning",
        "message": "Preflight alert A",
        "dedupe_key": "preflight:unknown:uni-base:size=500:shortfall=673000",
        "cooldown_s": 60,
    }
    event_two = {
        "type": "alert",
        "severity": "warning",
        "message": "Preflight alert B",
        "dedupe_key": "preflight:unknown:uni-base:size=650:shortfall=883000",
        "cooldown_s": 60,
    }

    await telegram.forward_alert(event_one)
    await telegram.forward_alert(event_two)

    assert fake_bot.calls == [
        ("12345", "⚠️ Preflight alert A"),
        ("12345", "⚠️ Preflight alert B"),
    ]


@pytest.mark.asyncio
async def test_forward_alert_allows_distinct_messages_without_explicit_dedupe_key(monkeypatch):
    fake_bot = FakeBot()
    monkeypatch.setattr(telegram, "_app", SimpleNamespace(bot=fake_bot))
    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(telegram, "_recent_alerts", {})

    event_one = {
        "type": "alert",
        "severity": "warning",
        "message": "Preflight alert A\nTrade size: $500.00",
        "cooldown_s": 60,
    }
    event_two = {
        "type": "alert",
        "severity": "warning",
        "message": "Preflight alert A\nTrade size: $650.00",
        "cooldown_s": 60,
    }

    await telegram.forward_alert(event_one)
    await telegram.forward_alert(event_two)

    assert fake_bot.calls == [
        ("12345", "⚠️ Preflight alert A\nTrade size: $500.00"),
        ("12345", "⚠️ Preflight alert A\nTrade size: $650.00"),
    ]
