"""CLI entry point for LP backtest grid search."""

import argparse
import csv
import sys
from pathlib import Path

from backtester.data import load_events, MintEvent, BurnEvent
from backtester.params import generate_grid, BacktestParams
from backtester.pool_state import PoolState
from backtester.simulator import (
    simulate_pool,
    SimResult,
    AERODROME_POOL,
    PANCAKESWAP_POOL,
    PoolConfig,
)
from backtester import metrics


POOLS = {
    "aerodrome": AERODROME_POOL,
    "pancakeswap": PANCAKESWAP_POOL,
}


def _run_grid(
    events, pool_config: PoolConfig, grid: list[BacktestParams],
    initial_pool_state: PoolState | None = None,
) -> list[tuple[BacktestParams, SimResult, dict]]:
    rows = []
    total = len(grid)
    for i, params in enumerate(grid, 1):
        if i % 100 == 0:
            print(f"  {i}/{total}", file=sys.stderr)
        sim = simulate_pool(events, params, pool_config, params.initial_capital_usd,
                            initial_pool_state=initial_pool_state)
        m = _compute_metrics(sim, params.initial_capital_usd)
        rows.append((params, sim, m))
    return rows


def _compute_metrics(sim: SimResult, initial_capital: float = 5000.0) -> dict:
    dr = sim.daily_returns
    mdd = metrics.max_drawdown(metrics._cumulative(dr)) if dr else 0.0
    tir = metrics.time_in_range_pct(sim)
    net_return = sim.final_value / initial_capital - 1.0 if initial_capital > 0 else 0.0
    return {
        "composite": metrics.composite_objective(net_return, mdd),
        "net_return": net_return,
        "sortino": metrics.sortino_ratio(dr),
        "calmar": metrics.calmar_ratio(dr),
        "omega": metrics.omega_ratio(dr),
        "max_drawdown": mdd,
        "time_in_range": tir,
        "rebalance_cost_ratio": metrics.rebalance_cost_ratio(sim),
        "win_rate": metrics.win_rate(dr),
        "profit_factor": metrics.profit_factor(dr),
        "cvar_5": metrics.cvar(dr, 0.05),
        "return_skew": metrics.return_skew(dr),
        "total_fees": sim.total_fees,
        "rebalance_count": sim.rebalance_count,
        "final_value": sim.final_value,
    }


def _walk_forward(
    events, pool_config: PoolConfig, grid: list[BacktestParams], top_n: int = 20
) -> list[tuple[BacktestParams, dict, dict]]:
    """Train on first 60% of events, validate top_n on remaining 40%."""
    split = int(len(events) * 0.6)
    train_events = events[:split]
    val_events = events[split:]

    print(f"Walk-forward: train={len(train_events)} events, val={len(val_events)} events",
          file=sys.stderr)

    # Build pool state from training events so validation starts with correct liquidity
    pool_state_at_split = PoolState()
    for ev in train_events:
        if isinstance(ev, MintEvent):
            pool_state_at_split.apply_mint(ev.tick_lower, ev.tick_upper, ev.liquidity_delta)
        elif isinstance(ev, BurnEvent):
            pool_state_at_split.apply_burn(ev.tick_lower, ev.tick_upper, ev.liquidity_delta)

    # Train
    print("Training...", file=sys.stderr)
    train_rows = _run_grid(train_events, pool_config, grid)
    train_rows.sort(key=lambda r: r[2]["composite"], reverse=True)

    # Validate top N (with pool state carried from training)
    top_params = [r[0] for r in train_rows[:top_n]]
    print(f"Validating top {top_n}...", file=sys.stderr)
    val_rows = _run_grid(val_events, pool_config, top_params,
                         initial_pool_state=pool_state_at_split)

    results = []
    for (p, _, train_m), (_, _, val_m) in zip(train_rows[:top_n], val_rows):
        results.append((p, train_m, val_m))
    return results


def _write_csv(rows, output_path: str) -> None:
    if not rows:
        return
    fieldnames = (
        ["sd_multiplier", "ewma_lambda", "downside_skew",
         "preemptive_rebalance", "rebalance_threshold_pct"]
        + list(rows[0][2].keys())
    )
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for params, sim, m in rows:
            row = {
                "sd_multiplier": params.sd_multiplier,
                "ewma_lambda": params.ewma_lambda,
                "downside_skew": params.downside_skew,
                "preemptive_rebalance": params.preemptive_rebalance,
                "rebalance_threshold_pct": params.rebalance_threshold_pct,
                **m,
            }
            writer.writerow(row)


def _write_walkforward_csv(results, output_path: str) -> None:
    if not results:
        return
    sample_m = results[0][1]
    metric_keys = list(sample_m.keys())
    fieldnames = (
        ["sd_multiplier", "ewma_lambda", "downside_skew",
         "preemptive_rebalance", "rebalance_threshold_pct"]
        + [f"train_{k}" for k in metric_keys]
        + [f"val_{k}" for k in metric_keys]
    )
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for params, train_m, val_m in results:
            row = {
                "sd_multiplier": params.sd_multiplier,
                "ewma_lambda": params.ewma_lambda,
                "downside_skew": params.downside_skew,
                "preemptive_rebalance": params.preemptive_rebalance,
                "rebalance_threshold_pct": params.rebalance_threshold_pct,
            }
            for k, v in train_m.items():
                row[f"train_{k}"] = v
            for k, v in val_m.items():
                row[f"val_{k}"] = v
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="LP backtest grid search")
    parser.add_argument("--csv", required=True, help="Path to historical data CSV")
    parser.add_argument(
        "--pool", choices=["aerodrome", "pancakeswap", "both"], default="both"
    )
    parser.add_argument("--output", default="backtest_results.csv")
    parser.add_argument("--walkforward", action="store_true", help="Run walk-forward validation")
    parser.add_argument("--top-n", type=int, default=20, help="Top N for walk-forward")
    args = parser.parse_args()

    pools_to_run = (
        list(POOLS.items()) if args.pool == "both"
        else [(args.pool, POOLS[args.pool])]
    )

    for pool_name, pool_config in pools_to_run:
        gas = 0.20 if pool_config.blockchain == "bnb" else 0.05
        grid = generate_grid(gas_cost_usd=gas)
        print(f"\n=== {pool_name} ({len(grid)} combos) ===", file=sys.stderr)

        events = load_events(args.csv, pool_address=pool_config.pool_address)
        print(f"Loaded {len(events)} events", file=sys.stderr)

        out = f"{pool_name}_{args.output}"

        if args.walkforward:
            results = _walk_forward(events, pool_config, grid, args.top_n)
            _write_walkforward_csv(results, f"wf_{out}")
            print(f"Walk-forward results → wf_{out}", file=sys.stderr)
        else:
            rows = _run_grid(events, pool_config, grid)
            rows.sort(key=lambda r: r[2]["composite"], reverse=True)
            _write_csv(rows, out)
            print(f"Results → {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
