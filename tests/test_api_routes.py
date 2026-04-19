"""API route tests for runtime-based dependency resolution."""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import engine.api as api_module
import engine.api.deps as api_deps
from engine.api import api_router
from engine.api.routes import venues as venue_routes
from engine.config import DexParams
from engine.market.portfolio_registry import DEFAULT_PORTFOLIO_SOURCE_REGISTRY
from engine.runtime import EngineRuntime
from engine.types import CexParams, LPPosition, Position


class _DummyVenue:
    enabled = True
    paused = False
    params = None
    market = None

    async def get_position(self):
        return None


class _DummyLpVenue(_DummyVenue):
    def __init__(self, params: DexParams):
        self.params = params


class _DummyCexVenue(_DummyVenue):
    def __init__(self, params: CexParams):
        self.params = params


def _make_runtime() -> EngineRuntime:
    async def _get_system_state(key: str) -> str | None:
        return "true" if key == "trading_enabled" else None

    db = SimpleNamespace(
        system_state=SimpleNamespace(get_system_state=AsyncMock(side_effect=_get_system_state), set_system_state=AsyncMock()),
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
        portfolio_source_registry=DEFAULT_PORTFOLIO_SOURCE_REGISTRY,
        quidax_lp=None,
        lp_managers={},
    )


def _make_app(runtime: EngineRuntime | None) -> FastAPI:
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    if runtime is not None:
        app.state.runtime = runtime
    return app


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


