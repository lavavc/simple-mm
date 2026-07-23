from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

import research.backtester.portfolio_simulator as portfolio_simulator
from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import V4Event
from research.backtester.entry_eligibility import (
    AlwaysEligibleOverlay,
    EntryEligibilityDecision,
)
from research.backtester.params import BacktestParams, TransactionCostModel
from research.backtester.pool_state import PoolState
from research.backtester.portfolio_allocation import Allocation
from research.backtester.portfolio_catalog import (
    SleeveAlias,
    SleeveDefinition,
    parameter_fingerprint,
)
from research.backtester.portfolio_path import NoValidationSwapError
from research.backtester.portfolio_simulator import (
    AGGREGATE_LIQUIDITY_SHARE_CAP,
    ExecutionAccountingError,
    LiquidityShareExceeded,
    _aggregate_variable_cost,
    _net_actions,
    _plan_affordable_actions,
    _propose_actions,
    _settle_actions,
    _validated_aggregate_share,
    _variable_cost,
    simulate_portfolio,
)
from research.backtester.position_runtime import SleeveAction, create_sleeve_runtime
from research.backtester.simulator import (
    UNISWAP_BASE_POOL,
    PortfolioComposition,
    TransactionCostBreakdown,
    _exit_cost_breakdown,
    _portfolio_value,
    _raw_amounts_to_wallet,
    _swap_cost_breakdown,
)
from research.backtester.sizing import EntryContext, FixedDeployment


class NeverEligibleOverlay:
    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
        return EntryEligibilityDecision(False, "disagreement")


def test_aggregate_liquidity_depth_overflow_fails_closed() -> None:
    with pytest.raises(ExecutionAccountingError, match="liquidity depth"):
        _validated_aggregate_share(1e308, 10**308)


class RaisingEligibilityOverlay:
    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
        raise AssertionError("zero-weight overlay must not be evaluated")


class SingleUseEligibilityOverlay:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("entry eligibility must be frozen before bisection")
        return EntryEligibilityDecision(True, "approved")


def _params(
    *, max_share: float | None = None, unwind_to_cash_on_exit: bool = False
) -> BacktestParams:
    return BacktestParams(
        strategy_mode="static",
        range_mode="fixed_tick_width",
        center_mode="spot",
        fixed_tick_width=100,
        max_active_liquidity_share=max_share,
        gas_cost_usd=0.0,
        transaction_costs=replace(
            TransactionCostModel(),
            mint_gas_usd=0.0,
            remove_gas_usd=0.0,
            fallback_price_impact_bps=0.0,
            unwind_to_cash_on_exit=unwind_to_cash_on_exit,
        ),
    )


def _sleeve(sleeve_id: str, params: BacktestParams) -> SleeveDefinition:
    payload = json.dumps(asdict(params), allow_nan=False, separators=(",", ":"), sort_keys=True)
    return SleeveDefinition(
        sleeve_id=sleeve_id,
        family="static",
        config_name=sleeve_id,
        parameter_payload=payload,
        parameter_fingerprint=parameter_fingerprint(params),
        source_constructor="test",
        aliases=(SleeveAlias("static", sleeve_id, "test"),),
    )


def _swap(minute: int, *, liquidity: int = 10**15, amount_usd: float = 1_000.0) -> V4Event:
    return V4Event(
        block_time=datetime(2026, 1, 1) + timedelta(minutes=minute),
        chain="base",
        pool_id=UNISWAP_BASE_POOL.pool_address,
        event_type="swap",
        tx_hash=f"0x{minute:064x}",
        log_index=0,
        block_number=100 + minute,
        sqrt_price_x96=tick_to_sqrt_price_x96(0),
        tick=0,
        active_liquidity=liquidity,
        fee_rate=UNISWAP_BASE_POOL.fee_rate,
        amount0=-amount_usd,
        amount1=amount_usd,
        amount_usd=amount_usd,
        cngn_usd_price=1.0,
        token0_symbol="cNGN",
        token1_symbol="USDC",
    )


