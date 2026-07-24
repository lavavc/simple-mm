"""One-window contracts for the corrected weighted-portfolio evaluation."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Literal, Mapping, Sequence, TypeVar, cast

from research.backtester import metrics
from research.backtester.data import Event
from research.backtester.params import BacktestParams
from research.backtester.pool_state import PoolState
from research.backtester.portfolio_allocation import (
    Allocation,
    AllocationRule,
    TrainingMetrics,
    equal_config_weights,
    equal_family_weights,
    is_eligible,
    rank_one_comparator_allocation,
    remove_sleeve_from_allocation,
    shrinkage_weights,
)
from research.backtester.portfolio_catalog import (
    PortfolioCatalog,
    SleeveDefinition,
)
from research.backtester.portfolio_comparators import (
    cash_comparator_result,
    mark_hold_cngn_result,
    routed_hold_cngn_result,
    valid_valuation_swaps,
)
from research.backtester.portfolio_errors import (
    ExecutionAccountingError,
    NoValidationSwapError,
)
from research.backtester.portfolio_path import (
    CarriedPathSnapshot,
    CarriedPathState,
    CarriedWindowOutcome,
    EconomicAttribution,
    EconomicResult,
    FailureDiagnostic,
    InvalidStatus,
    PathStatus,
    economic_result_from_portfolio,
    economic_result_from_sim,
)
from research.backtester.portfolio_simulator import (
    EntryScaleEvent,
    LiquidityShareExceeded,
    simulate_portfolio,
)
from research.backtester.position_runtime import TerminalLiquidationError
from research.backtester.run import WindowSlice, _build_pool_state
from research.backtester.simulator import PoolConfig, simulate_pool
from research.scripts.evaluate_directional_paper_lp import route_directional_archetype

ALLOCATION_RULE_IDS = ("equal_config", "equal_family", "shrinkage")
COMPARATOR_IDS = (
    "cash",
    "hold_cngn_mark",
    "hold_cngn_pool_routed",
    "static_spot_w0025",
    "ewma_rank_one",
    "paper_exclusive_rank_one",
    "frozen_rank_one",
    "directional_rank_one",
)
PRIMARY_METHOD_IDS = (*ALLOCATION_RULE_IDS, *COMPARATOR_IDS)
INVALID_PATH_STATUSES = (
    "invalid_liquidity_cap",
    "invalid_no_validation_swap",
    "invalid_execution_accounting",
    "invalid_terminal_liquidation",
    "invalid_opening_capital",
)

TrainingStatus = Literal[
    "valid",
    "no_position",
    "invalid_no_validation_swap",
    "invalid_execution_accounting",
    "invalid_terminal_liquidation",
]
RouteStatus = Literal["routed", "no_position"]
SelectionStatus = Literal[
    "not_applicable",
    "predeclared",
    "selected",
    "no_eligible",
]
CandidateMatrixStatus = Literal["complete_valid", "invalid_incomplete_matrix"]
ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True)
class EconomicSummary:
    opening_capital_usd: float
    closing_cash_usd: float
    window_net_return: float
    max_drawdown: float
    terminal_liquidation_cost_usd: float
    total_fees_usd: float
    total_fixed_cost_usd: float
    total_variable_cost_usd: float
    external_marked_notional_usd: float
    external_input_value_usd: float
    external_output_value_usd: float
    internal_cross_notional_usd: float
    entry_action_batch_count: int
    scaled_entry_action_batch_count: int
    minimum_entry_execution_scale: float
    terminal_position_settlement_count: int
    terminal_loose_cngn_settlement_count: int
    terminal_zero_settlement_count: int
    terminal_inventory_swap_count: int
    terminal_fixed_cost_usd: float
    terminal_variable_cost_usd: float
    terminal_external_marked_notional_usd: float
    value_sample_count: int

    def __post_init__(self) -> None:
        numeric = (
            self.opening_capital_usd,
            self.closing_cash_usd,
            self.window_net_return,
            self.max_drawdown,
            self.terminal_liquidation_cost_usd,
            self.total_fees_usd,
            self.total_fixed_cost_usd,
            self.total_variable_cost_usd,
            self.external_marked_notional_usd,
            self.external_input_value_usd,
            self.external_output_value_usd,
            self.internal_cross_notional_usd,
            self.minimum_entry_execution_scale,
            self.terminal_fixed_cost_usd,
            self.terminal_variable_cost_usd,
            self.terminal_external_marked_notional_usd,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("economic summary values must be finite")
        if self.opening_capital_usd <= 0.0 or self.closing_cash_usd < 0.0:
            raise ValueError("economic summary capital is invalid")
        expected_return = self.closing_cash_usd / self.opening_capital_usd - 1.0
        if not math.isclose(
            self.window_net_return,
            expected_return,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("economic summary return does not reconcile to capital")
        if self.max_drawdown > 0.0 or self.max_drawdown < -1.0:
            raise ValueError("economic summary drawdown is invalid")
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
        if any(value < 0.0 for value in non_negative):
            raise ValueError("economic summary accounting fields must be non-negative")
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
            raise ValueError("economic summary execution values do not reconcile")
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (
                    self.entry_action_batch_count,
                    self.scaled_entry_action_batch_count,
                    self.terminal_position_settlement_count,
                    self.terminal_loose_cngn_settlement_count,
                    self.terminal_zero_settlement_count,
                    self.terminal_inventory_swap_count,
                )
            )
            or self.scaled_entry_action_batch_count > self.entry_action_batch_count
            or not 0.0 <= self.minimum_entry_execution_scale <= 1.0
            or self.terminal_inventory_swap_count > 1
        ):
            raise ValueError("economic summary execution diagnostics are invalid")
        if self.entry_action_batch_count == 0 and self.minimum_entry_execution_scale != 1.0:
            raise ValueError("empty entry trace must have unit minimum scale")
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
            raise ValueError("economic summary terminal costs do not reconcile")
        if (
            isinstance(self.value_sample_count, bool)
            or not isinstance(self.value_sample_count, int)
            or self.value_sample_count < 2
        ):
            raise ValueError("economic summary requires at least two value samples")


@dataclass(frozen=True)
class TrainingRecord:
    economic_id: str
    family: str
    routed_config_name: str
    status: TrainingStatus
    eligible: bool
    metrics: TrainingMetrics | None
    failure: FailureDiagnostic | None

    def __post_init__(self) -> None:
        if self.status in {"valid", "no_position"}:
            if self.failure is not None:
                raise ValueError("valid training record cannot contain a failure")
        elif self.failure is None:
            raise ValueError("invalid training record requires a failure")


@dataclass(frozen=True)
class CandidateResetRecord:
    economic_id: str
    family: str
    routed_config_name: str
    route_status: RouteStatus
    status: PathStatus
    episode_count: int | None
    economics: EconomicSummary | None
    failure: FailureDiagnostic | None

    def __post_init__(self) -> None:
        if not self.economic_id or not self.family or not self.routed_config_name:
            raise ValueError("candidate reset record requires identity")
        valid_episode_count = (
            isinstance(self.episode_count, int)
            and not isinstance(self.episode_count, bool)
            and self.episode_count >= 0
        )
        if self.status == "valid":
            if self.economics is None or not valid_episode_count or self.failure is not None:
                raise ValueError("valid candidate requires economics and non-negative episodes")
        elif self.status in {
            "invalid_no_validation_swap",
            "invalid_execution_accounting",
            "invalid_terminal_liquidation",
        }:
            if self.economics is not None or self.episode_count is not None or self.failure is None:
                raise ValueError("invalid candidate cannot contain economic results")
        else:
            raise ValueError("candidate reset status is unsupported")
        if self.route_status == "no_position":
            if (
                self.family != "directional"
                or self.routed_config_name != "no_position"
                or self.status != "valid"
                or self.episode_count != 0
                or self.economics is None
                or self.economics.window_net_return != 0.0
                or self.economics.closing_cash_usd != self.economics.opening_capital_usd
                or any(
                    value != 0.0
                    for value in (
                        self.economics.max_drawdown,
                        self.economics.terminal_liquidation_cost_usd,
                        self.economics.total_fees_usd,
                        self.economics.total_fixed_cost_usd,
                        self.economics.total_variable_cost_usd,
                        self.economics.external_marked_notional_usd,
                        self.economics.external_input_value_usd,
                        self.economics.external_output_value_usd,
                        self.economics.internal_cross_notional_usd,
                        self.economics.entry_action_batch_count,
                        self.economics.scaled_entry_action_batch_count,
                        self.economics.terminal_position_settlement_count,
                        self.economics.terminal_loose_cngn_settlement_count,
                        self.economics.terminal_zero_settlement_count,
                        self.economics.terminal_inventory_swap_count,
                        self.economics.terminal_fixed_cost_usd,
                        self.economics.terminal_variable_cost_usd,
                        self.economics.terminal_external_marked_notional_usd,
                    )
                )
                or self.economics.minimum_entry_execution_scale != 1.0
            ):
                raise ValueError("no-position candidate must be an exact cash result")
        elif self.route_status == "routed":
            if self.routed_config_name == "no_position":
                raise ValueError("routed candidate requires a concrete configuration")
        else:
            raise ValueError("candidate route status is unsupported")


@dataclass(frozen=True)
class CandidateMatrixFailure:
    economic_id: str
    window_index: int
    status: str
    failure: FailureDiagnostic


@dataclass(frozen=True)
class CandidateMatrixAssessment:
    status: CandidateMatrixStatus
    expected_rows: int
    observed_rows: int
    valid_rows: int
    invalid_rows: int
    invalid_observations: tuple[CandidateMatrixFailure, ...]
    selected_economic_id: str | None


@dataclass(frozen=True)
class ResetOutcome:
    method_id: str
    status: PathStatus
    economics: EconomicSummary | None
    failure: FailureDiagnostic | None
    observed_share: float | None = None
    cap: float | None = None

    def __post_init__(self) -> None:
        if not self.method_id:
            raise ValueError("reset outcome requires a method identity")
        if self.status not in {
            "valid",
            "invalid_liquidity_cap",
            "invalid_no_validation_swap",
            "invalid_execution_accounting",
            "invalid_terminal_liquidation",
        }:
            raise ValueError("reset outcome status is unsupported")
        if self.status == "valid":
            if (
                self.economics is None
                or self.failure is not None
                or self.observed_share is not None
                or self.cap is not None
            ):
                raise ValueError("valid reset outcome requires only economics")
        elif self.economics is not None or self.failure is None:
            raise ValueError("invalid reset outcome cannot contain economics")
        if self.status == "invalid_liquidity_cap":
            if self.observed_share is None or self.cap is None:
                raise ValueError("liquidity-cap outcome requires observed share and cap")
        elif self.observed_share is not None or self.cap is not None:
            raise ValueError("only liquidity-cap outcomes may contain cap evidence")


@dataclass(frozen=True)
class ComparatorPlan:
    comparator_id: str
    selection_status: SelectionStatus
    selected_economic_id: str | None
    allocation: Allocation | None

    def __post_init__(self) -> None:
        if not self.comparator_id:
            raise ValueError("comparator plan requires an identity")
        if self.selection_status == "not_applicable":
            if self.selected_economic_id is not None or self.allocation is not None:
                raise ValueError("not-applicable comparator cannot select a sleeve")
            return
        if self.selection_status == "no_eligible":
            if (
                self.selected_economic_id is not None
                or self.allocation is None
                or self.allocation.weights
                or self.allocation.cash_weight != 1.0
            ):
                raise ValueError("no-eligible comparator must remain entirely in cash")
            return
        if self.selection_status not in {"predeclared", "selected"}:
            raise ValueError("comparator selection status is unsupported")
        if (
            self.selected_economic_id is None
            or self.allocation is None
            or set(self.allocation.weights) != {self.selected_economic_id}
            or self.allocation.weights[self.selected_economic_id] <= 0.0
        ):
            raise ValueError("selected comparator must bind one allocated sleeve")


@dataclass(frozen=True)
class PrimaryWindowRecord:
    pool: str
    window_index: int
    window_start: str
    window_end: str
    train_swap_count: int
    val_swap_count: int
    routed_config_names: Mapping[str, str]
    training: tuple[TrainingRecord, ...]
    allocations: Mapping[str, Allocation]
    candidates: tuple[CandidateResetRecord, ...]
    reset_rules: Mapping[str, ResetOutcome]
    carried_rules: Mapping[str, CarriedWindowOutcome]
    comparator_plans: Mapping[str, ComparatorPlan]
    comparators: Mapping[str, CarriedWindowOutcome]
    path_snapshots: Mapping[str, CarriedPathSnapshot]

    def __post_init__(self) -> None:
        if not self.pool or self.window_index < 0:
            raise ValueError("primary window identity is invalid")
        if tuple(self.allocations) != ALLOCATION_RULE_IDS:
            raise ValueError("primary window requires exactly three allocations")
        if tuple(self.reset_rules) != ALLOCATION_RULE_IDS:
            raise ValueError("primary window requires exactly three reset outcomes")
        if tuple(self.carried_rules) != ALLOCATION_RULE_IDS:
            raise ValueError("primary window requires exactly three carried rules")
        if tuple(self.comparator_plans) != COMPARATOR_IDS:
            raise ValueError("primary window requires exactly eight comparator plans")
        if tuple(self.comparators) != COMPARATOR_IDS:
            raise ValueError("primary window requires exactly eight comparators")
        if set(self.path_snapshots) != set(PRIMARY_METHOD_IDS):
            raise ValueError("primary window path snapshots are incomplete")
        for rule in ALLOCATION_RULE_IDS:
            if self.allocations[rule].rule != rule:
                raise ValueError("allocation rule identity is incoherent")
            if self.reset_rules[rule].method_id != rule:
                raise ValueError("reset outcome identity is incoherent")
            carried = self.carried_rules[rule]
            snapshot = self.path_snapshots[rule]
            if (
                carried.method_id != rule
                or carried.method_kind != "allocation_rule"
                or carried.window_index != self.window_index
                or snapshot.method_id != rule
                or snapshot.method_kind != "allocation_rule"
            ):
                raise ValueError("carried allocation identity is incoherent")
        for comparator_id in COMPARATOR_IDS:
            plan = self.comparator_plans[comparator_id]
            outcome = self.comparators[comparator_id]
            snapshot = self.path_snapshots[comparator_id]
            if plan.comparator_id != comparator_id:
                raise ValueError("comparator plan identity is incoherent")
            if (
                outcome.method_id != comparator_id
                or outcome.method_kind != "comparator"
                or outcome.window_index != self.window_index
                or snapshot.method_id != comparator_id
                or snapshot.method_kind != "comparator"
            ):
                raise ValueError("comparator path identity is incoherent")
        candidate_ids = [row.economic_id for row in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("primary window has duplicate candidate rows")


@dataclass(frozen=True)
class RemovalWindowRecord:
    pool: str
    window_index: int
    window_start: str
    window_end: str
    removed_economic_id: str
    removed_allocations: Mapping[str, Allocation]
    outcomes: Mapping[str, CarriedWindowOutcome]
    path_snapshots: Mapping[str, CarriedPathSnapshot]

    def __post_init__(self) -> None:
        if not self.pool or self.window_index < 0 or not self.removed_economic_id:
            raise ValueError("removal window identity is invalid")
        if tuple(self.removed_allocations) != ALLOCATION_RULE_IDS:
            raise ValueError("removal window requires exactly three allocations")
        if tuple(self.outcomes) != ALLOCATION_RULE_IDS:
            raise ValueError("removal window requires exactly three outcomes")
        if set(self.path_snapshots) != set(ALLOCATION_RULE_IDS):
            raise ValueError("removal window path snapshots are incomplete")
        for rule in ALLOCATION_RULE_IDS:
            if self.removed_allocations[rule].rule != rule:
                raise ValueError("removed allocation rule identity is incoherent")
            outcome = self.outcomes[rule]
            snapshot = self.path_snapshots[rule]
            if (
                outcome.method_id != rule
                or outcome.method_kind != "best_sleeve_removal"
                or outcome.window_index != self.window_index
                or snapshot.method_id != rule
                or snapshot.method_kind != "best_sleeve_removal"
            ):
                raise ValueError("removal path identity is incoherent")


PathRecord = TypeVar("PathRecord", PrimaryWindowRecord, RemovalWindowRecord)


def create_primary_path_states(
    reference_bankroll_usd: float,
) -> dict[str, CarriedPathState]:
    if not math.isfinite(reference_bankroll_usd) or reference_bankroll_usd <= 0:
        raise ValueError("reference bankroll must be finite positive")
    return {
        method_id: CarriedPathState(
            method_id,
            "allocation_rule" if method_id in ALLOCATION_RULE_IDS else "comparator",
            reference_bankroll_usd,
        )
        for method_id in PRIMARY_METHOD_IDS
    }


def create_removal_path_states(
    reference_bankroll_usd: float,
) -> dict[str, CarriedPathState]:
    if not math.isfinite(reference_bankroll_usd) or reference_bankroll_usd <= 0:
        raise ValueError("reference bankroll must be finite positive")
    return {
        rule: CarriedPathState(rule, "best_sleeve_removal", reference_bankroll_usd)
        for rule in ALLOCATION_RULE_IDS
    }


def restore_primary_path_states(
    records: Sequence[PrimaryWindowRecord],
    reference_bankroll_usd: float,
) -> dict[str, CarriedPathState]:
    if not records:
        return create_primary_path_states(reference_bankroll_usd)
    _validate_path_sequence(
        records,
        PRIMARY_METHOD_IDS,
        reference_bankroll_usd,
        lambda record, method_id: (
            record.carried_rules[method_id]
            if method_id in record.carried_rules
            else record.comparators[method_id]
        ),
    )
    return {
        method_id: CarriedPathState.from_snapshot(records[-1].path_snapshots[method_id])
        for method_id in PRIMARY_METHOD_IDS
    }


def restore_removal_path_states(
    records: Sequence[RemovalWindowRecord],
    reference_bankroll_usd: float,
) -> dict[str, CarriedPathState]:
    if not records:
        return create_removal_path_states(reference_bankroll_usd)
    removed_ids = {record.removed_economic_id for record in records}
    if len(removed_ids) != 1:
        raise ValueError("removal checkpoints bind more than one economic ID")
    _validate_path_sequence(
        records,
        ALLOCATION_RULE_IDS,
        reference_bankroll_usd,
        lambda record, method_id: record.outcomes[method_id],
    )
    return {
        rule: CarriedPathState.from_snapshot(records[-1].path_snapshots[rule])
        for rule in ALLOCATION_RULE_IDS
    }


def _validate_record_prefix(
    records: Sequence[PrimaryWindowRecord | RemovalWindowRecord],
) -> None:
    indexes = [record.window_index for record in records]
    if indexes != list(range(len(records))):
        raise ValueError("checkpoint records are not a contiguous window prefix")
    if len({record.pool for record in records}) != 1:
        raise ValueError("checkpoint records contain more than one pool")


def _same_capital(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def _validate_path_sequence(
    records: Sequence[PathRecord],
    method_ids: Sequence[str],
    reference_bankroll_usd: float,
    outcome_for: Callable[[PathRecord, str], CarriedWindowOutcome],
) -> None:
    _validate_record_prefix(records)
    expected_capital = {method_id: reference_bankroll_usd for method_id in method_ids}
    blocking: dict[
        str,
        tuple[InvalidStatus, int, FailureDiagnostic] | None,
    ] = {method_id: None for method_id in method_ids}
    for record in records:
        for method_id in method_ids:
            outcome = outcome_for(record, method_id)
            snapshot = record.path_snapshots[method_id]
            expected_kind = (
                "best_sleeve_removal"
                if isinstance(record, RemovalWindowRecord)
                else "allocation_rule"
                if method_id in ALLOCATION_RULE_IDS
                else "comparator"
            )
            if (
                outcome.method_id != method_id
                or snapshot.method_id != method_id
                or outcome.method_kind != expected_kind
                or snapshot.method_kind != expected_kind
                or outcome.window_index != record.window_index
            ):
                raise ValueError("checkpoint method identity is incoherent")
            expected = expected_capital[method_id]
            prior_block = blocking[method_id]
            if prior_block is not None:
                if (
                    outcome.status != "blocked_prior_invalid"
                    or outcome.result is not None
                    or outcome.opening_capital_usd is not None
                    or outcome.closing_cash_usd is not None
                    or outcome.blocking_status != prior_block[0]
                    or outcome.blocking_window_index != prior_block[1]
                    or snapshot.blocking_status != prior_block[0]
                    or snapshot.blocking_window_index != prior_block[1]
                    or outcome.failure != prior_block[2]
                    or snapshot.failure != prior_block[2]
                    or not _same_capital(
                        snapshot.next_opening_capital_usd,
                        expected,
                    )
                ):
                    raise ValueError("blocked checkpoint path is incoherent")
                continue
            if outcome.status == "valid":
                if (
                    outcome.result is None
                    or outcome.opening_capital_usd is None
                    or outcome.closing_cash_usd is None
                    or outcome.blocking_status is not None
                    or outcome.blocking_window_index is not None
                    or snapshot.blocking_status is not None
                    or snapshot.blocking_window_index is not None
                    or outcome.failure is not None
                    or snapshot.failure is not None
                    or not _same_capital(outcome.opening_capital_usd, expected)
                    or not _same_capital(
                        outcome.result.opening_capital_usd,
                        expected,
                    )
                    or not _same_capital(
                        outcome.result.closing_cash_usd,
                        outcome.closing_cash_usd,
                    )
                    or not _same_capital(
                        snapshot.next_opening_capital_usd,
                        outcome.closing_cash_usd,
                    )
                ):
                    raise ValueError("checkpoint snapshot closing capital drifted")
                expected_capital[method_id] = outcome.closing_cash_usd
                continue
            if outcome.status not in INVALID_PATH_STATUSES:
                raise ValueError("checkpoint path status is unsupported")
            if (
                outcome.result is not None
                or outcome.opening_capital_usd is None
                or outcome.closing_cash_usd is not None
                or not _same_capital(outcome.opening_capital_usd, expected)
                or outcome.blocking_status != outcome.status
                or outcome.blocking_window_index != record.window_index
                or snapshot.blocking_status != outcome.status
                or snapshot.blocking_window_index != record.window_index
                or outcome.failure is None
                or snapshot.failure != outcome.failure
                or not _same_capital(snapshot.next_opening_capital_usd, expected)
            ):
                raise ValueError("invalid checkpoint path is incoherent")
            blocking[method_id] = (
                outcome.status,
                record.window_index,
                outcome.failure,
            )


def route_directional_catalog(
    catalog: PortfolioCatalog,
    entry_state: Mapping[str, str],
) -> PortfolioCatalog:
    """Resolve each route-aware policy to its boundary-selected component."""
    archetype, _ = route_directional_archetype(dict(entry_state))
    routed = list(catalog.sleeves)
    if archetype != "no_position":
        for policy in catalog.directional_policies:
            member = policy.archetypes.get(archetype)
            if member is None:
                raise ValueError(
                    f"directional policy {policy.sleeve_id} lacks archetype {archetype}"
                )
            routed.append(
                replace(
                    member,
                    sleeve_id=policy.sleeve_id,
                    family="directional",
                )
            )
    return PortfolioCatalog(catalog.pool, tuple(routed), ())


def economic_summary(result: EconomicResult) -> EconomicSummary:
    return EconomicSummary(
        opening_capital_usd=result.opening_capital_usd,
        closing_cash_usd=result.closing_cash_usd,
        window_net_return=result.window_net_return,
        max_drawdown=result.max_drawdown,
        terminal_liquidation_cost_usd=result.terminal_liquidation_cost_usd,
        total_fees_usd=result.total_fees_usd,
        total_fixed_cost_usd=result.total_fixed_cost_usd,
        total_variable_cost_usd=result.total_variable_cost_usd,
        external_marked_notional_usd=result.external_marked_notional_usd,
        external_input_value_usd=result.external_input_value_usd,
        external_output_value_usd=result.external_output_value_usd,
        internal_cross_notional_usd=result.internal_cross_notional_usd,
        entry_action_batch_count=result.entry_action_batch_count,
        scaled_entry_action_batch_count=result.scaled_entry_action_batch_count,
        minimum_entry_execution_scale=result.minimum_entry_execution_scale,
        terminal_position_settlement_count=(result.terminal_position_settlement_count),
        terminal_loose_cngn_settlement_count=(result.terminal_loose_cngn_settlement_count),
        terminal_zero_settlement_count=result.terminal_zero_settlement_count,
        terminal_inventory_swap_count=result.terminal_inventory_swap_count,
        terminal_fixed_cost_usd=result.terminal_fixed_cost_usd,
        terminal_variable_cost_usd=result.terminal_variable_cost_usd,
        terminal_external_marked_notional_usd=(result.terminal_external_marked_notional_usd),
        value_sample_count=len(result.value_samples),
    )


def _report_unit_progress(
    callback: ProgressCallback | None,
    phase: str,
    completed_units: int,
    total_units: int,
) -> None:
    if callback is not None and (completed_units == total_units or completed_units % 250 == 0):
        callback(phase, completed_units, total_units)


def _training_records(
    catalog: PortfolioCatalog,
    routed: PortfolioCatalog,
    events: Sequence[Event],
    pool_config: PoolConfig,
    reference_bankroll_usd: float,
    progress_callback: ProgressCallback | None,
) -> tuple[TrainingRecord, ...]:
    routed_by_id = {sleeve.sleeve_id: sleeve for sleeve in routed.sleeves}
    records: list[TrainingRecord] = []
    total_units = len(catalog.allocation_units)
    for completed_units, unit in enumerate(catalog.allocation_units, start=1):
        sleeve = routed_by_id.get(unit.sleeve_id)
        if sleeve is None:
            if unit.family != "directional":
                raise ValueError(f"non-directional unit {unit.sleeve_id!r} was not routed")
            records.append(
                TrainingRecord(
                    economic_id=unit.sleeve_id,
                    family=unit.family,
                    routed_config_name="no_position",
                    status="no_position",
                    eligible=False,
                    metrics=None,
                    failure=None,
                )
            )
            _report_unit_progress(
                progress_callback,
                "training",
                completed_units,
                total_units,
            )
            continue
        failure: FailureDiagnostic | None = None
        try:
            simulation = simulate_pool(
                list(events),
                sleeve.params,
                pool_config,
                reference_bankroll_usd,
                settle_to_cash=True,
            )
            result = economic_result_from_sim(simulation, reference_bankroll_usd)
            training = TrainingMetrics(
                net_return=result.window_net_return,
                episode_count=len(simulation.episodes),
                fee_to_transaction_cost_ratio=metrics.fee_to_transaction_cost_ratio(simulation),
                max_drawdown=result.max_drawdown,
            )
        except NoValidationSwapError as exc:
            status: TrainingStatus = "invalid_no_validation_swap"
            training = None
            failure = FailureDiagnostic.from_exception(exc)
        except TerminalLiquidationError as exc:
            status = "invalid_terminal_liquidation"
            training = None
            failure = FailureDiagnostic.from_exception(exc)
        except ExecutionAccountingError as exc:
            status = "invalid_execution_accounting"
            training = None
            failure = FailureDiagnostic.from_exception(exc)
        else:
            status = "valid"
        records.append(
            TrainingRecord(
                economic_id=unit.sleeve_id,
                family=unit.family,
                routed_config_name=sleeve.config_name,
                status=status,
                eligible=training is not None and is_eligible(training),
                metrics=training,
                failure=failure,
            )
        )
        _report_unit_progress(
            progress_callback,
            "training",
            completed_units,
            total_units,
        )
    return tuple(records)


def _candidate_records(
    catalog: PortfolioCatalog,
    routed: PortfolioCatalog,
    events: list[Event],
    pool_config: PoolConfig,
    reference_bankroll_usd: float,
    initial_pool_state: PoolState,
    progress_callback: ProgressCallback | None,
) -> tuple[CandidateResetRecord, ...]:
    routed_by_id = {sleeve.sleeve_id: sleeve for sleeve in routed.sleeves}
    records: list[CandidateResetRecord] = []
    total_units = len(catalog.allocation_units)
    for completed_units, unit in enumerate(catalog.allocation_units, start=1):
        sleeve = routed_by_id.get(unit.sleeve_id)
        if sleeve is None:
            if unit.family != "directional":
                raise ValueError(f"non-directional unit {unit.sleeve_id!r} was not routed")
            result = cash_comparator_result(
                events,
                pool_config,
                reference_bankroll_usd,
            )
            records.append(
                CandidateResetRecord(
                    economic_id=unit.sleeve_id,
                    family=unit.family,
                    routed_config_name="no_position",
                    route_status="no_position",
                    status="valid",
                    episode_count=0,
                    economics=economic_summary(result),
                    failure=None,
                )
            )
            _report_unit_progress(
                progress_callback,
                "candidate_reset",
                completed_units,
                total_units,
            )
            continue
        status: PathStatus = "valid"
        result_summary: EconomicSummary | None = None
        episode_count: int | None = None
        failure: FailureDiagnostic | None = None
        try:
            simulation = simulate_pool(
                events,
                sleeve.params,
                pool_config,
                reference_bankroll_usd,
                initial_pool_state=initial_pool_state,
                settle_to_cash=True,
            )
            result_summary = economic_summary(
                economic_result_from_sim(simulation, reference_bankroll_usd)
            )
            episode_count = len(simulation.episodes)
        except NoValidationSwapError as exc:
            status = "invalid_no_validation_swap"
            failure = FailureDiagnostic.from_exception(exc)
        except TerminalLiquidationError as exc:
            status = "invalid_terminal_liquidation"
            failure = FailureDiagnostic.from_exception(exc)
        except ExecutionAccountingError as exc:
            status = "invalid_execution_accounting"
            failure = FailureDiagnostic.from_exception(exc)
        records.append(
            CandidateResetRecord(
                economic_id=unit.sleeve_id,
                family=unit.family,
                routed_config_name=sleeve.config_name,
                route_status="routed",
                status=status,
                episode_count=episode_count,
                economics=result_summary,
                failure=failure,
            )
        )
        _report_unit_progress(
            progress_callback,
            "candidate_reset",
            completed_units,
            total_units,
        )
    return tuple(records)


def _portfolio_result(
    *,
    events: Sequence[Event],
    routed_sleeves: Sequence[SleeveDefinition],
    allocation: Allocation,
    pool_config: PoolConfig,
    opening_capital_usd: float,
    initial_pool_state: PoolState,
) -> EconomicResult:
    simulation = simulate_portfolio(
        events=events,
        sleeves=routed_sleeves,
        allocation=allocation,
        pool_config=pool_config,
        bankroll_usd=opening_capital_usd,
        settle_to_cash=True,
        initial_pool_state=initial_pool_state,
    )
    return economic_result_from_portfolio(simulation)


def _reset_outcome(
    method_id: str,
    evaluator: Callable[[], EconomicResult],
) -> ResetOutcome:
    try:
        result = evaluator()
    except LiquidityShareExceeded as exc:
        return ResetOutcome(
            method_id,
            "invalid_liquidity_cap",
            None,
            FailureDiagnostic.from_exception(exc),
            observed_share=exc.observed_share,
            cap=exc.cap,
        )
    except NoValidationSwapError as exc:
        return ResetOutcome(
            method_id,
            "invalid_no_validation_swap",
            None,
            FailureDiagnostic.from_exception(exc),
        )
    except TerminalLiquidationError as exc:
        return ResetOutcome(
            method_id,
            "invalid_terminal_liquidation",
            None,
            FailureDiagnostic.from_exception(exc),
        )
    except ExecutionAccountingError as exc:
        return ResetOutcome(
            method_id,
            "invalid_execution_accounting",
            None,
            FailureDiagnostic.from_exception(exc),
        )
    return ResetOutcome(method_id, "valid", economic_summary(result), None)


def _portfolio_evaluator(
    *,
    events: Sequence[Event],
    routed_sleeves: Sequence[SleeveDefinition],
    allocation: Allocation,
    pool_config: PoolConfig,
    initial_pool_state: PoolState,
) -> Callable[[float], EconomicResult]:
    def evaluate(opening_capital_usd: float) -> EconomicResult:
        return _portfolio_result(
            events=events,
            routed_sleeves=routed_sleeves,
            allocation=allocation,
            pool_config=pool_config,
            opening_capital_usd=opening_capital_usd,
            initial_pool_state=initial_pool_state,
        )

    return evaluate


def _reference_evaluator(
    evaluator: Callable[[float], EconomicResult],
    reference_bankroll_usd: float,
) -> Callable[[], EconomicResult]:
    def evaluate() -> EconomicResult:
        return evaluator(reference_bankroll_usd)

    return evaluate


def _comparator_plans(
    catalog: PortfolioCatalog,
    training_metrics: Mapping[str, TrainingMetrics],
) -> dict[str, ComparatorPlan]:
    static = tuple(
        unit
        for unit in catalog.allocation_units
        if unit.family == "static" and getattr(unit, "config_name", None) == "static_spot_w0025"
    )
    if len(static) != 1:
        raise ValueError("catalog requires exactly one static_spot_w0025 comparator")
    static_unit = static[0]
    plans: dict[str, ComparatorPlan] = {
        "cash": ComparatorPlan("cash", "not_applicable", None, None),
        "hold_cngn_mark": ComparatorPlan("hold_cngn_mark", "not_applicable", None, None),
        "hold_cngn_pool_routed": ComparatorPlan(
            "hold_cngn_pool_routed", "not_applicable", None, None
        ),
        "static_spot_w0025": ComparatorPlan(
            "static_spot_w0025",
            "predeclared",
            static_unit.sleeve_id,
            Allocation(
                "rank_one_comparator",
                {static_unit.sleeve_id: 0.10},
                0.90,
            ),
        ),
    }
    for family, comparator_id in (
        ("ewma", "ewma_rank_one"),
        ("paper_exclusive", "paper_exclusive_rank_one"),
        ("frozen", "frozen_rank_one"),
        ("directional", "directional_rank_one"),
    ):
        units = tuple(unit for unit in catalog.allocation_units if unit.family == family)
        allocation = rank_one_comparator_allocation(units, training_metrics)
        selected = next(iter(allocation.weights), None)
        plans[comparator_id] = ComparatorPlan(
            comparator_id=comparator_id,
            selection_status="selected" if selected is not None else "no_eligible",
            selected_economic_id=selected,
            allocation=allocation,
        )
    return {comparator_id: plans[comparator_id] for comparator_id in COMPARATOR_IDS}


def _evaluate_comparator(
    comparator_id: str,
    plan: ComparatorPlan,
    state: CarriedPathState,
    *,
    window_index: int,
    events: Sequence[Event],
    routed_sleeves: Sequence[SleeveDefinition],
    pool_config: PoolConfig,
    initial_pool_state: PoolState,
    static_params: BacktestParams,
) -> CarriedWindowOutcome:
    allocation = plan.allocation
    if (
        comparator_id
        not in {
            "cash",
            "hold_cngn_mark",
            "hold_cngn_pool_routed",
        }
        and allocation is None
    ):
        raise ValueError(f"LP comparator {comparator_id!r} lacks an allocation")

    def evaluator(opening: float) -> EconomicResult:
        if comparator_id == "cash":
            return cash_comparator_result(events, pool_config, opening)
        if comparator_id == "hold_cngn_mark":
            return mark_hold_cngn_result(events, pool_config, opening)
        if comparator_id == "hold_cngn_pool_routed":
            return routed_hold_cngn_result(
                events,
                pool_config,
                static_params,
                opening,
                initial_pool_state=initial_pool_state,
            )
        if plan.selection_status == "no_eligible":
            return cash_comparator_result(events, pool_config, opening)
        assert allocation is not None
        return _portfolio_result(
            events=events,
            routed_sleeves=routed_sleeves,
            allocation=allocation,
            pool_config=pool_config,
            opening_capital_usd=opening,
            initial_pool_state=initial_pool_state,
        )

    return state.evaluate_window(window_index, evaluator)


def evaluate_primary_window(
    *,
    catalog: PortfolioCatalog,
    window_slice: WindowSlice,
    entry_state: Mapping[str, str],
    pool_config: PoolConfig,
    reference_bankroll_usd: float,
    path_states: Mapping[str, CarriedPathState],
    progress_callback: ProgressCallback | None = None,
) -> PrimaryWindowRecord:
    if window_slice.skipped_reason is not None:
        raise ValueError("cannot evaluate a skipped window")
    if set(path_states) != set(PRIMARY_METHOD_IDS):
        raise ValueError("primary path state set is incomplete")
    valid_valuation_swaps(window_slice.val_events, pool_config)
    routed = route_directional_catalog(catalog, entry_state)
    training = _training_records(
        catalog,
        routed,
        window_slice.train_events,
        pool_config,
        reference_bankroll_usd,
        progress_callback,
    )
    training_metrics = {row.economic_id: row.metrics for row in training if row.metrics is not None}
    allocation_functions = (
        equal_config_weights,
        equal_family_weights,
        shrinkage_weights,
    )
    allocations = {
        function.__name__.removesuffix("_weights"): function(
            catalog.allocation_units,
            training_metrics,
        )
        for function in allocation_functions
    }
    allocations = {rule: allocations[rule] for rule in ALLOCATION_RULE_IDS}
    initial_pool_state = _build_pool_state(window_slice.train_events)
    candidates = _candidate_records(
        catalog,
        routed,
        window_slice.val_events,
        pool_config,
        reference_bankroll_usd,
        initial_pool_state,
        progress_callback,
    )
    reset_rules: dict[str, ResetOutcome] = {}
    carried_rules: dict[str, CarriedWindowOutcome] = {}
    for rule in ALLOCATION_RULE_IDS:
        evaluator = _portfolio_evaluator(
            events=window_slice.val_events,
            routed_sleeves=routed.sleeves,
            allocation=allocations[rule],
            pool_config=pool_config,
            initial_pool_state=initial_pool_state,
        )
        reset_rules[rule] = _reset_outcome(
            rule,
            _reference_evaluator(evaluator, reference_bankroll_usd),
        )
        carried_rules[rule] = path_states[rule].evaluate_window(
            window_slice.window.index,
            evaluator,
        )
    comparator_plans = _comparator_plans(catalog, training_metrics)
    static_id = cast(str, comparator_plans["static_spot_w0025"].selected_economic_id)
    static_sleeve = next(sleeve for sleeve in routed.sleeves if sleeve.sleeve_id == static_id)
    comparators = {
        comparator_id: _evaluate_comparator(
            comparator_id,
            comparator_plans[comparator_id],
            path_states[comparator_id],
            window_index=window_slice.window.index,
            events=window_slice.val_events,
            routed_sleeves=routed.sleeves,
            pool_config=pool_config,
            initial_pool_state=initial_pool_state,
            static_params=static_sleeve.params,
        )
        for comparator_id in COMPARATOR_IDS
    }
    routed_names = {
        unit.sleeve_id: (
            next(
                (
                    sleeve.config_name
                    for sleeve in routed.sleeves
                    if sleeve.sleeve_id == unit.sleeve_id
                ),
                "no_position",
            )
        )
        for unit in catalog.allocation_units
    }
    snapshots = {method_id: path_states[method_id].snapshot() for method_id in PRIMARY_METHOD_IDS}
    return PrimaryWindowRecord(
        pool=catalog.pool,
        window_index=window_slice.window.index,
        window_start=window_slice.window.val_start.isoformat(),
        window_end=window_slice.window.val_end.isoformat(),
        train_swap_count=window_slice.train_counts.swap_count,
        val_swap_count=window_slice.val_counts.swap_count,
        routed_config_names=routed_names,
        training=training,
        allocations=allocations,
        candidates=candidates,
        reset_rules=reset_rules,
        carried_rules=carried_rules,
        comparator_plans=comparator_plans,
        comparators=comparators,
        path_snapshots=snapshots,
    )


def _economic_result_payload(result: EconomicResult) -> dict[str, object]:
    return {
        "opening_capital_usd": result.opening_capital_usd,
        "closing_cash_usd": result.closing_cash_usd,
        "max_drawdown": result.max_drawdown,
        "terminal_liquidation_cost_usd": result.terminal_liquidation_cost_usd,
        "total_fees_usd": result.total_fees_usd,
        "total_fixed_cost_usd": result.total_fixed_cost_usd,
        "total_variable_cost_usd": result.total_variable_cost_usd,
        "external_marked_notional_usd": result.external_marked_notional_usd,
        "external_input_value_usd": result.external_input_value_usd,
        "external_output_value_usd": result.external_output_value_usd,
        "internal_cross_notional_usd": result.internal_cross_notional_usd,
        "entry_scale_events": [
            {
                "block_time": event.block_time.isoformat(),
                "block_number": event.block_number,
                "tx_hash": event.tx_hash,
                "log_index": event.log_index,
                "scale": event.scale,
                "intended_entry_count": event.intended_entry_count,
                "executed_entry_count": event.executed_entry_count,
            }
            for event in result.entry_scale_events
        ],
        "terminal_position_settlement_count": (result.terminal_position_settlement_count),
        "terminal_loose_cngn_settlement_count": (result.terminal_loose_cngn_settlement_count),
        "terminal_zero_settlement_count": result.terminal_zero_settlement_count,
        "terminal_inventory_swap_count": result.terminal_inventory_swap_count,
        "terminal_fixed_cost_usd": result.terminal_fixed_cost_usd,
        "terminal_variable_cost_usd": result.terminal_variable_cost_usd,
        "terminal_external_marked_notional_usd": (result.terminal_external_marked_notional_usd),
        "value_samples": [
            [timestamp.isoformat(), value] for timestamp, value in result.value_samples
        ],
        "attribution": {key: asdict(result.attribution[key]) for key in sorted(result.attribution)},
    }


def _payload_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("checkpoint field must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("checkpoint numeric field must be finite")
    return parsed


def _payload_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("checkpoint field must be an integer")
    return value


def _payload_failure(value: object) -> FailureDiagnostic | None:
    if value is None:
        return None
    payload = cast(Mapping[str, object], value)
    return FailureDiagnostic(
        exception_type=cast(str, payload["exception_type"]),
        reason=cast(str, payload["reason"]),
    )


def _payload_attribution(payload: Mapping[str, object]) -> EconomicAttribution:
    return EconomicAttribution(
        economic_id=cast(str, payload["economic_id"]),
        opening_value_usd=_payload_float(payload["opening_value_usd"]),
        closing_value_usd=_payload_float(payload["closing_value_usd"]),
        fees_usd=_payload_float(payload["fees_usd"]),
        fixed_cost_usd=_payload_float(payload["fixed_cost_usd"]),
        variable_cost_usd=_payload_float(payload["variable_cost_usd"]),
        external_marked_notional_usd=_payload_float(payload["external_marked_notional_usd"]),
        external_input_value_usd=_payload_float(payload["external_input_value_usd"]),
        external_output_value_usd=_payload_float(payload["external_output_value_usd"]),
        internal_cross_notional_usd=_payload_float(payload["internal_cross_notional_usd"]),
        terminal_funding_transfer_usd=_payload_float(payload["terminal_funding_transfer_usd"]),
    )


def _payload_training_metrics(payload: Mapping[str, object]) -> TrainingMetrics:
    return TrainingMetrics(
        net_return=_payload_float(payload["net_return"]),
        episode_count=_payload_int(payload["episode_count"]),
        fee_to_transaction_cost_ratio=_payload_float(payload["fee_to_transaction_cost_ratio"]),
        max_drawdown=_payload_float(payload["max_drawdown"]),
    )


def _payload_economic_summary(payload: Mapping[str, object]) -> EconomicSummary:
    return EconomicSummary(
        opening_capital_usd=_payload_float(payload["opening_capital_usd"]),
        closing_cash_usd=_payload_float(payload["closing_cash_usd"]),
        window_net_return=_payload_float(payload["window_net_return"]),
        max_drawdown=_payload_float(payload["max_drawdown"]),
        terminal_liquidation_cost_usd=_payload_float(payload["terminal_liquidation_cost_usd"]),
        total_fees_usd=_payload_float(payload["total_fees_usd"]),
        total_fixed_cost_usd=_payload_float(payload["total_fixed_cost_usd"]),
        total_variable_cost_usd=_payload_float(payload["total_variable_cost_usd"]),
        external_marked_notional_usd=_payload_float(payload["external_marked_notional_usd"]),
        external_input_value_usd=_payload_float(payload["external_input_value_usd"]),
        external_output_value_usd=_payload_float(payload["external_output_value_usd"]),
        internal_cross_notional_usd=_payload_float(payload["internal_cross_notional_usd"]),
        entry_action_batch_count=_payload_int(payload["entry_action_batch_count"]),
        scaled_entry_action_batch_count=_payload_int(payload["scaled_entry_action_batch_count"]),
        minimum_entry_execution_scale=_payload_float(payload["minimum_entry_execution_scale"]),
        terminal_position_settlement_count=_payload_int(
            payload["terminal_position_settlement_count"]
        ),
        terminal_loose_cngn_settlement_count=_payload_int(
            payload["terminal_loose_cngn_settlement_count"]
        ),
        terminal_zero_settlement_count=_payload_int(payload["terminal_zero_settlement_count"]),
        terminal_inventory_swap_count=_payload_int(payload["terminal_inventory_swap_count"]),
        terminal_fixed_cost_usd=_payload_float(payload["terminal_fixed_cost_usd"]),
        terminal_variable_cost_usd=_payload_float(payload["terminal_variable_cost_usd"]),
        terminal_external_marked_notional_usd=_payload_float(
            payload["terminal_external_marked_notional_usd"]
        ),
        value_sample_count=_payload_int(payload["value_sample_count"]),
    )


def _payload_path_snapshot(payload: Mapping[str, object]) -> CarriedPathSnapshot:
    blocking_index = payload["blocking_window_index"]
    return CarriedPathSnapshot(
        method_id=cast(str, payload["method_id"]),
        method_kind=cast(str, payload["method_kind"]),
        next_opening_capital_usd=_payload_float(payload["next_opening_capital_usd"]),
        blocking_status=cast(InvalidStatus | None, payload["blocking_status"]),
        blocking_window_index=(
            _payload_int(blocking_index) if blocking_index is not None else None
        ),
        failure=_payload_failure(payload["failure"]),
    )


def _economic_result_from_payload(payload: Mapping[str, object]) -> EconomicResult:
    samples = tuple(
        (datetime.fromisoformat(cast(str, row[0])), _payload_float(row[1]))
        for row in cast(Sequence[Sequence[object]], payload["value_samples"])
    )
    attribution = {
        key: _payload_attribution(cast(Mapping[str, object], value))
        for key, value in cast(Mapping[str, object], payload["attribution"]).items()
    }
    entry_scale_events = tuple(
        EntryScaleEvent(
            block_time=datetime.fromisoformat(cast(str, row["block_time"])),
            block_number=(
                _payload_int(row["block_number"]) if row["block_number"] is not None else None
            ),
            tx_hash=cast(str | None, row["tx_hash"]),
            log_index=(_payload_int(row["log_index"]) if row["log_index"] is not None else None),
            scale=_payload_float(row["scale"]),
            intended_entry_count=_payload_int(row["intended_entry_count"]),
            executed_entry_count=_payload_int(row["executed_entry_count"]),
        )
        for row in cast(
            Sequence[Mapping[str, object]],
            payload["entry_scale_events"],
        )
    )
    return EconomicResult(
        opening_capital_usd=_payload_float(payload["opening_capital_usd"]),
        closing_cash_usd=_payload_float(payload["closing_cash_usd"]),
        max_drawdown=_payload_float(payload["max_drawdown"]),
        terminal_liquidation_cost_usd=_payload_float(payload["terminal_liquidation_cost_usd"]),
        total_fees_usd=_payload_float(payload["total_fees_usd"]),
        total_fixed_cost_usd=_payload_float(payload["total_fixed_cost_usd"]),
        total_variable_cost_usd=_payload_float(payload["total_variable_cost_usd"]),
        external_marked_notional_usd=_payload_float(payload["external_marked_notional_usd"]),
        external_input_value_usd=_payload_float(payload["external_input_value_usd"]),
        external_output_value_usd=_payload_float(payload["external_output_value_usd"]),
        internal_cross_notional_usd=_payload_float(payload["internal_cross_notional_usd"]),
        entry_scale_events=entry_scale_events,
        terminal_position_settlement_count=_payload_int(
            payload["terminal_position_settlement_count"]
        ),
        terminal_loose_cngn_settlement_count=_payload_int(
            payload["terminal_loose_cngn_settlement_count"]
        ),
        terminal_zero_settlement_count=_payload_int(payload["terminal_zero_settlement_count"]),
        terminal_inventory_swap_count=_payload_int(payload["terminal_inventory_swap_count"]),
        terminal_fixed_cost_usd=_payload_float(payload["terminal_fixed_cost_usd"]),
        terminal_variable_cost_usd=_payload_float(payload["terminal_variable_cost_usd"]),
        terminal_external_marked_notional_usd=_payload_float(
            payload["terminal_external_marked_notional_usd"]
        ),
        value_samples=samples,
        attribution=attribution,
    )


def _carried_payload(outcome: CarriedWindowOutcome) -> dict[str, object]:
    return {
        "method_id": outcome.method_id,
        "method_kind": outcome.method_kind,
        "window_index": outcome.window_index,
        "status": outcome.status,
        "blocking_status": outcome.blocking_status,
        "blocking_window_index": outcome.blocking_window_index,
        "opening_capital_usd": outcome.opening_capital_usd,
        "closing_cash_usd": outcome.closing_cash_usd,
        "failure": asdict(outcome.failure) if outcome.failure is not None else None,
        "result": (
            _economic_result_payload(outcome.result) if outcome.result is not None else None
        ),
    }


def _carried_from_payload(payload: Mapping[str, object]) -> CarriedWindowOutcome:
    result_payload = payload["result"]
    return CarriedWindowOutcome(
        method_id=cast(str, payload["method_id"]),
        method_kind=cast(str, payload["method_kind"]),
        window_index=_payload_int(payload["window_index"]),
        status=cast(PathStatus, payload["status"]),
        blocking_status=cast(InvalidStatus | None, payload["blocking_status"]),
        blocking_window_index=(
            _payload_int(payload["blocking_window_index"])
            if payload["blocking_window_index"] is not None
            else None
        ),
        opening_capital_usd=(
            _payload_float(payload["opening_capital_usd"])
            if payload["opening_capital_usd"] is not None
            else None
        ),
        closing_cash_usd=(
            _payload_float(payload["closing_cash_usd"])
            if payload["closing_cash_usd"] is not None
            else None
        ),
        result=(
            _economic_result_from_payload(cast(Mapping[str, object], result_payload))
            if result_payload is not None
            else None
        ),
        failure=_payload_failure(payload["failure"]),
    )


def primary_window_to_payload(record: PrimaryWindowRecord) -> dict[str, object]:
    return {
        "phase": "primary",
        "pool": record.pool,
        "window_index": record.window_index,
        "window_start": record.window_start,
        "window_end": record.window_end,
        "train_swap_count": record.train_swap_count,
        "val_swap_count": record.val_swap_count,
        "routed_config_names": dict(record.routed_config_names),
        "training": [
            {
                **asdict(row),
                "metrics": asdict(row.metrics) if row.metrics is not None else None,
            }
            for row in record.training
        ],
        "allocations": {
            rule: {
                "rule": allocation.rule,
                "weights": dict(allocation.weights),
                "cash_weight": allocation.cash_weight,
            }
            for rule, allocation in record.allocations.items()
        },
        "candidates": [asdict(row) for row in record.candidates],
        "reset_rules": {rule: asdict(outcome) for rule, outcome in record.reset_rules.items()},
        "carried_rules": {
            rule: _carried_payload(outcome) for rule, outcome in record.carried_rules.items()
        },
        "comparator_plans": {
            comparator_id: {
                "comparator_id": plan.comparator_id,
                "selection_status": plan.selection_status,
                "selected_economic_id": plan.selected_economic_id,
                "allocation": (
                    {
                        "rule": plan.allocation.rule,
                        "weights": dict(plan.allocation.weights),
                        "cash_weight": plan.allocation.cash_weight,
                    }
                    if plan.allocation is not None
                    else None
                ),
            }
            for comparator_id, plan in record.comparator_plans.items()
        },
        "comparators": {
            comparator_id: _carried_payload(outcome)
            for comparator_id, outcome in record.comparators.items()
        },
        "path_snapshots": {
            method_id: asdict(snapshot) for method_id, snapshot in record.path_snapshots.items()
        },
        "carried_closing_cash_usd": {
            method_id: snapshot.next_opening_capital_usd
            for method_id, snapshot in record.path_snapshots.items()
            if snapshot.blocking_status is None
        },
    }


def _allocation_from_payload(payload: Mapping[str, object]) -> Allocation:
    return Allocation(
        cast(AllocationRule, payload["rule"]),
        {
            key: _payload_float(value)
            for key, value in cast(Mapping[str, object], payload["weights"]).items()
        },
        _payload_float(payload["cash_weight"]),
    )


def primary_window_from_payload(payload: Mapping[str, object]) -> PrimaryWindowRecord:
    training = tuple(
        TrainingRecord(
            economic_id=cast(str, row["economic_id"]),
            family=cast(str, row["family"]),
            routed_config_name=cast(str, row["routed_config_name"]),
            status=cast(TrainingStatus, row["status"]),
            eligible=cast(bool, row["eligible"]),
            metrics=(
                _payload_training_metrics(cast(Mapping[str, object], row["metrics"]))
                if row["metrics"] is not None
                else None
            ),
            failure=_payload_failure(row["failure"]),
        )
        for row in cast(Sequence[Mapping[str, object]], payload["training"])
    )
    candidates = tuple(
        CandidateResetRecord(
            economic_id=cast(str, row["economic_id"]),
            family=cast(str, row["family"]),
            routed_config_name=cast(str, row["routed_config_name"]),
            route_status=cast(RouteStatus, row["route_status"]),
            status=cast(PathStatus, row["status"]),
            episode_count=(
                _payload_int(row["episode_count"]) if row["episode_count"] is not None else None
            ),
            economics=(
                _payload_economic_summary(cast(Mapping[str, object], row["economics"]))
                if row["economics"] is not None
                else None
            ),
            failure=_payload_failure(row["failure"]),
        )
        for row in cast(Sequence[Mapping[str, object]], payload["candidates"])
    )
    reset_rules = {
        rule: ResetOutcome(
            method_id=cast(str, row["method_id"]),
            status=cast(PathStatus, row["status"]),
            economics=(
                _payload_economic_summary(cast(Mapping[str, object], row["economics"]))
                if row["economics"] is not None
                else None
            ),
            failure=_payload_failure(row["failure"]),
            observed_share=(
                _payload_float(row["observed_share"]) if row["observed_share"] is not None else None
            ),
            cap=(_payload_float(row["cap"]) if row["cap"] is not None else None),
        )
        for rule, row in cast(Mapping[str, Mapping[str, object]], payload["reset_rules"]).items()
    }
    reset_rules = {rule: reset_rules[rule] for rule in ALLOCATION_RULE_IDS}
    comparator_plans: dict[str, ComparatorPlan] = {}
    for comparator_id, row in cast(
        Mapping[str, Mapping[str, object]], payload["comparator_plans"]
    ).items():
        allocation_payload = row["allocation"]
        comparator_plans[comparator_id] = ComparatorPlan(
            comparator_id=cast(str, row["comparator_id"]),
            selection_status=cast(SelectionStatus, row["selection_status"]),
            selected_economic_id=cast(str | None, row["selected_economic_id"]),
            allocation=(
                _allocation_from_payload(cast(Mapping[str, object], allocation_payload))
                if allocation_payload is not None
                else None
            ),
        )
    comparator_plans = {
        comparator_id: comparator_plans[comparator_id] for comparator_id in COMPARATOR_IDS
    }
    snapshots = {
        method_id: _payload_path_snapshot(row)
        for method_id, row in cast(
            Mapping[str, Mapping[str, object]], payload["path_snapshots"]
        ).items()
    }
    return PrimaryWindowRecord(
        pool=cast(str, payload["pool"]),
        window_index=_payload_int(payload["window_index"]),
        window_start=cast(str, payload["window_start"]),
        window_end=cast(str, payload["window_end"]),
        train_swap_count=_payload_int(payload["train_swap_count"]),
        val_swap_count=_payload_int(payload["val_swap_count"]),
        routed_config_names=cast(Mapping[str, str], payload["routed_config_names"]),
        training=training,
        allocations={
            rule: _allocation_from_payload(
                cast(
                    Mapping[str, Mapping[str, object]],
                    payload["allocations"],
                )[rule]
            )
            for rule in ALLOCATION_RULE_IDS
        },
        candidates=candidates,
        reset_rules=reset_rules,
        carried_rules={
            rule: _carried_from_payload(
                cast(
                    Mapping[str, Mapping[str, object]],
                    payload["carried_rules"],
                )[rule]
            )
            for rule in ALLOCATION_RULE_IDS
        },
        comparator_plans=comparator_plans,
        comparators={
            comparator_id: _carried_from_payload(
                cast(
                    Mapping[str, Mapping[str, object]],
                    payload["comparators"],
                )[comparator_id]
            )
            for comparator_id in COMPARATOR_IDS
        },
        path_snapshots=snapshots,
    )


def assess_candidate_reset_matrix(
    records: Sequence[PrimaryWindowRecord],
    expected_economic_ids: Sequence[str],
) -> CandidateMatrixAssessment:
    if not records:
        raise ValueError("candidate matrix requires completed primary windows")
    _validate_record_prefix(records)
    expected_ids = tuple(expected_economic_ids)
    if not expected_ids:
        raise ValueError("candidate matrix requires expected economic IDs")
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError("candidate matrix expected economic IDs are duplicated")
    returns: dict[str, list[float]] = {economic_id: [] for economic_id in expected_ids}
    failures: list[CandidateMatrixFailure] = []
    observed_rows = 0
    for record in records:
        observed_ids = tuple(row.economic_id for row in record.candidates)
        if observed_ids != expected_ids:
            raise ValueError("candidate matrix identities do not match the catalog")
        observed_rows += len(record.candidates)
        for row in record.candidates:
            values = tuple(asdict(row.economics).values()) if row.economics is not None else ()
            economics_are_finite = bool(values) and all(
                not isinstance(value, float) or math.isfinite(value) for value in values
            )
            if (
                row.status != "valid"
                or row.economics is None
                or not economics_are_finite
                or row.episode_count is None
                or row.episode_count < 0
            ):
                failure_status: str
                if row.status != "valid":
                    failure_status = row.status
                elif row.economics is None:
                    failure_status = "invalid_missing_economics"
                elif not economics_are_finite:
                    failure_status = "invalid_nonfinite_economics"
                else:
                    failure_status = "invalid_episode_count"
                failures.append(
                    CandidateMatrixFailure(
                        economic_id=row.economic_id,
                        window_index=record.window_index,
                        status=failure_status,
                        failure=(
                            row.failure
                            if row.failure is not None
                            else FailureDiagnostic(
                                "CandidateMatrixError",
                                failure_status,
                            )
                        ),
                    )
                )
                continue
            value = row.economics.window_net_return
            returns[row.economic_id].append(value)
    expected_rows = len(records) * len(expected_ids)
    if observed_rows != expected_rows:
        raise ValueError("candidate matrix row count does not match the catalog")
    if failures:
        return CandidateMatrixAssessment(
            status="invalid_incomplete_matrix",
            expected_rows=expected_rows,
            observed_rows=observed_rows,
            valid_rows=observed_rows - len(failures),
            invalid_rows=len(failures),
            invalid_observations=tuple(failures),
            selected_economic_id=None,
        )
    means = {economic_id: sum(values) / len(values) for economic_id, values in returns.items()}
    selected = min(
        means,
        key=lambda economic_id: (-means[economic_id], economic_id),
    )
    return CandidateMatrixAssessment(
        status="complete_valid",
        expected_rows=expected_rows,
        observed_rows=observed_rows,
        valid_rows=observed_rows,
        invalid_rows=0,
        invalid_observations=(),
        selected_economic_id=selected,
    )


def select_best_reset_sleeve(
    records: Sequence[PrimaryWindowRecord],
    expected_economic_ids: Sequence[str] | None = None,
) -> str:
    expected_ids = (
        tuple(expected_economic_ids)
        if expected_economic_ids is not None
        else tuple(row.economic_id for row in records[0].candidates)
        if records
        else ()
    )
    assessment = assess_candidate_reset_matrix(records, expected_ids)
    if assessment.status != "complete_valid":
        raise ValueError("best-sleeve selection requires finite valid candidates")
    assert assessment.selected_economic_id is not None
    return assessment.selected_economic_id


def evaluate_removal_window(
    *,
    catalog: PortfolioCatalog,
    primary_record: PrimaryWindowRecord,
    window_slice: WindowSlice,
    entry_state: Mapping[str, str],
    pool_config: PoolConfig,
    removed_economic_id: str,
    path_states: Mapping[str, CarriedPathState],
) -> RemovalWindowRecord:
    if primary_record.window_index != window_slice.window.index:
        raise ValueError("removal window does not match its primary record")
    if removed_economic_id not in {unit.sleeve_id for unit in catalog.allocation_units}:
        raise ValueError("removed economic ID is not canonical")
    if set(path_states) != set(ALLOCATION_RULE_IDS):
        raise ValueError("removal path state set is incomplete")
    valid_valuation_swaps(window_slice.val_events, pool_config)
    routed = route_directional_catalog(catalog, entry_state)
    initial_pool_state = _build_pool_state(window_slice.train_events)
    removed_allocations = {
        rule: remove_sleeve_from_allocation(
            primary_record.allocations[rule],
            removed_economic_id,
        )
        for rule in ALLOCATION_RULE_IDS
    }
    outcomes: dict[str, CarriedWindowOutcome] = {}
    for rule in ALLOCATION_RULE_IDS:
        evaluator = _portfolio_evaluator(
            events=window_slice.val_events,
            routed_sleeves=routed.sleeves,
            allocation=removed_allocations[rule],
            pool_config=pool_config,
            initial_pool_state=initial_pool_state,
        )
        outcomes[rule] = path_states[rule].evaluate_window(
            window_slice.window.index,
            evaluator,
        )
    return RemovalWindowRecord(
        pool=catalog.pool,
        window_index=window_slice.window.index,
        window_start=window_slice.window.val_start.isoformat(),
        window_end=window_slice.window.val_end.isoformat(),
        removed_economic_id=removed_economic_id,
        removed_allocations=removed_allocations,
        outcomes=outcomes,
        path_snapshots={rule: path_states[rule].snapshot() for rule in ALLOCATION_RULE_IDS},
    )


def removal_window_to_payload(record: RemovalWindowRecord) -> dict[str, object]:
    return {
        "phase": "best_sleeve_removal",
        "pool": record.pool,
        "window_index": record.window_index,
        "window_start": record.window_start,
        "window_end": record.window_end,
        "removed_economic_id": record.removed_economic_id,
        "removed_allocations": {
            rule: {
                "rule": allocation.rule,
                "weights": dict(allocation.weights),
                "cash_weight": allocation.cash_weight,
            }
            for rule, allocation in record.removed_allocations.items()
        },
        "outcomes": {rule: _carried_payload(outcome) for rule, outcome in record.outcomes.items()},
        "path_snapshots": {
            rule: asdict(snapshot) for rule, snapshot in record.path_snapshots.items()
        },
        "carried_closing_cash_usd": {
            rule: snapshot.next_opening_capital_usd
            for rule, snapshot in record.path_snapshots.items()
            if snapshot.blocking_status is None
        },
    }


def removal_window_from_payload(
    payload: Mapping[str, object],
) -> RemovalWindowRecord:
    return RemovalWindowRecord(
        pool=cast(str, payload["pool"]),
        window_index=_payload_int(payload["window_index"]),
        window_start=cast(str, payload["window_start"]),
        window_end=cast(str, payload["window_end"]),
        removed_economic_id=cast(str, payload["removed_economic_id"]),
        removed_allocations={
            rule: _allocation_from_payload(row)
            for rule, row in cast(
                Mapping[str, Mapping[str, object]], payload["removed_allocations"]
            ).items()
        },
        outcomes={
            rule: _carried_from_payload(row)
            for rule, row in cast(Mapping[str, Mapping[str, object]], payload["outcomes"]).items()
        },
        path_snapshots={
            rule: _payload_path_snapshot(row)
            for rule, row in cast(
                Mapping[str, Mapping[str, object]], payload["path_snapshots"]
            ).items()
        },
    )