@pytest.mark.asyncio
async def test_update_venue_params_persists_full_lp_params():
    runtime = _make_runtime()
    runtime.db.venue_config = SimpleNamespace(update_venue_config=AsyncMock())
    lp_venue = _DummyLpVenue(
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
    runtime.venues = {"uni-base": lp_venue}
    runtime.lp_managers = {"uni-base": lp_venue}

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
async def test_pause_venue_cancels_open_orders_when_supported():
    runtime = _make_runtime()
    venue = _DummyVenue()
    venue.cancel_all_orders = AsyncMock(return_value=2)
    runtime.venues = {"quidax": venue}

    response = await venue_routes.pause_venue("quidax", runtime=runtime)

    assert response == {"venue": "quidax", "paused": True, "cancelled_orders": 2}
    assert runtime.venues["quidax"].paused is True
    venue.cancel_all_orders.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_venue_triggers_sync_when_reference_price_available():
    runtime = _make_runtime()
    venue = _DummyVenue()
    venue.params = CexParams(anchor_source="quidax")
    venue.sync_order_ladder = AsyncMock()
    venue.get_position = AsyncMock()
    runtime.scheduler.market_jobs = SimpleNamespace(
        get_reference_price_ngn=AsyncMock(return_value=Decimal("1600"))
    )
    runtime.venues = {"quidax": venue}

    response = await venue_routes.resume_venue("quidax", runtime=runtime)

    assert response == {"venue": "quidax", "paused": False, "sync_triggered": True}
    assert venue.paused is False
    venue.sync_order_ladder.assert_awaited_once_with(Decimal("1600"))
    venue.get_position.assert_not_awaited()
    runtime.scheduler.market_jobs.get_reference_price_ngn.assert_awaited_once_with(anchor_source="quidax")


@pytest.mark.asyncio
async def test_resume_venue_falls_back_to_position_when_reference_price_unavailable():
    runtime = _make_runtime()
    venue = _DummyVenue()
    venue.sync_order_ladder = AsyncMock()
    venue.get_position = AsyncMock()
    runtime.scheduler.market_jobs = SimpleNamespace(
        get_reference_price_ngn=AsyncMock(return_value=None)
    )
    runtime.venues = {"quidax": venue}

    response = await venue_routes.resume_venue("quidax", runtime=runtime)

    assert response == {"venue": "quidax", "paused": False, "sync_triggered": False}
    assert venue.paused is False
    venue.sync_order_ladder.assert_not_awaited()
    venue.get_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_venue_skips_auto_sync_when_global_trading_is_paused():
    runtime = _make_runtime()
    runtime.scheduler.trading_enabled = False
    venue = _DummyVenue()
    venue.sync_order_ladder = AsyncMock()
    venue.get_position = AsyncMock()
    runtime.venues = {"quidax": venue}

    response = await venue_routes.resume_venue("quidax", runtime=runtime)

    assert response == {
        "venue": "quidax",
        "paused": False,
        "sync_triggered": False,
        "sync_skipped": "trading_paused",
    }
    assert venue.paused is False
    venue.sync_order_ladder.assert_not_awaited()
    venue.get_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_venue_reports_sync_failure_without_repausing_venue():
    runtime = _make_runtime()
    venue = _DummyVenue()
    venue.sync_order_ladder = AsyncMock(side_effect=RuntimeError("boom"))
    venue.get_position = AsyncMock()
    runtime.scheduler.market_jobs = SimpleNamespace(
        get_reference_price_ngn=AsyncMock(return_value=Decimal("1600"))
    )
    runtime.venues = {"quidax": venue}

    response = await venue_routes.resume_venue("quidax", runtime=runtime)

    assert response == {
        "venue": "quidax",
        "paused": False,
        "sync_triggered": False,
        "sync_error": "boom",
    }
    assert venue.paused is False


def test_get_venue_orders_http_requires_token(monkeypatch):
    runtime = _make_runtime()
    venue = _DummyVenue()
    venue.market = "usdtcngn"
    venue.get_open_order_summaries = AsyncMock(return_value=[])
    runtime.venues = {"quidax": venue}
    app = _make_app(runtime)
    monkeypatch.setattr(api_deps.settings, "engine_api_token", "test-token")

    with TestClient(app) as client:
        unauthenticated = client.get("/api/venues/quidax/orders")
        authenticated = client.get(
            "/api/venues/quidax/orders",
            headers={"Authorization": "Bearer test-token"},
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json()["count"] == 0


@pytest.mark.asyncio
async def test_withdraw_route_uses_lp_rebalancer_path():
    runtime = _make_runtime()
    lp_venue = _DummyLpVenue(
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
    runtime.venues = {"uni-base": lp_venue}
    runtime.lp_managers = {"uni-base": lp_venue}
    runtime.scheduler.lp_rebalancer.withdraw_positions = AsyncMock(
        return_value=[{"token_id": 1, "status": "confirmed", "hash": "0xabc", "error": None}]
    )
    lp_venue.get_owned_positions = MagicMock(return_value=[1])

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
    lp_venue = _DummyLpVenue(
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
    runtime.venues = {"uni-base": lp_venue}
    runtime.lp_managers = {"uni-base": lp_venue}
    lp_venue.get_owned_positions = MagicMock(return_value=[])
    runtime.scheduler.lp_rebalancer.withdraw_positions = AsyncMock(
        return_value=[{"token_id": 7, "status": "confirmed", "hash": "0xdef", "error": None}]
    )

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
    lp_venue = _DummyLpVenue(
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
    runtime.venues = {"uni-base": lp_venue}
    runtime.lp_managers = {"uni-base": lp_venue}
    runtime.scheduler.lp_rebalancer.unwind_all_positions = AsyncMock(
        return_value={
            "uni-base": [
                {"token_id": 1, "status": "confirmed", "hash": "0xabc", "error": None}
            ]
        }
    )

    fake_loop = SimpleNamespace(call_later=MagicMock())

    with patch("asyncio.get_event_loop", return_value=fake_loop):
        response = await system_routes.shutdown(unwind=True, runtime=runtime)

    assert response == {"status": "shutting_down", "unwind": True}
    runtime.scheduler.pause.assert_awaited_once()
    runtime.scheduler.lp_rebalancer.unwind_all_positions.assert_awaited_once()


def test_status_route_uses_lp_manager_position_not_venue_adapter():
    """The /status route must call lp_manager.get_position_as_schema(), not venue.get_position()."""

    lp_position = LPPosition(
        token_id="42",
        liquidity="1000000",
        range_min=Decimal("0.0005"),
        range_max=Decimal("0.0007"),
        in_range=True,
    )
    expected_position = Position(
        venue="uni-base",
        pair="cNGN/USDC",
        timestamp=0,
        balances={"cngn": Decimal("1000"), "usdc": Decimal("50")},
        lp_position=lp_position,
    )

    class _LpManagerWithPosition(_DummyLpVenue):
        get_position_as_schema = AsyncMock(return_value=expected_position)

    lp_mgr = _LpManagerWithPosition(DexParams(
        sd_multiplier=Decimal("2.75"),
        min_tick_width=100,
        max_tick_width=1000,
        lookback_points=None,
        rebalance_threshold_percent=Decimal("10.0"),
        max_slippage_percent=Decimal("1.0"),
        downside_skew=Decimal("0.45"),
        ewma_lambda=Decimal("0.975"),
    ))

    runtime = _make_runtime()
    runtime.venues = {"uni-base": lp_mgr}
    runtime.lp_managers = {"uni-base": lp_mgr}
    app = _make_app(runtime)

    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    lp_mgr.get_position_as_schema.assert_awaited_once()
    venue_status = next(v for v in response.json()["venues"] if v["name"] == "uni-base")
    assert venue_status["position"]["lp_position"]["token_id"] == "42"
    assert venue_status["position"]["lp_position"]["in_range"] is True


def test_status_route_returns_lp_manager_params_not_venue_params():
    """The /status route must return lp_manager.params, not venue.params."""
    from decimal import Decimal

    class _LpManagerWithPosition(_DummyLpVenue):
        get_position_as_schema = AsyncMock(return_value=None)

    lp_params = DexParams(
        sd_multiplier=Decimal("3.5"),
        min_tick_width=200,
        max_tick_width=2000,
        lookback_points=None,
        rebalance_threshold_percent=Decimal("5.0"),
        max_slippage_percent=Decimal("2.0"),
        downside_skew=Decimal("0.6"),
        ewma_lambda=Decimal("0.99"),
    )
    lp_mgr = _LpManagerWithPosition(lp_params)

    runtime = _make_runtime()
    runtime.venues = {"uni-base": lp_mgr}
    runtime.lp_managers = {"uni-base": lp_mgr}
    app = _make_app(runtime)

    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    venue_status = next(v for v in response.json()["venues"] if v["name"] == "uni-base")
    params = venue_status["params"]
    assert params["sd_multiplier"] == "3.5"
    assert params["downside_skew"] == "0.6"
    assert params["min_tick_width"] == 200


@pytest.mark.asyncio
async def test_restore_venue_params_rehydrates_lp_and_cex_configs():
    """startup restore_venue_params() reads DB and applies to both DexParams and CexParams."""
    from engine.main import restore_venue_params

    quidax = SimpleNamespace(params=CexParams())
    blockradar = SimpleNamespace()
    lp_manager = SimpleNamespace(
        params=DexParams(
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
    db = SimpleNamespace(
        venue_config=SimpleNamespace(
            get_venue_config=AsyncMock(
                side_effect=[
                    {
                        "venue": "uni-base",
                        "params": {
                            "sd_multiplier": "3.00",
                            "min_tick_width": 120,
                            "max_tick_width": 1100,
                            "lookback_points": None,
                            "rebalance_threshold_percent": "11.0",
                            "max_slippage_percent": "1.2",
                            "downside_skew": "0.55",
                            "ewma_lambda": "0.970",
                        },
                    },
                    {
                        "venue": "quidax",
                        "params": {
                            "ladder_enabled": True,
                            "spread_offset_ngn": 50,
                            "ladder_step_ngn": 1,
                            "ladder_levels_per_side": 5,
                            "anchor_source": "dex_vwap",
                            "anchor_requote_threshold_bps": 10,
                            "anchor_requote_cooldown_seconds": 30,
                            "order_size_cngn": "2000",
                            "order_size_usdt": "10",
                        },
                    },
                ]
            )
        )
    )

    await restore_venue_params(
        db,
        {"quidax": quidax, "blockradar": blockradar, "uni-base": lp_manager},
        {"uni-base": lp_manager},
    )

    assert lp_manager.params.sd_multiplier == Decimal("3.00")
    assert lp_manager.params.min_tick_width == 120
    assert quidax.params.ladder_enabled is True
    assert quidax.params.anchor_source == "dex_vwap"
    assert quidax.params.order_size_usdt == Decimal("10")
