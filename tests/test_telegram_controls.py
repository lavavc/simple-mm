from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import engine.bot.telegram as telegram


class _FakeMessage:
    def __init__(self) -> None:
        self.reply_text = AsyncMock()


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id="12345"))
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()


@pytest.mark.asyncio
async def test_cmd_pause_with_venue_requires_confirmation(monkeypatch):
    message = _FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="12345"),
        effective_message=message,
    )

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(
        telegram,
        "_runtime",
        SimpleNamespace(venues={"quidax": SimpleNamespace()}, quidax_lp=None),
    )

    await telegram.cmd_pause(update, SimpleNamespace(args=["quidax"]))

    args = message.reply_text.await_args.args
    kwargs = message.reply_text.await_args.kwargs
    assert "Pause *quidax* and cancel open orders" in args[0]
    assert kwargs["parse_mode"] == "Markdown"
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "confirm:pause_venue:quidax"


@pytest.mark.asyncio
async def test_cmd_pause_quidax_resolves_to_active_quidax_lp(monkeypatch):
    message = _FakeMessage()
    lp_venue = SimpleNamespace()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="12345"),
        effective_message=message,
    )

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(
        telegram,
        "_runtime",
        SimpleNamespace(
            venues={"quidax": SimpleNamespace(), "quidax-lp": lp_venue},
            quidax_lp=lp_venue,
        ),
    )

    await telegram.cmd_pause(update, SimpleNamespace(args=["quidax"]))

    args = message.reply_text.await_args.args
    kwargs = message.reply_text.await_args.kwargs
    assert "quidax (effective venue: quidax-lp)" in args[0]
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "confirm:pause_venue:quidax-lp"


@pytest.mark.asyncio
async def test_cmd_resume_with_venue_requires_confirmation(monkeypatch):
    message = _FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="12345"),
        effective_message=message,
    )

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(
        telegram,
        "_runtime",
        SimpleNamespace(venues={"quidax": SimpleNamespace()}, quidax_lp=None),
    )

    await telegram.cmd_resume(update, SimpleNamespace(args=["quidax"]))

    args = message.reply_text.await_args.args
    kwargs = message.reply_text.await_args.kwargs
    assert "Resume *quidax*" in args[0]
    assert kwargs["parse_mode"] == "Markdown"
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "confirm:resume_venue:quidax"


@pytest.mark.asyncio
async def test_cmd_resume_quidax_resolves_to_active_quidax_lp(monkeypatch):
    message = _FakeMessage()
    lp_venue = SimpleNamespace()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="12345"),
        effective_message=message,
    )

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(
        telegram,
        "_runtime",
        SimpleNamespace(
            venues={"quidax": SimpleNamespace(), "quidax-lp": lp_venue},
            quidax_lp=lp_venue,
        ),
    )

    await telegram.cmd_resume(update, SimpleNamespace(args=["quidax"]))

    args = message.reply_text.await_args.args
    kwargs = message.reply_text.await_args.kwargs
    assert "quidax (effective venue: quidax-lp)" in args[0]
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "confirm:resume_venue:quidax-lp"


@pytest.mark.asyncio
async def test_handle_callback_global_pause_cancels_open_cex_orders(monkeypatch):
    quidax = SimpleNamespace(paused=False, cancel_all_orders=AsyncMock(return_value=2))
    runtime = SimpleNamespace(
        scheduler=SimpleNamespace(pause=AsyncMock()),
        venues={"quidax": quidax, "uni-base": SimpleNamespace(paused=False)},
    )
    query = _FakeQuery("confirm:pause")
    update = SimpleNamespace(callback_query=query)

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(telegram, "_runtime", runtime)

    await telegram.handle_callback(update, SimpleNamespace())

    runtime.scheduler.pause.assert_awaited_once()
    quidax.cancel_all_orders.assert_awaited_once()
    assert quidax.paused is False
    assert "Trading paused." in query.edit_message_text.await_args.args[0]
    assert "Cancelled open orders: quidax=2." in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_handle_callback_pause_venue_sets_paused_and_cancels_orders(monkeypatch):
    quidax = SimpleNamespace(paused=False, cancel_all_orders=AsyncMock(return_value=1))
    runtime = SimpleNamespace(
        scheduler=SimpleNamespace(),
        venues={"quidax": quidax},
        db=SimpleNamespace(system_state=SimpleNamespace(set_system_state=AsyncMock())),
    )
    query = _FakeQuery("confirm:pause_venue:quidax")
    update = SimpleNamespace(callback_query=query)

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(telegram, "_runtime", runtime)

    await telegram.handle_callback(update, SimpleNamespace())

    assert quidax.paused is True
    quidax.cancel_all_orders.assert_awaited_once()
    assert query.edit_message_text.await_args.args[0] == "⏸ quidax paused. Cancelled open orders: 1."


@pytest.mark.asyncio
async def test_handle_callback_resume_venue_syncs_when_global_trading_enabled(monkeypatch):
    quidax = SimpleNamespace(
        paused=True,
        params=SimpleNamespace(anchor_source="quidax"),
        sync_order_ladder=AsyncMock(),
        get_position=AsyncMock(),
    )
    runtime = SimpleNamespace(
        scheduler=SimpleNamespace(
            trading_enabled=True,
            market_jobs=SimpleNamespace(get_reference_price_ngn=AsyncMock(return_value=Decimal("1600"))),
        ),
        venues={"quidax": quidax},
        blended_calculator=None,
        price_aggregator=None,
        db=SimpleNamespace(system_state=SimpleNamespace(set_system_state=AsyncMock())),
    )
    query = _FakeQuery("confirm:resume_venue:quidax")
    update = SimpleNamespace(callback_query=query)

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(telegram, "_runtime", runtime)

    await telegram.handle_callback(update, SimpleNamespace())

    assert quidax.paused is False
    runtime.scheduler.market_jobs.get_reference_price_ngn.assert_awaited_once_with(anchor_source="quidax")
    quidax.sync_order_ladder.assert_awaited_once_with(Decimal("1600"))
    quidax.get_position.assert_not_awaited()
    assert query.edit_message_text.await_args.args[0] == "▶️ quidax resumed. Sync triggered."


@pytest.mark.asyncio
async def test_handle_callback_resume_venue_skips_sync_when_global_trading_paused(monkeypatch):
    quidax = SimpleNamespace(
        paused=True,
        params=SimpleNamespace(anchor_source="quidax"),
        sync_order_ladder=AsyncMock(),
        get_position=AsyncMock(),
    )
    runtime = SimpleNamespace(
        scheduler=SimpleNamespace(
            trading_enabled=False,
            market_jobs=SimpleNamespace(get_reference_price_ngn=AsyncMock(return_value=Decimal("1600"))),
        ),
        venues={"quidax": quidax},
        blended_calculator=None,
        price_aggregator=None,
        db=SimpleNamespace(system_state=SimpleNamespace(set_system_state=AsyncMock())),
    )
    query = _FakeQuery("confirm:resume_venue:quidax")
    update = SimpleNamespace(callback_query=query)

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(telegram, "_runtime", runtime)

    await telegram.handle_callback(update, SimpleNamespace())

    assert quidax.paused is False
    runtime.scheduler.market_jobs.get_reference_price_ngn.assert_not_awaited()
    quidax.sync_order_ladder.assert_not_awaited()
    quidax.get_position.assert_not_awaited()
    assert query.edit_message_text.await_args.args[0] == (
        "▶️ quidax resumed, but global trading is still paused so sync was skipped."
    )
