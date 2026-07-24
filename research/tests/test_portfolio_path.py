from __future__ import annotations

from datetime import UTC, datetime

import pytest

from research.backtester.portfolio_path import (
    CarriedPathSnapshot,
    CarriedPathState,
    EconomicResult,
    FailureDiagnostic,
    NoValidationSwapError,
    economic_result_from_sim,
)
from research.backtester.portfolio_simulator import (
    ExecutionAccountingError,
    LiquidityShareExceeded,
)
from research.backtester.position_runtime import TerminalLiquidationError
from research.backtester.simulator import SimResult


def _result(opening: float, closing: float) -> EconomicResult:
    now = datetime.now(UTC)
    return EconomicResult(
        opening_capital_usd=opening,
        closing_cash_usd=closing,
        max_drawdown=min(closing / opening - 1.0, 0.0),
        terminal_liquidation_cost_usd=1.0,
        total_fees_usd=2.0,
        total_fixed_cost_usd=0.5,
        total_variable_cost_usd=0.5,
        external_marked_notional_usd=10.0,
        external_input_value_usd=10.5,
        external_output_value_usd=10.0,
        internal_cross_notional_usd=0.0,
        entry_scale_events=(),
        terminal_position_settlement_count=1,
        terminal_loose_cngn_settlement_count=0,
        terminal_zero_settlement_count=0,
        terminal_inventory_swap_count=1,
        terminal_fixed_cost_usd=0.5,
        terminal_variable_cost_usd=0.5,
        terminal_external_marked_notional_usd=10.0,
        value_samples=((now, opening), (now, closing)),
    )


def test_carried_path_passes_exact_closing_cash_to_the_next_window() -> None:
    state = CarriedPathState("equal_config", "allocation_rule", 100.0)

    first = state.evaluate_window(0, lambda opening: _result(opening, 110.0))
    second = state.evaluate_window(1, lambda opening: _result(opening, 99.0))

    assert first.status == "valid"
    assert first.opening_capital_usd == 100.0
    assert first.closing_cash_usd == 110.0
    assert second.opening_capital_usd == 110.0
    assert second.closing_cash_usd == 99.0
    assert state.next_opening_capital_usd == 99.0


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (LiquidityShareExceeded(0.11, 0.10), "invalid_liquidity_cap"),
        (NoValidationSwapError("none"), "invalid_no_validation_swap"),
        (
            TerminalLiquidationError("terminal"),
            "invalid_terminal_liquidation",
        ),
    ],
)
def test_invalid_path_blocks_all_later_windows(
    failure: Exception,
    status: str,
) -> None:
    state = CarriedPathState("method", "comparator", 100.0)

    def fail(_: float) -> EconomicResult:
        raise failure

    trigger = state.evaluate_window(2, fail)
    later = state.evaluate_window(3, lambda opening: _result(opening, opening))

    assert trigger.status == status
    assert trigger.blocking_status == status
    assert trigger.blocking_window_index == 2
    assert trigger.failure == FailureDiagnostic(
        type(failure).__name__,
        str(failure),
    )
    assert later.status == "blocked_prior_invalid"
    assert later.blocking_status == status
    assert later.blocking_window_index == 2
    assert later.failure == trigger.failure
    assert later.opening_capital_usd is None
    assert later.closing_cash_usd is None


def test_one_invalid_method_does_not_block_another() -> None:
    invalid = CarriedPathState("invalid", "allocation_rule", 100.0)
    valid = CarriedPathState("valid", "allocation_rule", 100.0)

    invalid.evaluate_window(
        0,
        lambda opening: (_ for _ in ()).throw(LiquidityShareExceeded(0.11, 0.10)),
    )
    row = valid.evaluate_window(0, lambda opening: _result(opening, 101.0))

    assert row.status == "valid"
    assert valid.next_opening_capital_usd == 101.0


