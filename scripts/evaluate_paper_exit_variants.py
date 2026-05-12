"""Evaluate paper-strategy exit-quality variants on uni-base swap-count windows."""

from __future__ import annotations

import csv
import contextlib
import os
from dataclasses import dataclass
from itertools import product
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/private/tmp/matplotlib")

import matplotlib.pyplot as plt

from backtester.data import load_v4_events
from backtester.params import (
    STOP_LOSS_RETURNS,
    BacktestParams,
)
from backtester.run import (
    V4_POOLS,
    WindowResult,
    WindowSpec,
    _params_row,
    evaluate_rolling_windows,
)


@dataclass(frozen=True)
class ExitVariant:
    name: str
    stop_losses: tuple[float | None, ...] = tuple(STOP_LOSS_RETURNS)
    min_exit_swap_volume_usd: float | None = None
    exit_confirmation_swaps: int = 1
    exit_confirmation_minutes: float = 0.0


VARIANTS = [
    ExitVariant("baseline"),
    ExitVariant("loose_stops", stop_losses=(-0.0025, -0.005, -0.01, -0.02, -0.03, -0.05, None)),
    ExitVariant("min_notional_1", min_exit_swap_volume_usd=1.0),
    ExitVariant("min_notional_10", min_exit_swap_volume_usd=10.0),
    ExitVariant("min_notional_50", min_exit_swap_volume_usd=50.0),
    ExitVariant("min_notional_100", min_exit_swap_volume_usd=100.0),
    ExitVariant("confirm_2_swaps", exit_confirmation_swaps=2),
    ExitVariant("confirm_3_swaps", exit_confirmation_swaps=3),
    ExitVariant("confirm_5m", exit_confirmation_minutes=5.0),
    ExitVariant("confirm_30m", exit_confirmation_minutes=30.0),
    ExitVariant("notional_1_confirm_2", min_exit_swap_volume_usd=1.0, exit_confirmation_swaps=2),
    ExitVariant("notional_10_confirm_2", min_exit_swap_volume_usd=10.0, exit_confirmation_swaps=2),
    ExitVariant("notional_50_confirm_2", min_exit_swap_volume_usd=50.0, exit_confirmation_swaps=2),
    ExitVariant(
        "robust_combo",
        stop_losses=(-0.0025, -0.005, -0.01, -0.02, -0.03, -0.05, None),
        min_exit_swap_volume_usd=10.0,
        exit_confirmation_swaps=2,
    ),
]

FOCUSED_WIDTH_PCTS = (0.01, 0.02, 0.05)
FOCUSED_HARVEST_RANGE_FRACTIONS = (0.04, 0.06, 0.08, 0.10)
FOCUSED_PROFIT_TAKE_RETURNS = (0.0025, 0.005, 0.01)
FOCUSED_OUT_OF_RANGE_OVERSHOOTS = (0.0, 0.05, 0.10)


def _paper_grid_for_variant(variant: ExitVariant) -> list[BacktestParams]:
    combos = product(
        FOCUSED_WIDTH_PCTS,
        ("spot", "ewma"),
        FOCUSED_HARVEST_RANGE_FRACTIONS,
        FOCUSED_PROFIT_TAKE_RETURNS,
        variant.stop_losses,
        FOCUSED_OUT_OF_RANGE_OVERSHOOTS,
    )
    return [
        BacktestParams(
            strategy_mode="paper",
            range_mode="fixed_pct_width",
            center_mode=center_mode,
            fixed_width_pct=width,
            harvest_upward_range_fraction=harvest_fraction,
            profit_take_return=profit_take,
            stop_loss_return=stop_loss,
            out_of_range_overshoot_fraction=overshoot,
            min_exit_swap_volume_usd=variant.min_exit_swap_volume_usd,
            exit_confirmation_swaps=variant.exit_confirmation_swaps,
            exit_confirmation_minutes=variant.exit_confirmation_minutes,
            cooldown_minutes=0.0,
            gas_cost_usd=0.05,
            initial_capital_usd=500.0,
            min_tick_width=50,
            max_tick_width=5000,
        )
        for width, center_mode, harvest_fraction, profit_take, stop_loss, overshoot in combos
    ]


def _selected_rows(variant: ExitVariant, rows: list[WindowResult]) -> list[dict]:
    selected: list[dict] = []
    for row in rows:
        if row.skipped_reason is not None or row.params is None or row.validation_metrics is None:
            continue
        if row.train_rank != 1:
            continue
        metrics = row.validation_metrics
        selected.append(
            {
                "variant": variant.name,
                "window_index": row.window_index,
                "window_start": row.window_start.isoformat(),
                "window_end": row.window_end.isoformat(),
                "validation_rank": row.validation_rank,
                "validation_net_return": metrics["net_return"],
                "validation_max_drawdown": metrics["max_drawdown"],
                "validation_final_value": metrics["final_value"],
                "validation_total_fees": metrics["total_fees"],
                "validation_rebalance_count": metrics["rebalance_count"],
                "validation_time_in_range": metrics["time_in_range"],
                "validation_episode_count": metrics["episode_count"],
                **_params_row(row.params),
            }
        )
    return selected


