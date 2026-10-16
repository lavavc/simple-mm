"""Inventory skew direction, quote budgets, and integration with live wallet reads."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from engine.venues.cex.ladder_planner import resolve_inventory_order_sizes
from engine.venues.cex.quidax_client import QuidaxWalletBalance
from tests.test_quidax_adapter import _make_adapter
from engine.types import CexParams


def sizes(cngn, usdt, *, strength="0.5", cngn_budget=None, usdt_budget=None):
    return resolve_inventory_order_sizes(
        reference_price=Decimal("1000"),
        order_size_cngn=Decimal("2000"),
        order_size_usdt=Decimal("10"),
        cngn_total=Decimal(cngn), usdt_total=Decimal(usdt),
        cngn_budget=Decimal(cngn if cngn_budget is None else cngn_budget),
        usdt_budget=Decimal(usdt if usdt_budget is None else usdt_budget),
        levels=5, skew_strength=Decimal(strength),
    )


@pytest.mark.parametrize("cngn,usdt,bid,ask", [
    ("100000", "100", "2000", "2"),
    ("20000", "180", "1200", "2.8"),
    ("180000", "20", "2800", "1.2"),
])
def test_skew_spends_more_of_overweight_asset(cngn, usdt, bid, ask):
    cngn_size, usdt_size = sizes(cngn, usdt)
    assert cngn_size == Decimal(bid)
    assert usdt_size == Decimal(ask)
    assert cngn_size / 1000 + usdt_size == 4


def test_zero_strength_restores_symmetric_notional():
    assert sizes("20000", "180", strength="0") == (Decimal("2000"), Decimal("2"))


@pytest.mark.parametrize("cngn,usdt", [("0", "100"), ("100000", "0"), ("0", "0")])
def test_empty_asset_is_never_offered(cngn, usdt):
    bid, ask = sizes(cngn, usdt)
    assert bid * 5 <= Decimal(cngn) * Decimal("0.99")
    assert ask * 5 <= Decimal(usdt) * Decimal("0.99")
    if Decimal(cngn) == 0:
        assert bid == 0
    if Decimal(usdt) == 0:
        assert ask == 0


def test_locked_funds_count_for_skew_but_cannot_fund_new_orders():
    bid, ask = sizes("100000", "100", cngn_budget="1000", usdt_budget="1")
    assert bid == Decimal("198")
    assert ask == Decimal("0.198")


@pytest.mark.asyncio
async def test_ladder_uses_own_wallets_and_caps_full_ladder():
    adapter = _make_adapter(CexParams(
        ladder_enabled=True, ladder_levels_per_side=5,
        order_size_cngn=Decimal("2000"), order_size_usdt=Decimal("10"),
    ))
    adapter.get_open_orders = AsyncMock(return_value=[])
    adapter.place_order = AsyncMock()
    adapter._api.get_wallet_inventory = AsyncMock(side_effect=lambda currency: QuidaxWalletBalance(
        available=Decimal("1000") if currency == "cngn" else Decimal("180"),
        locked=Decimal("19000") if currency == "cngn" else Decimal("0"),
    ))

    await adapter.sync_order_ladder(Decimal("1000"))

    orders = adapter.place_order.await_args_list
    assert len(orders) == 10
    assert sum(c.args[1] * c.args[2] for c in orders if c.args[0] == "buy") <= Decimal("990")
    assert all(c.args[2] == Decimal("2.8") for c in orders if c.args[0] == "sell")


@pytest.mark.asyncio
async def test_wallet_failure_preserves_existing_ladder():
    adapter = _make_adapter(CexParams(ladder_enabled=True))
    adapter.get_open_orders = AsyncMock(return_value=[{"id": "existing"}])
    adapter._api.get_wallet_inventory = AsyncMock(side_effect=RuntimeError("wallet unavailable"))
    adapter.cancel_all_orders = AsyncMock()
    adapter.place_order = AsyncMock()
    with pytest.raises(RuntimeError, match="wallet unavailable"):
        await adapter.sync_order_ladder(Decimal("1000"))
    adapter.cancel_all_orders.assert_not_awaited()
    adapter.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_requote_refreshes_available_funds_after_cancel_settlement():
    adapter = _make_adapter(CexParams(
        ladder_enabled=True, ladder_levels_per_side=5,
        order_size_cngn=Decimal("2000"), order_size_usdt=Decimal("10"),
    ))
    adapter.get_open_orders = AsyncMock(side_effect=[
        [{"id": "old", "side": "buy", "price": "900", "volume": "1"}], [], [],
    ])
    adapter.cancel_all_orders = AsyncMock()
    adapter.place_order = AsyncMock()
    adapter._api.get_wallet_inventory = AsyncMock(side_effect=[
        QuidaxWalletBalance(Decimal("1000"), Decimal("99000")),
        QuidaxWalletBalance(Decimal("1"), Decimal("99")),
        QuidaxWalletBalance(Decimal("500"), Decimal("99500")),
        QuidaxWalletBalance(Decimal("0.5"), Decimal("99.5")),
    ])

    await adapter.sync_order_ladder(Decimal("1000"))

    adapter.cancel_all_orders.assert_awaited_once()
    assert adapter._api.get_wallet_inventory.await_count == 4
    orders = adapter.place_order.await_args_list
    assert sum(c.args[1] * c.args[2] for c in orders if c.args[0] == "buy") <= Decimal("495")
    assert sum(c.args[2] for c in orders if c.args[0] == "sell") <= Decimal("0.495")


def test_skew_multipliers_stay_within_soft_bounds():
    bid, ask = sizes("10000", "10000")
    assert Decimal("1000") <= bid <= Decimal("3000")
    assert Decimal("1") <= ask <= Decimal("3")
    assert ask / (bid / 1000) <= 3


@pytest.mark.parametrize("cngn_size,usdt_size,disabled_side", [
    ("0", "10", 0), ("2000", "0", 1),
])
def test_explicitly_disabled_side_stays_disabled(cngn_size, usdt_size, disabled_side):
    result = resolve_inventory_order_sizes(
        reference_price=Decimal("1000"),
        order_size_cngn=Decimal(cngn_size), order_size_usdt=Decimal(usdt_size),
        cngn_total=Decimal("100000"), usdt_total=Decimal("100"),
        cngn_budget=Decimal("100000"), usdt_budget=Decimal("100"),
        levels=5, skew_strength=Decimal("0.5"),
    )
    assert result[disabled_side] == 0
