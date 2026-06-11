"""H4 — Base Paper fine-grid around the apparent optimum.

Tests whether the existing Base Paper winner cluster (width=0.01–0.02, profit_take=0.01,
0-rebalance) extends to a finer grid between the existing breakpoints, and whether
extending width upward picks up more time-in-range without sacrificing fee/cost.

Mechanism check: is the width=0.02 / profit=0.01 plateau a real optimum, or just an
edge-of-grid artifact from the coarse paper_grid in params.py?
"""

from __future__ import annotations

import argparse
from itertools import product

from backtester.data import load_v4_events
from backtester.params import BacktestParams, TransactionCostModel
from backtester.run import (
    V4_POOLS,
    WindowSpec,
    aggregate_window_results,
    evaluate_rolling_windows,
    _write_aggregate_results,
    _write_window_results,
)


WIDTHS = [0.01, 0.0125, 0.015, 0.0175, 0.02, 0.025, 0.03, 0.04, 0.05]
CENTER_MODES = ("spot", "ewma")
PROFIT_TAKES = [0.003, 0.005, 0.0075, 0.01, 0.015, 0.02]

# Held fixed (effectively no-op at 0-rebalance Base Paper winners)
HARVEST = 0.04
STOP_LOSS = -0.01
OVERSHOOT = 0.05


def build_grid(initial_capital_usd: float, mint_gas: float, remove_gas: float) -> list[BacktestParams]:
    tc = TransactionCostModel(mint_gas_usd=mint_gas, remove_gas_usd=remove_gas)
    grid: list[BacktestParams] = []
    for width, center, profit in product(WIDTHS, CENTER_MODES, PROFIT_TAKES):
        grid.append(
            BacktestParams(
                strategy_mode="paper",
                range_mode="fixed_pct_width",
                center_mode=center,
                fixed_width_pct=width,
                harvest_upward_range_fraction=HARVEST,
                profit_take_return=profit,
                stop_loss_return=STOP_LOSS,
                out_of_range_overshoot_fraction=OVERSHOOT,
                cooldown_minutes=0.0,
                gas_cost_usd=mint_gas,
                initial_capital_usd=initial_capital_usd,
                min_tick_width=50,
                max_tick_width=5000,
                transaction_costs=tc,
            )
        )
    return grid


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="data/uni_base_pool_history.csv")
    p.add_argument("--pool", default="uni-base")
    p.add_argument("--initial-capital-usd", type=float, default=1200.0)
    p.add_argument("--mint-gas-usd", type=float, default=0.073)
    p.add_argument("--remove-gas-usd", type=float, default=0.022)
    p.add_argument("--train-swaps", type=int, default=200)
    p.add_argument("--val-swaps", type=int, default=50)
    p.add_argument("--stride-swaps", type=int, default=50)
    p.add_argument("--top-n", type=int, default=100)
    p.add_argument("--output-prefix", default="backtester/results/h4/uni_base_paper_fine_grid")
    args = p.parse_args()

    pool_config = V4_POOLS[args.pool]
    events = load_v4_events(args.csv, pool_config.pool_address)
    grid = build_grid(args.initial_capital_usd, args.mint_gas_usd, args.remove_gas_usd)
    print(f"Grid: {len(grid)} configs")

    spec = WindowSpec(
        mode="swap_count",
        train_swaps=args.train_swaps,
        val_swaps=args.val_swaps,
        stride_swaps=args.stride_swaps,
        min_train_swaps=args.train_swaps,
        min_train_liquidity_events=0,
        min_val_swaps=args.val_swaps,
    )
    results = evaluate_rolling_windows(events, pool_config, grid, spec, top_n=args.top_n)
    aggregate = aggregate_window_results(results)
    _write_window_results(results, f"{args.output_prefix}_windows.csv")
    _write_aggregate_results(aggregate, f"{args.output_prefix}_aggregate.csv")
    print(f"Wrote {args.output_prefix}_{{windows,aggregate}}.csv")


if __name__ == "__main__":
    main()
