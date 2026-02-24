"""Parameter grid for backtest search."""

from dataclasses import dataclass, field
from itertools import product


@dataclass
class BacktestParams:
    sd_multiplier: float = 1.5
    ewma_lambda: float = 0.99
    downside_skew: float = 0.5
    preemptive_rebalance: bool = False
    rebalance_threshold_pct: float = 5.0
    # Fixed guardrails
    min_tick_width: int = 50
    max_tick_width: int = 1000
    initial_capital_usd: float = 1000.0
    gas_cost_usd: float = 0.05


# Grid axes
SD_MULTIPLIERS = [0.5 + 0.25 * i for i in range(11)]  # 0.5 – 3.0
EWMA_LAMBDAS = [0.95, 0.975, 0.99, 0.999]
DOWNSIDE_SKEWS = [0.5, 0.6, 0.7, 0.8]
PREEMPTIVE = [True, False]
REBALANCE_THRESHOLDS = [1.0, 3.0, 5.0, 10.0, 15.0]


def generate_grid(gas_cost_usd: float = 0.05) -> list[BacktestParams]:
    """11×4×4×2×5 = 1,760 parameter combinations."""
    combos = product(
        SD_MULTIPLIERS,
        EWMA_LAMBDAS,
        DOWNSIDE_SKEWS,
        PREEMPTIVE,
        REBALANCE_THRESHOLDS,
    )
    return [
        BacktestParams(
            sd_multiplier=sd,
            ewma_lambda=lam,
            downside_skew=ds,
            preemptive_rebalance=pre,
            rebalance_threshold_pct=rt,
            gas_cost_usd=gas_cost_usd,
        )
        for sd, lam, ds, pre, rt in combos
    ]
