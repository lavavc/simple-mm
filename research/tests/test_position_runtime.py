from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import Event, V4Event
from research.backtester.entry_eligibility import (
    EntryEligibilityDecision,
    EntryEligibilityOverlay,
)
from research.backtester.params import BacktestParams, TransactionCostModel
from research.backtester.position_runtime import (
    SleeveRuntime,
    SleeveSettlement,
    create_sleeve_runtime,
)
from research.backtester.simulator import (
    UNISWAP_BASE_POOL,
    PortfolioComposition,
    TransactionCostBreakdown,
    simulate_pool,
)
from research.backtester.sizing import EntryContext, SizingPolicy


class RecordingSizingPolicy:
    free_parameter_count = 0

    def __init__(self) -> None:
        self.contexts: list[EntryContext] = []

    def deployed_capital_usd(self, context: EntryContext) -> float:
        self.contexts.append(context)
        return float(context.wallet_value_usd)


class RecordingEligibilityOverlay:
    def __init__(
        self,
        decisions: tuple[bool, ...],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.decisions = decisions
        self.failure = failure
        self.contexts: list[EntryContext] = []

    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
        self.contexts.append(context)
        if self.failure is not None:
            raise self.failure
        decision_index = len(self.contexts) - 1
        if decision_index >= len(self.decisions):
            raise AssertionError("eligibility overlay was evaluated unexpectedly")
        eligible = self.decisions[decision_index]
        return EntryEligibilityDecision(
            eligible=eligible,
            reason="agreement" if eligible else "disagreement",
        )


def _swap(*, minute: int, tick: int, amount0: float, amount1: float) -> V4Event:
    return V4Event(
        block_time=datetime(2026, 1, 1) + timedelta(minutes=minute),
        chain="base",
        pool_id=UNISWAP_BASE_POOL.pool_address,
        event_type="swap",
        tx_hash=f"0x{minute:064x}",
        log_index=0,
        block_number=100 + minute,
        sqrt_price_x96=tick_to_sqrt_price_x96(tick),
        tick=tick,
        active_liquidity=10**15,
        fee_rate=UNISWAP_BASE_POOL.fee_rate,
        amount0=amount0,
        amount1=amount1,
        amount_usd=1_000.0,
        cngn_usd_price=1.0001**tick,
        token0_symbol="cNGN",
        token1_symbol="USDC",
    )


def _events_that_enter_accrue_fee_exit_and_reenter() -> list[Event]:
    return [
        _swap(minute=0, tick=0, amount0=-1_000_000_000, amount1=1_000_000_000),
        _swap(minute=1, tick=1, amount0=-1_000_000_000, amount1=1_000_000_000),
        _swap(minute=2, tick=2, amount0=-1_000_000_000, amount1=1_000_000_000),
        _swap(minute=3, tick=-60, amount0=1_000_000_000, amount1=-1_000_000_000),
        _swap(minute=4, tick=-59, amount0=-1_000_000_000, amount1=1_000_000_000),
    ]


def _paper_params_with_zero_gas() -> BacktestParams:
    return BacktestParams(
        strategy_mode="paper",
        range_mode="fixed_tick_width",
        center_mode="spot",
        fixed_tick_width=50,
        stop_loss_return=None,
        downward_range_fraction=0.5,
        gas_cost_usd=0.0,
        transaction_costs=replace(
            TransactionCostModel(),
            mint_gas_usd=0.0,
            remove_gas_usd=0.0,
            fallback_price_impact_bps=0.0,
        ),
    )


def test_runtime_matches_simulate_pool_for_complete_lifecycle() -> None:
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    params = _paper_params_with_zero_gas()
    expected = simulate_pool(events, params, UNISWAP_BASE_POOL, 500.0)
    assert expected.total_fees > 0
    assert expected.rebalance_count == 1
    assert len(expected.episodes) == 2

    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=params,
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )
    actual = runtime.run(events)

    assert actual == expected


def test_runtime_exposes_ordered_phases_and_preserves_complete_lifecycle() -> None:
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    params = _paper_params_with_zero_gas()
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=params,
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )

    for method_name in (
        "_apply_liquidity_event",
        "_observe_swap",
        "_roll_daily_return",
        "_accrue_position_fee",
        "_exit_decision",
        "_apply_exit",
        "_maybe_enter",
        "_record_event_value",
        "finalize",
    ):
        assert callable(getattr(runtime, method_name, None)), method_name

    assert runtime.run(events) == simulate_pool(events, params, UNISWAP_BASE_POOL, 500.0)


