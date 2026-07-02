"""Evaluate frozen DEX LP families against causal gates and baselines."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.data import Event, load_v4_events
from research.backtester.params import BacktestParams, TransactionCostModel
from research.backtester.run import (
    WindowSpec,
    _build_pool_state,
    _compute_metrics,
    _iter_window_slices,
    resolve_gas_costs,
)
from research.backtester.simulator import (
    UNISWAP_BASE_POOL,
    UNISWAP_BSC_POOL,
    PoolConfig,
    simulate_pool,
)
from research.scripts.evaluate_flow_gated_lp import (
    DEFAULT_GATE_FIELDS,
    build_entry_states,
    build_gate_summary_rows,
)

FROZEN_WIDTHS = (0.0025, 0.005, 0.01, 0.015, 0.02)
FROZEN_PROFIT_TAKES = (0.005, 0.01)
FROZEN_STOP_LOSSES = (-0.0025, -0.005, -0.01, -0.02)
FROZEN_HARVEST_FRACTION = 0.04
FROZEN_MAX_TICK_WIDTH = 5000

RESULT_FIELDS = (
    "window_index",
    "window_start",
    "window_end",
    "strategy",
    "config",
    "validation_net_return",
    "validation_total_fees",
    "validation_total_transaction_cost",
    "validation_rebalance_count",
    "validation_max_drawdown",
    "validation_final_value",
    "validation_fee_to_transaction_cost_ratio",
)


@dataclass(frozen=True)
class PoolExperiment:
    pool: str
    pool_config: PoolConfig
    history_csv: Path
    feature_csv: Path
    qts_feature_csv: Path
    initial_capital_usd: float
    train_swaps: int
    val_swaps: int


POOL_EXPERIMENTS = {
    "uni-base": PoolExperiment(
        pool="uni-base",
        pool_config=UNISWAP_BASE_POOL,
        history_csv=Path("research/data/derived/uni_base_pool_history_replay.csv"),
        feature_csv=Path("research/data/derived/uni_base_pool_features.csv"),
        qts_feature_csv=Path("research/data/derived/uni_base_flow_markout_features.csv"),
        initial_capital_usd=1200.0,
        train_swaps=200,
        val_swaps=50,
    ),
    "uni-bsc": PoolExperiment(
        pool="uni-bsc",
        pool_config=UNISWAP_BSC_POOL,
        history_csv=Path("research/data/derived/uni_bsc_pool_history_replay.csv"),
        feature_csv=Path("research/data/derived/uni_bsc_pool_features.csv"),
        qts_feature_csv=Path("research/data/derived/uni_bsc_flow_markout_features.csv"),
        initial_capital_usd=450.0,
        train_swaps=300,
        val_swaps=75,
    ),
}


def frozen_paper_configs(
    *,
    initial_capital_usd: float,
    mint_gas_usd: float | None,
    remove_gas_usd: float | None,
) -> list[tuple[str, BacktestParams]]:
    costs = TransactionCostModel(mint_gas_usd=mint_gas_usd, remove_gas_usd=remove_gas_usd)
    configs: list[tuple[str, BacktestParams]] = []
    for width in FROZEN_WIDTHS:
        for profit_take in FROZEN_PROFIT_TAKES:
            for stop_loss in FROZEN_STOP_LOSSES:
                name = (
                    f"paper_spot_w{_bps_label(width)}"
                    f"_pt{_bps_label(profit_take)}"
                    f"_sl-{_bps_label(abs(stop_loss))}"
                )
                configs.append(
                    (
                        name,
                        BacktestParams(
                            strategy_mode="paper",
                            range_mode="fixed_pct_width",
                            center_mode="spot",
                            fixed_width_pct=width,
                            harvest_upward_range_fraction=FROZEN_HARVEST_FRACTION,
                            profit_take_return=profit_take,
                            stop_loss_return=stop_loss,
                            out_of_range_overshoot_fraction=0.0,
                            exit_confirmation_swaps=1,
                            exit_price_mode="spot",
                            transaction_costs=costs,
                            initial_capital_usd=initial_capital_usd,
                            min_tick_width=50,
                            max_tick_width=FROZEN_MAX_TICK_WIDTH,
                        ),
                    )
                )
    return configs


def static_lp_configs(
    *,
    initial_capital_usd: float,
    mint_gas_usd: float | None,
    remove_gas_usd: float | None,
) -> list[tuple[str, BacktestParams]]:
    costs = TransactionCostModel(mint_gas_usd=mint_gas_usd, remove_gas_usd=remove_gas_usd)
    return [
        (
            f"static_spot_w{_bps_label(width)}",
            BacktestParams(
                strategy_mode="static",
                range_mode="fixed_pct_width",
                center_mode="spot",
                fixed_width_pct=width,
                harvest_upward_range_fraction=None,
                profit_take_return=None,
                stop_loss_return=None,
                out_of_range_overshoot_fraction=None,
                transaction_costs=costs,
                initial_capital_usd=initial_capital_usd,
                min_tick_width=50,
                max_tick_width=FROZEN_MAX_TICK_WIDTH,
            ),
        )
        for width in FROZEN_WIDTHS
    ]


def no_position_rows(entry_states: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    return [
        _baseline_row(state, strategy="no_position", config="idle_cash", net_return="0")
        for state in entry_states
    ]


def hold_cngn_rows(entry_states: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    return [
        _baseline_row(
            state,
            strategy="hold_cngn",
            config="mark_to_pool_sqrt_mid",
            net_return=state["validation_price_return"],
        )
        for state in entry_states
    ]


def evaluate_pool(
    experiment: PoolExperiment,
    *,
    out_dir: Path,
    max_windows: int | None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    mint_gas_usd, remove_gas_usd = resolve_gas_costs(experiment.pool, None, None)
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
    _write_csv(out_dir / "entry_state_windows.csv", entry_states)

    events = load_v4_events(
        str(experiment.history_csv),
        pool_id=experiment.pool_config.pool_address,
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
    configs = [
        *(
            ("frozen_paper", name, params)
            for name, params in frozen_paper_configs(
                initial_capital_usd=experiment.initial_capital_usd,
                mint_gas_usd=mint_gas_usd,
                remove_gas_usd=remove_gas_usd,
            )
        ),
        *(
            ("passive_static_lp", name, params)
            for name, params in static_lp_configs(
                initial_capital_usd=experiment.initial_capital_usd,
                mint_gas_usd=mint_gas_usd,
                remove_gas_usd=remove_gas_usd,
            )
        ),
    ]
    result_rows = [
        *no_position_rows(entry_states),
        *hold_cngn_rows(entry_states),
        *_simulate_config_rows(
            events,
            experiment.pool_config,
            spec,
            configs,
            max_windows=max_windows,
        ),
    ]
    _write_csv(out_dir / "frozen_family_window_results.csv", result_rows)
    summary_rows = build_gate_summary_rows(
        result_rows,
        entry_states,
        gate_fields=DEFAULT_GATE_FIELDS,
        identity_fields=("strategy", "config"),
    )
    _write_csv(out_dir / "frozen_family_gate_summary.csv", summary_rows)
    (out_dir / "frozen_family_report.md").write_text(
        render_markdown(pool=experiment.pool, summary_rows=summary_rows)
    )
    print(f"{experiment.pool}: wrote {out_dir}")


def render_markdown(*, pool: str, summary_rows: Sequence[dict[str, str]]) -> str:
    lines = [
        f"# Frozen-Family LP Gate Report: {pool}",
        "",
        "Rows are costed validation-window summaries. Inactive gate windows are counted "
        "as no-position in `mean_all_window_return`; `inactive_window_return` records "
        "the return the same row would have earned in skipped windows.",
        "",
    ]
    for gate in DEFAULT_GATE_FIELDS:
        gate_rows = [row for row in summary_rows if row["gate"] == gate]
        if not gate_rows:
            continue
        lines.extend(
            [
                f"## {gate}",
                "",
                _summary_table(gate_rows[:12]),
                "",
            ]
        )
    return "\n".join(lines)


def _simulate_config_rows(
    events: list[Event],
    pool_config: PoolConfig,
    spec: WindowSpec,
    configs: Sequence[tuple[str, str, BacktestParams]],
    *,
    max_windows: int | None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for window_slice in _iter_window_slices(events, spec, max_windows=max_windows):
        if window_slice.skipped_reason is not None:
            continue
        initial_pool_state = _build_pool_state(window_slice.train_events)
        for strategy, config_name, params in configs:
            sim = simulate_pool(
                window_slice.val_events,
                params,
                pool_config,
                params.initial_capital_usd,
                initial_pool_state=initial_pool_state,
            )
            metrics = _compute_metrics(sim, params.initial_capital_usd)
            rows.append(
                {
                    "window_index": str(window_slice.window.index),
                    "window_start": window_slice.window.val_start.isoformat(),
                    "window_end": window_slice.window.val_end.isoformat(),
                    "strategy": strategy,
                    "config": config_name,
                    "validation_net_return": str(metrics["net_return"]),
                    "validation_total_fees": str(metrics["total_fees"]),
                    "validation_total_transaction_cost": str(metrics["total_transaction_cost"]),
                    "validation_rebalance_count": str(metrics["rebalance_count"]),
                    "validation_max_drawdown": str(metrics["max_drawdown"]),
                    "validation_final_value": str(metrics["final_value"]),
                    "validation_fee_to_transaction_cost_ratio": str(
                        metrics["fee_to_transaction_cost_ratio"]
                    ),
                }
            )
    return rows


def _baseline_row(
    state: dict[str, str],
    *,
    strategy: str,
    config: str,
    net_return: str,
) -> dict[str, str]:
    return {
        "window_index": state["window_index"],
        "window_start": state.get("validation_start_timestamp_ms", ""),
        "window_end": state.get("validation_end_timestamp_ms", ""),
        "strategy": strategy,
        "config": config,
        "validation_net_return": net_return,
        "validation_total_fees": "0",
        "validation_total_transaction_cost": "0",
        "validation_rebalance_count": "0",
        "validation_max_drawdown": "0",
        "validation_final_value": "",
        "validation_fee_to_transaction_cost_ratio": "0",
    }


def _summary_table(rows: Sequence[dict[str, str]]) -> str:
    fields = [
        "strategy",
        "config",
        "active_windows",
        "sum_active_return",
        "mean_all_window_return",
        "worst_active_return",
        "positive_active_rate",
        "fee_cost_ratio",
        "leave_one_active_window_out_min_return",
        "inactive_window_return",
        "active_minus_hold_cngn",
    ]
    lines = [
        "| " + " | ".join(fields) + " |",
        "| " + " | ".join("---" for _field in fields) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row.get(field, "") for field in fields) + " |")
    return "\n".join(lines)


def _bps_label(value: float) -> str:
    return f"{int(round(value * 10_000)):04d}"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a CSV header")
        return [dict(row) for row in reader]


def _write_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", choices=[*POOL_EXPERIMENTS.keys(), "all"], default="all")
    parser.add_argument("--out-root", default="research/results/flow_gated_lp")
    parser.add_argument("--max-windows", type=int)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    pools = POOL_EXPERIMENTS.keys() if args.pool == "all" else (args.pool,)
    out_root = Path(args.out_root)
    for pool in pools:
        evaluate_pool(
            POOL_EXPERIMENTS[pool],
            out_dir=out_root / pool.replace("-", "_"),
            max_windows=args.max_windows,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