def test_overlapping_positions_split_one_fee_pool() -> None:
    sleeves = (_sleeve("a", _params()), _sleeve("b", _params()))
    fee_swap = _swap(2)
    result = simulate_portfolio(
        events=[_swap(0), _swap(1), fee_swap],
        sleeves=sleeves,
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        settle_to_cash=False,
        initial_pool_state=PoolState(),
    )

    l_a = result.attribution["a"].liquidity_at(fee_swap.block_time)
    l_b = result.attribution["b"].liquidity_at(fee_swap.block_time)
    total_fee = fee_swap.amount_usd * fee_swap.fee_rate
    denominator = fee_swap.active_liquidity + l_a + l_b
    assert result.attribution["a"].fees_usd == pytest.approx(total_fee * l_a / denominator)
    assert result.attribution["b"].fees_usd == pytest.approx(total_fee * l_b / denominator)
    assert result.total_fees <= total_fee


def test_always_eligible_overlay_matches_legacy_portfolio_exactly() -> None:
    sleeves = (_sleeve("a", _params()), _sleeve("b", _params()))
    allocation = Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8)
    kwargs = {
        "events": [_swap(0), _swap(1), _swap(2)],
        "sleeves": sleeves,
        "allocation": allocation,
        "pool_config": UNISWAP_BASE_POOL,
        "bankroll_usd": 500.0,
        "settle_to_cash": False,
        "initial_pool_state": PoolState(),
    }

    legacy = simulate_portfolio(**kwargs)
    empty = simulate_portfolio(**kwargs, entry_overlays_by_sleeve={})
    partial = simulate_portfolio(
        **kwargs,
        entry_overlays_by_sleeve={"a": AlwaysEligibleOverlay()},
    )
    full = simulate_portfolio(
        **kwargs,
        entry_overlays_by_sleeve={
            "a": AlwaysEligibleOverlay(),
            "b": AlwaysEligibleOverlay(),
        },
    )

    assert legacy.attribution["a"].liquidity_samples[-1][1] > 0
    assert empty == legacy
    assert partial == legacy
    assert full == legacy


def test_overlay_is_forwarded_only_to_matching_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeves = (_sleeve("a", _params()), _sleeve("b", _params()))
    overlay = AlwaysEligibleOverlay()
    factory = Mock(wraps=create_sleeve_runtime)
    monkeypatch.setattr(portfolio_simulator, "create_sleeve_runtime", factory)

    simulate_portfolio(
        events=[],
        sleeves=sleeves,
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        settle_to_cash=False,
        entry_overlays_by_sleeve={"a": overlay},
    )

    forwarded = {
        call.kwargs["sleeve_id"]: call.kwargs["entry_eligibility"]
        for call in factory.call_args_list
    }
    assert forwarded == {"a": overlay, "b": None}


def test_denied_sleeve_keeps_budget_without_redistribution() -> None:
    sleeves = (_sleeve("a", _params()), _sleeve("b", _params()))
    events = [_swap(0), _swap(1), _swap(2)]

    result = simulate_portfolio(
        events=events,
        sleeves=sleeves,
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        settle_to_cash=False,
        initial_pool_state=PoolState(),
        entry_overlays_by_sleeve={"a": NeverEligibleOverlay()},
    )

    assert result.attribution["a"].capital_budget_usd == 50.0
    assert result.attribution["a"].final_value == 50.0
    assert result.attribution["a"].liquidity_at(events[-1].block_time) == 0.0
    assert result.attribution["b"].capital_budget_usd == 50.0
    assert result.attribution["b"].liquidity_at(events[-1].block_time) > 0.0
    assert result.cash_value == 400.0


def test_unknown_overlay_ids_are_rejected_in_sorted_order() -> None:
    with pytest.raises(
        ValueError,
        match=r"entry overlays reference unknown sleeves: \['c', 'z'\]",
    ):
        simulate_portfolio(
            events=[],
            sleeves=(_sleeve("a", _params()),),
            allocation=Allocation("equal_config", {"a": 0.1}, 0.9),
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=500.0,
            settle_to_cash=False,
            entry_overlays_by_sleeve={
                "z": AlwaysEligibleOverlay(),
                "c": AlwaysEligibleOverlay(),
            },
        )


def test_unknown_allocation_validation_precedes_unknown_overlay_validation() -> None:
    with pytest.raises(
        ValueError,
        match=r"allocation references unknown sleeves: \['allocation-missing'\]",
    ):
        simulate_portfolio(
            events=[],
            sleeves=(_sleeve("a", _params()),),
            allocation=Allocation(
                "equal_config",
                {"allocation-missing": 0.1},
                0.9,
            ),
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=500.0,
            settle_to_cash=False,
            entry_overlays_by_sleeve={"overlay-missing": AlwaysEligibleOverlay()},
        )