def test_exit_decision_does_not_mutate_wallet_or_position() -> None:
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    params = _paper_params_with_zero_gas()
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=params,
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )
    runtime.run(events[:3])
    assert runtime.position is not None

    exit_event = events[3]
    assert isinstance(exit_event, V4Event)
    active_liquidity, price_is_valid = runtime._observe_swap(exit_event)
    assert price_is_valid
    wallet_before = deepcopy(runtime.wallet)
    position_before = deepcopy(runtime.position)

    exit_reason, exit_cost = runtime._exit_decision(exit_event, active_liquidity)

    assert exit_reason is not None
    assert exit_cost.action == "exit"
    assert runtime.wallet == wallet_before
    assert runtime.position == position_before


def _runtime_ready_to_enter(
    *,
    sizing_policy: SizingPolicy | None = None,
    entry_eligibility: EntryEligibilityOverlay | None = None,
) -> tuple[SleeveRuntime, V4Event, int]:
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=_paper_params_with_zero_gas(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
        sizing_policy=sizing_policy,
        entry_eligibility=entry_eligibility,
    )
    for event in events[:3]:
        assert isinstance(event, V4Event)
        active_liquidity, price_is_valid = runtime._observe_swap(event)
        assert price_is_valid
        if runtime.ewma.ready:
            return runtime, event, active_liquidity
        runtime.previous_swap_time = event.block_time
    raise AssertionError("fixture did not make EWMA ready")


def _runtime_ready_to_exit() -> tuple[SleeveRuntime, V4Event, int]:
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=_paper_params_with_zero_gas(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
    )
    runtime.run(events[:3])
    exit_event = events[3]
    assert isinstance(exit_event, V4Event)
    active_liquidity, price_is_valid = runtime._observe_swap(exit_event)
    assert price_is_valid
    runtime._accrue_position_fee(exit_event, active_liquidity)
    return runtime, exit_event, active_liquidity


def test_entry_proposal_is_pure_and_application_mutates_runtime() -> None:
    runtime, event, active_liquidity = _runtime_ready_to_enter()
    wallet_before = deepcopy(runtime.wallet)

    action = runtime.propose_entry(event, active_liquidity)

    assert action is not None
    assert action.kind == "enter"
    assert runtime.wallet == wallet_before
    assert runtime.position is None

    runtime.apply_action(action)

    assert runtime.wallet == action.wallet_after
    assert runtime.position == action.position_after
    assert runtime.position is not None


def test_entry_overlay_and_sizing_receive_the_same_context_object() -> None:
    overlay = RecordingEligibilityOverlay((True,))
    sizing = RecordingSizingPolicy()
    runtime, event, active_liquidity = _runtime_ready_to_enter(
        sizing_policy=sizing,
        entry_eligibility=overlay,
    )

    action = runtime.propose_entry(event, active_liquidity)

    assert action is not None
    assert len(overlay.contexts) == 1
    assert len(sizing.contexts) == 1
    assert overlay.contexts[0] is sizing.contexts[0]
    assert overlay.contexts[0] == EntryContext(
        block_time=event.block_time,
        wallet_value_usd=500.0,
        current_price=runtime.current_price,
        current_tick=runtime.current_tick,
        active_liquidity=active_liquidity,
    )


def test_entry_denial_precedes_sizing_and_preserves_runtime_state() -> None:
    overlay = RecordingEligibilityOverlay((False,))
    sizing = RecordingSizingPolicy()
    runtime, event, active_liquidity = _runtime_ready_to_enter(
        sizing_policy=sizing,
        entry_eligibility=overlay,
    )
    wallet_before = deepcopy(runtime.wallet)
    result_before = deepcopy(runtime.result)
    position_before = deepcopy(runtime.position)
    cooldown_time_before = runtime.cooldown_until_time
    cooldown_block_before = runtime.cooldown_until_block

    action = runtime.propose_entry(event, active_liquidity)

    assert action is None
    assert len(overlay.contexts) == 1
    assert sizing.contexts == []
    assert runtime.wallet == wallet_before
    assert runtime.result == result_before
    assert runtime.position == position_before
    assert runtime.cooldown_until_time == cooldown_time_before
    assert runtime.cooldown_until_block == cooldown_block_before


