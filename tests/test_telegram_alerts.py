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
