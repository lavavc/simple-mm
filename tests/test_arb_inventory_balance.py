"""Inventory preference changes route ranking, never funding or monetary profit."""

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.arb.risk.balance_scoring import project_route
from engine.arb.risk.inventory import InventoryTracker
from engine.arb.routing.route_registry import ROUTES_BY_DIRECTION
from engine.arb.routing.router import RouteCandidate, select_route
from engine.arb.wallet_state import refresh_inventory_for_venues
from engine.types import ArbitrageParams, TradeInventorySnapshot


def snapshot(stable, cngn, *, locked_stable=0, locked_cngn=0, age=0):
    stable, cngn = Decimal(str(stable)), Decimal(str(cngn))
    return TradeInventorySnapshot(
        stable,
        cngn,
        stable + Decimal(str(locked_stable)),
        cngn + Decimal(str(locked_cngn)),
        int(time.time() * 1000) - age * 1000,
    )


def inventory(qx=(900, 100), base=(100, 900), **params):
    tracker = InventoryTracker(ArbitrageParams(**params))
    tracker.set_account_snapshot("quidax", snapshot(*qx))
    tracker.set_account_snapshot("uni-base", snapshot(*base))
    return tracker


def candidate(direction="QUIDAX_TO_UNI_BASE", *, profit="1", size="100"):
    route = ROUTES_BY_DIRECTION[direction]
    now = int(time.time() * 1000)
    return RouteCandidate(
        direction,
        route.buy_leg.venue,
        route.sell_leg.venue,
        Decimal(size),
        Decimal(profit),
        Decimal("0.1"),
        {
            "timestamp": now,
            "price_timestamps": {"quidax": now, "uni-base": now},
            "prices": {"quidax": "1", "uni-base": "1"},
            "optimal_arb": {"cngn_transferred": size},
            "depth": {"quidax": object()},
        },
    )


@pytest.fixture(autouse=True)
def route_caps(monkeypatch):
    import engine.arb.routing.router as router

    monkeypatch.setattr(router, "estimate_max_cex_buy_usd_for_cngn", lambda depth, amount: amount)
    monkeypatch.setattr(
        router,
        "estimate_max_cex_dex_buy_usd_for_cngn",
        lambda direction, depth, amount: {
            "optimal_size_usd": amount,
            "cngn_transferred": amount,
            "expected_profit_usd": 2,
        },
    )


def test_lower_profit_balancing_route_beats_worsening_route():
    healing = candidate(profit="1")
    worsening = candidate("UNI_BASE_TO_QUIDAX", profit="2")
    chosen = select_route([worsening, healing], inventory())
    assert chosen.candidate is healing
    assert chosen.net_profit_usd == Decimal("0.9")
    assert chosen.inventory_improvement_usd > 0
    assert chosen.selection_score > chosen.net_profit_usd


def test_reversing_account_imbalances_reverses_preference():
    qx_to_base = candidate(profit="2")
    base_to_qx = candidate("UNI_BASE_TO_QUIDAX", profit="1")
    chosen = select_route([qx_to_base, base_to_qx], inventory(qx=(100, 900), base=(900, 100)))
    assert chosen.candidate is base_to_qx


def test_zero_weight_selects_highest_monetary_profit():
    healing = candidate(profit="1")
    worsening = candidate("UNI_BASE_TO_QUIDAX", profit="2")
    chosen = select_route([healing, worsening], inventory(inventory_weight=Decimal("0")))
    assert chosen.candidate is worsening
    assert chosen.selection_score == chosen.net_profit_usd


def test_balancing_credit_cannot_make_loss_eligible():
    assert select_route([candidate(profit="0.05")], inventory(inventory_weight=100)) is None


def test_overshooting_target_loses_inventory_credit():
    inv = inventory(qx=(550, 450), base=(450, 550))
    prices = {"quidax": Decimal("1"), "uni-base": Decimal("1")}
    balanced = project_route(
        inv, "quidax", "uni-base", prices, Decimal(50), Decimal(50), Decimal(50)
    )
    overshot = project_route(
        inv, "quidax", "uni-base", prices, Decimal(200), Decimal(200), Decimal(200)
    )
    assert sum(p.improvement_usd for p in balanced) > 0
    assert sum(p.improvement_usd for p in overshot) < 0


