from __future__ import annotations

import json
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
from research.backtester.portfolio_catalog import SleeveDefinition, parameter_fingerprint
from research.backtester.portfolio_simulator import (
    LiquidityShareExceeded,
    _net_actions,
    _settle_actions,
    simulate_portfolio,
)
from research.backtester.position_runtime import SleeveAction, create_sleeve_runtime
from research.backtester.simulator import (
    UNISWAP_BASE_POOL,
    PortfolioComposition,
    TransactionCostBreakdown,
    _portfolio_value,
    _raw_amounts_to_wallet,
    _swap_cost_breakdown,
)
from research.backtester.sizing import EntryContext


class NeverEligibleOverlay:
    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
        return EntryEligibilityDecision(False, "disagreement")


class RaisingEligibilityOverlay:
    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
        raise AssertionError("zero-weight overlay must not be evaluated")


def _params(*, max_share: float | None = None) -> BacktestParams:
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
            entry_overlays_by_sleeve={"overlay-missing": AlwaysEligibleOverlay()},
        )


def test_valid_zero_weight_overlay_is_inert() -> None:
    result = simulate_portfolio(
        events=[_swap(0), _swap(1)],
        sleeves=(_sleeve("a", _params()), _sleeve("b", _params())),
        allocation=Allocation("equal_config", {"a": 0.1}, 0.9),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
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

    with pytest.raises(ValueError, match="one action kind"):
        _settle_actions(
            [actions[0], replace(actions[1], kind="exit")],
            {},
            event.active_liquidity,
            UNISWAP_BASE_POOL,
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

    external, internal, settlements = _settle_actions(
        [buy_action, sell_action],
        {"buy": buy, "sell": sell},
        event1.active_liquidity,
        UNISWAP_BASE_POOL,
    )

    assert external == pytest.approx(abs(gross_buy - gross_sell))
    assert internal == pytest.approx(min(gross_buy, gross_sell))
    assert len(settlements) == 2
    assert sell.wallet.stable_usd == pytest.approx(original_sell_stable)
    assert sell.wallet.cngn_amount > original_sell_cngn
    assert sum(cost.swap_notional_usd for _, cost in settlements) == pytest.approx(external)
    joint_swap_fee = sum(cost.swap_fee_cost for _, cost in settlements)
    joint_price_impact = sum(cost.price_impact_cost for _, cost in settlements)
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
    total_cost = sum(cost.total for _, cost in settlements)
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


def test_aggregate_share_fails_even_when_each_sleeve_is_below_cap() -> None:
    sleeves = (_sleeve("a", _params(max_share=0.08)), _sleeve("b", _params(max_share=0.08)))
    initial_pool_state = PoolState()
    initial_pool_state.apply_mint(-10, 10, 123)
    before = initial_pool_state.tick_map.copy()
    kwargs = dict(
        events=[_swap(0, liquidity=10**9), _swap(1, liquidity=10**9)],
        sleeves=sleeves,
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        initial_pool_state=initial_pool_state,
    )

    overlay_cases = (
        None,
        {"a": AlwaysEligibleOverlay(), "b": AlwaysEligibleOverlay()},
    )
    for entry_overlays_by_sleeve in overlay_cases:
        for _ in range(2):
            with pytest.raises(LiquidityShareExceeded, match="aggregate synthetic"):
                simulate_portfolio(
                    **kwargs,
                    entry_overlays_by_sleeve=entry_overlays_by_sleeve,
                )
            assert initial_pool_state.tick_map == before


def test_portfolio_and_sleeve_values_reconcile_exactly() -> None:
    sleeves = (_sleeve("a", _params()), _sleeve("b", _params()))
    result = simulate_portfolio(
        events=[_swap(0), _swap(1), _swap(2)],
        sleeves=sleeves,
        allocation=Allocation("equal_config", {"a": 0.1, "b": 0.1}, 0.8),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=500.0,
        initial_pool_state=PoolState(),
    )

    assert result.final_value == pytest.approx(
        result.cash_value + sum(a.final_value for a in result.attribution.values())
    )
    for index, (_, portfolio_value) in enumerate(result.value_samples):
        attributed = sum(a.value_samples[index][1] for a in result.attribution.values())
        assert portfolio_value == pytest.approx(result.cash_value + attributed)