def _summary_for_variant(variant: ExitVariant, selected: list[dict]) -> dict:
    net_returns = [float(row["validation_net_return"]) for row in selected]
    drawdowns = [float(row["validation_max_drawdown"]) for row in selected]
    rebalances = [float(row["validation_rebalance_count"]) for row in selected]
    fees = [float(row["validation_total_fees"]) for row in selected]
    by_window = {int(row["window_index"]): row for row in selected}
    return {
        "variant": variant.name,
        "window_count": len(selected),
        "mean_net_return": sum(net_returns) / len(net_returns),
        "median_net_return": sorted(net_returns)[len(net_returns) // 2],
        "worst_net_return": min(net_returns),
        "best_net_return": max(net_returns),
        "worst_max_drawdown": max(drawdowns),
        "positive_window_count": sum(1 for value in net_returns if value > 0),
        "mean_rebalance_count": sum(rebalances) / len(rebalances),
        "mean_total_fees": sum(fees) / len(fees),
        "window_9_net_return": by_window.get(9, {}).get("validation_net_return"),
        "window_9_max_drawdown": by_window.get(9, {}).get("validation_max_drawdown"),
        "window_9_rebalance_count": by_window.get(9, {}).get("validation_rebalance_count"),
        "window_10_net_return": by_window.get(10, {}).get("validation_net_return"),
        "window_10_max_drawdown": by_window.get(10, {}).get("validation_max_drawdown"),
        "window_10_rebalance_count": by_window.get(10, {}).get("validation_rebalance_count"),
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _plot_summary(summary_rows: list[dict], selected_rows: list[dict], out_dir: Path) -> None:
    ranked = sorted(summary_rows, key=lambda row: (row["mean_net_return"], -row["worst_max_drawdown"]), reverse=True)
    top_names = [row["variant"] for row in ranked[:8]]

    fig, axes = plt.subplots(2, 1, figsize=(14, 9))
    labels = [row["variant"] for row in ranked]
    x = range(len(labels))
    axes[0].bar(x, [row["mean_net_return"] for row in ranked], label="mean net return")
    axes[0].bar(x, [row["worst_net_return"] for row in ranked], alpha=0.45, label="worst window net return")
    axes[0].axhline(0, color="black", lw=0.8)
    axes[0].set_ylabel("return")
    axes[0].set_title("Paper exit variants: selected walk-forward validation return")
    axes[0].legend()
    axes[0].grid(True, axis="y", alpha=0.3)

    axes[1].bar(x, [row["worst_max_drawdown"] for row in ranked], color="tab:red", alpha=0.7)
    axes[1].set_ylabel("worst max drawdown")
    axes[1].set_xticks(list(x))
    axes[1].set_xticklabels(labels, rotation=35, ha="right")
    axes[1].grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "uni_base_paper_exit_variant_summary.png", dpi=170)

    fig, ax = plt.subplots(figsize=(14, 7))
    for name in top_names:
        rows = sorted([row for row in selected_rows if row["variant"] == name], key=lambda row: int(row["window_index"]))
        ax.plot(
            [int(row["window_index"]) for row in rows],
            [float(row["validation_net_return"]) for row in rows],
            marker="o",
            label=name,
        )
    ax.axhline(0, color="black", lw=0.8)
    ax.axvspan(9, 10, color="tab:orange", alpha=0.08)
    ax.set_title("Top paper exit variants by validation window")
    ax.set_xlabel("window index")
    ax.set_ylabel("selected validation net return")
    ax.grid(True, alpha=0.3)
    ax.legend(ncol=2)
    fig.tight_layout()
    fig.savefig(out_dir / "uni_base_paper_exit_variant_windows.png", dpi=170)


def main() -> None:
    output_dir = Path("backtester/results/exit_variant_experiments")
    plot_dir = Path("backtester/results/plots")
    output_dir.mkdir(parents=True, exist_ok=True)
    plot_dir.mkdir(parents=True, exist_ok=True)

    pool_config = V4_POOLS["uni-base"]
    events = load_v4_events("data/uni_base_pool_history.csv", pool_id=pool_config.pool_address)
    spec = WindowSpec(
        mode="swap_count",
        train_swaps=200,
        val_swaps=50,
        stride_swaps=50,
        min_train_swaps=200,
        min_train_liquidity_events=0,
        min_val_swaps=50,
    )

    all_selected: list[dict] = []
    summaries: list[dict] = []
    for variant in VARIANTS:
        grid = _paper_grid_for_variant(variant)
        print(f"running {variant.name} ({len(grid)} params)", flush=True)
        with open(os.devnull, "w") as devnull, contextlib.redirect_stderr(devnull):
            rows = evaluate_rolling_windows(
                events=events,
                pool_config=pool_config,
                grid=grid,
                spec=spec,
                top_n=20,
            )
        selected = _selected_rows(variant, rows)
        all_selected.extend(selected)
        summaries.append(_summary_for_variant(variant, selected))

    summaries.sort(key=lambda row: (row["mean_net_return"], -row["worst_max_drawdown"]), reverse=True)
    _write_csv(output_dir / "uni_base_paper_exit_variant_summary.csv", summaries)
    _write_csv(output_dir / "uni_base_paper_exit_variant_selected_windows.csv", all_selected)
    _plot_summary(summaries, all_selected, plot_dir)
    print(f"wrote {output_dir}")


if __name__ == "__main__":
    main()
