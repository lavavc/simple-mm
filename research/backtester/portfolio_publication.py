"""Deterministic artifact rows for the corrected weighted-portfolio study."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from research.backtester.pbo import compute_pbo
from research.backtester.portfolio_allocation import remove_sleeve_from_allocation
from research.backtester.portfolio_catalog import PortfolioCatalog
from research.backtester.portfolio_evaluation import (
    ALLOCATION_RULE_IDS,
    COMPARATOR_IDS,
    CandidateMatrixAssessment,
    EconomicSummary,
    PrimaryWindowRecord,
    RemovalWindowRecord,
    assess_candidate_reset_matrix,
)
from research.backtester.portfolio_path import (
    CarriedWindowOutcome,
    EconomicAttribution,
    signed_max_drawdown,
)

ARTIFACT_SCHEMA_VERSION = "weighted-portfolio-artifacts/v2"

ECONOMIC_FIELDS = (
    "opening_capital_usd",
    "closing_cash_usd",
    "window_net_return",
    "max_drawdown",
    "terminal_liquidation_cost_usd",
    "total_fees_usd",
    "total_fixed_cost_usd",
    "total_variable_cost_usd",
    "external_marked_notional_usd",
    "external_input_value_usd",
    "external_output_value_usd",
    "internal_cross_notional_usd",
    "value_sample_count",
)

CSV_FIELDS: Mapping[str, tuple[str, ...]] = {
    "configuration_catalog.csv": (
        "schema_version",
        "pool",
        "economic_id",
        "declaration_kind",
        "allocation_family",
        "declared_family",
        "config_name",
        "source_constructor",
        "behavioral_fingerprint",
    ),
    "training_eligibility.csv": (
        "schema_version",
        "pool",
        "window_index",
        "window_start",
        "window_end",
        "economic_id",
        "family",
        "routed_config_name",
        "status",
        "eligible",
        "net_return",
        "episode_count",
        "fee_to_transaction_cost_ratio",
        "max_drawdown",
    ),
    "window_weights.csv": (
        "schema_version",
        "pool",
        "window_index",
        "window_start",
        "window_end",
        "allocation_rule",
        "economic_id",
        "family",
        "weight",
        "deployed_weight",
        "cash_weight",
    ),
    "sleeve_validation_matrix.csv": (
        "schema_version",
        "pool",
        "window_index",
        "window_start",
        "window_end",
        "economic_id",
        "family",
        "routed_config_name",
        "route_status",
        "status",
        "episode_count",
        *ECONOMIC_FIELDS,
    ),
    "reset_portfolio_validation_matrix.csv": (
        "schema_version",
        "pool",
        "window_index",
        "window_start",
        "window_end",
        "allocation_rule",
        "status",
        "observed_share",
        "cap",
        "deployed_weight",
        "cash_weight",
        *ECONOMIC_FIELDS,
    ),
    "carried_portfolio_path.csv": (
        "schema_version",
        "pool",
        "window_index",
        "window_start",
        "window_end",
        "method_id",
        "method_kind",
        "status",
        "blocking_status",
        "blocking_window_index",
        *ECONOMIC_FIELDS,
    ),
    "comparators.csv": (
        "schema_version",
        "pool",
        "window_index",
        "window_start",
        "window_end",
        "comparator_id",
        "status",
        "blocking_status",
        "blocking_window_index",
        "selection_status",
        "selected_economic_id",
        *ECONOMIC_FIELDS,
    ),
    "joint_attribution.csv": (
        "schema_version",
        "pool",
        "window_index",
        "window_start",
        "window_end",
        "allocation_rule",
        "row_type",
        "economic_id",
        "family",
        "opening_value_usd",
        "closing_value_usd",
        "pnl_usd",
        "portfolio_return_contribution",
        "total_fees_usd",
        "total_fixed_cost_usd",
        "total_variable_cost_usd",
        "external_marked_notional_usd",
        "external_input_value_usd",
        "external_output_value_usd",
        "internal_cross_notional_usd",
    ),
    "concentration_and_contribution.csv": (
        "schema_version",
        "pool",
        "window_index",
        "window_start",
        "window_end",
        "allocation_rule",
        "status",
        "deployed_weight",
        "cash_weight",
        "herfindahl",
        "largest_weight",
        "effective_sleeve_count",
        "best_sleeve_removed_economic_id",
        "best_sleeve_removed_weight",
        "removal_status",
        "removal_blocking_status",
        "removal_blocking_window_index",
        "removal_opening_capital_usd",
        "removal_closing_cash_usd",
        "removal_window_net_return",
        "removal_max_drawdown",
    ),
    "method_stability.csv": (
        "schema_version",
        "pool",
        "method_id",
        "method_kind",
        "completed_windows",
        "valid_windows",
        "invalid_windows",
        "first_invalid_status",
        "first_invalid_window_index",
        "cumulative_return",
        "mean_window_return",
        "median_window_return",
        "positive_window_rate",
        "worst_window_return",
        "continuous_max_drawdown",
        "total_fees_usd",
        "total_fixed_cost_usd",
        "total_variable_cost_usd",
    ),
    "comparator_conclusions.csv": (
        "schema_version",
        "pool",
        "allocation_rule",
        "comparator_id",
        "paired_valid_windows",
        "invalid_rule_windows",
        "invalid_comparator_windows",
        "mean_paired_excess_return",
        "median_paired_excess_return",
        "worst_paired_excess_return",
        "positive_paired_excess_rate",
        "candidate_pbo_status",
        "allocation_pbo_status",
        "conclusion_status",
    ),
}

REQUIRED_ARTIFACTS = (
    "run_manifest.json",
    *CSV_FIELDS,
    "pbo_allocation_rules.json",
    "summary.md",
)


def _common(record: PrimaryWindowRecord) -> dict[str, object]:
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "pool": record.pool,
        "window_index": record.window_index,
        "window_start": record.window_start,
        "window_end": record.window_end,
    }


def _blank_economics() -> dict[str, object]:
    return {field: "" for field in ECONOMIC_FIELDS}


def _summary_fields(summary: EconomicSummary) -> dict[str, object]:
    return {field: getattr(summary, field) for field in ECONOMIC_FIELDS}


def _carried_fields(outcome: CarriedWindowOutcome) -> dict[str, object]:
    if outcome.status != "valid" or outcome.result is None:
        return _blank_economics()
    result = outcome.result
    return {
        "opening_capital_usd": result.opening_capital_usd,
        "closing_cash_usd": result.closing_cash_usd,
        "window_net_return": result.window_net_return,
        "max_drawdown": result.max_drawdown,
        "terminal_liquidation_cost_usd": result.terminal_liquidation_cost_usd,
        "total_fees_usd": result.total_fees_usd,
        "total_fixed_cost_usd": result.total_fixed_cost_usd,
        "total_variable_cost_usd": result.total_variable_cost_usd,
        "external_marked_notional_usd": result.external_marked_notional_usd,
        "external_input_value_usd": result.external_input_value_usd,
        "external_output_value_usd": result.external_output_value_usd,
        "internal_cross_notional_usd": result.internal_cross_notional_usd,
        "value_sample_count": len(result.value_samples),
    }


def build_artifact_rows(
    catalog: PortfolioCatalog,
    records: Sequence[PrimaryWindowRecord],
    removal_records: Sequence[RemovalWindowRecord],
) -> dict[str, list[dict[str, object]]]:
    if not records:
        raise ValueError("publication requires primary window records")
    units = {unit.sleeve_id: unit for unit in catalog.allocation_units}
    expected_ids = tuple(units)
    candidate_matrix = assess_candidate_reset_matrix(records, expected_ids)
    if candidate_matrix.status == "complete_valid":
        if len(removal_records) != len(records):
            raise ValueError(
                "valid candidate matrix requires one removal record per primary window"
            )
        primary_indexes = tuple(record.window_index for record in records)
        removal_indexes = tuple(record.window_index for record in removal_records)
        if removal_indexes != primary_indexes:
            raise ValueError("removal windows do not match primary windows")
        removed_ids = {record.removed_economic_id for record in removal_records}
        if removed_ids != {candidate_matrix.selected_economic_id}:
            raise ValueError("removal records do not bind the selected economic ID")
        for primary, removal_record in zip(records, removal_records, strict=True):
            if (
                removal_record.pool != primary.pool
                or removal_record.window_start != primary.window_start
                or removal_record.window_end != primary.window_end
            ):
                raise ValueError("removal window identity does not match primary")
            for rule in ALLOCATION_RULE_IDS:
                expected = remove_sleeve_from_allocation(
                    primary.allocations[rule],
                    removal_record.removed_economic_id,
                )
                if removal_record.removed_allocations[rule] != expected:
                    raise ValueError(
                        "removed allocation does not match the primary allocation"
                    )
    elif removal_records:
        raise ValueError("invalid candidate matrix cannot contain removal records")
    rows: dict[str, list[dict[str, object]]] = {
        name: [] for name in CSV_FIELDS
    }
    for declaration in catalog.declarations:
        rows["configuration_catalog.csv"].append(
            {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "pool": catalog.pool,
                "economic_id": declaration.economic_id,
                "declaration_kind": declaration.kind,
                "allocation_family": units[declaration.economic_id].family,
                "declared_family": declaration.family,
                "config_name": declaration.config_name,
                "source_constructor": declaration.source_constructor,
                "behavioral_fingerprint": declaration.behavioral_fingerprint,
            }
        )

    removal_by_window = {record.window_index: record for record in removal_records}
    if len(removal_by_window) != len(removal_records):
        raise ValueError("duplicate removal window")
    for record in records:
        common = _common(record)
        training_by_id = {row.economic_id: row for row in record.training}
        candidate_by_id = {row.economic_id: row for row in record.candidates}
        if set(training_by_id) != set(units) or set(candidate_by_id) != set(units):
            raise ValueError("primary record does not cover the canonical catalog")
        for economic_id, unit in units.items():
            training = training_by_id[economic_id]
            metric_fields: dict[str, object]
            if training.metrics is None:
                metric_fields = {
                    "net_return": "",
                    "episode_count": "",
                    "fee_to_transaction_cost_ratio": "",
                    "max_drawdown": "",
                }
            else:
                metric_fields = asdict(training.metrics)
            rows["training_eligibility.csv"].append(
                {
                    **common,
                    "economic_id": economic_id,
                    "family": unit.family,
                    "routed_config_name": training.routed_config_name,
                    "status": training.status,
                    "eligible": training.eligible,
                    **metric_fields,
                }
            )
            candidate = candidate_by_id[economic_id]
            candidate_economics = (
                _summary_fields(candidate.economics)
                if candidate.status == "valid" and candidate.economics is not None
                else _blank_economics()
            )
            rows["sleeve_validation_matrix.csv"].append(
                {
                    **common,
                    "economic_id": economic_id,
                    "family": unit.family,
                    "routed_config_name": candidate.routed_config_name,
                    "route_status": candidate.route_status,
                    "status": candidate.status,
                    "episode_count": (
                        candidate.episode_count
                        if candidate.status == "valid"
                        else ""
                    ),
                    **candidate_economics,
                }
            )
        for rule in ALLOCATION_RULE_IDS:
            allocation = record.allocations[rule]
            deployed = sum(allocation.weights.values())
            for economic_id, unit in units.items():
                rows["window_weights.csv"].append(
                    {
                        **common,
                        "allocation_rule": rule,
                        "economic_id": economic_id,
                        "family": unit.family,
                        "weight": allocation.weights.get(economic_id, 0.0),
                        "deployed_weight": deployed,
                        "cash_weight": allocation.cash_weight,
                    }
                )
            reset = record.reset_rules[rule]
            reset_economics = (
                _summary_fields(reset.economics)
                if reset.status == "valid" and reset.economics is not None
                else _blank_economics()
            )
            rows["reset_portfolio_validation_matrix.csv"].append(
                {
                    **common,
                    "allocation_rule": rule,
                    "status": reset.status,
                    "observed_share": (
                        reset.observed_share if reset.observed_share is not None else ""
                    ),
                    "cap": reset.cap if reset.cap is not None else "",
                    "deployed_weight": deployed,
                    "cash_weight": allocation.cash_weight,
                    **reset_economics,
                }
            )
            carried = record.carried_rules[rule]
            rows["carried_portfolio_path.csv"].append(
                {
                    **common,
                    "method_id": carried.method_id,
                    "method_kind": carried.method_kind,
                    "status": carried.status,
                    "blocking_status": carried.blocking_status or "",
                    "blocking_window_index": (
                        carried.blocking_window_index
                        if carried.blocking_window_index is not None
                        else ""
                    ),
                    **_carried_fields(carried),
                }
            )
            if carried.status == "valid" and carried.result is not None:
                rows["joint_attribution.csv"].extend(
                    _attribution_rows(record, rule, catalog, carried)
                )

        for comparator_id in COMPARATOR_IDS:
            plan = record.comparator_plans[comparator_id]
            outcome = record.comparators[comparator_id]
            rows["comparators.csv"].append(
                {
                    **common,
                    "comparator_id": comparator_id,
                    "status": outcome.status,
                    "blocking_status": outcome.blocking_status or "",
                    "blocking_window_index": (
                        outcome.blocking_window_index
                        if outcome.blocking_window_index is not None
                        else ""
                    ),
                    "selection_status": plan.selection_status,
                    "selected_economic_id": plan.selected_economic_id or "",
                    **_carried_fields(outcome),
                }
            )
        removal = removal_by_window.get(record.window_index)
        rows["concentration_and_contribution.csv"].extend(
            _concentration_rows(record, removal)
        )

    rows["method_stability.csv"].extend(_method_stability_rows(records))
    pbo = build_pbo_payload(records, expected_economic_ids=expected_ids)
    rows["comparator_conclusions.csv"].extend(
        _comparator_conclusion_rows(records, pbo)
    )
    _validate_row_shapes(rows)
    return rows


def _attribution_rows(
    record: PrimaryWindowRecord,
    rule: str,
    catalog: PortfolioCatalog,
    outcome: CarriedWindowOutcome,
) -> list[dict[str, object]]:
    assert outcome.result is not None
    result = outcome.result
    common = _common(record)
    zero = EconomicAttribution("zero", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    rows: list[dict[str, object]] = []
    by_family: dict[str, list[EconomicAttribution]] = {
        family: [] for family in catalog.family_names
    }
    for unit in catalog.allocation_units:
        item = result.attribution.get(unit.sleeve_id, zero)
        by_family[unit.family].append(item)
        rows.append(
            _attribution_row(
                common,
                rule,
                "sleeve",
                unit.sleeve_id,
                unit.family,
                item,
                result.opening_capital_usd,
            )
        )
    for family in catalog.family_names:
        rows.append(
            _attribution_row(
                common,
                rule,
                "family",
                f"family:{family}",
                family,
                _sum_attribution(f"family:{family}", by_family[family]),
                result.opening_capital_usd,
            )
        )
    cash = result.attribution["cash"]
    rows.append(
        _attribution_row(
            common,
            rule,
            "cash",
            "cash",
            "cash",
            cash,
            result.opening_capital_usd,
        )
    )
    return rows


def _sum_attribution(
    economic_id: str,
    items: Sequence[EconomicAttribution],
) -> EconomicAttribution:
    return EconomicAttribution(
        economic_id=economic_id,
        opening_value_usd=sum(item.opening_value_usd for item in items),
        closing_value_usd=sum(item.closing_value_usd for item in items),
        fees_usd=sum(item.fees_usd for item in items),
        fixed_cost_usd=sum(item.fixed_cost_usd for item in items),
        variable_cost_usd=sum(item.variable_cost_usd for item in items),
        external_marked_notional_usd=sum(
            item.external_marked_notional_usd for item in items
        ),
        external_input_value_usd=sum(item.external_input_value_usd for item in items),
        external_output_value_usd=sum(
            item.external_output_value_usd for item in items
        ),
        internal_cross_notional_usd=sum(
            item.internal_cross_notional_usd for item in items
        ),
    )


def _attribution_row(
    common: Mapping[str, object],
    rule: str,
    row_type: str,
    economic_id: str,
    family: str,
    item: EconomicAttribution,
    opening_capital_usd: float,
) -> dict[str, object]:
    return {
        **common,
        "allocation_rule": rule,
        "row_type": row_type,
        "economic_id": economic_id,
        "family": family,
        "opening_value_usd": item.opening_value_usd,
        "closing_value_usd": item.closing_value_usd,
        "pnl_usd": item.pnl_usd,
        "portfolio_return_contribution": item.pnl_usd / opening_capital_usd,
        "total_fees_usd": item.fees_usd,
        "total_fixed_cost_usd": item.fixed_cost_usd,
        "total_variable_cost_usd": item.variable_cost_usd,
        "external_marked_notional_usd": item.external_marked_notional_usd,
        "external_input_value_usd": item.external_input_value_usd,
        "external_output_value_usd": item.external_output_value_usd,
        "internal_cross_notional_usd": item.internal_cross_notional_usd,
    }


def _concentration_rows(
    record: PrimaryWindowRecord,
    removal: RemovalWindowRecord | None,
) -> list[dict[str, object]]:
    if removal is not None and removal.removed_economic_id == "":
        raise ValueError("removal record lacks a frozen economic ID")
    rows: list[dict[str, object]] = []
    for rule in ALLOCATION_RULE_IDS:
        allocation = record.allocations[rule]
        weights = tuple(allocation.weights.values())
        removal_outcome = removal.outcomes[rule] if removal is not None else None
        removal_economics = (
            _carried_fields(removal_outcome)
            if removal_outcome is not None
            else {
                "opening_capital_usd": "",
                "closing_cash_usd": "",
                "window_net_return": "",
                "max_drawdown": "",
            }
        )
        rows.append(
            {
                **_common(record),
                "allocation_rule": rule,
                "status": record.carried_rules[rule].status,
                "deployed_weight": sum(weights),
                "cash_weight": allocation.cash_weight,
                "herfindahl": sum(weight * weight for weight in weights),
                "largest_weight": max(weights, default=0.0),
                "effective_sleeve_count": (
                    1.0 / sum(weight * weight for weight in weights)
                    if weights and sum(weight * weight for weight in weights) > 0
                    else 0.0
                ),
                "best_sleeve_removed_economic_id": (
                    removal.removed_economic_id if removal is not None else ""
                ),
                "best_sleeve_removed_weight": (
                    allocation.weights.get(removal.removed_economic_id, 0.0)
                    if removal is not None
                    else ""
                ),
                "removal_status": (
                    removal_outcome.status
                    if removal_outcome is not None
                    else "not_run_incomplete_candidate_reset_matrix"
                ),
                "removal_blocking_status": (
                    removal_outcome.blocking_status or ""
                    if removal_outcome is not None
                    else ""
                ),
                "removal_blocking_window_index": (
                    removal_outcome.blocking_window_index
                    if removal_outcome is not None
                    and removal_outcome.blocking_window_index is not None
                    else ""
                ),
                "removal_opening_capital_usd": removal_economics[
                    "opening_capital_usd"
                ],
                "removal_closing_cash_usd": removal_economics[
                    "closing_cash_usd"
                ],
                "removal_window_net_return": removal_economics[
                    "window_net_return"
                ],
                "removal_max_drawdown": removal_economics["max_drawdown"],
            }
        )
    return rows


def _method_outcomes(
    records: Sequence[PrimaryWindowRecord],
    method_id: str,
) -> list[CarriedWindowOutcome]:
    return [
        (
            record.carried_rules[method_id]
            if method_id in record.carried_rules
            else record.comparators[method_id]
        )
        for record in records
    ]


def _method_stability_rows(
    records: Sequence[PrimaryWindowRecord],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for method_id in (*ALLOCATION_RULE_IDS, *COMPARATOR_IDS):
        outcomes = _method_outcomes(records, method_id)
        valid = [row for row in outcomes if row.status == "valid" and row.result]
        invalid = [row for row in outcomes if row.status != "valid"]
        base = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "pool": records[0].pool,
            "method_id": method_id,
            "method_kind": outcomes[0].method_kind,
            "completed_windows": len(outcomes),
            "valid_windows": len(valid),
            "invalid_windows": len(invalid),
            "first_invalid_status": (
                invalid[0].blocking_status or invalid[0].status if invalid else ""
            ),
            "first_invalid_window_index": (
                invalid[0].blocking_window_index
                if invalid and invalid[0].blocking_window_index is not None
                else ""
            ),
        }
        metrics_fields: dict[str, object]
        if invalid:
            metrics_fields = {
                "cumulative_return": "",
                "mean_window_return": "",
                "median_window_return": "",
                "positive_window_rate": "",
                "worst_window_return": "",
                "continuous_max_drawdown": "",
                "total_fees_usd": "",
                "total_fixed_cost_usd": "",
                "total_variable_cost_usd": "",
            }
        else:
            results = [row.result for row in valid]
            assert all(result is not None for result in results)
            realized = [result for result in results if result is not None]
            returns = [result.window_net_return for result in realized]
            samples = tuple(
                sample
                for result in realized
                for sample in result.value_samples
            )
            metrics_fields = {
                "cumulative_return": (
                    realized[-1].closing_cash_usd
                    / realized[0].opening_capital_usd
                    - 1.0
                ),
                "mean_window_return": statistics.fmean(returns),
                "median_window_return": statistics.median(returns),
                "positive_window_rate": sum(value > 0 for value in returns)
                / len(returns),
                "worst_window_return": min(returns),
                "continuous_max_drawdown": signed_max_drawdown(
                    samples,
                    realized[0].opening_capital_usd,
                ),
                "total_fees_usd": sum(result.total_fees_usd for result in realized),
                "total_fixed_cost_usd": sum(
                    result.total_fixed_cost_usd for result in realized
                ),
                "total_variable_cost_usd": sum(
                    result.total_variable_cost_usd for result in realized
                ),
            }
        rows.append({**base, **metrics_fields})
    return rows


def _pbo_result(matrix: Sequence[Sequence[float]]) -> dict[str, object]:
    window_count = len(matrix[0])
    partitions = min(8, window_count)
    if partitions % 2:
        partitions -= 1
    result = compute_pbo([list(row) for row in matrix], partitions=partitions)
    return {"status": "computed", **asdict(result)}


def build_pbo_payload(
    records: Sequence[PrimaryWindowRecord],
    *,
    expected_economic_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    if not records:
        raise ValueError("PBO requires primary window records")
    candidate_ids = (
        tuple(expected_economic_ids)
        if expected_economic_ids is not None
        else tuple(row.economic_id for row in records[0].candidates)
    )
    candidate_assessment = assess_candidate_reset_matrix(records, candidate_ids)
    candidate_matrix: list[list[float]] = []
    for economic_id in candidate_ids:
        values: list[float] = []
        for record in records:
            row = next(
                item for item in record.candidates if item.economic_id == economic_id
            )
            if row.status != "valid" or row.economics is None:
                continue
            values.append(row.economics.window_net_return)
        if len(values) == len(records):
            candidate_matrix.append(values)
    if candidate_assessment.status == "invalid_incomplete_matrix":
        invalid_status_counts: dict[str, int] = {}
        for failure in candidate_assessment.invalid_observations:
            invalid_status_counts[failure.status] = (
                invalid_status_counts.get(failure.status, 0) + 1
            )
        candidate_pbo: dict[str, object] = {
            "status": "invalid_incomplete_matrix",
            "expected_rows": candidate_assessment.expected_rows,
            "observed_rows": candidate_assessment.observed_rows,
            "valid_rows": candidate_assessment.valid_rows,
            "invalid_rows": candidate_assessment.invalid_rows,
            "invalid_status_counts": invalid_status_counts,
            "invalid_observations": [
                asdict(failure)
                for failure in candidate_assessment.invalid_observations
            ],
        }
    elif len(records) < 2 or len(candidate_matrix) < 2:
        candidate_pbo = {"status": "insufficient_complete_matrix"}
    else:
        candidate_pbo = _pbo_result(candidate_matrix)

    allocation_invalid: list[dict[str, object]] = []
    allocation_matrix: list[list[float]] = []
    for rule in ALLOCATION_RULE_IDS:
        values = []
        for record in records:
            outcome = record.reset_rules[rule]
            if (
                outcome.status != "valid"
                or outcome.economics is None
                or not math.isfinite(outcome.economics.window_net_return)
            ):
                allocation_invalid.append(
                    {"allocation_rule": rule, "window_index": record.window_index}
                )
                continue
            values.append(outcome.economics.window_net_return)
        if len(values) == len(records):
            allocation_matrix.append(values)
    if allocation_invalid or len(allocation_matrix) != len(ALLOCATION_RULE_IDS):
        allocation_pbo: dict[str, object] = {
            "status": "invalid_incomplete_matrix",
            "invalid_observations": allocation_invalid,
        }
    elif len(records) < 2:
        allocation_pbo = {"status": "insufficient_complete_matrix"}
    else:
        allocation_pbo = _pbo_result(allocation_matrix)
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "pool": records[0].pool,
        "candidate_sleeves": candidate_pbo,
        "allocation_rules": allocation_pbo,
        "carried_paths": {
            "status": "not_applicable_path_dependent_carried_bankroll"
        },
    }


def _comparator_conclusion_rows(
    records: Sequence[PrimaryWindowRecord],
    pbo: Mapping[str, object],
) -> list[dict[str, object]]:
    candidate_status = str(
        cast_mapping(pbo["candidate_sleeves"])["status"]
    )
    allocation_status = str(
        cast_mapping(pbo["allocation_rules"])["status"]
    )
    rows: list[dict[str, object]] = []
    for rule in ALLOCATION_RULE_IDS:
        for comparator_id in COMPARATOR_IDS:
            pairs: list[float] = []
            invalid_rule = 0
            invalid_comparator = 0
            for record in records:
                rule_outcome = record.carried_rules[rule]
                comparator = record.comparators[comparator_id]
                if rule_outcome.status != "valid" or rule_outcome.result is None:
                    invalid_rule += 1
                if comparator.status != "valid" or comparator.result is None:
                    invalid_comparator += 1
                if (
                    rule_outcome.status == "valid"
                    and rule_outcome.result is not None
                    and comparator.status == "valid"
                    and comparator.result is not None
                ):
                    pairs.append(
                        rule_outcome.result.window_net_return
                        - comparator.result.window_net_return
                    )
            if invalid_rule or invalid_comparator:
                conclusion = "not_evaluable_invalid_path"
            elif (
                candidate_status != "computed"
                or allocation_status != "computed"
                or len(pairs) < 2
            ):
                conclusion = "insufficient_complete_matrix"
            else:
                conclusion = "diagnostic_only"
            if conclusion == "diagnostic_only":
                paired_fields: dict[str, object] = {
                    "mean_paired_excess_return": statistics.fmean(pairs),
                    "median_paired_excess_return": statistics.median(pairs),
                    "worst_paired_excess_return": min(pairs),
                    "positive_paired_excess_rate": sum(value > 0 for value in pairs)
                    / len(pairs),
                }
            else:
                paired_fields = {
                    "mean_paired_excess_return": "",
                    "median_paired_excess_return": "",
                    "worst_paired_excess_return": "",
                    "positive_paired_excess_rate": "",
                }
            rows.append(
                {
                    "schema_version": ARTIFACT_SCHEMA_VERSION,
                    "pool": records[0].pool,
                    "allocation_rule": rule,
                    "comparator_id": comparator_id,
                    "paired_valid_windows": len(pairs),
                    "invalid_rule_windows": invalid_rule,
                    "invalid_comparator_windows": invalid_comparator,
                    **paired_fields,
                    "candidate_pbo_status": candidate_status,
                    "allocation_pbo_status": allocation_status,
                    "conclusion_status": conclusion,
                }
            )
    return rows


def cast_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("expected a mapping payload")
    return value


def _validate_row_shapes(
    rows: Mapping[str, Sequence[Mapping[str, object]]],
) -> None:
    for name, fieldnames in CSV_FIELDS.items():
        for index, row in enumerate(rows[name]):
            if tuple(row) != fieldnames:
                raise ValueError(
                    f"{name} row {index} does not match the frozen field order"
                )


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_durable(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _candidate_matrix_manifest(
    assessment: CandidateMatrixAssessment,
) -> dict[str, object]:
    invalid_status_counts: dict[str, int] = {}
    for failure in assessment.invalid_observations:
        invalid_status_counts[failure.status] = (
            invalid_status_counts.get(failure.status, 0) + 1
        )
    return {
        "status": assessment.status,
        "expected_rows": assessment.expected_rows,
        "observed_rows": assessment.observed_rows,
        "valid_rows": assessment.valid_rows,
        "invalid_rows": assessment.invalid_rows,
        "invalid_status_counts": invalid_status_counts,
    }


def publish_artifacts(
    out_dir: Path,
    *,
    catalog: PortfolioCatalog,
    records: Sequence[PrimaryWindowRecord],
    removal_records: Sequence[RemovalWindowRecord],
    run_identity: Mapping[str, object],
    run_kind: str,
) -> dict[str, object]:
    """Validate and atomically publish one complete pool-local artifact set."""
    if run_kind not in {
        "smoke",
        "completed_amended_protocol_run",
        "completed_primary_invalid_candidate_matrix",
    }:
        raise ValueError("unsupported publication run kind")
    if run_identity.get("pool") != catalog.pool:
        raise ValueError("publication identity pool does not match the catalog")
    total_windows = run_identity.get("total_windows")
    if (
        isinstance(total_windows, bool)
        or not isinstance(total_windows, int)
        or total_windows != len(records)
    ):
        raise ValueError("publication identity window count is incomplete")
    if out_dir.exists():
        raise FileExistsError(f"publication directory already exists: {out_dir}")
    expected_ids = tuple(unit.sleeve_id for unit in catalog.allocation_units)
    candidate_matrix = assess_candidate_reset_matrix(records, expected_ids)
    if (
        run_kind == "completed_amended_protocol_run"
        and candidate_matrix.status != "complete_valid"
    ):
        raise ValueError("completed amended run requires a valid candidate matrix")
    if (
        run_kind == "completed_primary_invalid_candidate_matrix"
        and candidate_matrix.status != "invalid_incomplete_matrix"
    ):
        raise ValueError("primary-only invalid run requires an invalid candidate matrix")
    rows = build_artifact_rows(catalog, records, removal_records)
    pbo = build_pbo_payload(records, expected_economic_ids=expected_ids)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            dir=out_dir.parent,
            prefix=f".{out_dir.name}.staging-",
        )
    )
    try:
        for name, fieldnames in CSV_FIELDS.items():
            with (staging / name).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fieldnames,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(
                    {
                        field: _csv_cell(row[field])
                        for field in fieldnames
                    }
                    for row in rows[name]
                )
                handle.flush()
                os.fsync(handle.fileno())
        _write_bytes_durable(
            staging / "pbo_allocation_rules.json",
            _canonical_json_bytes(pbo),
        )
        candidate_status = cast_mapping(pbo["candidate_sleeves"])["status"]
        allocation_status = cast_mapping(pbo["allocation_rules"])["status"]
        summary = "\n".join(
            (
                f"# Corrected weighted portfolio: {catalog.pool}",
                "",
                f"- run kind: `{run_kind}`",
                f"- completed windows: {len(records)}",
                f"- canonical economic units: {len(catalog.allocation_units)}",
                f"- retained declarations: {len(catalog.declarations)}",
                f"- candidate reset PBO status: `{candidate_status}`",
                f"- allocation reset PBO status: `{allocation_status}`",
                "- carried paths: sequential boundary-settled USD capital",
                "- conclusions: diagnostic only; no live-promotion authorization",
                "",
            )
        )
        _write_bytes_durable(staging / "summary.md", summary.encode())

        artifacts: dict[str, dict[str, object]] = {}
        for name in (*CSV_FIELDS, "pbo_allocation_rules.json", "summary.md"):
            path = staging / name
            artifacts[name] = {
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
                "row_count": len(rows[name]) if name in rows else None,
            }
        removed_economic_id = (
            removal_records[0].removed_economic_id
            if removal_records
            else None
        )
        removal_phase = (
            {
                "status": "completed",
                "completed_windows": len(removal_records),
                "expected_windows": len(records),
                "removed_economic_id": removed_economic_id,
            }
            if removal_records
            else {
                "status": "not_run_incomplete_candidate_reset_matrix",
                "completed_windows": 0,
                "expected_windows": len(records),
                "removed_economic_id": None,
            }
        )
        publication_status = (
            "completed"
            if candidate_matrix.status == "complete_valid"
            else "completed_with_invalid_candidate_reset_matrix"
        )
        manifest: dict[str, object] = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "protocol_version": "2026-07-23",
            "run_kind": run_kind,
            "publication_status": publication_status,
            "pool": catalog.pool,
            "completed_windows": len(records),
            "canonical_economic_units": len(catalog.allocation_units),
            "retained_declarations": len(catalog.declarations),
            "primary_run_identity": dict(run_identity),
            "primary_phase": {
                "status": "completed",
                "completed_windows": len(records),
                "expected_windows": len(records),
            },
            "candidate_reset_matrix": _candidate_matrix_manifest(
                candidate_matrix
            ),
            "removal_phase": removal_phase,
            "best_sleeve_removal_economic_id": removed_economic_id,
            "pbo_status": {
                "candidate_sleeves": candidate_status,
                "allocation_rules": allocation_status,
                "carried_paths": "not_applicable_path_dependent_carried_bankroll",
            },
            "artifacts": artifacts,
        }
        _write_bytes_durable(
            staging / "run_manifest.json",
            _canonical_json_bytes(manifest),
        )
        observed = {path.name for path in staging.iterdir()}
        if observed != set(REQUIRED_ARTIFACTS):
            raise ValueError("staged artifact set is incomplete")
        for name, metadata in artifacts.items():
            if _sha256(staging / name) != metadata["sha256"]:
                raise ValueError(f"staged artifact hash drifted for {name}")
        directory_descriptor = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        os.replace(staging, out_dir)
        parent_descriptor = os.open(out_dir.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _csv_cell(value: object) -> object:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("CSV artifacts cannot contain non-finite values")
        return format(value, ".17g")
    return value
