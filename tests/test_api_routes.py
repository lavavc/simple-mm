"""API route tests for runtime-based dependency resolution."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import engine.api as api_module
from engine.api import api_router
from engine.api.schemas import ArbitrageOpportunity
from engine.runtime import EngineRuntime


class _DummyVenue:
    enabled = True
    paused = False
    params = None

    async def get_position(self):
        return None


def _make_runtime() -> EngineRuntime:
    db = SimpleNamespace(
        system_state=SimpleNamespace(get_system_state=AsyncMock(return_value="true")),
        arbitrage=SimpleNamespace(get_arbitrage_opportunity=AsyncMock(return_value=None)),
    )
    scheduler = SimpleNamespace(trading_enabled=True, broadcast=MagicMock())
    return EngineRuntime(
        db=db,
        scheduler=scheduler,
        venues={"quidax": _DummyVenue()},
        price_aggregator=SimpleNamespace(get_all_prices=lambda: {}, last_fetch_time=123.0),
        start_time=100.0,
        arbitrage_engine=SimpleNamespace(enabled=False),
        account_manager=None,
        token_contracts={},
        blended_calculator=None,
        normalizer=None,
        quidax_lp=None,
    )


def _make_app(runtime: EngineRuntime | None) -> FastAPI:
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    if runtime is not None:
        app.state.runtime = runtime
    return app


def test_api_router_is_the_canonical_public_import():
    assert api_module.api_router is api_router


def test_health_route_reads_runtime_state():
    runtime = _make_runtime()
    app = _make_app(runtime)

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["trading_enabled"] is True
    assert response.json()["arbitrage_enabled"] is False


def test_status_route_reads_db_and_runtime_services():
    runtime = _make_runtime()
    app = _make_app(runtime)

    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert body["trading_enabled"] is True
    assert body["last_price_update"] == 123000
    assert body["venues"][0]["name"] == "quidax"
    runtime.db.system_state.get_system_state.assert_awaited_once_with("trading_enabled")


def test_global_position_uses_historical_blended_fallback_when_vwap_is_zero():
    runtime = _make_runtime()
    runtime.venues = {
        "quidax": SimpleNamespace(
            enabled=True,
            paused=False,
            params=None,
            get_position=AsyncMock(
                return_value=SimpleNamespace(
                    balances={"cngn": Decimal("1000"), "usdt": Decimal("50"), "usdc": Decimal("0")}
                )
            ),
        )
    }
    runtime.blended_calculator = SimpleNamespace(
        get_blended_price=AsyncMock(
            return_value=SimpleNamespace(
                vwap=Decimal("0"),
                twap_5m=Decimal("0.0007"),
                twap_1h=Decimal("0.00069"),
            )
        )
    )
    app = _make_app(runtime)

    with TestClient(app) as client:
        response = client.get("/api/positions/global")

    assert response.status_code == 200
    body = response.json()
    assert body["total_usd_value"] == "50.7000"
    assert body["delta_ratio"] != "0"


def test_arbitrage_opportunity_route_uses_direct_lookup():
    runtime = _make_runtime()
    opp = ArbitrageOpportunity(
        id="opp-123",
        timestamp=100,
        buy_venue="quidax",
        sell_venue="uni-bsc",
        buy_price=Decimal("0.00061"),
        sell_price=Decimal("0.00071"),
        gross_spread_bps=164,
        net_spread_bps=92,
        recommended_size_usd=Decimal("500"),
        expected_profit_usd=Decimal("4.50"),
        status="detected",
    )
    runtime.db.arbitrage = SimpleNamespace(get_arbitrage_opportunity=AsyncMock(return_value=opp))
    app = _make_app(runtime)

    with TestClient(app) as client:
        response = client.get("/api/arbitrage/opportunities/opp-123")

    assert response.status_code == 200
    assert response.json()["id"] == "opp-123"
    runtime.db.arbitrage.get_arbitrage_opportunity.assert_awaited_once_with("opp-123")


def test_missing_runtime_returns_503():
    app = _make_app(None)

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["detail"] == "Engine runtime not configured"
