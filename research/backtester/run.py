"""CLI entry point for LP backtests and rolling walk-forward validation."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from research.backtester import metrics
from research.backtester.data import BurnEvent, Event, MintEvent, V4Event, load_events, load_v4_events
from research.backtester.params import BacktestParams, TransactionCostModel, generate_grid, generate_paper_grid
from research.backtester.pool_state import PoolState
from research.backtester.simulator import (
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
class GasDefaults:
    mint_gas_usd: float
    remove_gas_usd: float


# H7-calibrated medians from on-chain receipts of the pools' own liquidity
# events (research_log.md). CLI --mint-gas-usd / --remove-gas-usd override.
V4_POOL_GAS_DEFAULTS = {
    "uni-base": GasDefaults(mint_gas_usd=0.073, remove_gas_usd=0.022),
    "uni-bsc": GasDefaults(mint_gas_usd=0.015, remove_gas_usd=0.015),
}


def resolve_gas_costs(
    pool: str,
    mint_gas_arg: float | None,
    remove_gas_arg: float | None,
) -> tuple[float | None, float | None]:
    defaults = V4_POOL_GAS_DEFAULTS.get(pool)
    if defaults is None:
        return mint_gas_arg, remove_gas_arg
    return (
        defaults.mint_gas_usd if mint_gas_arg is None else mint_gas_arg,
        defaults.remove_gas_usd if remove_gas_arg is None else remove_gas_arg,
    )


@dataclass(frozen=True)
class WindowSpec:
    mode: str = "time"
    train_days: int = 30
    val_days: int = 7
    stride_days: int = 7
    train_swaps: int | None = None
    val_swaps: int | None = None
    stride_swaps: int | None = None
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
    train_start_index: int | None = None
    train_end_index: int | None = None
    val_start_index: int | None = None
    val_end_index: int | None = None


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


def _is_swap_event(event: Event) -> bool:
    return (isinstance(event, V4Event) and event.event_type == "swap") or event.__class__.__name__ == "SwapEvent"


ESSENTIAL_METRIC_FIELDS = [
    "composite",
    "net_return",
    "apy",
    "win_score",
    "max_drawdown",
    "time_in_range",
    "episode_count",
    "rebalance_count",
    "total_fees",
    "total_transaction_cost",
    "fee_to_transaction_cost_ratio",
    "total_price_impact_cost",
    "final_value",
    "divergent_loss",
]


def _compute_metrics(sim: SimResult, initial_capital: float) -> dict:
    daily_returns = sim.daily_returns
    max_drawdown = metrics.max_drawdown(metrics._cumulative(daily_returns)) if daily_returns else 0.0
    net_return = sim.final_value / initial_capital - 1.0 if initial_capital > 0 else 0.0
    return {
        "composite": metrics.composite_objective(
            net_return,
            max_drawdown,
            fees=sim.total_fees,
            tx_cost=sim.total_transaction_cost,
        ),
        "net_return": net_return,
        "apy": metrics.annualized_return(net_return, sim.start_time, sim.end_time),
        "win_score": metrics.win_score(sim.value_samples, initial_capital),
        "max_drawdown": max_drawdown,
        "time_in_range": metrics.time_in_range_pct(sim),
        "episode_count": len(sim.episodes),
        "rebalance_count": sim.rebalance_count,
        "total_fees": sim.total_fees,
        "total_transaction_cost": sim.total_transaction_cost,
        "fee_to_transaction_cost_ratio": metrics.fee_to_transaction_cost_ratio(sim),
        "total_price_impact_cost": sim.total_price_impact_cost,
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


def _with_transaction_costs(
    grid: list[BacktestParams],
    transaction_costs: TransactionCostModel,
) -> list[BacktestParams]:
    return [replace(params, transaction_costs=transaction_costs) for params in grid]


def _with_exit_controls(
    grid: list[BacktestParams],
    min_exit_swap_volume_usd: float | None = None,
    exit_confirmation_swaps: int | None = None,
    exit_confirmation_minutes: float | None = None,
    exit_price_mode: str | None = None,
    exit_price_min_swap_volume_usd: float | None = None,
    exit_price_twap_lookback_minutes: float | None = None,
) -> list[BacktestParams]:
    updates = {}
    if min_exit_swap_volume_usd is not None:
        updates["min_exit_swap_volume_usd"] = min_exit_swap_volume_usd
    if exit_confirmation_swaps is not None:
        updates["exit_confirmation_swaps"] = exit_confirmation_swaps
    if exit_confirmation_minutes is not None:
        updates["exit_confirmation_minutes"] = exit_confirmation_minutes
    if exit_price_mode is not None:
        updates["exit_price_mode"] = exit_price_mode
    if exit_price_min_swap_volume_usd is not None:
        updates["exit_price_min_swap_volume_usd"] = exit_price_min_swap_volume_usd
    if exit_price_twap_lookback_minutes is not None:
        updates["exit_price_twap_lookback_minutes"] = exit_price_twap_lookback_minutes
    if not updates:
        return grid
    return [replace(params, **updates) for params in grid]


def _window_counts(events: Iterable[Event]) -> WindowCounts:
    event_list = list(events)
    swap_count = sum(1 for event in event_list if _is_swap_event(event))
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


def generate_swap_count_windows(events: list[Event], spec: WindowSpec, max_windows: int | None = None) -> list[Window]:
    if not events:
        return []
    if spec.train_swaps is None or spec.val_swaps is None:
        raise ValueError("train_swaps and val_swaps are required for swap-count windows")
    stride_swaps = spec.stride_swaps or spec.val_swaps
    if spec.train_swaps <= 0 or spec.val_swaps <= 0 or stride_swaps <= 0:
        raise ValueError("swap-count window sizes must be positive")

    swap_event_indexes = [idx for idx, event in enumerate(events) if _is_swap_event(event)]
    windows: list[Window] = []
    cursor = 0
    index = 0
    required = spec.train_swaps + spec.val_swaps
    while cursor + required <= len(swap_event_indexes):
        train_start_index = swap_event_indexes[cursor]
        train_end_index = swap_event_indexes[cursor + spec.train_swaps]
        val_start_index = train_end_index
        last_val_swap_index = swap_event_indexes[cursor + required - 1]
        val_end_index = last_val_swap_index + 1
        windows.append(
            Window(
                index=index,
                train_start=_event_time(events[train_start_index]),
                train_end=_event_time(events[train_end_index]),
                val_start=_event_time(events[val_start_index]),
                val_end=_event_time(events[last_val_swap_index]),
                train_start_index=train_start_index,
                train_end_index=train_end_index,
                val_start_index=val_start_index,
                val_end_index=val_end_index,
            )
        )
        index += 1
        if max_windows is not None and len(windows) >= max_windows:
            break
        cursor += stride_swaps
    return windows


def _slice_events(events: list[Event], start: datetime, end: datetime) -> list[Event]:
    return [event for event in events if start <= _event_time(event) < end]


def _window_train_events(events: list[Event], window: Window) -> list[Event]:
    if window.train_start_index is not None and window.train_end_index is not None:
        return events[window.train_start_index:window.train_end_index]
    return _slice_events(events, window.train_start, window.train_end)


def _window_val_events(events: list[Event], window: Window) -> list[Event]:
    if window.val_start_index is not None and window.val_end_index is not None:
        return events[window.val_start_index:window.val_end_index]
    return _slice_events(events, window.val_start, window.val_end)


def _build_pool_state(events: list[Event]) -> PoolState:
    pool_state = PoolState()
    for event in events:
        if isinstance(event, MintEvent):
            pool_state.apply_mint(event.tick_lower, event.tick_upper, event.liquidity_delta)
        elif isinstance(event, BurnEvent):
            pool_state.apply_burn(event.tick_lower, event.tick_upper, event.liquidity_delta)
    return pool_state


@dataclass(frozen=True)
class WindowSlice:
    window: Window
    train_events: list[Event]
    val_events: list[Event]
    train_counts: WindowCounts
    val_counts: WindowCounts
    skipped_reason: str | None


def _iter_window_slices(
    events: list[Event],
    spec: WindowSpec,
    max_windows: int | None = None,
) -> list[WindowSlice]:
    if spec.mode == "swap_count":
        windows = generate_swap_count_windows(events, spec, max_windows=max_windows)
    else:
        windows = generate_windows(events, spec, max_windows=max_windows)
    slices: list[WindowSlice] = []
    for window in windows:
        train_events = _window_train_events(events, window)
        val_events = _window_val_events(events, window)
        train_counts = _window_counts(train_events)
        val_counts = _window_counts(val_events)
        skipped_reason = None
        if train_counts.swap_count < spec.min_train_swaps:
            skipped_reason = "train_swaps_below_min"
        elif train_counts.liquidity_event_count < spec.min_train_liquidity_events:
            skipped_reason = "train_liquidity_events_below_min"
        elif val_counts.swap_count < spec.min_val_swaps:
            skipped_reason = "val_swaps_below_min"
        slices.append(
            WindowSlice(window, train_events, val_events, train_counts, val_counts, skipped_reason)
        )
    return slices


def evaluate_rolling_windows(
    events: list[Event],
    pool_config: PoolConfig,
    grid: list[BacktestParams],
    spec: WindowSpec,
    top_n: int,
    max_windows: int | None = None,
) -> list[WindowResult]:
    results: list[WindowResult] = []
    for window_slice in _iter_window_slices(events, spec, max_windows=max_windows):
        window = window_slice.window
        train_events = window_slice.train_events
        val_events = window_slice.val_events
        train_counts = window_slice.train_counts
        val_counts = window_slice.val_counts
        skipped_reason = window_slice.skipped_reason

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


def evaluate_validation_matrix(
    events: list[Event],
    pool_config: PoolConfig,
    grid: list[BacktestParams],
    spec: WindowSpec,
    max_windows: int | None = None,
) -> list[dict]:
    """Every grid config evaluated on every valid validation window.

    No top-n selection: this is the unfiltered configs x windows performance
    matrix CSCV/PBO needs (research/scripts/compute_pbo.py). Pool state is seeded from
    the window's train slice, matching evaluate_rolling_windows.
    """
    rows: list[dict] = []
    for window_slice in _iter_window_slices(events, spec, max_windows=max_windows):
        if window_slice.skipped_reason is not None:
            continue
        window = window_slice.window
        print(f"matrix window {window.index}", file=sys.stderr)
        val_rows = _run_grid(
            window_slice.val_events,
            pool_config,
            grid,
            initial_pool_state=_build_pool_state(window_slice.train_events),
        )
        for params, _, metric_row in val_rows:
            rows.append(
                {
                    "window_index": window.index,
                    "window_start": window.val_start.isoformat(),
                    "window_end": window.val_end.isoformat(),
                    "val_swap_count": window_slice.val_counts.swap_count,
                    **_params_row(params),
                    **{f"validation_{key}": metric_row[key] for key in ESSENTIAL_METRIC_FIELDS},
                }
            )
    return rows


ESSENTIAL_PARAM_FIELDS = [
    "strategy_mode",
    "range_mode",
    "center_mode",
    "sd_multiplier",
    "ewma_lambda",
    "downside_skew",
    "preemptive_rebalance",
    "rebalance_threshold_pct",
    "fixed_width_pct",
    "fixed_tick_width",
    "harvest_upward_range_fraction",
    "profit_take_return",
    "stop_loss_return",
    "out_of_range_overshoot_fraction",
    "min_exit_swap_volume_usd",
    "exit_confirmation_swaps",
    "exit_price_mode",
    "exit_price_min_swap_volume_usd",
    "initial_capital_usd",
    "mint_gas_usd",
    "remove_gas_usd",
    "unwind_to_cash_on_exit",
]


def _full_params_row(params: BacktestParams) -> dict:
    return {
        "strategy_mode": params.strategy_mode,
        "range_mode": params.range_mode,
        "center_mode": params.center_mode,
        "sd_multiplier": params.sd_multiplier,
        "ewma_lambda": params.ewma_lambda,
        "downside_skew": params.downside_skew,
        "preemptive_rebalance": params.preemptive_rebalance,
        "rebalance_threshold_pct": params.rebalance_threshold_pct,
        "fixed_width_pct": params.fixed_width_pct,
        "fixed_tick_width": params.fixed_tick_width,
        "lower_width_pct": params.lower_width_pct,
        "upper_width_pct": params.upper_width_pct,
        "center_offset_pct": params.center_offset_pct,
        "harvest_upward_range_fraction": params.harvest_upward_range_fraction,
        "profit_take_return": params.profit_take_return,
        "profit_take_pnl_mode": params.profit_take_pnl_mode,
        "require_profit_after_cost": params.require_profit_after_cost,
        "stop_loss_return": params.stop_loss_return,
        "downward_range_fraction": params.downward_range_fraction,
        "out_of_range_overshoot_fraction": params.out_of_range_overshoot_fraction,
        "min_exit_swap_volume_usd": params.min_exit_swap_volume_usd,
        "exit_confirmation_swaps": params.exit_confirmation_swaps,
        "exit_confirmation_minutes": params.exit_confirmation_minutes,
        "exit_price_mode": params.exit_price_mode,
        "exit_price_min_swap_volume_usd": params.exit_price_min_swap_volume_usd,
        "exit_price_twap_lookback_minutes": params.exit_price_twap_lookback_minutes,
        "cooldown_minutes": params.cooldown_minutes,
        "cooldown_blocks": params.cooldown_blocks,
        "max_active_liquidity_share": params.max_active_liquidity_share,
        "min_swap_volume_usd": params.entry_filters.min_swap_volume_usd,
        "min_active_liquidity": params.entry_filters.min_active_liquidity,
        "min_expected_fee_apr": params.entry_filters.min_expected_fee_apr,
        "max_realized_volatility": params.entry_filters.max_realized_volatility,
        "max_fair_price_deviation": params.entry_filters.max_fair_price_deviation,
        "trend_filter": params.entry_filters.trend_filter,
        "min_tick_width": params.min_tick_width,
        "max_tick_width": params.max_tick_width,
        "initial_capital_usd": params.initial_capital_usd,
        "gas_cost_usd": params.gas_cost_usd,
        "mint_gas_usd": params.transaction_costs.mint_gas_usd,
        "remove_gas_usd": params.transaction_costs.remove_gas_usd,
        "swap_slippage_bps": params.transaction_costs.swap_slippage_bps,
        "latency_slippage_bps": params.transaction_costs.latency_slippage_bps,
        "failed_tx_probability": params.transaction_costs.failed_tx_probability,
        "failed_tx_gas_usd": params.transaction_costs.failed_tx_gas_usd,
        "fallback_price_impact_bps": params.transaction_costs.fallback_price_impact_bps,
        "unwind_to_cash_on_exit": params.transaction_costs.unwind_to_cash_on_exit,
    }


def _params_row(params: BacktestParams) -> dict:
    full = _full_params_row(params)
    return {field: full[field] for field in ESSENTIAL_PARAM_FIELDS}


def _params_key(params: BacktestParams) -> tuple:
    return tuple(sorted(_full_params_row(params).items()))


def aggregate_window_results(
    window_results: list[WindowResult],
    min_valid_windows: int = 1,
    max_drawdown_limit: float = 1.0,
    divergent_loss_floor: float = -1.0,
    min_fee_cost_ratio: float = 1.0,
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
        median_validation_composite = statistics.median(metric["composite"] for metric in validation_metrics)
        worst_window_max_drawdown = max(metric["max_drawdown"] for metric in validation_metrics)
        worst_window_divergent_loss = min(metric["divergent_loss"] for metric in validation_metrics)
        median_validation_net_return = statistics.median(metric["net_return"] for metric in validation_metrics)
        min_validation_net_return = min(metric["net_return"] for metric in validation_metrics)
        mean_metric = lambda name: statistics.fmean(metric.get(name, 0.0) for metric in validation_metrics)
        mean_fees = mean_metric("total_fees")
        mean_tx_cost = mean_metric("total_transaction_cost")
        fee_cost_ratio = (
            mean_fees / mean_tx_cost
            if mean_tx_cost > metrics._FEE_COST_EPS
            else (float("inf") if mean_fees > 0 else 0.0)
        )
        agg = {
            **_params_row(params),
            "valid_window_count": len(rows),
            "robust_validation_score": median_validation_composite - worst_window_max_drawdown + worst_window_divergent_loss,
            "mean_validation_net_return": statistics.fmean(metric["net_return"] for metric in validation_metrics),
            "median_validation_net_return": median_validation_net_return,
            "min_validation_net_return": min_validation_net_return,
            "worst_window_max_drawdown": worst_window_max_drawdown,
            "worst_window_divergent_loss": worst_window_divergent_loss,
            "mean_validation_apy": mean_metric("apy"),
            "median_validation_apy": statistics.median(metric.get("apy", 0.0) for metric in validation_metrics),
            "mean_validation_win_score": mean_metric("win_score"),
            "positive_window_rate": sum(1 for metric in validation_metrics if metric["net_return"] > 0) / len(rows),
            "validation_top_rank_rate": win_counter / len(rows),
            "windows_won": win_counter,
            "mean_validation_total_fees": mean_fees,
            "mean_validation_total_transaction_cost": mean_tx_cost,
            "mean_validation_fee_to_tx_cost_ratio": fee_cost_ratio,
            "mean_validation_rebalance_count": statistics.fmean(metric["rebalance_count"] for metric in validation_metrics),
            "eligible": (
                len(rows) >= min_valid_windows
                and median_validation_net_return > 0
                and fee_cost_ratio >= min_fee_cost_ratio
                and max(metric["max_drawdown"] for metric in validation_metrics) <= max_drawdown_limit
                and min(metric["divergent_loss"] for metric in validation_metrics) >= divergent_loss_floor
            ),
        }
        aggregate_rows.append(agg)

    aggregate_rows.sort(
        key=lambda row: (
            row["robust_validation_score"],
            row["median_validation_net_return"],
            row["mean_validation_net_return"],
            -row["worst_window_max_drawdown"],
            row["worst_window_divergent_loss"],
            row["positive_window_rate"],
            -row["mean_validation_rebalance_count"],
        ),
        reverse=True,
    )
    return aggregate_rows


def _write_csv(rows: list[tuple[BacktestParams, SimResult, dict]], output_path: str) -> None:
    if not rows:
        return
    fieldnames = [
        *_params_row(rows[0][0]).keys(),
        *rows[0][2].keys(),
    ]
    with open(output_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for params, _, metric_row in rows:
            writer.writerow(
                {
                    **_params_row(params),
                    **metric_row,
                }
            )


def _write_window_results(rows: list[WindowResult], output_path: str) -> None:
    param_fields = list(_params_row(rows[0].params).keys()) if rows and rows[0].params is not None else list(_params_row(BacktestParams()).keys())
    metric_fields = ESSENTIAL_METRIC_FIELDS
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
        *param_fields,
        "train_rank",
        "validation_rank",
        *(f"train_{field}" for field in metric_fields),
        *(f"validation_{field}" for field in metric_fields),
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
                base.update(_params_row(row.params))
            for prefix, metrics_row in (("train", row.train_metrics), ("validation", row.validation_metrics)):
                if metrics_row is None:
                    continue
                for key in metric_fields:
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
    parser.add_argument("--strategy-mode", choices=["ewma", "paper", "static"], default="ewma")
    parser.add_argument("--walkforward", action="store_true")
    parser.add_argument("--window-mode", choices=["time", "swap_count"], default="time")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--val-days", type=int, default=7)
    parser.add_argument("--stride-days", type=int, default=7)
    parser.add_argument("--train-swaps", type=int)
    parser.add_argument("--val-swaps", type=int)
    parser.add_argument("--stride-swaps", type=int)
    parser.add_argument("--min-train-swaps", type=int, default=500)
    parser.add_argument("--min-train-liquidity-events", type=int, default=50)
    parser.add_argument("--min-val-swaps", type=int, default=100)
    parser.add_argument("--max-windows", type=int)
    parser.add_argument(
        "--matrix-output",
        help="Walk-forward only: also evaluate the FULL grid on every validation window "
        "and write the configs x windows matrix CSV for research/scripts/compute_pbo.py",
    )
    parser.add_argument("--legacy-priors-csv")
    parser.add_argument("--mint-gas-usd", type=float)
    parser.add_argument("--remove-gas-usd", type=float)
    parser.add_argument("--swap-slippage-bps", type=float, default=0.0)
    parser.add_argument("--latency-slippage-bps", type=float, default=0.0)
    parser.add_argument("--failed-tx-probability", type=float, default=0.0)
    parser.add_argument("--failed-tx-gas-usd", type=float)
    parser.add_argument("--fallback-price-impact-bps", type=float, default=200.0)
    parser.add_argument("--unwind-to-cash-on-exit", dest="unwind_to_cash_on_exit", action="store_true")
    parser.add_argument("--no-unwind-to-cash-on-exit", dest="unwind_to_cash_on_exit", action="store_false")
    parser.add_argument("--initial-capital-usd", type=float)
    parser.add_argument("--min-exit-swap-volume-usd", type=float)
    parser.add_argument("--exit-confirmation-swaps", type=int)
    parser.add_argument("--exit-confirmation-minutes", type=float)
    parser.add_argument("--exit-price-mode", choices=["spot", "fair_price", "qualified_pool_twap"])
    parser.add_argument("--exit-price-min-swap-volume-usd", type=float)
    parser.add_argument("--exit-price-twap-lookback-minutes", type=float)
    parser.set_defaults(unwind_to_cash_on_exit=False)
    args = parser.parse_args()

    pools = LEGACY_POOLS if args.dataset_format == "legacy" else V4_POOLS
    if args.pool not in pools:
        parser.error(f"--pool must be one of {', '.join(sorted(pools))} for {args.dataset_format} mode")
    pool_config = pools[args.pool]

    gas_cost = 0.20 if pool_config.blockchain in {"bnb", "bsc"} else 0.05
    default_capital = 200.0 if args.pool in {"pancakeswap", "uni-bsc"} else 500.0
    initial_capital = args.initial_capital_usd if args.initial_capital_usd is not None else default_capital
    if args.strategy_mode == "paper":
        grid = generate_paper_grid(gas_cost_usd=gas_cost, initial_capital_usd=initial_capital)
    elif args.strategy_mode == "static":
        grid = [
            replace(
                params,
                strategy_mode="static",
                harvest_upward_range_fraction=None,
                profit_take_return=None,
                stop_loss_return=None,
                out_of_range_overshoot_fraction=None,
            )
            for params in generate_paper_grid(gas_cost_usd=gas_cost, initial_capital_usd=initial_capital)
        ]
    else:
        grid = generate_grid(gas_cost_usd=gas_cost, initial_capital_usd=initial_capital)
    mint_gas_usd, remove_gas_usd = resolve_gas_costs(args.pool, args.mint_gas_usd, args.remove_gas_usd)
    grid = _with_transaction_costs(
        grid,
        TransactionCostModel(
            mint_gas_usd=mint_gas_usd,
            remove_gas_usd=remove_gas_usd,
            swap_slippage_bps=args.swap_slippage_bps,
            latency_slippage_bps=args.latency_slippage_bps,
            failed_tx_probability=args.failed_tx_probability,
            failed_tx_gas_usd=args.failed_tx_gas_usd,
            fallback_price_impact_bps=args.fallback_price_impact_bps,
            unwind_to_cash_on_exit=args.unwind_to_cash_on_exit,
        ),
    )
    grid = _with_exit_controls(
        grid,
        min_exit_swap_volume_usd=args.min_exit_swap_volume_usd,
        exit_confirmation_swaps=args.exit_confirmation_swaps,
        exit_confirmation_minutes=args.exit_confirmation_minutes,
        exit_price_mode=args.exit_price_mode,
        exit_price_min_swap_volume_usd=args.exit_price_min_swap_volume_usd,
        exit_price_twap_lookback_minutes=args.exit_price_twap_lookback_minutes,
    )

    if args.dataset_format == "legacy":
        events = load_events(args.csv, pool_address=pool_config.pool_address)
    else:
        events = load_v4_events(args.csv, pool_id=pool_config.pool_address)

    print(f"Loaded {len(events)} events for {args.pool}", file=sys.stderr)
    if args.matrix_output and not args.walkforward:
        parser.error("--matrix-output requires --walkforward")
    if not args.walkforward:
        rows = _run_grid(events, pool_config, grid)
        rows.sort(key=lambda row: row[2]["composite"], reverse=True)
        _write_csv(rows, args.output)
        return

    spec = WindowSpec(
        mode=args.window_mode,
        train_days=args.train_days,
        val_days=args.val_days,
        stride_days=args.stride_days,
        train_swaps=args.train_swaps,
        val_swaps=args.val_swaps,
        stride_swaps=args.stride_swaps,
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
    if args.matrix_output:
        matrix_rows = evaluate_validation_matrix(
            events=events,
            pool_config=pool_config,
            grid=grid,
            spec=spec,
            max_windows=args.max_windows,
        )
        _write_aggregate_results(matrix_rows, args.matrix_output)
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
