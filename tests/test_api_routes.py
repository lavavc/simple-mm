"""API route tests for runtime-based dependency resolution."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import engine.api as api_module
from engine.api import api_router
from engine.api.routes import venues as venue_routes
from engine.api.schemas import ArbitrageOpportunity
from engine.config import DexParams
from engine.runtime import EngineRuntime


class _DummyVenue:
    enabled = True
    paused = False
    params = None

    async def get_position(self):
        return None


class _DummyLpVenue(_DummyVenue):
    def __init__(self, params: DexParams):
        self.params = params


def _make_runtime() -> EngineRuntime:
    db = SimpleNamespace(
        system_state=SimpleNamespace(get_system_state=AsyncMock(return_value="true")),
        arbitrage=SimpleNamespace(get_arbitrage_opportunity=AsyncMock(return_value=None)),
    )
    scheduler = SimpleNamespace(
        trading_enabled=True,
        broadcast=MagicMock(),
        pause=AsyncMock(),
        lp_rebalancer=SimpleNamespace(
            withdraw_positions=AsyncMock(return_value=[]),
            unwind_all_positions=AsyncMock(return_value={}),
        ),
    )
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
        portfolio_exposure_calculator=None,
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


def test_portfolio_exposure_includes_source_breakdown():
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
                vwap=Decimal("0.0007"),
                twap_5m=Decimal("0.00069"),
                twap_1h=Decimal("0.00068"),
            )
        )
    )
    app = _make_app(runtime)

    with TestClient(app) as client:
        response = client.get("/api/portfolio/exposure")

    assert response.status_code == 200
    body = response.json()
    assert body["total_usd_value"] == "50.7000"
    assert body["sources"][0]["source"] == "quidax"


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


@pytest.mark.asyncio
async def test_update_venue_params_persists_full_lp_params():
    runtime = _make_runtime()
    runtime.db.venue_config = SimpleNamespace(update_venue_config=AsyncMock())
    runtime.venues = {
        "uni-base": _DummyLpVenue(
            DexParams(
                sd_multiplier=Decimal("2.75"),
                min_tick_width=100,
                max_tick_width=1000,
                lookback_points=None,
                rebalance_threshold_percent=Decimal("10.0"),
                max_slippage_percent=Decimal("1.0"),
                downside_skew=Decimal("0.45"),
                ewma_lambda=Decimal("0.975"),
            )
        )
    }

    with patch.object(venue_routes, "V4LPAdapter", _DummyLpVenue):
        response = await venue_routes.update_venue_params(
            "uni-base",
            {"downside_skew": "0.55"},
            runtime=runtime,
            db=runtime.db,
        )

    assert response == {"venue": "uni-base", "params": {"downside_skew": "0.55"}}
    runtime.db.venue_config.update_venue_config.assert_awaited_once()
    venue_arg, saved_params = runtime.db.venue_config.update_venue_config.await_args.args
    assert venue_arg == "uni-base"
    assert saved_params["downside_skew"] == "0.55"
    assert DexParams(**saved_params).downside_skew == Decimal("0.55")
    assert set(saved_params) == {
        "sd_multiplier",
        "min_tick_width",
        "max_tick_width",
        "lookback_points",
        "rebalance_threshold_percent",
        "max_slippage_percent",
        "downside_skew",
        "ewma_lambda",
    }


@pytest.mark.asyncio
async def test_withdraw_route_uses_lp_rebalancer_path():
    runtime = _make_runtime()
    runtime.venues = {
        "uni-base": _DummyLpVenue(
            DexParams(
                sd_multiplier=Decimal("2.75"),
                min_tick_width=100,
                max_tick_width=1000,
                lookback_points=None,
                rebalance_threshold_percent=Decimal("10.0"),
                max_slippage_percent=Decimal("1.0"),
                downside_skew=Decimal("0.45"),
                ewma_lambda=Decimal("0.975"),
            )
        )
    }
    runtime.scheduler.lp_rebalancer.withdraw_positions = AsyncMock(
        return_value=[{"token_id": 1, "status": "confirmed", "hash": "0xabc", "error": None}]
    )
    runtime.venues["uni-base"].get_owned_positions = MagicMock(return_value=[1])

    with patch.object(venue_routes, "V4LPAdapter", _DummyLpVenue):
        response = await venue_routes.withdraw_venue_position(
            "uni-base",
            venue_routes.WithdrawRequest(to_address="0x0000000000000000000000000000000000000001"),
            runtime=runtime,
        )

    assert response["removed"][0]["token_id"] == 1
    runtime.scheduler.lp_rebalancer.withdraw_positions.assert_awaited_once()
    runtime.venues["uni-base"].get_owned_positions.assert_not_called()
    kwargs = runtime.scheduler.lp_rebalancer.withdraw_positions.await_args.kwargs
    assert kwargs["action_type"] == "manual_withdraw"
    assert kwargs["triggered_by"] == "api:withdraw"


@pytest.mark.asyncio
async def test_withdraw_route_does_not_short_circuit_on_stale_empty_positions_read():
    runtime = _make_runtime()
    runtime.venues = {
        "uni-base": _DummyLpVenue(
            DexParams(
                sd_multiplier=Decimal("2.75"),
                min_tick_width=100,
                max_tick_width=1000,
                lookback_points=None,
                rebalance_threshold_percent=Decimal("10.0"),
                max_slippage_percent=Decimal("1.0"),
                downside_skew=Decimal("0.45"),
                ewma_lambda=Decimal("0.975"),
            )
        )
    }
    runtime.venues["uni-base"].get_owned_positions = MagicMock(return_value=[])
    runtime.scheduler.lp_rebalancer.withdraw_positions = AsyncMock(
        return_value=[{"token_id": 7, "status": "confirmed", "hash": "0xdef", "error": None}]
    )

    with patch.object(venue_routes, "V4LPAdapter", _DummyLpVenue):
        response = await venue_routes.withdraw_venue_position(
            "uni-base",
            venue_routes.WithdrawRequest(to_address="0x0000000000000000000000000000000000000001"),
            runtime=runtime,
        )

    assert response["removed"][0]["token_id"] == 7
    runtime.scheduler.lp_rebalancer.withdraw_positions.assert_awaited_once()
    runtime.venues["uni-base"].get_owned_positions.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_unwind_pauses_and_uses_lp_rebalancer():
    from engine.api.routes import system as system_routes

    runtime = _make_runtime()
    runtime.venues = {
        "uni-base": _DummyLpVenue(
            DexParams(
                sd_multiplier=Decimal("2.75"),
                min_tick_width=100,
                max_tick_width=1000,
                lookback_points=None,
                rebalance_threshold_percent=Decimal("10.0"),
                max_slippage_percent=Decimal("1.0"),
                downside_skew=Decimal("0.45"),
                ewma_lambda=Decimal("0.975"),
            )
        )
    }
    runtime.scheduler.lp_rebalancer.unwind_all_positions = AsyncMock(
        return_value={"uni-base": [{"token_id": 1, "status": "confirmed", "hash": "0xabc", "error": None}]}
    )

    fake_loop = SimpleNamespace(call_later=MagicMock())

    with patch.object(system_routes, "V4LPAdapter", _DummyLpVenue), \
         patch("asyncio.get_event_loop", return_value=fake_loop):
        response = await system_routes.shutdown(unwind=True, runtime=runtime)

    assert response == {"status": "shutting_down", "unwind": True}
    runtime.scheduler.pause.assert_awaited_once()
    runtime.scheduler.lp_rebalancer.unwind_all_positions.assert_awaited_once()
