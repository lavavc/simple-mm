from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.api.schemas import Position
from engine.market.portfolio_exposure import PortfolioExposureCalculator
from engine.market.portfolio_registry import DEFAULT_PORTFOLIO_SOURCE_REGISTRY
from tests.fakes import FakeDexAdapter


def test_portfolio_exposure_module_imports_in_fresh_process():
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import engine.market.portfolio_exposure; print('ok')",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


@pytest.mark.asyncio
async def test_portfolio_exposure_aggregates_registered_sources_only():
    lp_venue = FakeDexAdapter(name="uni-base")
    lp_venue.get_position = AsyncMock(
        return_value=Position(
            venue="uni-base",
            pair="cNGN/USDC",
            timestamp=0,
            balances={"cngn": Decimal("1000"), "usdc": Decimal("25"), "usdt": Decimal("0")},
            position_value_usd=Decimal("25.7"),
        )
    )
    quidax_venue = SimpleNamespace(
        get_position=AsyncMock(
            return_value=Position(
                venue="quidax",
                pair="CNGN/USDT",
                timestamp=0,
                balances={"cngn": Decimal("500"), "usdt": Decimal("20"), "usdc": Decimal("0")},
            )
        )
    )
    quidax_lp_venue = SimpleNamespace(
        get_position=AsyncMock(
            return_value=Position(
                venue="quidax-lp",
                pair="CNGN/USDT",
                timestamp=0,
                balances={"cngn": Decimal("777"), "usdt": Decimal("33"), "usdc": Decimal("0")},
            )
        )
    )
    blockradar_venue = SimpleNamespace(
        get_position=AsyncMock(
            return_value=Position(
                venue="blockradar",
                pair="cNGN/*",
                timestamp=0,
                balances={"cngn": Decimal("999999"), "usdt": Decimal("999999"), "usdc": Decimal("999999")},
            )
        )
    )
    account_manager = SimpleNamespace(
        check_all_balances=AsyncMock(
            return_value=[
                SimpleNamespace(role="uni-base-lp", token_balances={"cNGN": Decimal("50"), "USDC": Decimal("5")}),
                SimpleNamespace(role="uni-base-trade", token_balances={"cNGN": Decimal("10"), "USDC": Decimal("2")}),
                SimpleNamespace(role="quidax-trade-fund", token_balances={"cNGN": Decimal("20"), "USDT": Decimal("3")}),
                SimpleNamespace(role="blockradar", token_balances={"cNGN": Decimal("30"), "USDC": Decimal("4")}),
            ]
        )
    )
    blended_calculator = SimpleNamespace(
        get_blended_price=AsyncMock(
            return_value=SimpleNamespace(
                vwap=Decimal("0.0007"),
                twap_5m=Decimal("0.00069"),
                twap_1h=Decimal("0.00068"),
            )
        )
    )

    calculator = PortfolioExposureCalculator(
        venues={"uni-base": lp_venue, "quidax": quidax_venue, "blockradar": blockradar_venue},
        account_manager=account_manager,
        token_contracts={},
        blended_calculator=blended_calculator,
        portfolio_source_registry=DEFAULT_PORTFOLIO_SOURCE_REGISTRY,
    )
    calculator.venues["quidax-lp"] = quidax_lp_venue

    exposure = await calculator.calculate()

    assert exposure.total_cngn == Decimal("1610")
    assert exposure.total_usdt == Decimal("23")
    assert exposure.total_usdc == Decimal("36")
    assert exposure.total_usd_value == Decimal("60.1270")
    assert [source.source for source in exposure.sources] == [
        "uni-base-lp",
        "uni-base-trade",
        "quidax-trade-fund",
        "blockradar",
        "uni-base",
        "quidax",
    ]
    assert [source.kind for source in exposure.sources] == [
        "account",
        "account",
        "account",
        "account",
        "lp_position",
        "exchange",
    ]
    blockradar_venue.get_position.assert_not_called()
    quidax_lp_venue.get_position.assert_not_called()


@pytest.mark.asyncio
async def test_portfolio_exposure_ignores_negative_balance_sentinels():
    account_manager = SimpleNamespace(
        check_all_balances=AsyncMock(
            return_value=[
                SimpleNamespace(role="uni-base-trade", token_balances={"cNGN": Decimal("-1"), "USDC": Decimal("2")})
            ]
        )
    )
    blended_calculator = SimpleNamespace(
        get_blended_price=AsyncMock(
            return_value=SimpleNamespace(
                vwap=Decimal("0.0007"),
                twap_5m=Decimal("0.00069"),
                twap_1h=Decimal("0.00068"),
            )
        )
    )
    calculator = PortfolioExposureCalculator(
        venues={},
        account_manager=account_manager,
        token_contracts={},
        blended_calculator=blended_calculator,
        portfolio_source_registry=DEFAULT_PORTFOLIO_SOURCE_REGISTRY,
    )

    exposure = await calculator.calculate()

    assert exposure.total_cngn == Decimal("0")
    assert exposure.total_usdc == Decimal("2")
    assert exposure.total_usd_value == Decimal("2")
