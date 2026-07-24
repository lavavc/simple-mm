"""Fail-closed carried-capital state for portfolio and comparator methods."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Literal, Mapping

from research.backtester.portfolio_errors import (
    ExecutionAccountingError,
    NoValidationSwapError,
)
from research.backtester.portfolio_simulator import (
    EntryScaleEvent,
    LiquidityShareExceeded,
    PortfolioResult,
)
from research.backtester.position_runtime import TerminalLiquidationError
from research.backtester.simulator import SimResult

InvalidStatus = Literal[
    "invalid_liquidity_cap",
    "invalid_no_validation_swap",
    "invalid_execution_accounting",
    "invalid_terminal_liquidation",
    "invalid_opening_capital",
]
PathStatus = Literal["valid", "blocked_prior_invalid"] | InvalidStatus


@dataclass(frozen=True)
class FailureDiagnostic:
    exception_type: str
    reason: str

    def __post_init__(self) -> None:
        if not self.exception_type or not self.reason or len(self.reason) > 512:
            raise ValueError("failure diagnostic type and reason must be non-empty and bounded")

    @classmethod
    def from_exception(cls, error: Exception) -> FailureDiagnostic:
        return cls(type(error).__name__, str(error))


def signed_max_drawdown(
    value_samples: tuple[tuple[datetime, float], ...],
    opening_capital_usd: float,
) -> float:
    if not math.isfinite(opening_capital_usd) or opening_capital_usd <= 0:
        return math.nan
    peak = opening_capital_usd
    drawdown = 0.0
    for _, value in value_samples:
        if not math.isfinite(value) or value < 0:
            return math.nan
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1.0)
    return drawdown


@dataclass(frozen=True)
class EconomicAttribution:
    economic_id: str
    opening_value_usd: float
    closing_value_usd: float
    fees_usd: float
    fixed_cost_usd: float
    variable_cost_usd: float
    external_marked_notional_usd: float
    external_input_value_usd: float
    external_output_value_usd: float
    internal_cross_notional_usd: float
    terminal_funding_transfer_usd: float

    @property
    def pnl_usd(self) -> float:
        return self.closing_value_usd - self.opening_value_usd


@dataclass(frozen=True)
class EconomicResult:
    opening_capital_usd: float
    closing_cash_usd: float
    max_drawdown: float
    terminal_liquidation_cost_usd: float
    total_fees_usd: float
    total_fixed_cost_usd: float
    total_variable_cost_usd: float
    external_marked_notional_usd: float
    external_input_value_usd: float
    external_output_value_usd: float
    internal_cross_notional_usd: float
    entry_scale_events: tuple[EntryScaleEvent, ...]
    terminal_position_settlement_count: int
    terminal_loose_cngn_settlement_count: int
    terminal_zero_settlement_count: int
    terminal_inventory_swap_count: int
    terminal_fixed_cost_usd: float
    terminal_variable_cost_usd: float
    terminal_external_marked_notional_usd: float
    value_samples: tuple[tuple[datetime, float], ...]
    attribution: Mapping[str, EconomicAttribution] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = (
            self.opening_capital_usd,
            self.closing_cash_usd,
            self.max_drawdown,
            self.terminal_liquidation_cost_usd,
            self.total_fees_usd,
            self.total_fixed_cost_usd,
            self.total_variable_cost_usd,
            self.external_marked_notional_usd,
            self.external_input_value_usd,
            self.external_output_value_usd,
            self.internal_cross_notional_usd,
            self.terminal_fixed_cost_usd,
            self.terminal_variable_cost_usd,
            self.terminal_external_marked_notional_usd,
        )
        if not all(math.isfinite(value) for value in values):
            raise ExecutionAccountingError("economic result contains non-finite values")
        if self.opening_capital_usd <= 0 or self.closing_cash_usd < 0:
            raise ExecutionAccountingError("economic capital values are invalid")
        if self.max_drawdown > 0:
            raise ExecutionAccountingError("economic drawdown must be non-positive")
        non_negative = (
            self.terminal_liquidation_cost_usd,
            self.total_fees_usd,
            self.total_fixed_cost_usd,
            self.total_variable_cost_usd,
            self.external_marked_notional_usd,
            self.external_input_value_usd,
            self.external_output_value_usd,
            self.internal_cross_notional_usd,
            self.terminal_fixed_cost_usd,
            self.terminal_variable_cost_usd,
            self.terminal_external_marked_notional_usd,
        )
        if any(value < 0 for value in non_negative):
            raise ExecutionAccountingError("economic accounting fields must be non-negative")
        tolerance = 1e-9 * max(
            1.0,
            self.external_input_value_usd,
            self.external_output_value_usd,
            self.total_variable_cost_usd,
        )
        if (
            abs(
                self.external_input_value_usd
                - self.external_output_value_usd
                - self.total_variable_cost_usd
            )
            > tolerance
        ):
            raise ExecutionAccountingError("external execution values do not reconcile")
        counts = (
            self.terminal_position_settlement_count,
            self.terminal_loose_cngn_settlement_count,
            self.terminal_zero_settlement_count,
            self.terminal_inventory_swap_count,
        )
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in counts
            )
            or self.terminal_inventory_swap_count > 1
        ):
            raise ExecutionAccountingError("terminal settlement counts are invalid")
        terminal_tolerance = 1e-9 * max(
            1.0,
            self.terminal_liquidation_cost_usd,
            self.terminal_fixed_cost_usd,
            self.terminal_variable_cost_usd,
        )
        if (
            abs(
                self.terminal_fixed_cost_usd
                + self.terminal_variable_cost_usd
                - self.terminal_liquidation_cost_usd
            )
            > terminal_tolerance
        ):
            raise ExecutionAccountingError("terminal settlement costs do not reconcile")
        if self.terminal_inventory_swap_count == 0 and (
            self.terminal_variable_cost_usd != 0.0
            or self.terminal_external_marked_notional_usd != 0.0
        ):
            raise ExecutionAccountingError("zero terminal swaps contain execution values")
        if self.terminal_inventory_swap_count == 1 and (
            self.terminal_external_marked_notional_usd <= 0.0
        ):
            raise ExecutionAccountingError("terminal inventory swap lacks notional")
        observed_drawdown = signed_max_drawdown(
            self.value_samples,
            self.opening_capital_usd,
        )
        if (
            not math.isfinite(observed_drawdown)
            or abs(observed_drawdown - self.max_drawdown) > 1e-12
        ):
            raise ExecutionAccountingError("economic value path drawdown is incoherent")
        if self.attribution:
            self._validate_attribution()

    @property
    def window_net_return(self) -> float:
        return self.closing_cash_usd / self.opening_capital_usd - 1.0

    @property
    def entry_action_batch_count(self) -> int:
        return len(self.entry_scale_events)

    @property
    def scaled_entry_action_batch_count(self) -> int:
        return sum(event.scale < 1.0 for event in self.entry_scale_events)

    @property
    def minimum_entry_execution_scale(self) -> float:
        return min(
            (event.scale for event in self.entry_scale_events),
            default=1.0,
        )

    def _validate_attribution(self) -> None:
        fields = (
            ("opening", self.opening_capital_usd, "opening_value_usd"),
            ("closing", self.closing_cash_usd, "closing_value_usd"),
            ("fees", self.total_fees_usd, "fees_usd"),
            ("fixed cost", self.total_fixed_cost_usd, "fixed_cost_usd"),
            ("variable cost", self.total_variable_cost_usd, "variable_cost_usd"),
            (
                "external marked notional",
                self.external_marked_notional_usd,
                "external_marked_notional_usd",
            ),
            (
                "external input",
                self.external_input_value_usd,
                "external_input_value_usd",
            ),
            (
                "external output",
                self.external_output_value_usd,
                "external_output_value_usd",
            ),
            (
                "internal cross",
                self.internal_cross_notional_usd,
                "internal_cross_notional_usd",
            ),
        )
        for label, expected, attribute in fields:
            actual = sum(getattr(item, attribute) for item in self.attribution.values())
            tolerance = 1e-8 * max(1.0, abs(expected), abs(actual))
            if abs(actual - expected) > tolerance:
                raise ExecutionAccountingError(f"economic attribution {label} does not reconcile")
        funding_transfer = sum(
            item.terminal_funding_transfer_usd for item in self.attribution.values()
        )
        tolerance = 1e-10 * max(1.0, self.opening_capital_usd)
        if not math.isfinite(funding_transfer) or abs(funding_transfer) > tolerance:
            raise ExecutionAccountingError(
                "economic attribution terminal funding does not reconcile"
            )


def economic_result_from_portfolio(result: PortfolioResult) -> EconomicResult:
    if (
        not result.settled_to_cash
        or result.terminal_open_position_count != 0
        or result.terminal_cngn_amount != 0.0
    ):
        raise TerminalLiquidationError("portfolio result is not completely settled to USD cash")
    fixed_cost = result.total_transaction_cost - result.total_variable_execution_cost_usd
    if fixed_cost < -1e-9:
        raise ExecutionAccountingError("portfolio fixed cost is negative")
    opening_cash = result.bankroll_usd - sum(
        item.capital_budget_usd for item in result.attribution.values()
    )
    if opening_cash < -1e-9:
        raise ExecutionAccountingError("portfolio opening cash is negative")
    attribution: dict[str, EconomicAttribution] = {
        "cash": EconomicAttribution(
            economic_id="cash",
            opening_value_usd=max(opening_cash, 0.0),
            closing_value_usd=result.cash_value,
            fees_usd=0.0,
            fixed_cost_usd=0.0,
            variable_cost_usd=0.0,
            external_marked_notional_usd=0.0,
            external_input_value_usd=0.0,
            external_output_value_usd=0.0,
            internal_cross_notional_usd=0.0,
            terminal_funding_transfer_usd=(result.terminal_cash_funding_transfer_usd),
        )
    }
    for sleeve_id, item in result.attribution.items():
        sleeve_fixed_cost = item.transaction_cost_usd - item.allocated_variable_cost_usd
        if sleeve_fixed_cost < -1e-9:
            raise ExecutionAccountingError(f"sleeve {sleeve_id!r} has negative fixed cost")
        attribution[sleeve_id] = EconomicAttribution(
            economic_id=sleeve_id,
            opening_value_usd=item.capital_budget_usd,
            closing_value_usd=item.final_value,
            fees_usd=item.fees_usd,
            fixed_cost_usd=max(sleeve_fixed_cost, 0.0),
            variable_cost_usd=item.allocated_variable_cost_usd,
            external_marked_notional_usd=item.external_marked_notional_usd,
            external_input_value_usd=item.external_input_value_usd,
            external_output_value_usd=item.external_output_value_usd,
            internal_cross_notional_usd=max(
                item.signed_internal_cngn_value_usd,
                0.0,
            ),
            terminal_funding_transfer_usd=(item.terminal_funding_transfer_usd),
        )
    samples = tuple(result.value_samples)
    return EconomicResult(
        opening_capital_usd=result.bankroll_usd,
        closing_cash_usd=result.final_value,
        max_drawdown=signed_max_drawdown(samples, result.bankroll_usd),
        terminal_liquidation_cost_usd=result.terminal_liquidation_cost,
        total_fees_usd=result.total_fees,
        total_fixed_cost_usd=max(fixed_cost, 0.0),
        total_variable_cost_usd=result.total_variable_execution_cost_usd,
        external_marked_notional_usd=result.external_swap_notional_usd,
        external_input_value_usd=result.external_input_value_usd,
        external_output_value_usd=result.external_output_value_usd,
        internal_cross_notional_usd=result.internal_netting_notional_usd,
        entry_scale_events=result.entry_scale_events,
        terminal_position_settlement_count=(result.terminal_position_settlement_count),
        terminal_loose_cngn_settlement_count=(result.terminal_loose_cngn_settlement_count),
        terminal_zero_settlement_count=result.terminal_zero_settlement_count,
        terminal_inventory_swap_count=result.terminal_inventory_swap_count,
        terminal_fixed_cost_usd=result.terminal_fixed_cost_usd,
        terminal_variable_cost_usd=result.terminal_variable_cost_usd,
        terminal_external_marked_notional_usd=(result.terminal_external_marked_notional_usd),
        value_samples=samples,
        attribution=attribution,
    )


def economic_result_from_sim(
    result: SimResult,
    opening_capital_usd: float,
) -> EconomicResult:
    if (
        not result.settled_to_cash
        or result.terminal_open_position_count != 0
        or result.terminal_cngn_amount != 0.0
    ):
        raise TerminalLiquidationError("standalone result is not completely settled to USD cash")
    fixed_cost = result.total_gas_cost + result.total_failed_tx_expected_cost
    variable_cost = result.total_variable_execution_cost_usd
    tolerance = 1e-9 * max(
        1.0,
        result.total_transaction_cost,
        fixed_cost,
        variable_cost,
    )
    if abs(result.total_transaction_cost - fixed_cost - variable_cost) > tolerance:
        raise ExecutionAccountingError("standalone transaction cost components do not reconcile")
    samples = tuple(result.value_samples)
    return EconomicResult(
        opening_capital_usd=opening_capital_usd,
        closing_cash_usd=result.final_value,
        max_drawdown=signed_max_drawdown(samples, opening_capital_usd),
        terminal_liquidation_cost_usd=result.terminal_liquidation_cost,
        total_fees_usd=result.total_fees,
        total_fixed_cost_usd=fixed_cost,
        total_variable_cost_usd=variable_cost,
        external_marked_notional_usd=result.external_swap_notional_usd,
        external_input_value_usd=result.external_input_value_usd,
        external_output_value_usd=result.external_output_value_usd,
        internal_cross_notional_usd=0.0,
        entry_scale_events=(),
        terminal_position_settlement_count=(result.terminal_position_settlement_count),
        terminal_loose_cngn_settlement_count=(result.terminal_loose_cngn_settlement_count),
        terminal_zero_settlement_count=result.terminal_zero_settlement_count,
        terminal_inventory_swap_count=result.terminal_inventory_swap_count,
        terminal_fixed_cost_usd=result.terminal_fixed_cost_usd,
        terminal_variable_cost_usd=result.terminal_variable_cost_usd,
        terminal_external_marked_notional_usd=(result.terminal_external_marked_notional_usd),
        value_samples=samples,
    )


@dataclass(frozen=True)
class CarriedWindowOutcome:
    method_id: str
    method_kind: str
    window_index: int
    status: PathStatus
    blocking_status: InvalidStatus | None
    blocking_window_index: int | None
    opening_capital_usd: float | None
    closing_cash_usd: float | None
    result: EconomicResult | None
    failure: FailureDiagnostic | None

    def __post_init__(self) -> None:
        if self.status == "valid":
            if self.failure is not None:
                raise ValueError("valid carried outcome cannot contain a failure")
        elif self.failure is None:
            raise ValueError("invalid carried outcome requires a failure diagnostic")


@dataclass(frozen=True)
class CarriedPathSnapshot:
    method_id: str
    method_kind: str
    next_opening_capital_usd: float
    blocking_status: InvalidStatus | None
    blocking_window_index: int | None
    failure: FailureDiagnostic | None

    def __post_init__(self) -> None:
        if not self.method_id or not self.method_kind:
            raise ValueError("carried path snapshot requires method identity")
        if not math.isfinite(self.next_opening_capital_usd) or self.next_opening_capital_usd < 0:
            raise ValueError("carried path snapshot requires finite non-negative capital")
        if (self.blocking_status is None) != (self.blocking_window_index is None):
            raise ValueError("blocking status and window must be present together")
        if (self.blocking_status is None) != (self.failure is None):
            raise ValueError("blocking state and failure must be present together")
        if self.blocking_window_index is not None and self.blocking_window_index < 0:
            raise ValueError("blocking window index must be non-negative")


@dataclass
class CarriedPathState:
    method_id: str
    method_kind: str
    next_opening_capital_usd: float
    blocking_status: InvalidStatus | None = None
    blocking_window_index: int | None = None
    failure: FailureDiagnostic | None = None

    def snapshot(self) -> CarriedPathSnapshot:
        return CarriedPathSnapshot(
            method_id=self.method_id,
            method_kind=self.method_kind,
            next_opening_capital_usd=self.next_opening_capital_usd,
            blocking_status=self.blocking_status,
            blocking_window_index=self.blocking_window_index,
            failure=self.failure,
        )

    @classmethod
    def from_snapshot(cls, snapshot: CarriedPathSnapshot) -> CarriedPathState:
        return cls(
            method_id=snapshot.method_id,
            method_kind=snapshot.method_kind,
            next_opening_capital_usd=snapshot.next_opening_capital_usd,
            blocking_status=snapshot.blocking_status,
            blocking_window_index=snapshot.blocking_window_index,
            failure=snapshot.failure,
        )

    def evaluate_window(
        self,
        window_index: int,
        evaluator: Callable[[float], EconomicResult],
    ) -> CarriedWindowOutcome:
        if self.blocking_status is not None:
            return CarriedWindowOutcome(
                method_id=self.method_id,
                method_kind=self.method_kind,
                window_index=window_index,
                status="blocked_prior_invalid",
                blocking_status=self.blocking_status,
                blocking_window_index=self.blocking_window_index,
                opening_capital_usd=None,
                closing_cash_usd=None,
                result=None,
                failure=self.failure,
            )
        opening = self.next_opening_capital_usd
        if not math.isfinite(opening) or opening <= 0:
            return self._block(
                window_index,
                "invalid_opening_capital",
                FailureDiagnostic(
                    "InvalidOpeningCapital",
                    "carried opening capital must be finite and positive",
                ),
            )
        try:
            result = evaluator(opening)
            tolerance = 1e-12 * max(1.0, opening, result.opening_capital_usd)
            if abs(result.opening_capital_usd - opening) > tolerance:
                raise ExecutionAccountingError(
                    "economic result opening capital does not match carried state"
                )
        except LiquidityShareExceeded as exc:
            return self._block(
                window_index,
                "invalid_liquidity_cap",
                FailureDiagnostic.from_exception(exc),
            )
        except NoValidationSwapError as exc:
            return self._block(
                window_index,
                "invalid_no_validation_swap",
                FailureDiagnostic.from_exception(exc),
            )
        except TerminalLiquidationError as exc:
            return self._block(
                window_index,
                "invalid_terminal_liquidation",
                FailureDiagnostic.from_exception(exc),
            )
        except ExecutionAccountingError as exc:
            return self._block(
                window_index,
                "invalid_execution_accounting",
                FailureDiagnostic.from_exception(exc),
            )

        self.next_opening_capital_usd = result.closing_cash_usd
        return CarriedWindowOutcome(
            method_id=self.method_id,
            method_kind=self.method_kind,
            window_index=window_index,
            status="valid",
            blocking_status=None,
            blocking_window_index=None,
            opening_capital_usd=opening,
            closing_cash_usd=result.closing_cash_usd,
            result=result,
            failure=None,
        )

    def _block(
        self,
        window_index: int,
        status: InvalidStatus,
        failure: FailureDiagnostic,
    ) -> CarriedWindowOutcome:
        self.blocking_status = status
        self.blocking_window_index = window_index
        self.failure = failure
        return CarriedWindowOutcome(
            method_id=self.method_id,
            method_kind=self.method_kind,
            window_index=window_index,
            status=status,
            blocking_status=status,
            blocking_window_index=window_index,
            opening_capital_usd=self.next_opening_capital_usd,
            closing_cash_usd=None,
            result=None,
            failure=failure,
        )