def test_intrinsic_entry_filters_run_before_overlay_and_open_positions_skip_it() -> None:
    overlay = RecordingEligibilityOverlay((True,))
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=_paper_params_with_zero_gas(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
        entry_eligibility=overlay,
    )
    first = events[0]
    assert isinstance(first, V4Event)
    first_liquidity, price_is_valid = runtime._observe_swap(first)
    assert price_is_valid

    assert runtime.propose_entry(first, first_liquidity) is None
    assert overlay.contexts == []

    runtime, event, active_liquidity = _runtime_ready_to_enter(
        entry_eligibility=overlay
    )
    runtime.cooldown_until_time = event.block_time + timedelta(minutes=1)
    assert runtime.propose_entry(event, active_liquidity) is None
    assert overlay.contexts == []

    runtime.cooldown_until_time = None
    action = runtime.propose_entry(event, active_liquidity)
    assert action is not None
    runtime.apply_action(action)
    overlay.contexts.clear()

    assert runtime.propose_entry(event, active_liquidity) is None
    assert overlay.contexts == []


def test_entry_overlay_exception_propagates_before_sizing() -> None:
    failure = RuntimeError("forecast lookup failed")
    overlay = RecordingEligibilityOverlay((), failure=failure)
    sizing = RecordingSizingPolicy()
    runtime, event, active_liquidity = _runtime_ready_to_enter(
        sizing_policy=sizing,
        entry_eligibility=overlay,
    )

    with pytest.raises(RuntimeError, match="forecast lookup failed") as exc_info:
        runtime.propose_entry(event, active_liquidity)

    assert exc_info.value is failure
    assert sizing.contexts == []


def test_factory_preserves_entry_overlay_identity() -> None:
    overlay = RecordingEligibilityOverlay((True,))

    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=_paper_params_with_zero_gas(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
        entry_eligibility=overlay,
    )

    assert runtime.entry_eligibility is overlay


def test_normal_exit_precedes_fresh_same_event_entry_eligibility_check() -> None:
    events = _events_that_enter_accrue_fee_exit_and_reenter()
    overlay = RecordingEligibilityOverlay((True, False))
    runtime = create_sleeve_runtime(
        sleeve_id="paper",
        params=_paper_params_with_zero_gas(),
        pool_config=UNISWAP_BASE_POOL,
        capital_usd=500.0,
        entry_eligibility=overlay,
    )

    result = runtime.run(events[:4])

    assert [context.block_time for context in overlay.contexts] == [
        events[1].block_time,
        events[3].block_time,
    ]
    assert result.rebalance_count == 1
    assert len(result.episodes) == 1
    assert runtime.position is None


def test_exit_proposal_is_pure_and_application_records_episode_once() -> None:
    runtime, event, active_liquidity = _runtime_ready_to_exit()
    wallet_before = deepcopy(runtime.wallet)
    position_before = deepcopy(runtime.position)
    episode_count = len(runtime.result.episodes)

    action = runtime.propose_exit(event, active_liquidity)

    assert action is not None
    assert action.kind == "exit"
    assert runtime.wallet == wallet_before
    assert runtime.position == position_before

    runtime.apply_action(action)

    assert runtime.wallet == action.wallet_after
    assert runtime.position is None
    assert len(runtime.result.episodes) == episode_count + 1


def test_applying_stale_action_after_wallet_mutation_fails_loudly() -> None:
    runtime, event, active_liquidity = _runtime_ready_to_enter()
    action = runtime.propose_entry(event, active_liquidity)
    assert action is not None
    runtime.wallet = replace(runtime.wallet, stable_usd=runtime.wallet.stable_usd - 1.0)

    try:
        runtime.apply_action(action)
    except ValueError as exc:
        assert "wallet" in str(exc)
    else:
        raise AssertionError("stale action was applied")


def test_applying_action_for_another_sleeve_fails_loudly() -> None:
    runtime, event, active_liquidity = _runtime_ready_to_enter()
    action = runtime.propose_entry(event, active_liquidity)
    assert action is not None
    other = create_sleeve_runtime(
        sleeve_id="other",
        params=runtime.params,
        pool_config=runtime.pool_config,
        capital_usd=500.0,
    )

    try:
        other.apply_action(action)
    except ValueError as exc:
        assert "sleeve" in str(exc)
    else:
        raise AssertionError("foreign action was applied")


def test_supplied_settlement_replaces_wallet_and_exit_cost_only() -> None:
    runtime, event, active_liquidity = _runtime_ready_to_exit()
    action = runtime.propose_exit(event, active_liquidity)
    assert action is not None
    settlement = SleeveSettlement(
        wallet_after=PortfolioComposition(stable_usd=321.0, cngn_amount=4.0),
        transaction_cost=TransactionCostBreakdown("exit", gas_cost=7.0),
    )

    runtime.apply_action(action, settlement)

    assert runtime.wallet == settlement.wallet_after
    assert runtime.position == action.position_after
    assert runtime.result.episodes[-1].exit_reason == action.reason
    assert runtime.result.total_rebalance_cost == settlement.transaction_cost.total
    assert runtime.result.rebalance_count == 1