def test_opposite_account_imbalances_do_not_net_out():
    inv = inventory()
    projections = project_route(
        inv,
        "quidax",
        "uni-base",
        {"quidax": Decimal(1), "uni-base": Decimal(1)},
        Decimal(100),
        Decimal(100),
        Decimal(100),
    )
    assert all(p.improvement_usd > 0 for p in projections)


def test_improvement_at_one_account_is_offset_by_worsening_at_the_other():
    inv = inventory(base=(500, 500))
    projections = project_route(
        inv,
        "quidax",
        "uni-base",
        {"quidax": Decimal(1), "uni-base": Decimal(1)},
        Decimal(100),
        Decimal(100),
        Decimal(100),
    )
    assert projections[0].improvement_usd > 0
    assert projections[1].improvement_usd < 0
    assert 0 < sum(p.improvement_usd for p in projections) < projections[0].improvement_usd


def test_balanced_accounts_do_not_reward_moving_away():
    chosen = select_route([candidate()], inventory(qx=(500, 500), base=(500, 500)))
    assert chosen.inventory_improvement_usd < 0


@pytest.mark.parametrize("bad_data", ["balances", "signal", "price", "missing", "unknown_price"])
def test_unknown_or_stale_inputs_block_route(bad_data):
    inv, route = inventory(), candidate()
    if bad_data == "balances":
        inv.set_account_snapshot("quidax", snapshot(900, 100, age=31))
    elif bad_data == "signal":
        route.signal["timestamp"] -= 31000
    elif bad_data == "price":
        route.signal["price_timestamps"]["quidax"] -= 31000
    elif bad_data == "unknown_price":
        route.signal["prices"].pop("quidax")
    else:
        inv.invalidate_snapshots("quidax")
    assert select_route([route], inv) is None


def test_locked_funds_count_as_ownership_but_not_spendable(monkeypatch):
    import engine.arb.routing.router as router

    inv = inventory()
    inv.set_account_snapshot("quidax", snapshot(20, 100, locked_stable=880))
    observed = []

    def estimate(direction, depth, size):
        observed.append(size)
        return {"expected_profit_usd": Decimal(1), "cngn_transferred": size}

    monkeypatch.setattr(router, "estimate_cex_dex_trade", estimate)
    chosen = select_route([candidate()], inv)
    assert chosen.adjusted_size_usd == 20
    assert chosen.inventory_improvement_usd > 0
    assert observed == [Decimal(20)]


def test_balance_error_invalidates_projection_without_other_asset_mutation():
    inv = inventory()
    inv.reconcile_cngn({"quidax": Decimal(0)})
    assert inv.fresh_snapshot("quidax") is None
    assert inv.state.per_account_stable["quidax"] == 900


def test_exposure_limit_allows_correction_but_blocks_worsening():
    inv = inventory(qx=(19000, 1000), base=(1000, 19000), max_inventory_imbalance_usd=1000)
    assert select_route([candidate()], inv) is not None
    assert select_route([candidate("UNI_BASE_TO_QUIDAX")], inv) is None


def test_custom_targets_apply_to_individual_accounts():
    inv = inventory(
        qx=(900, 100),
        base=(900, 100),
        quidax_target_cngn_ratio=Decimal("0.1"),
        uni_base_target_cngn_ratio=Decimal("0.1"),
    )
    result = select_route([candidate()], inv)
    assert result.inventory_improvement_usd < 0


@pytest.mark.asyncio
async def test_inventory_refresh_never_reads_lp_account():
    inv = inventory()
    qx = SimpleNamespace(get_trade_inventory=AsyncMock(return_value=snapshot(500, 500)))
    lp = SimpleNamespace(get_trade_inventory=AsyncMock())
    engine = SimpleNamespace(venues={"quidax": qx, "quidax-lp": lp}, inventory=inv)
    await refresh_inventory_for_venues(engine, "quidax", "quidax-lp")
    qx.get_trade_inventory.assert_awaited_once()
    lp.get_trade_inventory.assert_not_awaited()
    assert inv.fresh_snapshot("quidax").stable_total == 500