def test_valid_zero_weight_overlay_is_inert() -> None:
    result = simulate_portfolio(
        events=[_swap(0), _swap(1)],
        sleeves=(_sleeve("a", _params()), _sleeve("b", _params())),
        allocation=Allocation("equal_config", {"a": 0.1}, 0.9),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        settle_to_cash=False,
        entry_overlays_by_sleeve={"b": RaisingEligibilityOverlay()},
    )

    assert set(result.attribution) == {"a"}


def test_opposing_inventory_actions_are_netted() -> None:
    event = _swap(2)
    common = dict(
        kind="enter",
        event=event,
        active_liquidity=event.active_liquidity,
        cngn_usd_price=1.0,
        wallet_before=PortfolioComposition(100.0, 100.0),
        wallet_after=PortfolioComposition(100.0, 100.0),
        position_before=None,
        position_after=None,
        transaction_cost=TransactionCostBreakdown("enter"),
        reason=None,
        variable_cost_asset=None,
    )
    actions = [
        SleeveAction(
            sleeve_id="buy",
            inventory_swap_direction="stable_to_cngn",
            inventory_swap_notional_usd=100.0,
            **common,
        ),
        SleeveAction(
            sleeve_id="sell",
            inventory_swap_direction="cngn_to_stable",
            inventory_swap_notional_usd=60.0,
            **common,
        ),
    ]

    execution = _net_actions(actions)

    assert execution.external_notional_usd == pytest.approx(40.0)
    assert execution.internal_notional_usd == pytest.approx(60.0)
    assert execution.direction == "stable_to_cngn"
    assert execution.signed_notional_by_leg == {
        ("buy", "enter"): 100.0,
        ("sell", "enter"): -60.0,
    }
    assert execution.external_notional_by_leg == {
        ("buy", "enter"): 40.0,
        ("sell", "enter"): 0.0,
    }

    mixed = _net_actions([actions[0], replace(actions[1], kind="exit")])
    assert mixed.signed_notional_by_leg == {
        ("buy", "enter"): 100.0,
        ("sell", "exit"): -60.0,
    }


@pytest.mark.parametrize("notional", [float("nan"), float("inf"), -1.0])
def test_inventory_netting_rejects_non_finite_or_negative_notional(
    notional: float,
) -> None:
    event = _swap(0)
    action = SleeveAction(
        sleeve_id="invalid",
        kind="enter",
        event=event,
        active_liquidity=event.active_liquidity,
        cngn_usd_price=1.0,
        wallet_before=PortfolioComposition(100.0, 0.0),
        wallet_after=PortfolioComposition(100.0, 0.0),
        position_before=None,
        position_after=None,
        transaction_cost=TransactionCostBreakdown("enter"),
        reason=None,
        variable_cost_asset=None,
        inventory_swap_direction="stable_to_cngn",
        inventory_swap_notional_usd=notional,
    )

    with pytest.raises(ExecutionAccountingError, match="inventory notional"):
        _net_actions((action,))


