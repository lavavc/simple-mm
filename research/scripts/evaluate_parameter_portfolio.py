"""Causal walk-forward evaluation of the frozen LP parameter portfolio."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.data import Event, load_v4_events
from research.backtester.pbo import PBOResult, compute_pbo
from research.backtester.pool_state import PoolState
from research.backtester.portfolio_allocation import (
    Allocation,
    TrainingMetrics,
    equal_config_weights,
    equal_family_weights,
    is_eligible,
    shrinkage_weights,
)
from research.backtester.portfolio_catalog import (
    PortfolioCatalog,
    SleeveDefinition,
    build_portfolio_catalog,
)
from research.backtester.portfolio_simulator import (
    LiquidityShareExceeded,
    PortfolioResult,
    simulate_portfolio,
)
from research.backtester.run import (
    WindowSlice,
    WindowSpec,
    _build_pool_state,
    _compute_metrics,
    _iter_window_slices,
)
from research.backtester.simulator import PoolConfig, simulate_pool
from research.scripts.evaluate_directional_paper_lp import route_directional_archetype
from research.scripts.evaluate_flow_gated_lp import build_entry_states
from research.scripts.evaluate_frozen_family_lp import POOL_EXPERIMENTS, PoolExperiment

ARTIFACT_NAMES = (
    "configuration_catalog.csv",
    "training_eligibility.csv",
    "window_weights.csv",
    "sleeve_validation_matrix.csv",
    "family_validation_matrix.csv",
    "portfolio_validation_matrix.csv",
    "comparators.csv",
    "concentration_and_contribution.csv",
    "pbo_allocation_rules.json",
    "summary.md",
)
ALLOCATION_RULE_NAMES = ("equal_config", "equal_family", "shrinkage")


@dataclass(frozen=True)
class PortfolioRuleOutcome:
    allocation: Allocation
    status: Literal["valid", "invalid_liquidity_cap"]
    result: PortfolioResult | None
    observed_share: float | None
    cap: float | None

    def __post_init__(self) -> None:
        if self.status == "valid":
            if (
                self.result is None
                or self.observed_share is not None
                or self.cap is not None
            ):
                raise ValueError("valid portfolio outcome requires only a result")
            return
        if self.status != "invalid_liquidity_cap":
            raise ValueError(f"unsupported portfolio outcome status {self.status!r}")
        if self.result is not None or self.observed_share is None or self.cap is None:
            raise ValueError(
                "invalid liquidity-cap outcome requires evidence without a result"
            )
        if (
            not math.isfinite(self.observed_share)
            or not math.isfinite(self.cap)
            or not 0.0 < self.cap < self.observed_share <= 1.0
        ):
            raise ValueError("invalid liquidity-cap evidence is incoherent")


@dataclass(frozen=True)
class WindowEvaluation:
    pool: str
    window_index: int
    window_start: str
    window_end: str
    training_metrics: Mapping[str, TrainingMetrics]
    rule_outcomes: Mapping[str, PortfolioRuleOutcome]
    comparator_metrics: Mapping[str, Mapping[str, float]]
    routed_catalog: PortfolioCatalog

    def __post_init__(self) -> None:
        if set(self.rule_outcomes) != set(ALLOCATION_RULE_NAMES):
            raise ValueError("window evaluation requires exactly three allocation rules")
        for rule, outcome in self.rule_outcomes.items():
            if outcome.allocation.rule != rule:
                raise ValueError(
                    f"rule outcome {rule!r} contains allocation "
                    f"{outcome.allocation.rule!r}"
                )


def route_directional_catalog(
    catalog: PortfolioCatalog, entry_state: Mapping[str, str]
) -> PortfolioCatalog:
    """Resolve every directional policy to one causal archetype or cash."""
    archetype, _ = route_directional_archetype(dict(entry_state))
    routed = list(catalog.sleeves)
    if archetype != "no_position":
        for policy in catalog.directional_policies:
            member = policy.archetypes.get(archetype)
            if member is None:
                raise ValueError(
                    f"directional policy {policy.sleeve_id} lacks archetype {archetype}"
                )
            routed.append(replace(member, sleeve_id=policy.sleeve_id, family="directional"))
    return PortfolioCatalog(catalog.pool, tuple(routed), ())


def _training_metrics(
    sleeves: Sequence[SleeveDefinition],
    events: list[Event],
    pool_config: PoolConfig,
    bankroll_usd: float,
) -> dict[str, TrainingMetrics]:
    metrics: dict[str, TrainingMetrics] = {}
    for sleeve in sleeves:
        sim = simulate_pool(events, sleeve.params, pool_config, bankroll_usd)
        values = _compute_metrics(sim, bankroll_usd)
        metrics[sleeve.sleeve_id] = TrainingMetrics(
            net_return=float(values["net_return"]),
            episode_count=int(values["episode_count"]),
            fee_to_transaction_cost_ratio=float(values["fee_to_transaction_cost_ratio"]),
            max_drawdown=float(values["max_drawdown"]),
        )
    return metrics


def _portfolio_return(result: PortfolioResult) -> float:
    return result.final_value / result.bankroll_usd - 1.0


def _simulate_rule_outcome(
    *,
    events: Sequence[Event],
    sleeves: Sequence[SleeveDefinition],
    allocation: Allocation,
    pool_config: PoolConfig,
    bankroll_usd: float,
    initial_pool_state: PoolState | None,
) -> PortfolioRuleOutcome:
    try:
        result = simulate_portfolio(
            events=events,
            sleeves=sleeves,
            allocation=allocation,
            pool_config=pool_config,
            bankroll_usd=bankroll_usd,
            initial_pool_state=initial_pool_state,
        )
    except LiquidityShareExceeded as exc:
        return PortfolioRuleOutcome(
            allocation=allocation,
            status="invalid_liquidity_cap",
            result=None,
            observed_share=exc.observed_share,
            cap=exc.cap,
        )
    return PortfolioRuleOutcome(
        allocation=allocation,
        status="valid",
        result=result,
        observed_share=None,
        cap=None,
    )


def evaluate_window(
    *,
    pool: str,
    catalog: PortfolioCatalog,
    window_slice: WindowSlice,
    entry_state: Mapping[str, str],
    pool_config: PoolConfig,
    bankroll_usd: float,
) -> WindowEvaluation:
    """Train once, freeze three allocations, then jointly validate them."""
    routed = route_directional_catalog(catalog, entry_state)
    training = _training_metrics(
        routed.sleeves, window_slice.train_events, pool_config, bankroll_usd
    )
    rules = (equal_config_weights, equal_family_weights, shrinkage_weights)
    allocations = {
        rule.__name__.removesuffix("_weights"): rule(catalog.allocation_units, training)
        for rule in rules
    }
    initial_pool_state = _build_pool_state(window_slice.train_events)
    rule_outcomes: dict[str, PortfolioRuleOutcome] = {}
    for name in ALLOCATION_RULE_NAMES:
        rule_outcomes[name] = _simulate_rule_outcome(
            events=window_slice.val_events,
            sleeves=routed.sleeves,
            allocation=allocations[name],
            pool_config=pool_config,
            bankroll_usd=bankroll_usd,
            initial_pool_state=initial_pool_state,
        )
    comparators: dict[str, Mapping[str, float]] = {}
    for sleeve in routed.sleeves:
        sim = simulate_pool(
            window_slice.val_events,
            sleeve.params,
            pool_config,
            bankroll_usd,
            initial_pool_state=initial_pool_state,
        )
        comparators[sleeve.sleeve_id] = {
            key: float(value)
            for key, value in _compute_metrics(sim, bankroll_usd).items()
            if isinstance(value, (int, float))
        }
    return WindowEvaluation(
        pool=pool,
        window_index=window_slice.window.index,
        window_start=window_slice.window.val_start.isoformat(),
        window_end=window_slice.window.val_end.isoformat(),
        training_metrics=training,
        rule_outcomes=rule_outcomes,
        comparator_metrics=comparators,
        routed_catalog=routed,
    )


def remove_sleeve_from_allocation(allocation: Allocation, sleeve_id: str) -> Allocation:
    weights = {key: value for key, value in allocation.weights.items() if key != sleeve_id}
    return Allocation(allocation.rule, weights, 1.0 - sum(weights.values()))


def compute_matrix_pbo(
    rows: Mapping[str, Mapping[int, float]], *, partitions: int | None = None
) -> PBOResult:
    if len(rows) < 2:
        raise ValueError("PBO needs at least two matrix rows")
    window_sets = [set(values) for values in rows.values()]
    if not window_sets or any(item != window_sets[0] for item in window_sets[1:]):
        raise ValueError("ragged matrix: rows cover different windows")
    windows = sorted(window_sets[0])
    if len(windows) < 2:
        raise ValueError("PBO needs at least two completed windows")
    selected_partitions = partitions or min(8, len(windows))
    if selected_partitions % 2:
        selected_partitions -= 1
    matrix = [[values[index] for index in windows] for values in rows.values()]
    return compute_pbo(matrix, partitions=selected_partitions)


def _limit_catalog(catalog: PortfolioCatalog, limit: int | None) -> PortfolioCatalog:
    if limit is None:
        return catalog
    if limit <= 0:
        raise ValueError("catalog limit must be positive")
    counts: defaultdict[str, int] = defaultdict(int)
    sleeves: list[SleeveDefinition] = []
    for sleeve in catalog.sleeves:
        if counts[sleeve.family] < limit:
            sleeves.append(sleeve)
            counts[sleeve.family] += 1
    policies = catalog.directional_policies[:limit]
    return PortfolioCatalog(catalog.pool, tuple(sleeves), tuple(policies))


def _common(evaluation: WindowEvaluation) -> dict[str, object]:
    return {
        "pool": evaluation.pool,
        "window_index": evaluation.window_index,
        "window_start": evaluation.window_start,
        "window_end": evaluation.window_end,
    }


def _max_drawdown(result: PortfolioResult) -> float:
    peak = result.bankroll_usd
    drawdown = 0.0
    for _, value in result.value_samples:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1.0)
    return drawdown


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        path.write_text("")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a CSV header")
        return [dict(row) for row in reader]


def _cash_comparator_metrics(bankroll_usd: float) -> dict[str, float]:
    return {
        "composite": 0.0,
        "net_return": 0.0,
        "apy": 0.0,
        "win_score": 0.0,
        "max_drawdown": 0.0,
        "time_in_range": 0.0,
        "episode_count": 0.0,
        "rebalance_count": 0.0,
        "total_fees": 0.0,
        "total_transaction_cost": 0.0,
        "fee_to_transaction_cost_ratio": 0.0,
        "total_price_impact_cost": 0.0,
        "final_value": bankroll_usd,
        "divergent_loss": 0.0,
    }


def _require_complete_matrix(
    matrix: Mapping[str, Mapping[int, float]],
    expected_rows: Sequence[str],
    completed_windows: set[int],
    *,
    label: str,
) -> None:
    for identity in expected_rows:
        observed = set(matrix.get(identity, {}))
        if observed != completed_windows:
            missing = sorted(completed_windows - observed)
            raise ValueError(f"ragged {label} matrix: {identity} missing windows {missing}")


def _artifact_rows(
    catalog: PortfolioCatalog,
    evaluations: Sequence[WindowEvaluation],
    window_slices: Mapping[int, WindowSlice],
    pool_config: PoolConfig,
    bankroll_usd: float,
) -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    sleeve_matrix: defaultdict[str, dict[int, float]] = defaultdict(dict)
    family_matrix: defaultdict[str, dict[int, float]] = defaultdict(dict)
    portfolio_matrix: defaultdict[str, dict[int, float]] = defaultdict(dict)
    invalid_windows_by_rule: defaultdict[str, list[int]] = defaultdict(list)
    for evaluation in evaluations:
        common = _common(evaluation)
        routed_by_id = {item.sleeve_id: item for item in evaluation.routed_catalog.sleeves}
        for unit in catalog.allocation_units:
            routed_unit = routed_by_id.get(unit.sleeve_id)
            rows["configuration_catalog.csv"].append(
                {
                    **common,
                    "sleeve_id": unit.sleeve_id,
                    "family": unit.family,
                    "config_name": getattr(unit, "config_name", getattr(unit, "profile", "")),
                    "routed_config_name": (
                        routed_unit.config_name if routed_unit is not None else "no_position"
                    ),
                }
            )
            metric = evaluation.training_metrics.get(unit.sleeve_id)
            rows["training_eligibility.csv"].append(
                {
                    **common,
                    "sleeve_id": unit.sleeve_id,
                    "eligible": metric is not None and is_eligible(metric),
                    **(asdict(metric) if metric is not None else {}),
                }
            )
        for rule in ALLOCATION_RULE_NAMES:
            allocation = evaluation.rule_outcomes[rule].allocation
            for unit in catalog.allocation_units:
                rows["window_weights.csv"].append(
                    {
                        **common,
                        "rule": rule,
                        "sleeve_id": unit.sleeve_id,
                        "weight": allocation.weights.get(unit.sleeve_id, 0.0),
                        "cash_weight": allocation.cash_weight,
                    }
                )
        family_values: defaultdict[str, list[float]] = defaultdict(list)
        units_by_id = {unit.sleeve_id: unit for unit in catalog.allocation_units}
        for sleeve_id, unit in units_by_id.items():
            metrics = evaluation.comparator_metrics.get(sleeve_id)
            sleeve = routed_by_id.get(sleeve_id)
            if metrics is None:
                if unit.family != "directional" or sleeve is not None:
                    continue
                metrics = _cash_comparator_metrics(bankroll_usd)
            elif sleeve is None:
                raise ValueError(
                    f"comparator {sleeve_id} has metrics without a routed sleeve"
                )
            value = metrics["net_return"]
            sleeve_matrix[sleeve_id][evaluation.window_index] = value
            family_values[unit.family].append(value)
            row = {**common, "sleeve_id": sleeve_id, "family": unit.family, **metrics}
            rows["sleeve_validation_matrix.csv"].append(row)
            rows["comparators.csv"].append(row)
        for family, values in family_values.items():
            value = sum(values) / len(values)
            family_matrix[family][evaluation.window_index] = value
            rows["family_validation_matrix.csv"].append(
                {**common, "family": family, "mean_standalone_net_return": value}
            )
        for rule in ALLOCATION_RULE_NAMES:
            outcome = evaluation.rule_outcomes[rule]
            allocation = outcome.allocation
            result = outcome.result
            if result is None:
                invalid_windows_by_rule[rule].append(evaluation.window_index)
                performance: dict[str, object] = {
                    "net_return": "",
                    "max_drawdown": "",
                    "final_value": "",
                    "cash_value": "",
                    "total_fees": "",
                    "total_transaction_cost": "",
                    "max_aggregate_liquidity_share": "",
                }
            else:
                value = _portfolio_return(result)
                portfolio_matrix[rule][evaluation.window_index] = value
                performance = {
                    "net_return": value,
                    "max_drawdown": _max_drawdown(result),
                    "final_value": result.final_value,
                    "cash_value": result.cash_value,
                    "total_fees": result.total_fees,
                    "total_transaction_cost": result.total_transaction_cost,
                    "max_aggregate_liquidity_share": (
                        result.max_aggregate_liquidity_share
                    ),
                }
            rows["portfolio_validation_matrix.csv"].append(
                {
                    **common,
                    "rule": rule,
                    "status": outcome.status,
                    "observed_share": (
                        ""
                        if outcome.observed_share is None
                        else f"{outcome.observed_share:.6f}"
                    ),
                    "cap": "" if outcome.cap is None else f"{outcome.cap:.2f}",
                    **performance,
                    "deployed_weight": sum(allocation.weights.values()),
                    "cash_weight": allocation.cash_weight,
                }
            )

    completed = {evaluation.window_index for evaluation in evaluations}
    _require_complete_matrix(
        sleeve_matrix,
        [unit.sleeve_id for unit in catalog.allocation_units],
        completed,
        label="sleeve",
    )
    _require_complete_matrix(
        family_matrix, catalog.family_names, completed, label="family"
    )
    sleeve_means = {
        key: sum(values.values()) / len(values) for key, values in sleeve_matrix.items()
    }
    best_sleeve = (
        max(sleeve_means, key=lambda sleeve_id: sleeve_means[sleeve_id]) if sleeve_means else None
    )
    for evaluation in evaluations:
        common = _common(evaluation)
        initial_pool_state = _build_pool_state(
            window_slices[evaluation.window_index].train_events
        )
        for rule in ALLOCATION_RULE_NAMES:
            allocation = evaluation.rule_outcomes[rule].allocation
            weights = list(allocation.weights.values())
            contribution = sum(
                allocation.weights.get(sleeve_id, 0.0) * metrics["net_return"]
                for sleeve_id, metrics in evaluation.comparator_metrics.items()
            )
            removed_weight: float | str = ""
            removal_deployed_weight: float | str = ""
            removal_cash_weight: float | str = ""
            removal_status = ""
            removal_observed_share = ""
            removal_cap = ""
            removal_return: float | str = ""
            if best_sleeve is not None:
                removed_weight = allocation.weights.get(best_sleeve, 0.0)
                removed = remove_sleeve_from_allocation(allocation, best_sleeve)
                removal_deployed_weight = sum(removed.weights.values())
                removal_cash_weight = removed.cash_weight
                removal_outcome = _simulate_rule_outcome(
                    events=window_slices[evaluation.window_index].val_events,
                    sleeves=evaluation.routed_catalog.sleeves,
                    allocation=removed,
                    pool_config=pool_config,
                    bankroll_usd=bankroll_usd,
                    initial_pool_state=initial_pool_state,
                )
                removal_status = removal_outcome.status
                if removal_outcome.observed_share is not None:
                    removal_observed_share = f"{removal_outcome.observed_share:.6f}"
                if removal_outcome.cap is not None:
                    removal_cap = f"{removal_outcome.cap:.2f}"
                if removal_outcome.result is not None:
                    removal_return = _portfolio_return(removal_outcome.result)
            rows["concentration_and_contribution.csv"].append(
                {
                    **common,
                    "rule": rule,
                    "herfindahl": sum(weight * weight for weight in weights),
                    "largest_weight": max(weights, default=0.0),
                    "standalone_weighted_contribution": contribution,
                    "best_sleeve_removed": best_sleeve or "",
                    "best_sleeve_removed_weight": removed_weight,
                    "best_sleeve_removal_deployed_weight": removal_deployed_weight,
                    "best_sleeve_removal_cash_weight": removal_cash_weight,
                    "best_sleeve_removal_status": removal_status,
                    "best_sleeve_removal_observed_share": removal_observed_share,
                    "best_sleeve_removal_cap": removal_cap,
                    "best_sleeve_removal_net_return": removal_return,
                }
            )

    pbo_payload: dict[str, object] = {}
    matrices = {
        "sleeves": sleeve_matrix,
        "families": family_matrix,
    }
    for name, matrix in matrices.items():
        if len(matrix) >= 2 and len(completed) >= 2:
            pbo_payload[name] = asdict(compute_matrix_pbo(matrix))
        else:
            pbo_payload[name] = {"status": "insufficient_complete_matrix"}
    if invalid_windows_by_rule:
        pbo_payload["allocation_rules"] = {
            "status": "invalid_incomplete_matrix",
            "invalid_window_indexes_by_rule": {
                rule: sorted(invalid_windows_by_rule[rule])
                for rule in ALLOCATION_RULE_NAMES
                if rule in invalid_windows_by_rule
            },
        }
    elif len(portfolio_matrix) >= 2 and len(completed) >= 2:
        _require_complete_matrix(
            portfolio_matrix,
            ALLOCATION_RULE_NAMES,
            completed,
            label="portfolio",
        )
        pbo_payload["allocation_rules"] = asdict(compute_matrix_pbo(portfolio_matrix))
    else:
        pbo_payload["allocation_rules"] = {"status": "insufficient_complete_matrix"}
    return rows, pbo_payload


def evaluate_pool(
    experiment: PoolExperiment,
    *,
    out_dir: Path,
    max_windows: int | None,
    catalog_limit_per_family: int | None,
    full_run: bool,
) -> list[WindowEvaluation]:
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog = _limit_catalog(
        build_portfolio_catalog(experiment.pool, experiment.initial_capital_usd),
        catalog_limit_per_family,
    )
    feature_rows = _read_csv(experiment.feature_csv)
    qts_rows = _read_csv(experiment.qts_feature_csv)
    entry_states = build_entry_states(
        feature_rows,
        qts_rows=qts_rows,
        train_swaps=experiment.train_swaps,
        val_swaps=experiment.val_swaps,
        stride_swaps=experiment.val_swaps,
        flow_threshold=Decimal("0.90"),
        train_return_max=Decimal("0"),
        max_windows=max_windows,
    )
    states = {int(row["window_index"]): row for row in entry_states}
    events: list[Event] = list(
        load_v4_events(
            str(experiment.history_csv),
            pool_id=experiment.pool_config.pool_address,
        )
    )
    spec = WindowSpec(
        mode="swap_count",
        train_swaps=experiment.train_swaps,
        val_swaps=experiment.val_swaps,
        stride_swaps=experiment.val_swaps,
        min_train_swaps=experiment.train_swaps,
        min_train_liquidity_events=0,
        min_val_swaps=experiment.val_swaps,
    )
    slices = [
        item
        for item in _iter_window_slices(events, spec, max_windows=max_windows)
        if item.skipped_reason is None
    ]
    evaluations = [
        evaluate_window(
            pool=experiment.pool,
            catalog=catalog,
            window_slice=item,
            entry_state=states[item.window.index],
            pool_config=experiment.pool_config,
            bankroll_usd=experiment.initial_capital_usd,
        )
        for item in slices
    ]
    artifact_rows, pbo = _artifact_rows(
        catalog,
        evaluations,
        {item.window.index: item for item in slices},
        experiment.pool_config,
        experiment.initial_capital_usd,
    )
    for name in ARTIFACT_NAMES[:-2]:
        _write_csv(out_dir / name, artifact_rows[name])
    (out_dir / "pbo_allocation_rules.json").write_text(
        json.dumps(pbo, indent=2, sort_keys=True) + "\n"
    )
    label = "FULL RUN" if full_run else "SMOKE / NOT EVIDENCE"
    summary_lines = (
        f"# Parameter Portfolio: {experiment.pool}",
        "",
        f"- run label: **{label}**",
        f"- completed windows: {len(evaluations)}",
        f"- allocation units: {len(catalog.allocation_units)}",
        "- causal routing: directional policy resolved before training and allocation",
        "- validation: joint simulator only for portfolio claims",
    )
    (out_dir / "summary.md").write_text("\n".join(summary_lines) + "\n")
    return evaluations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", choices=["uni-base", "uni-bsc", "all"], default="all")
    parser.add_argument(
        "--out-dir", type=Path, default=Path("research/results/parameter_portfolio")
    )
    parser.add_argument("--max-windows", type=int)
    parser.add_argument(
        "--catalog-limit-per-family",
        type=int,
        help="Smoke-test only; forbidden when --full-run is set",
    )
    parser.add_argument("--full-run", action="store_true")
    return parser


def validate_cli_args(args: argparse.Namespace) -> None:
    if args.full_run and (
        args.max_windows is not None or args.catalog_limit_per_family is not None
    ):
        raise ValueError("full run forbids truncation flags")
    if args.max_windows is not None and args.max_windows <= 0:
        raise ValueError("max windows must be positive")
    if args.catalog_limit_per_family is not None and args.catalog_limit_per_family <= 0:
        raise ValueError("catalog limit must be positive")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_cli_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    pools = POOL_EXPERIMENTS if args.pool == "all" else (args.pool,)
    for pool in pools:
        evaluate_pool(
            POOL_EXPERIMENTS[pool],
            out_dir=args.out_dir / pool.replace("-", "_"),
            max_windows=args.max_windows,
            catalog_limit_per_family=args.catalog_limit_per_family,
            full_run=args.full_run,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