@pytest.mark.asyncio
async def test_failed_balance_refresh_invalidates_previous_snapshot():
    inv = inventory()
    engine = SimpleNamespace(
        venues={
            "quidax": SimpleNamespace(
                get_trade_inventory=AsyncMock(side_effect=RuntimeError("offline"))
            )
        },
        inventory=inv,
    )
    await refresh_inventory_for_venues(engine, "quidax")
    assert inv.fresh_snapshot("quidax") is None


@pytest.mark.asyncio
async def test_engine_routes_and_records_selected_dex_direction(monkeypatch):
    import engine.arb.detection.dex_dex as detection
    import engine.arb.engine as engine_module
    from engine.arb.engine import ArbitrageEngine
    from engine.arb.routing.router import SelectedRoute

    directions = ["UNI_BASE_TO_UNI_BSC_DELTA_BALANCE", "UNI_BSC_TO_UNI_BASE_DELTA_BALANCE"]
    arbs = [
        {
            "direction": d,
            "optimal_size_usd": 100,
            "expected_profit_usd": 2 - i,
            "gas_usd": 0.1,
            "cngn_transferred": 100,
        }
        for i, d in enumerate(directions)
    ]
    signal = {"all_arbs": arbs, "optimal_arb": arbs[0], "prices": {}, "timestamp": 0}
    monkeypatch.setattr(detection, "find_optimal_dex_arb", lambda: signal)
    observed = []

    def choose(candidates, inventory):
        observed.extend(candidates)
        selected = candidates[1]
        return SelectedRoute(
            selected, Decimal(50), Decimal("0.9"), Decimal(1), Decimal(10), Decimal("1.1")
        )

    monkeypatch.setattr(engine_module, "select_route", choose)
    engine = ArbitrageEngine.__new__(ArbitrageEngine)
    engine._inventory_seeded = engine._trade_approvals_seeded = engine._enabled = True
    engine.execute_dex_dex_enabled = True
    engine._arb_executing = False
    engine._dex_curve_task = None
    engine.inventory = inventory()
    engine._ensure_route_inventory = AsyncMock()
    engine._ensure_pool_marks = AsyncMock()
    engine._record_dex_opportunity = AsyncMock(return_value="selected-opportunity")
    engine._execute_route = AsyncMock()
    engine._broadcast_dex_curve = AsyncMock()

    await engine.on_dex_dex_update()
    assert engine._arb_executing is True
    assert {c.direction for c in observed} == set(directions)
    assert (
        engine._record_dex_opportunity.await_args.args[0]["optimal_arb"]["direction"]
        == directions[1]
    )
    await asyncio.sleep(0)
    assert engine._execute_route.await_args.args[0].direction == directions[1]
    assert engine._execute_route.await_args.args[2] == "selected-opportunity"
    await engine.on_dex_dex_update()
    await asyncio.sleep(0)
    engine._execute_route.assert_awaited_once()


@pytest.mark.asyncio
async def test_global_lp_exposure_is_not_sent_to_arb_risk():
    from engine.scheduler.jobs.positions import PositionJobs

    from tests.test_portfolio_exposure import calculator, manager, wallet

    global_exposure = await calculator(accounts=manager([wallet("lp", cNGN=1000)])).calculate()
    arb = SimpleNamespace(update_portfolio_snapshot=MagicMock())
    context = SimpleNamespace(
        portfolio_exposure_calculator=SimpleNamespace(
            calculate=AsyncMock(return_value=global_exposure)
        ),
        arbitrage_engine=arb,
        broadcast=MagicMock(),
    )
    await PositionJobs(context, SimpleNamespace()).broadcast_portfolio_exposure()
    arb.update_portfolio_snapshot.assert_not_called()
    assert context.broadcast.call_args.args[0]["type"] == "portfolio_exposure"
