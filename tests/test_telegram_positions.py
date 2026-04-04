from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.api.schemas import Position
import engine.bot.telegram as telegram
from tests.fakes import FakeDexAdapter


class _FakeMessage:
    def __init__(self) -> None:
        self.reply_text = AsyncMock()


@pytest.mark.asyncio
async def test_cmd_positions_shows_true_lp_snapshot_for_dex_and_balances_for_non_lp(monkeypatch):
    lp_venue = FakeDexAdapter(name="uni-base")
    lp_venue.get_position = AsyncMock(
        return_value=Position(
            venue="uni-base",
            pair="cNGN/USDC",
            timestamp=0,
            balances={"cngn": 999999, "usdc": 999999, "usdt": 0},
        )
    )
    lp_venue.get_active_lp_position_snapshot = lambda: SimpleNamespace(
        token_id=77,
        token0_amount=1.25,
        token1_amount=198.75,
        token0_symbol="cNGN",
        token1_symbol="USDC",
        range_min=0.0005,
        range_max=0.0007,
        in_range=True,
        position_value_usd=200.0,
        our_share_pct=12.5,
    )

    non_lp_venue = SimpleNamespace(
        get_position=AsyncMock(
            return_value=Position(
                venue="quidax",
                pair="CNGN/USDT",
                timestamp=0,
                balances={"cngn": 10, "usdt": 5},
            )
        )
    )

    message = _FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="12345"),
        effective_message=message,
    )

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(
        telegram,
        "_runtime",
        SimpleNamespace(venues={"uni-base": lp_venue, "quidax": non_lp_venue}),
    )

    await telegram.cmd_positions(update, SimpleNamespace())

    reply_text = message.reply_text.await_args.args[0]
    assert "*Positions*" in reply_text
    assert "*uni-base*" in reply_text
    assert "token_id: 77" in reply_text
    assert "cngn: 1.2500" in reply_text
    assert "usdc: 198.7500" in reply_text
    assert "value_usd: 200.0000" in reply_text
    assert "our_share_pct: 12.5000" in reply_text
    assert "*quidax*" in reply_text
    assert "  cngn: 10.0000" in reply_text
    assert "  usdt: 5.0000" in reply_text
    lp_venue.get_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_positions_shows_no_active_lp_position(monkeypatch):
    lp_venue = FakeDexAdapter(name="uni-base")
    lp_venue.get_active_lp_position_snapshot = lambda: None
    lp_venue._positions = []

    message = _FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="12345"),
        effective_message=message,
    )

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(
        telegram,
        "_runtime",
        SimpleNamespace(venues={"uni-base": lp_venue}),
    )

    await telegram.cmd_positions(update, SimpleNamespace())

    reply_text = message.reply_text.await_args.args[0]
    assert "*uni-base*" in reply_text
    assert "No active LP position" in reply_text
