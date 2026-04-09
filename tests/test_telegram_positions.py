from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.types import LPPosition, Position, VenueOrderSummary
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
            balances={"cngn": 1.25, "usdc": 198.75, "usdt": 0},
            lp_position=LPPosition(
                token_id="77",
                liquidity="1000000",
                range_min=0.0005,
                range_max=0.0007,
                in_range=True,
                our_share_pct=12.5,
                snapshot_status="live",
            ),
            position_value_usd=200.0,
        )
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
        SimpleNamespace(venues={"uni-base": lp_venue, "quidax": non_lp_venue}, lp_managers={}),
    )

    await telegram.cmd_positions(update, SimpleNamespace())

    reply_text = message.reply_text.await_args.args[0]
    assert "*Positions*" in reply_text
    assert "*uni-base*" in reply_text
    assert "  cngn: 1.2500" in reply_text
    assert "  usdc: 198.7500" in reply_text
    assert "token_id: 77" in reply_text
    assert "snapshot_status: live" in reply_text
    assert "range: 0.000500 -> 0.000700" in reply_text
    assert "in_range: yes" in reply_text
    assert "value_usd: 200.0000" in reply_text
    assert "our_share_pct: 12.5000" in reply_text
    assert "*quidax*" in reply_text
    assert "  cngn: 10.0000" in reply_text
    assert "  usdt: 5.0000" in reply_text
    lp_venue.get_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_positions_shows_multi_position_degraded_message(monkeypatch):
    lp_venue = FakeDexAdapter(name="uni-base")
    lp_venue.get_position = AsyncMock(
        return_value=Position(
            venue="uni-base",
            pair="cNGN/USDC",
            timestamp=0,
            balances={"cngn": 0, "usdc": 0, "usdt": 0},
            lp_position=LPPosition(
                token_id=None,
                liquidity=None,
                range_min=None,
                range_max=None,
                in_range=None,
                our_share_pct=None,
                snapshot_status="degraded",
                snapshot_message="Multiple LP NFTs detected; automatic LP management is halted until manual cleanup.",
            ),
            position_value_usd=None,
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
        SimpleNamespace(venues={"uni-base": lp_venue}, lp_managers={}),
    )

    await telegram.cmd_positions(update, SimpleNamespace())

    reply_text = message.reply_text.await_args.args[0]
    assert "token_id: unavailable" in reply_text
    assert "snapshot_status: degraded" in reply_text
    assert "snapshot_message: Multiple LP NFTs detected; automatic LP management is halted until manual cleanup." in reply_text


@pytest.mark.asyncio
async def test_cmd_positions_shows_degraded_snapshot_without_hiding_lp(monkeypatch):
    lp_venue = FakeDexAdapter(name="uni-base")
    lp_venue.get_position = AsyncMock(
        return_value=Position(
            venue="uni-base",
            pair="cNGN/USDC",
            timestamp=0,
            balances={"cngn": 0, "usdc": 0, "usdt": 0},
            lp_position=LPPosition(
                token_id="77",
                liquidity=None,
                range_min=None,
                range_max=None,
                in_range=None,
                our_share_pct=None,
                snapshot_status="degraded",
                snapshot_message="LP position exists, but live composition is unavailable.",
            ),
            position_value_usd=None,
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
        SimpleNamespace(venues={"uni-base": lp_venue}, lp_managers={}),
    )

    await telegram.cmd_positions(update, SimpleNamespace())

    reply_text = message.reply_text.await_args.args[0]
    assert "snapshot_status: degraded" in reply_text
    assert "snapshot_message: LP position exists, but live composition is unavailable." in reply_text
    assert "range: unavailable" in reply_text
    assert "in_range: unknown" in reply_text


@pytest.mark.asyncio
async def test_cmd_positions_shows_no_active_lp_position(monkeypatch):
    lp_venue = FakeDexAdapter(name="uni-base")
    lp_venue.get_position = AsyncMock(
        return_value=Position(
            venue="uni-base",
            pair="cNGN/USDC",
            timestamp=0,
            balances={"cngn": 0, "usdc": 0, "usdt": 0},
            lp_position=None,
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
        SimpleNamespace(venues={"uni-base": lp_venue}, lp_managers={}),
    )

    await telegram.cmd_positions(update, SimpleNamespace())

    reply_text = message.reply_text.await_args.args[0]
    assert "*uni-base*" in reply_text
    assert "cngn: 0.0000" in reply_text
    assert "usdc: 0.0000" in reply_text


@pytest.mark.asyncio
async def test_cmd_orders_defaults_to_quidax_and_lists_open_orders(monkeypatch):
    venue = SimpleNamespace(
        get_open_order_summaries=AsyncMock(
            return_value=[
                VenueOrderSummary(
                    id="ord-1",
                    market="usdtcngn",
                    side="buy",
                    status="wait",
                    price=Decimal("1345.10"),
                    volume=Decimal("1.48"),
                    remaining_volume=Decimal("1.48"),
                    executed_volume=Decimal("0"),
                    notional=Decimal("1990.748"),
                    created_at=1712520000000,
                )
            ]
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
        SimpleNamespace(venues={"quidax": venue}, lp_managers={}, quidax_lp=None),
    )

    await telegram.cmd_orders(update, SimpleNamespace(args=[]))

    reply_text = message.reply_text.await_args.args[0]
    assert "*Open Orders · quidax*" in reply_text
    assert "count: 1" in reply_text
    assert "`BUY` `wait` `usdtcngn`" in reply_text
    assert "1.4800 @ 1345.1000" in reply_text
    assert "`ord-1`" in reply_text
    venue.get_open_order_summaries.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_orders_reports_when_no_open_orders(monkeypatch):
    venue = SimpleNamespace(get_open_order_summaries=AsyncMock(return_value=[]))

    message = _FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="12345"),
        effective_message=message,
    )

    monkeypatch.setattr(telegram, "_settings", SimpleNamespace(telegram_chat_id="12345"))
    monkeypatch.setattr(
        telegram,
        "_runtime",
        SimpleNamespace(venues={"quidax": venue}, lp_managers={}, quidax_lp=None),
    )

    await telegram.cmd_orders(update, SimpleNamespace(args=[]))

    message.reply_text.assert_awaited_once_with("No open orders on quidax.")


@pytest.mark.asyncio
async def test_cmd_orders_prefers_dedicated_quidax_lp_when_available(monkeypatch):
    main_venue = SimpleNamespace(get_open_order_summaries=AsyncMock(return_value=[]))
    lp_venue = SimpleNamespace(
        get_open_order_summaries=AsyncMock(
            return_value=[
                VenueOrderSummary(
                    id="lp-1",
                    market="usdtcngn",
                    side="sell",
                    status="wait",
                    price=Decimal("1445.10"),
                    volume=Decimal("1.43"),
                    remaining_volume=Decimal("1.43"),
                    executed_volume=Decimal("0"),
                    notional=Decimal("2066.493"),
                    created_at=1712520000000,
                )
            ]
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
        SimpleNamespace(
            venues={"quidax": main_venue, "quidax-lp": lp_venue},
            lp_managers={},
            quidax_lp=lp_venue,
        ),
    )

    await telegram.cmd_orders(update, SimpleNamespace(args=[]))

    reply_text = message.reply_text.await_args.args[0]
    assert "*Open Orders · quidax-lp*" in reply_text
    assert "`SELL` `wait` `usdtcngn`" in reply_text
    assert "`lp-1`" in reply_text
    main_venue.get_open_order_summaries.assert_not_awaited()
    lp_venue.get_open_order_summaries.assert_awaited_once()