def test_carried_path_snapshot_round_trips_live_and_blocked_state() -> None:
    live = CarriedPathState("cash", "comparator", 101.0)
    blocked = CarriedPathState("equal_config", "allocation_rule", 100.0)
    blocked.evaluate_window(
        2,
        lambda opening: (_ for _ in ()).throw(ExecutionAccountingError("failed reconciliation")),
    )

    live_snapshot = live.snapshot()
    blocked_snapshot = blocked.snapshot()

    assert live_snapshot == CarriedPathSnapshot(
        method_id="cash",
        method_kind="comparator",
        next_opening_capital_usd=101.0,
        blocking_status=None,
        blocking_window_index=None,
        failure=None,
    )
    assert CarriedPathState.from_snapshot(live_snapshot) == live
    assert CarriedPathState.from_snapshot(blocked_snapshot) == blocked
    assert blocked_snapshot.blocking_status == "invalid_execution_accounting"
    assert blocked_snapshot.blocking_window_index == 2
    assert blocked_snapshot.failure == FailureDiagnostic(
        "ExecutionAccountingError",
        "failed reconciliation",
    )


def test_zero_closing_capital_checkpoints_then_blocks_the_next_window() -> None:
    state = CarriedPathState("cash", "comparator", 100.0)

    terminal = state.evaluate_window(0, lambda opening: _result(opening, 0.0))
    resumed = CarriedPathState.from_snapshot(state.snapshot())
    blocked = resumed.evaluate_window(1, lambda opening: _result(opening, opening))

    assert terminal.status == "valid"
    assert terminal.closing_cash_usd == 0.0
    assert resumed.next_opening_capital_usd == 0.0
    assert blocked.status == "invalid_opening_capital"
    assert blocked.opening_capital_usd == 0.0


def test_carried_path_snapshot_rejects_incoherent_blocking_evidence() -> None:
    with pytest.raises(ValueError, match="blocking status and window"):
        CarriedPathSnapshot(
            method_id="equal_config",
            method_kind="allocation_rule",
            next_opening_capital_usd=100.0,
            blocking_status="invalid_execution_accounting",
            blocking_window_index=None,
            failure=FailureDiagnostic(
                "ExecutionAccountingError",
                "failed reconciliation",
            ),
        )


def test_standalone_simulation_converts_to_the_common_economic_contract() -> None:
    now = datetime.now(UTC)
    simulation = SimResult(
        total_fees=2.0,
        total_transaction_cost=1.5,
        total_gas_cost=0.5,
        total_swap_fee_cost=0.4,
        total_price_impact_cost=0.3,
        total_slippage_cost=0.2,
        total_latency_slippage_cost=0.1,
        final_value=101.0,
        settled_to_cash=True,
        terminal_liquidation_cost=0.4,
        terminal_position_settlement_count=1,
        terminal_inventory_swap_count=1,
        terminal_fixed_cost_usd=0.1,
        terminal_variable_cost_usd=0.3,
        terminal_external_marked_notional_usd=10.0,
        terminal_open_position_count=0,
        terminal_cngn_amount=0.0,
        value_samples=[(now, 100.0), (now, 101.0)],
        external_swap_notional_usd=20.0,
        external_input_value_usd=21.0,
        external_output_value_usd=20.0,
        total_variable_execution_cost_usd=1.0,
    )

    result = economic_result_from_sim(simulation, 100.0)

    assert result.opening_capital_usd == 100.0
    assert result.closing_cash_usd == 101.0
    assert result.total_fixed_cost_usd == 0.5
    assert result.total_variable_cost_usd == 1.0
    assert result.external_input_value_usd - result.external_output_value_usd == 1.0


def test_standalone_conversion_rejects_any_terminal_cngn_residual() -> None:
    now = datetime.now(UTC)
    simulation = SimResult(
        final_value=100.0,
        settled_to_cash=True,
        terminal_open_position_count=0,
        terminal_cngn_amount=1e-13,
        value_samples=[(now, 100.0), (now, 100.0)],
    )

    with pytest.raises(TerminalLiquidationError, match="settled to USD cash"):
        economic_result_from_sim(simulation, 100.0)