def test_joint_cost_pricing_preserves_fractional_synthetic_liquidity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_event = _swap(0, liquidity=1_000)
    event = _swap(1, liquidity=1_000)
    runtime = create_sleeve_runtime(
        sleeve_id="fractional",
        params=_params(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=100.0,
    )
    runtime._observe_swap(first_event)
    runtime.previous_swap_time = first_event.block_time
    runtime._observe_swap(event)
    entry = runtime.propose_entry(event, event.active_liquidity)
    assert entry is not None and entry.position_after is not None
    runtime.position = replace(entry.position_after, liquidity_L=0.75)
    action = replace(
        entry,
        position_before=runtime.position,
        position_after=runtime.position,
        inventory_swap_notional_usd=10.0,
    )
    captured: list[float] = []

    def fake_cost(
        action_name: str,
        notional: float,
        active_liquidity: float,
        *args: object,
        **kwargs: object,
    ) -> TransactionCostBreakdown:
        captured.append(active_liquidity)
        return TransactionCostBreakdown(action_name, swap_notional_usd=notional)

    monkeypatch.setattr(portfolio_simulator, "_swap_cost_breakdown", fake_cost)
    execution = _net_actions((action,))

    _aggregate_variable_cost(
        actions=(action,),
        execution=execution,
        runtimes={"fractional": runtime},
        historical_liquidity=event.active_liquidity,
        pool_config=UNISWAP_BASE_POOL,
    )

    assert captured == [1000.75]


def test_fully_crossed_batch_rejects_divergent_runtime_marks() -> None:
    event = _swap(0)
    left = create_sleeve_runtime(
        sleeve_id="left",
        params=_params(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=100.0,
    )
    right = create_sleeve_runtime(
        sleeve_id="right",
        params=_params(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=100.0,
    )
    left._observe_swap(event)
    right._observe_swap(event)
    right.current_price *= 1.01
    common = {
        "kind": "enter",
        "event": event,
        "active_liquidity": event.active_liquidity,
        "wallet_before": PortfolioComposition(100.0, 0.0),
        "wallet_after": PortfolioComposition(100.0, 0.0),
        "position_before": None,
        "position_after": None,
        "transaction_cost": TransactionCostBreakdown("enter"),
        "reason": None,
        "variable_cost_asset": None,
        "inventory_swap_notional_usd": 10.0,
    }
    actions = (
        SleeveAction(
            sleeve_id="left",
            cngn_usd_price=left.current_price,
            inventory_swap_direction="stable_to_cngn",
            **common,
        ),
        SleeveAction(
            sleeve_id="right",
            cngn_usd_price=right.current_price,
            inventory_swap_direction="cngn_to_stable",
            **common,
        ),
    )

    with pytest.raises(ExecutionAccountingError, match="one observed pool state"):
        _settle_actions(
            actions,
            {"left": left, "right": right},
            event.active_liquidity,
            UNISWAP_BASE_POOL,
        )


def test_action_batch_is_frozen_before_mutation_and_exit_takes_precedence() -> None:
    event = _swap(2)
    common = dict(
        event=event,
        active_liquidity=event.active_liquidity,
        cngn_usd_price=1.0,
        wallet_before=PortfolioComposition(100.0, 0.0),
        wallet_after=PortfolioComposition(100.0, 0.0),
        position_before=None,
        position_after=None,
        transaction_cost=TransactionCostBreakdown("enter"),
        reason=None,
        variable_cost_asset=None,
    )
    exit_action = SleeveAction(sleeve_id="a", kind="exit", **common)
    entry_action = SleeveAction(sleeve_id="b", kind="enter", **common)
    exiting = Mock(sleeve_id="a")
    exiting.propose_exit.return_value = exit_action
    entering = Mock(sleeve_id="b")
    entering.propose_exit.return_value = None
    entering.propose_entry.return_value = entry_action

    actions = _propose_actions(
        {"b": entering, "a": exiting}, event, event.active_liquidity
    )

    assert actions == (exit_action, entry_action)
    exiting.propose_entry.assert_not_called()
    entering.propose_entry.assert_called_once_with(
        event,
        event.active_liquidity,
        deployment_scale=1.0,
    )


def test_real_opposing_entries_settle_without_changing_refund_denomination() -> None:
    event0 = replace(
        _swap(0, liquidity=10**12),
        tick=100,
        sqrt_price_x96=tick_to_sqrt_price_x96(100),
    )
    event1 = replace(
        _swap(1, liquidity=10**12),
        tick=100,
        sqrt_price_x96=tick_to_sqrt_price_x96(100),
    )
    buy = create_sleeve_runtime(
        sleeve_id="buy",
        params=_params(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=1_000.0,
    )
    sell = create_sleeve_runtime(
        sleeve_id="sell",
        params=_params(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=1_500.0,
    )
    sell.wallet = PortfolioComposition(stable_usd=0.0, cngn_amount=1_500.0)
    for runtime in (buy, sell):
        runtime._observe_swap(event0)
        runtime.previous_swap_time = event0.block_time
        runtime._observe_swap(event1)
    buy_action = buy.propose_entry(event1, event1.active_liquidity)
    sell_action = sell.propose_entry(event1, event1.active_liquidity)
    assert buy_action is not None
    assert sell_action is not None
    assert buy_action.inventory_swap_direction == "stable_to_cngn"
    assert sell_action.inventory_swap_direction == "cngn_to_stable"
    gross_buy = buy_action.inventory_swap_notional_usd
    gross_sell = sell_action.inventory_swap_notional_usd
    assert gross_sell > gross_buy
    original_sell_stable = sell_action.wallet_after.stable_usd
    original_sell_cngn = sell_action.wallet_after.cngn_amount
    residual = gross_sell - gross_buy
    expected_exact_output = _swap_cost_breakdown(
        "enter",
        residual,
        event1.active_liquidity,
        event1.fee_rate,
        sell.current_tick,
        sell.params,
        direction="cngn_to_stable",
        current_price=sell.current_price,
        current_sqrt_price_x96=sell.current_sqrt_price_x96,
        pool_config=UNISWAP_BASE_POOL,
        exact_output=True,
    )
    wrong_exact_input = _swap_cost_breakdown(
        "enter",
        residual,
        event1.active_liquidity,
        event1.fee_rate,
        sell.current_tick,
        sell.params,
        direction="cngn_to_stable",
        current_price=sell.current_price,
        current_sqrt_price_x96=sell.current_sqrt_price_x96,
        pool_config=UNISWAP_BASE_POOL,
        exact_output=False,
    )

    batch = _settle_actions(
        [buy_action, sell_action],
        {"buy": buy, "sell": sell},
        event1.active_liquidity,
        UNISWAP_BASE_POOL,
    )

    assert batch.external_marked_notional_usd == pytest.approx(
        abs(gross_buy - gross_sell)
    )
    assert batch.internal_cross_notional_usd == pytest.approx(
        min(gross_buy, gross_sell)
    )
    assert len(batch.costs) == 2
    assert sell.wallet.stable_usd == pytest.approx(original_sell_stable)
    assert sell.wallet.cngn_amount > original_sell_cngn
    assert sum(cost.swap_notional_usd for _, cost in batch.costs) == pytest.approx(
        batch.external_marked_notional_usd
    )
    joint_swap_fee = sum(cost.swap_fee_cost for _, cost in batch.costs)
    joint_price_impact = sum(cost.price_impact_cost for _, cost in batch.costs)
    assert joint_swap_fee == pytest.approx(
        expected_exact_output.swap_fee_cost, rel=1e-12, abs=1e-12
    )
    assert joint_price_impact == pytest.approx(
        expected_exact_output.price_impact_cost, rel=1e-12, abs=1e-12
    )
    assert joint_swap_fee != pytest.approx(
        wrong_exact_input.swap_fee_cost, rel=1e-12, abs=1e-12
    )
    assert joint_price_impact != pytest.approx(
        wrong_exact_input.price_impact_cost, rel=1e-12, abs=1e-12
    )
    total_cost = sum(cost.total for _, cost in batch.costs)
    holdings = []
    for runtime in (buy, sell):
        assert runtime.position is not None
        amount0, amount1 = runtime.position.amounts_at_sqrt_price_x96(
            runtime.current_sqrt_price_x96
        )
        position_wallet = _raw_amounts_to_wallet(
            amount0, amount1, runtime.current_price, UNISWAP_BASE_POOL
        )
        holdings.append(
            PortfolioComposition(
                stable_usd=runtime.wallet.stable_usd + position_wallet.stable_usd,
                cngn_amount=runtime.wallet.cngn_amount + position_wallet.cngn_amount,
            )
        )
    same_mark = sell.current_price
    initial_value = 1_000.0 + 1_500.0 * same_mark
    assert sum(item.value_usd(same_mark) for item in holdings) == pytest.approx(
        initial_value - total_cost
    )

    later_price = 1.20
    independently_marked = sum(
        item.stable_usd + item.cngn_amount * later_price for item in holdings
    )
    runtime_mark = sum(
        _portfolio_value(
            runtime.position,
            runtime.wallet,
            later_price,
            runtime.current_tick,
            runtime.current_sqrt_price_x96,
            UNISWAP_BASE_POOL,
        )
        for runtime in (buy, sell)
    )
    assert runtime_mark == pytest.approx(independently_marked)
    wrong_stable_refund_mark = independently_marked + (
        original_sell_cngn - sell.wallet.cngn_amount
    ) * later_price + (sell.wallet.cngn_amount - original_sell_cngn)
    assert runtime_mark != pytest.approx(wrong_stable_refund_mark)


def test_same_direction_mixed_exactness_uses_one_order_invariant_hybrid_swap() -> None:
    events = [
        replace(
            _swap(index, liquidity=10**12),
            tick=100,
            sqrt_price_x96=tick_to_sqrt_price_x96(100),
        )
        for index in range(3)
    ]
    exiting = create_sleeve_runtime(
        sleeve_id="exit",
        params=_params(unwind_to_cash_on_exit=True),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=1_000.0,
    )
    for event in events[:2]:
        exiting._observe_swap(event)
        action = exiting.propose_entry(event, event.active_liquidity)
        if action is not None:
            exiting.apply_action(action)
        exiting.previous_swap_time = event.block_time
    assert exiting.position is not None
    exiting._observe_swap(events[2])
    exit_cost = _exit_cost_breakdown(
        exiting.position,
        exiting.current_tick,
        exiting.current_sqrt_price_x96,
        exiting.current_price,
        events[2].active_liquidity,
        events[2].fee_rate,
        UNISWAP_BASE_POOL,
        exiting.params,
    )
    exit_action = exiting._build_exit_action(
        events[2], events[2].active_liquidity, "test_exit", exit_cost
    )
    assert exit_action is not None
    assert exit_action.inventory_swap_direction == "cngn_to_stable"

    entering = create_sleeve_runtime(
        sleeve_id="enter",
        params=_params(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=1_500.0,
        sizing_policy=FixedDeployment(1_000.0),
    )
    entering.wallet = PortfolioComposition(stable_usd=0.0, cngn_amount=1_500.0)
    for event in events:
        entering._observe_swap(event)
        entering.previous_swap_time = event.block_time
    entry_action = entering.propose_entry(events[2], events[2].active_liquidity)
    assert entry_action is not None
    assert entry_action.inventory_swap_direction == "cngn_to_stable"

    actions = [exit_action, entry_action]
    runtimes = {"exit": exiting, "enter": entering}
    copied_actions, copied_runtimes = deepcopy((actions, runtimes))
    batch = _settle_actions(
        actions,
        runtimes,
        events[2].active_liquidity,
        UNISWAP_BASE_POOL,
    )
    reverse_batch = _settle_actions(
        list(reversed(copied_actions)),
        copied_runtimes,
        events[2].active_liquidity,
        UNISWAP_BASE_POOL,
    )
    assert batch.external_marked_notional_usd == pytest.approx(
        exit_action.inventory_swap_notional_usd
        + entry_action.inventory_swap_notional_usd
    )
    assert batch.internal_cross_notional_usd == 0.0
    assert reverse_batch.external_marked_notional_usd == pytest.approx(
        batch.external_marked_notional_usd
    )
    assert (
        reverse_batch.internal_cross_notional_usd
        == batch.internal_cross_notional_usd
    )
    assert batch.external_input_value_usd > batch.external_output_value_usd
    assert batch.external_input_value_usd - batch.external_output_value_usd == pytest.approx(
        batch.allocated_variable_cost_usd
    )
    assert reverse_batch.external_input_value_usd == pytest.approx(
        batch.external_input_value_usd
    )
    assert reverse_batch.external_output_value_usd == pytest.approx(
        batch.external_output_value_usd
    )
    joint_variable_cost = sum(_variable_cost(cost) for _, cost in batch.costs)
    reverse_variable_cost = sum(
        _variable_cost(cost) for _, cost in reverse_batch.costs
    )
    assert joint_variable_cost == pytest.approx(reverse_variable_cost)
    assert [(sleeve_id, cost.total) for sleeve_id, cost in batch.costs] == pytest.approx(
        [(sleeve_id, cost.total) for sleeve_id, cost in reverse_batch.costs]
    )

    synthetic = exit_action.position_before.liquidity_L
    exact_input = _swap_cost_breakdown(
        "joint_inventory",
        batch.external_marked_notional_usd,
        int(events[2].active_liquidity + synthetic),
        events[2].fee_rate,
        exiting.current_tick,
        exiting.params,
        direction="cngn_to_stable",
        current_price=exiting.current_price,
        current_sqrt_price_x96=exiting.current_sqrt_price_x96,
        pool_config=UNISWAP_BASE_POOL,
        exact_output=False,
    )
    exact_output = _swap_cost_breakdown(
        "joint_inventory",
        batch.external_marked_notional_usd,
        int(events[2].active_liquidity + synthetic),
        events[2].fee_rate,
        exiting.current_tick,
        exiting.params,
        direction="cngn_to_stable",
        current_price=exiting.current_price,
        current_sqrt_price_x96=exiting.current_sqrt_price_x96,
        pool_config=UNISWAP_BASE_POOL,
        exact_output=True,
    )
    assert joint_variable_cost != pytest.approx(_variable_cost(exact_input))
    assert joint_variable_cost != pytest.approx(_variable_cost(exact_output))


def test_action_value_reconciliation_fails_before_runtime_mutation() -> None:
    events = [_swap(0), _swap(1), _swap(2)]
    runtime = create_sleeve_runtime(
        sleeve_id="a",
        params=_params(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )
    for event in events:
        runtime._observe_swap(event)
        runtime.previous_swap_time = event.block_time
    action = runtime.propose_entry(events[-1], events[-1].active_liquidity)
    assert action is not None
    bad_action = replace(
        action,
        wallet_after=PortfolioComposition(
            stable_usd=action.wallet_after.stable_usd + 1.0,
            cngn_amount=action.wallet_after.cngn_amount,
        ),
    )
    wallet_before = deepcopy(runtime.wallet)
    position_before = deepcopy(runtime.position)

    with pytest.raises(ValueError, match="value reconciliation"):
        _settle_actions(
            [bad_action],
            {"a": runtime},
            events[-1].active_liquidity,
            UNISWAP_BASE_POOL,
        )

    assert runtime.wallet == wallet_before
    assert runtime.position == position_before


def test_full_wallet_entries_are_jointly_scaled_without_redistribution() -> None:
    events = [
        replace(
            _swap(index, liquidity=10**12),
            tick=100,
            sqrt_price_x96=tick_to_sqrt_price_x96(100),
        )
        for index in range(3)
    ]
    overlays = {sleeve_id: SingleUseEligibilityOverlay() for sleeve_id in ("a", "b")}
    runtimes = {
        sleeve_id: create_sleeve_runtime(
            sleeve_id=sleeve_id,
            params=_params(),
            pool_config=UNISWAP_BASE_POOL,
            capital_usd=1_000.0,
            entry_eligibility=overlays[sleeve_id],
        )
        for sleeve_id in ("a", "b")
    }
    for runtime in runtimes.values():
        for event in events:
            runtime._observe_swap(event)
            runtime.previous_swap_time = event.block_time

    actions, scale = _plan_affordable_actions(
        runtimes,
        events[-1],
        events[-1].active_liquidity,
        UNISWAP_BASE_POOL,
    )
    batch = _settle_actions(
        actions,
        runtimes,
        events[-1].active_liquidity,
        UNISWAP_BASE_POOL,
    )

    assert 0.0 < scale < 1.0
    assert batch.external_marked_notional_usd > 0.0
    assert all(runtime.position is not None for runtime in runtimes.values())
    assert all(runtime.wallet.stable_usd >= 0.0 for runtime in runtimes.values())
    assert all(runtime.wallet.cngn_amount >= 0.0 for runtime in runtimes.values())
    assert all(overlay.calls == 1 for overlay in overlays.values())
    final_value = sum(
        _portfolio_value(
            runtime.position,
            runtime.wallet,
            runtime.current_price,
            runtime.current_tick,
            runtime.current_sqrt_price_x96,
            UNISWAP_BASE_POOL,
        )
        for runtime in runtimes.values()
    )
    assert 0.0 < final_value <= 2_000.0 + 1e-9


def test_aggregate_share_fails_before_the_projected_entry_batch_mutates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeves = (_sleeve("a", _params(max_share=0.08)), _sleeve("b", _params(max_share=0.08)))
    initial_pool_state = PoolState()
    initial_pool_state.apply_mint(-10, 10, 123)
    before = initial_pool_state.tick_map.copy()
    created: dict[str, object] = {}
    original_factory = portfolio_simulator.create_sleeve_runtime

    def recording_factory(**kwargs: object) -> object:
        runtime = original_factory(**kwargs)  # type: ignore[arg-type]
        created[str(kwargs["sleeve_id"])] = runtime
        return runtime

    monkeypatch.setattr(
        portfolio_simulator,
        "create_sleeve_runtime",
        recording_factory,
    )
    kwargs = dict(
        events=[_swap(0, liquidity=10**9), _swap(1, liquidity=10**9)],
        sleeves=sleeves,
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        settle_to_cash=False,
        initial_pool_state=initial_pool_state,
    )

    overlay_cases = (
        None,
        {"a": AlwaysEligibleOverlay(), "b": AlwaysEligibleOverlay()},
    )
    for entry_overlays_by_sleeve in overlay_cases:
        for _ in range(2):
            with pytest.raises(
                LiquidityShareExceeded,
                match="aggregate synthetic",
            ) as exc_info:
                simulate_portfolio(
                    **kwargs,
                    entry_overlays_by_sleeve=entry_overlays_by_sleeve,
                )
            assert exc_info.value.observed_share > exc_info.value.cap
            assert exc_info.value.cap == AGGREGATE_LIQUIDITY_SHARE_CAP
            assert str(exc_info.value) == (
                "aggregate synthetic liquidity share "
                f"{exc_info.value.observed_share:.6f} exceeds "
                f"{AGGREGATE_LIQUIDITY_SHARE_CAP:.2f}"
            )
            assert initial_pool_state.tick_map == before
            assert all(
                getattr(runtime, "position") is None for runtime in created.values()
            )
            assert all(
                getattr(runtime, "wallet").value_usd(1.0) == pytest.approx(50.0)
                for runtime in created.values()
            )


def test_portfolio_and_sleeve_values_reconcile_exactly() -> None:
    sleeves = (_sleeve("a", _params()), _sleeve("b", _params()))
    result = simulate_portfolio(
        events=[_swap(0), _swap(1), _swap(2)],
        sleeves=sleeves,
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        settle_to_cash=False,
        initial_pool_state=PoolState(),
    )

    assert result.final_value == pytest.approx(
        result.cash_value + sum(a.final_value for a in result.attribution.values())
    )
    for index, (_, portfolio_value) in enumerate(result.value_samples):
        attributed = sum(a.value_samples[index][1] for a in result.attribution.values())
        assert portfolio_value == pytest.approx(result.cash_value + attributed)
    assert result.external_input_value_usd == pytest.approx(
        sum(item.external_input_value_usd for item in result.attribution.values())
    )
    assert result.external_output_value_usd == pytest.approx(
        sum(item.external_output_value_usd for item in result.attribution.values())
    )
    assert result.total_variable_execution_cost_usd == pytest.approx(
        sum(
            item.allocated_variable_cost_usd
            for item in result.attribution.values()
        )
    )
    assert (
        result.external_input_value_usd - result.external_output_value_usd
    ) == pytest.approx(result.total_variable_execution_cost_usd)
    assert sum(
        item.signed_internal_cngn_value_usd
        for item in result.attribution.values()
    ) == pytest.approx(0.0, abs=1e-9)


def test_terminal_cash_settlement_closes_positions_and_loose_inventory() -> None:
    events = [_swap(0), _swap(1), _swap(2)]
    result = simulate_portfolio(
        events=events,
        sleeves=(_sleeve("a", _params()), _sleeve("b", _params())),
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        initial_pool_state=PoolState(),
        settle_to_cash=True,
    )

    assert result.settled_to_cash
    assert result.terminal_open_position_count == 0
    assert result.terminal_cngn_amount == pytest.approx(0.0, abs=1e-12)
    assert result.terminal_liquidation_cost > 0.0
    assert result.value_samples[-1][0] == events[-1].block_time
    assert result.value_samples[-1][1] == pytest.approx(result.final_value)
    assert all(
        attribution.liquidity_samples[-1] == (events[-1].block_time, 0.0)
        for attribution in result.attribution.values()
    )
    assert result.final_value == pytest.approx(
        result.cash_value
        + sum(attribution.final_value for attribution in result.attribution.values())
    )


def test_terminal_cash_settlement_requires_a_valid_swap() -> None:
    with pytest.raises(NoValidationSwapError, match="requires a valid swap"):
        simulate_portfolio(
            events=[],
            sleeves=(_sleeve("a", _params()),),
            allocation=Allocation("equal_config", {"a": 0.1}, 0.9),
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=500.0,
            initial_pool_state=PoolState(),
            settle_to_cash=True,
        )


def test_portfolio_rejects_non_finite_bankroll() -> None:
    with pytest.raises(ValueError, match="finite positive"):
        simulate_portfolio(
            events=[_swap(0)],
            sleeves=(),
            allocation=Allocation("equal_config", {}, 1.0),
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=float("nan"),
            initial_pool_state=PoolState(),
            settle_to_cash=True,
        )


def test_all_cash_allocation_is_a_valid_settled_window_with_opening_sample() -> None:
    events = [_swap(0), _swap(1)]

    result = simulate_portfolio(
        events=events,
        sleeves=(),
        allocation=Allocation("equal_config", {}, 1.0),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        initial_pool_state=PoolState(),
        settle_to_cash=True,
    )

    assert result.settled_to_cash
    assert result.final_value == pytest.approx(500.0)
    assert result.cash_value == pytest.approx(500.0)
    assert result.terminal_liquidation_cost == 0.0
    assert result.terminal_open_position_count == 0
    assert result.terminal_cngn_amount == 0.0
    assert result.attribution == {}
    assert result.value_samples == [
        (events[0].block_time, 500.0),
        (events[0].block_time, 500.0),
        (events[1].block_time, 500.0),
        (events[1].block_time, 500.0),
    ]
