"""H12 capital sweep: capacity curves for the frozen robust winner configs.

Constant-deployment sizing policies (FixedDeployment) swept over a dollar
grid against a fixed $5,000 bankroll, evaluated per validation window with
idle capital credited at the sGHO hurdle (report-only). Output is the
capacity-curve artifact consumed by research/scripts/h12_capacity_analysis.py — the
same schema future sizing policies, CEX, and arb accounts should emit.

No selection happens here, so PBO does not apply. The winner configs are
frozen from the top_n=100 walk-forward (research_log.md); revisit after the
extended-data re-run (task: re-run walk-forwards) and update WINNERS if the
robust set changes.

Usage: python research/scripts/h12_capital_sweep.py [--output research/results/h12/capacity_curve.csv]
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester import metrics
from research.backtester.data import load_v4_events
from research.backtester.params import BacktestParams, TransactionCostModel
from research.backtester.run import WindowSpec, _build_pool_state, _iter_window_slices, resolve_gas_costs
from research.backtester.simulator import UNISWAP_BASE_POOL, UNISWAP_BSC_POOL, simulate_pool
from research.backtester.sizing import FixedDeployment

BANKROLL_USD = 5000.0
IDLE_APR = 0.0425  # sGHO hurdle
CAPITALS = [100.0, 250.0, 500.0, 1000.0, 2000.0, 3500.0, 5000.0]
SHARE_VALIDITY_CEILING = 0.30  # diluted share above this is replay fiction

# Frozen robust winners (top_n=100 walk-forward, research_log.md).
WINNERS: dict[str, list[tuple[str, BacktestParams]]] = {
    "uni-base": [
        (
            "base_paper_w015_spot",
            BacktestParams(
                strategy_mode="paper", range_mode="fixed_pct_width", center_mode="spot",
                fixed_width_pct=0.015, harvest_upward_range_fraction=0.04,
                profit_take_return=0.005, stop_loss_return=-0.01,
                out_of_range_overshoot_fraction=0.05, max_tick_width=5000,
            ),
        ),
        (
            "base_ewma_sd25_pre",
            BacktestParams(
                strategy_mode="ewma", range_mode="volatility", center_mode="ewma",
                sd_multiplier=2.5, ewma_lambda=0.95, downside_skew=0.5,
                preemptive_rebalance=True, rebalance_threshold_pct=5.0,
            ),
        ),
    ],
    "uni-bsc": [
        (
            "bsc_paper_w005_ewma",
            BacktestParams(
                strategy_mode="paper", range_mode="fixed_pct_width", center_mode="ewma",
                fixed_width_pct=0.005, harvest_upward_range_fraction=0.04,
                profit_take_return=0.01, stop_loss_return=-0.0025,
                out_of_range_overshoot_fraction=0.0, max_tick_width=5000,
            ),
        ),
        (
            "bsc_ewma_sd20",
            BacktestParams(
                strategy_mode="ewma", range_mode="volatility", center_mode="ewma",
                sd_multiplier=2.0, ewma_lambda=0.975, downside_skew=0.6,
                preemptive_rebalance=False, rebalance_threshold_pct=15.0,
            ),
        ),
    ],
}

POOLS = {
    "uni-base": (
        UNISWAP_BASE_POOL,
        "research/data/uni_base_pool_history.csv",
        WindowSpec(mode="swap_count", train_swaps=200, val_swaps=50, stride_swaps=50,
                   min_train_swaps=200, min_train_liquidity_events=0, min_val_swaps=50),
    ),
    "uni-bsc": (
        UNISWAP_BSC_POOL,
        "research/data/uni_bsc_pool_history.csv",
        WindowSpec(mode="swap_count", train_swaps=300, val_swaps=75, stride_swaps=75,
                   min_train_swaps=300, min_train_liquidity_events=0, min_val_swaps=75),
    ),
}

FIELDS = [
    "pool", "config", "capital_usd", "bankroll_usd", "window_index",
    "window_start", "window_end", "val_swap_count", "duration_years",
    "pnl_usd", "idle_hurdle_credit_usd", "sgho_full_bankroll_usd",
    "net_return_bankroll", "net_return_deployed",
    "fees_usd", "tx_cost_usd", "gas_cost_usd", "price_impact_cost_usd",
    "rebalance_count", "episode_count", "time_in_range",
    "mean_share_diluted", "share_validity_ok", "win_score",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="H12 capital sweep")
    parser.add_argument("--output", default="research/results/h12/capacity_curve.csv")
    args = parser.parse_args()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for pool_name, (pool_config, csv_path, spec) in POOLS.items():
        events = load_v4_events(csv_path, pool_id=pool_config.pool_address)
        slices = [s for s in _iter_window_slices(events, spec) if s.skipped_reason is None]
        print(f"{pool_name}: {len(events)} events, {len(slices)} valid windows", file=sys.stderr)
        mint_gas, remove_gas = resolve_gas_costs(pool_name, None, None)
        costs = TransactionCostModel(mint_gas_usd=mint_gas, remove_gas_usd=remove_gas)
        for config_name, base_params in WINNERS[pool_name]:
            params = replace(base_params, transaction_costs=costs, initial_capital_usd=BANKROLL_USD)
            for capital in CAPITALS:
                for window_slice in slices:
                    window = window_slice.window
                    sim = simulate_pool(
                        window_slice.val_events,
                        params,
                        pool_config,
                        BANKROLL_USD,
                        initial_pool_state=_build_pool_state(window_slice.train_events),
                        sizing_policy=FixedDeployment(capital_usd=capital),
                        idle_apr=IDLE_APR,
                        settle_to_cash=False,
                    )
                    duration_years = (
                        (sim.end_time - sim.start_time).total_seconds() / (365.25 * 86400)
                        if sim.start_time and sim.end_time
                        else 0.0
                    )
                    pnl = sim.final_value - BANKROLL_USD
                    undiluted = metrics.average_active_liquidity_share(sim)
                    diluted = undiluted / (1.0 + undiluted) if undiluted > 0 else 0.0
                    rows.append(
                        {
                            "pool": pool_name,
                            "config": config_name,
                            "capital_usd": capital,
                            "bankroll_usd": BANKROLL_USD,
                            "window_index": window.index,
                            "window_start": window.val_start.isoformat(),
                            "window_end": window.val_end.isoformat(),
                            "val_swap_count": window_slice.val_counts.swap_count,
                            "duration_years": duration_years,
                            "pnl_usd": pnl,
                            "idle_hurdle_credit_usd": sim.idle_hurdle_credit,
                            "sgho_full_bankroll_usd": BANKROLL_USD * IDLE_APR * duration_years,
                            "net_return_bankroll": pnl / BANKROLL_USD,
                            "net_return_deployed": pnl / capital,
                            "fees_usd": sim.total_fees,
                            "tx_cost_usd": sim.total_transaction_cost,
                            "gas_cost_usd": sim.total_gas_cost,
                            "price_impact_cost_usd": sim.total_price_impact_cost,
                            "rebalance_count": sim.rebalance_count,
                            "episode_count": len(sim.episodes),
                            "time_in_range": metrics.time_in_range_pct(sim),
                            "mean_share_diluted": diluted,
                            "share_validity_ok": diluted <= SHARE_VALIDITY_CEILING,
                            "win_score": metrics.win_score(sim.value_samples, BANKROLL_USD),
                        }
                    )
            print(f"  {config_name}: {len(CAPITALS) * len(slices)} rows", file=sys.stderr)

    with open(args.output, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
