import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.market.portfolio_exposure import PortfolioExposureCalculator
from engine.market.portfolio_registry import DEFAULT_PORTFOLIO_SOURCE_REGISTRY
from engine.types import TradeInventorySnapshot


def wallet(role, **balances):
    return SimpleNamespace(role=role, token_balances={s: Decimal(str(v)) for s, v in balances.items()})


def manager(rows, expected=None):
    return SimpleNamespace(
        list_accounts=lambda: dict.fromkeys(expected or [r.role for r in rows], "address"),
        check_all_balances=AsyncMock(return_value=rows),
    )


def exchange(cngn, usdt, locked_cngn=0, locked_usdt=0):
    return SimpleNamespace(get_trade_inventory=AsyncMock(return_value=TradeInventorySnapshot(
        Decimal(usdt), Decimal(cngn), Decimal(usdt) + Decimal(locked_usdt),
        Decimal(cngn) + Decimal(locked_cngn), 1,
    )))


def calculator(venues=None, accounts=None, lps=None, price="0.001", aggregator=None):
    return PortfolioExposureCalculator(
        venues=venues or {}, account_manager=accounts, token_contracts={},
        blended_calculator=SimpleNamespace(get_blended_price=AsyncMock(return_value=SimpleNamespace(
            vwap=Decimal(price), twap_5m=Decimal(0), twap_1h=Decimal(0),
        ))), price_aggregator=aggregator,
        portfolio_source_registry=DEFAULT_PORTFOLIO_SOURCE_REGISTRY, lp_managers=lps,
    )


@pytest.mark.asyncio
async def test_additive_buckets_include_locked_exchange_funds_without_duplicate_venues():
    accounts = manager([
        wallet("uni-base-lp", cNGN=50, USDC=5),
        wallet("uni-base-trade", cNGN=10, USDC=2),
        wallet("blockradar", cNGN=30, USDC=4),
    ])
    lp = SimpleNamespace(get_portfolio_balances=MagicMock(return_value={
        "cngn": Decimal(1000), "usdc": Decimal(25),
    }))
    unregistered = SimpleNamespace(get_position=AsyncMock())
    report = await calculator(
        venues={"uni-base": object(), "quidax": exchange(500, 20, 100, 10),
                "quidax-lp": exchange(300, 15, 200, 5), "blockradar": unregistered},
        accounts=accounts, lps={"uni-base": lp},
    ).calculate()
    assert report.complete
    assert report.total_cngn == Decimal(2190)
    assert report.total_usdt == Decimal(50)
    assert report.total_usdc == Decimal(36)
    assert report.total_usd_value == Decimal("88.190")
    assert len(report.sources) == 6
    unregistered.get_position.assert_not_called()
    lp.get_portfolio_balances.assert_called_once()


@pytest.mark.asyncio
async def test_failed_token_read_preserves_other_known_quantities():
    report = await calculator(accounts=manager([wallet("trade", cNGN=-1, USDC=2)])).calculate()
    assert not report.complete
    assert report.total_cngn is None
    assert report.total_usdc == 2
    assert report.total_usd_value is None
    assert report.cngn_share is None
    assert report.sources[0].error


@pytest.mark.asyncio
async def test_missing_wallet_is_explicitly_unknown():
    accounts = manager([wallet("trade", USDC=2)], expected=["trade", "lp"])
    report = await calculator(accounts=accounts).calculate()
    assert not report.complete
    assert report.total_usd_value is None
    assert report.sources[1].source == "lp"
    assert report.sources[1].error == "Wallet read failed"


@pytest.mark.asyncio
async def test_failed_wallet_batch_does_not_look_empty():
    accounts = manager([], expected=["trade"])
    accounts.check_all_balances.side_effect = RuntimeError("RPC failed")
    report = await calculator(accounts=accounts).calculate()
    assert not report.complete
    assert report.total_cngn is None


@pytest.mark.asyncio
async def test_unknown_lp_holdings_do_not_disappear_from_total():
    report = await calculator(
        venues={"uni-base": object()},
        lps={"uni-base": SimpleNamespace(get_portfolio_balances=lambda: None)},
    ).calculate()
    assert not report.complete
    assert report.total_usd_value is None
    assert report.sources[0].source == "uni-base"


@pytest.mark.asyncio
async def test_exchange_failure_does_not_use_available_only_fallback():
    venue = exchange(1, 2)
    venue.get_trade_inventory.side_effect = RuntimeError("unavailable")
    report = await calculator(venues={"quidax": venue}).calculate()
    assert not report.complete
    assert report.total_usdt is None


@pytest.mark.asyncio
async def test_missing_price_keeps_quantities_but_not_valuation():
    report = await calculator(accounts=manager([wallet("trade", cNGN=1000, USDC=2)]), price="0").calculate()
    assert not report.complete
    assert report.total_cngn == 1000
    assert report.total_usdc == 2
    assert report.total_usd_value is None
    assert report.cngn_share is None
    assert report.sources[0].error == "cNGN price unavailable"


@pytest.mark.asyncio
async def test_cash_only_portfolio_needs_no_cngn_price():
    report = await calculator(accounts=manager([wallet("trade", USDC=2)]), price="0").calculate()
    assert report.complete
    assert report.total_usd_value == 2
    assert report.cngn_share == 0


@pytest.mark.asyncio
async def test_concurrent_reporting_reads_share_snapshot_until_refresh():
    accounts = manager([wallet("trade", USDC=2)])
    calc = calculator(accounts=accounts)
    first, second = await asyncio.gather(calc.calculate(), calc.calculate())
    assert first is second
    accounts.check_all_balances.assert_awaited_once()
    calc._refreshed_at -= 121
    accounts.check_all_balances.return_value = [wallet("trade", USDC=3)]
    refreshed = await calc.calculate()
    assert refreshed.total_usd_value == 3
    assert accounts.check_all_balances.await_count == 2


@pytest.mark.asyncio
async def test_bybit_inverse_price_fallback():
    aggregator = SimpleNamespace(get_price=lambda venue: (
        SimpleNamespace(quote=SimpleNamespace(mid=Decimal(2000))) if venue == "bybit" else None
    ))
    report = await calculator(
        accounts=manager([wallet("trade", cNGN=1000)]), price="0", aggregator=aggregator,
    ).calculate()
    assert report.total_usd_value == Decimal("0.5")
    assert report.cngn_share == 1
