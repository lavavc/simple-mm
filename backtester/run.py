"""CLI entry point for LP backtests and rolling walk-forward validation."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from backtester import metrics
from backtester.data import BurnEvent, Event, MintEvent, V4Event, load_events, load_v4_events
from backtester.params import BacktestParams, generate_grid
from backtester.pool_state import PoolState
from backtester.simulator import (
    AERODROME_POOL,
    PANCAKESWAP_POOL,
    UNISWAP_BASE_POOL,
    UNISWAP_BSC_POOL,
    PoolConfig,
    SimResult,
    simulate_pool,
)


LEGACY_POOLS = {
    "aerodrome": AERODROME_POOL,
    "pancakeswap": PANCAKESWAP_POOL,
}

V4_POOLS = {
    "uni-base": UNISWAP_BASE_POOL,
    "uni-bsc": UNISWAP_BSC_POOL,
}


@dataclass(frozen=True)
class WindowSpec:
    train_days: int = 30
    val_days: int = 7
    stride_days: int = 7
    min_train_swaps: int = 500
    min_train_liquidity_events: int = 50
    min_val_swaps: int = 100


@dataclass(frozen=True)
class WindowCounts:
    event_count: int
    swap_count: int
    liquidity_event_count: int


@dataclass(frozen=True)
class Window:
    index: int
    train_start: datetime
    train_end: datetime
    val_start: datetime
    val_end: datetime


@dataclass(frozen=True)
class WindowResult:
    window_index: int
    window_start: datetime
    window_end: datetime
    train_event_count: int
    train_swap_count: int
    train_liquidity_event_count: int
    val_event_count: int
    val_swap_count: int
    skipped_reason: str | None
    params: BacktestParams | None = None
    train_metrics: dict | None = None
    validation_metrics: dict | None = None
    train_rank: int | None = None
    validation_rank: int | None = None


def _event_time(event: Event) -> datetime:
    return event.block_time


def _compute_metrics(sim: SimResult, initial_capital: float) -> dict:
    daily_returns = sim.daily_returns
    max_drawdown = metrics.max_drawdown(metrics._cumulative(daily_returns)) if daily_returns else 0.0
    net_return = sim.final_value / initial_capital - 1.0 if initial_capital > 0 else 0.0
    return {
        "composite": metrics.composite_objective(net_return, max_drawdown),
        "net_return": net_return,
        "sortino": metrics.sortino_ratio(daily_returns),
        "calmar": metrics.calmar_ratio(daily_returns),
        "omega": metrics.omega_ratio(daily_returns),
        "max_drawdown": max_drawdown,
        "time_in_range": metrics.time_in_range_pct(sim),
        "rebalance_cost_ratio": metrics.rebalance_cost_ratio(sim),
        "win_rate": metrics.win_rate(daily_returns),
        "profit_factor": metrics.profit_factor(daily_returns),
        "cvar_5": metrics.cvar(daily_returns, 0.05),
        "return_skew": metrics.return_skew(daily_returns),
        "total_fees": sim.total_fees,
        "rebalance_count": sim.rebalance_count,
        "final_value": sim.final_value,
        "divergent_loss": sim.divergent_loss,
    }


def _run_grid(
    events: list[Event],
    pool_config: PoolConfig,
    grid: list[BacktestParams],
    initial_pool_state: PoolState | None = None,
) -> list[tuple[BacktestParams, SimResult, dict]]:
    rows = []
    total = len(grid)
    for index, params in enumerate(grid, start=1):
        if index % 100 == 0:
            print(f"  {index}/{total}", file=sys.stderr)
        sim = simulate_pool(
            events,
            params,
            pool_config,
            params.initial_capital_usd,
            initial_pool_state=initial_pool_state,
        )
        rows.append((params, sim, _compute_metrics(sim, params.initial_capital_usd)))
    return rows


def _window_counts(events: Iterable[Event]) -> WindowCounts:
    event_list = list(events)
    swap_count = sum(
        1
        for event in event_list
        if (isinstance(event, V4Event) and event.event_type == "swap") or event.__class__.__name__ == "SwapEvent"
    )
    liquidity_event_count = sum(
        1
        for event in event_list
        if (
            isinstance(event, (MintEvent, BurnEvent))
            or (isinstance(event, V4Event) and event.event_type in {"mint", "burn"})
        )
    )
    return WindowCounts(
        event_count=len(event_list),
        swap_count=swap_count,
        liquidity_event_count=liquidity_event_count,
    )


def generate_windows(events: list[Event], spec: WindowSpec, max_windows: int | None = None) -> list[Window]:
    if not events:
        return []
    start = _event_time(events[0])
    end = _event_time(events[-1])
    windows: list[Window] = []
    cursor = start
    index = 0
    while cursor + timedelta(days=spec.train_days + spec.val_days) <= end:
        train_start = cursor
        train_end = train_start + timedelta(days=spec.train_days)
        val_start = train_end
        val_end = val_start + timedelta(days=spec.val_days)
        windows.append(
            Window(
                index=index,
                train_start=train_start,
                train_end=train_end,
                val_start=val_start,
                val_end=val_end,
            )
        )
        index += 1
        if max_windows is not None and len(windows) >= max_windows:
            break
        cursor = cursor + timedelta(days=spec.stride_days)
    return windows


def _slice_events(events: list[Event], start: datetime, end: datetime) -> list[Event]:
    return [event for event in events if start <= _event_time(event) < end]


def _build_pool_state(events: list[Event]) -> PoolState:
    pool_state = PoolState()
    for event in events:
        if isinstance(event, MintEvent):
            pool_state.apply_mint(event.tick_lower, event.tick_upper, event.liquidity_delta)
        elif isinstance(event, BurnEvent):
            pool_state.apply_burn(event.tick_lower, event.tick_upper, event.liquidity_delta)
    return pool_state


def evaluate_rolling_windows(
    events: list[Event],
    pool_config: PoolConfig,
    grid: list[BacktestParams],
    spec: WindowSpec,
    top_n: int,
    max_windows: int | None = None,
) -> list[WindowResult]:
    results: list[WindowResult] = []
    for window in generate_windows(events, spec, max_windows=max_windows):
        train_events = _slice_events(events, window.train_start, window.train_end)
        val_events = _slice_events(events, window.val_start, window.val_end)
        train_counts = _window_counts(train_events)
        val_counts = _window_counts(val_events)
        skipped_reason = None
        if train_counts.swap_count < spec.min_train_swaps:
            skipped_reason = "train_swaps_below_min"
        elif train_counts.liquidity_event_count < spec.min_train_liquidity_events:
            skipped_reason = "train_liquidity_events_below_min"
        elif val_counts.swap_count < spec.min_val_swaps:
            skipped_reason = "val_swaps_below_min"

        if skipped_reason is not None:
            results.append(
                WindowResult(
                    window_index=window.index,
                    window_start=window.val_start,
                    window_end=window.val_end,
                    train_event_count=train_counts.event_count,
                    train_swap_count=train_counts.swap_count,
                    train_liquidity_event_count=train_counts.liquidity_event_count,
                    val_event_count=val_counts.event_count,
                    val_swap_count=val_counts.swap_count,
                    skipped_reason=skipped_reason,
                )
            )
            continue

        train_rows = _run_grid(train_events, pool_config, grid)
        train_rows.sort(key=lambda row: row[2]["composite"], reverse=True)
        selected = train_rows[:top_n]
        validation_rows = _run_grid(
            val_events,
            pool_config,
            [params for params, _, _ in selected],
            initial_pool_state=_build_pool_state(train_events),
        )
        validation_rows.sort(key=lambda row: row[2]["composite"], reverse=True)
        validation_rank_by_key = {
            _params_key(params): rank
            for rank, (params, _, _) in enumerate(validation_rows, start=1)
        }
        validation_metrics_by_key = {
            _params_key(params): val_metrics for params, _, val_metrics in validation_rows
        }
        for train_rank, (params, _, train_metrics) in enumerate(selected, start=1):
            key = _params_key(params)
            results.append(
                WindowResult(
                    window_index=window.index,
                    window_start=window.val_start,
                    window_end=window.val_end,
                    train_event_count=train_counts.event_count,
                    train_swap_count=train_counts.swap_count,
                    train_liquidity_event_count=train_counts.liquidity_event_count,
                    val_event_count=val_counts.event_count,
                    val_swap_count=val_counts.swap_count,
                    skipped_reason=None,
                    params=params,
                    train_metrics=train_metrics,
                    validation_metrics=validation_metrics_by_key[key],
                    train_rank=train_rank,
                    validation_rank=validation_rank_by_key[key],
                )
            )
    return results


def _params_key(params: BacktestParams) -> tuple:
    return (
        params.sd_multiplier,
        params.ewma_lambda,
        params.downside_skew,
        params.preemptive_rebalance,
        params.rebalance_threshold_pct,
    )


def aggregate_window_results(
    window_results: list[WindowResult],
    min_valid_windows: int = 1,
    max_drawdown_limit: float = 1.0,
    divergent_loss_floor: float = -1.0,
) -> list[dict]:
    grouped: dict[tuple, list[WindowResult]] = defaultdict(list)
    for result in window_results:
        if result.params is not None and result.validation_metrics is not None:
            grouped[_params_key(result.params)].append(result)

    aggregate_rows: list[dict] = []
    for key, rows in grouped.items():
        params = rows[0].params
        assert params is not None
        validation_metrics = [row.validation_metrics for row in rows if row.validation_metrics is not None]
        train_metrics = [row.train_metrics for row in rows if row.train_metrics is not None]
        assert validation_metrics
        validation_ranks = [row.validation_rank for row in rows if row.validation_rank is not None]
        win_counter = sum(1 for rank in validation_ranks if rank == 1)
        agg = {
            "sd_multiplier": params.sd_multiplier,
            "ewma_lambda": params.ewma_lambda,
            "downside_skew": params.downside_skew,
            "preemptive_rebalance": params.preemptive_rebalance,
            "rebalance_threshold_pct": params.rebalance_threshold_pct,
            "valid_window_count": len(rows),
            "mean_validation_composite": statistics.fmean(metric["composite"] for metric in validation_metrics),
            "median_validation_composite": statistics.median(metric["composite"] for metric in validation_metrics),
            "mean_validation_net_return": statistics.fmean(metric["net_return"] for metric in validation_metrics),
            "median_validation_net_return": statistics.median(metric["net_return"] for metric in validation_metrics),
            "worst_window_max_drawdown": max(metric["max_drawdown"] for metric in validation_metrics),
            "worst_window_divergent_loss": min(metric["divergent_loss"] for metric in validation_metrics),
            "validation_win_rate": win_counter / len(rows),
            "windows_won": win_counter,
            "validation_rank_stddev": statistics.pstdev(validation_ranks) if len(validation_ranks) > 1 else 0.0,
            "median_validation_time_in_range": statistics.median(metric["time_in_range"] for metric in validation_metrics),
            "mean_validation_rebalance_count": statistics.fmean(metric["rebalance_count"] for metric in validation_metrics),
            "mean_train_composite": statistics.fmean(metric["composite"] for metric in train_metrics),
            "eligible": (
                len(rows) >= min_valid_windows
                and statistics.median(metric["net_return"] for metric in validation_metrics) > 0
                and max(metric["max_drawdown"] for metric in validation_metrics) <= max_drawdown_limit
                and min(metric["divergent_loss"] for metric in validation_metrics) >= divergent_loss_floor
            ),
        }
        aggregate_rows.append(agg)

    aggregate_rows.sort(
        key=lambda row: (
            row["mean_validation_composite"],
            -row["worst_window_max_drawdown"],
            row["worst_window_divergent_loss"],
            -row["validation_rank_stddev"],
            row["median_validation_time_in_range"],
            -row["mean_validation_rebalance_count"],
        ),
        reverse=True,
    )
    return aggregate_rows


def _write_csv(rows: list[tuple[BacktestParams, SimResult, dict]], output_path: str) -> None:
    if not rows:
        return
    fieldnames = [
        "sd_multiplier",
        "ewma_lambda",
        "downside_skew",
        "preemptive_rebalance",
        "rebalance_threshold_pct",
        *rows[0][2].keys(),
    ]
    with open(output_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for params, _, metric_row in rows:
            writer.writerow(
                {
                    "sd_multiplier": params.sd_multiplier,
                    "ewma_lambda": params.ewma_lambda,
                    "downside_skew": params.downside_skew,
                    "preemptive_rebalance": params.preemptive_rebalance,
                    "rebalance_threshold_pct": params.rebalance_threshold_pct,
                    **metric_row,
                }
            )


def _write_window_results(rows: list[WindowResult], output_path: str) -> None:
    fieldnames = [
        "window_index",
        "window_start",
        "window_end",
        "train_event_count",
        "train_swap_count",
        "train_liquidity_event_count",
        "val_event_count",
        "val_swap_count",
        "skipped_reason",
        "sd_multiplier",
        "ewma_lambda",
        "downside_skew",
        "preemptive_rebalance",
        "rebalance_threshold_pct",
        "train_rank",
        "validation_rank",
        "train_composite",
        "train_net_return",
        "train_sortino",
        "train_calmar",
        "train_omega",
        "train_max_drawdown",
        "train_time_in_range",
        "train_rebalance_cost_ratio",
        "train_total_fees",
        "train_rebalance_count",
        "train_final_value",
        "train_divergent_loss",
        "validation_composite",
        "validation_net_return",
        "validation_sortino",
        "validation_calmar",
        "validation_omega",
        "validation_max_drawdown",
        "validation_time_in_range",
        "validation_rebalance_cost_ratio",
        "validation_total_fees",
        "validation_rebalance_count",
        "validation_final_value",
        "validation_divergent_loss",
    ]
    with open(output_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            base = {
                "window_index": row.window_index,
                "window_start": row.window_start.isoformat(),
                "window_end": row.window_end.isoformat(),
                "train_event_count": row.train_event_count,
                "train_swap_count": row.train_swap_count,
                "train_liquidity_event_count": row.train_liquidity_event_count,
                "val_event_count": row.val_event_count,
                "val_swap_count": row.val_swap_count,
                "skipped_reason": row.skipped_reason,
                "train_rank": row.train_rank,
                "validation_rank": row.validation_rank,
            }
            if row.params is not None:
                base.update(
                    {
                        "sd_multiplier": row.params.sd_multiplier,
                        "ewma_lambda": row.params.ewma_lambda,
                        "downside_skew": row.params.downside_skew,
                        "preemptive_rebalance": row.params.preemptive_rebalance,
                        "rebalance_threshold_pct": row.params.rebalance_threshold_pct,
                    }
                )
            for prefix, metrics_row in (("train", row.train_metrics), ("validation", row.validation_metrics)):
                if metrics_row is None:
                    continue
                for key in (
                    "composite",
                    "net_return",
                    "sortino",
                    "calmar",
                    "omega",
                    "max_drawdown",
                    "time_in_range",
                    "rebalance_cost_ratio",
                    "total_fees",
                    "rebalance_count",
                    "final_value",
                    "divergent_loss",
                ):
                    base[f"{prefix}_{key}"] = metrics_row[key]
            writer.writerow(base)


def _write_aggregate_results(rows: list[dict], output_path: str) -> None:
    if not rows:
        return
    with open(output_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_summary_json(
    output_path: str,
    dataset_format: str,
    pool: str,
    spec: WindowSpec,
    events: list[Event],
    window_results: list[WindowResult],
    top_n: int,
    max_windows: int | None,
    legacy_priors_csv: str | None,
) -> None:
    skipped = Counter(row.skipped_reason for row in window_results if row.skipped_reason)
    summary = {
        "dataset_format": dataset_format,
        "pool": pool,
        "defaults": asdict(spec),
        "top_n": top_n,
        "max_windows": max_windows,
        "legacy_priors_csv": legacy_priors_csv,
        "dataset_coverage": {
            "event_count": len(events),
            "start": _event_time(events[0]).isoformat() if events else None,
            "end": _event_time(events[-1]).isoformat() if events else None,
        },
        "valid_window_count": len({row.window_index for row in window_results if row.skipped_reason is None}),
        "skipped_window_counts": dict(skipped),
    }
    Path(output_path).write_text(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="LP backtest grid search")
    parser.add_argument("--csv", required=True, help="Path to historical data CSV")
    parser.add_argument("--dataset-format", choices=["legacy", "v4"], default="legacy")
    parser.add_argument("--pool", required=True)
    parser.add_argument("--output", default="backtest_results.csv")
    parser.add_argument("--walkforward", action="store_true")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--val-days", type=int, default=7)
    parser.add_argument("--stride-days", type=int, default=7)
    parser.add_argument("--min-train-swaps", type=int, default=500)
    parser.add_argument("--min-train-liquidity-events", type=int, default=50)
    parser.add_argument("--min-val-swaps", type=int, default=100)
    parser.add_argument("--max-windows", type=int)
    parser.add_argument("--legacy-priors-csv")
    args = parser.parse_args()

    pools = LEGACY_POOLS if args.dataset_format == "legacy" else V4_POOLS
    if args.pool not in pools:
        parser.error(f"--pool must be one of {', '.join(sorted(pools))} for {args.dataset_format} mode")
    pool_config = pools[args.pool]

    gas_cost = 0.20 if pool_config.blockchain in {"bnb", "bsc"} else 0.05
    default_capital = 200.0 if args.pool in {"pancakeswap", "uni-bsc"} else 500.0
    grid = generate_grid(gas_cost_usd=gas_cost, initial_capital_usd=default_capital)

    if args.dataset_format == "legacy":
        events = load_events(args.csv, pool_address=pool_config.pool_address)
    else:
        events = load_v4_events(args.csv, pool_id=pool_config.pool_address)

    print(f"Loaded {len(events)} events for {args.pool}", file=sys.stderr)
    if not args.walkforward:
        rows = _run_grid(events, pool_config, grid)
        rows.sort(key=lambda row: row[2]["composite"], reverse=True)
        _write_csv(rows, args.output)
        return

    spec = WindowSpec(
        train_days=args.train_days,
        val_days=args.val_days,
        stride_days=args.stride_days,
        min_train_swaps=args.min_train_swaps,
        min_train_liquidity_events=args.min_train_liquidity_events,
        min_val_swaps=args.min_val_swaps,
    )
    window_results = evaluate_rolling_windows(
        events=events,
        pool_config=pool_config,
        grid=grid,
        spec=spec,
        top_n=args.top_n,
        max_windows=args.max_windows,
    )
    aggregate_results = aggregate_window_results(window_results)
    stem = Path(args.output)
    _write_window_results(window_results, str(stem.with_name(f"{stem.stem}_windows.csv")))
    _write_aggregate_results(aggregate_results, str(stem.with_name(f"{stem.stem}_aggregate.csv")))
    _write_summary_json(
        str(stem.with_name(f"{stem.stem}_summary.json")),
        args.dataset_format,
        args.pool,
        spec,
        events,
        window_results,
        args.top_n,
        args.max_windows,
        args.legacy_priors_csv,
    )


if __name__ == "__main__":
    main()
