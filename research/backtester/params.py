"""Parameter grid for backtest search."""

from dataclasses import dataclass, field
from itertools import product
from typing import Literal


@dataclass
class EntryFilters:
    min_swap_volume_usd: float | None = None
    min_active_liquidity: int | None = None
    min_expected_fee_apr: float | None = None
    max_realized_volatility: float | None = None
    max_fair_price_deviation: float | None = None
    trend_filter: Literal["flat_or_up"] | None = None


@dataclass
class TransactionCostModel:
    mint_gas_usd: float | None = None
    remove_gas_usd: float | None = None
    swap_slippage_bps: float = 0.0
    latency_slippage_bps: float = 0.0
    failed_tx_probability: float = 0.0
    failed_tx_gas_usd: float | None = None
    fallback_price_impact_bps: float = 200.0
    unwind_to_cash_on_exit: bool = False
    close_position_on_end: bool = False


@dataclass
class BacktestParams:
    strategy_mode: Literal["ewma", "paper", "static"] = "ewma"
    range_mode: Literal["volatility", "fixed_pct_width", "fixed_tick_width"] = "volatility"
    center_mode: Literal["spot", "fair_price", "ewma"] = "ewma"
    sd_multiplier: float = 1.5
    ewma_lambda: float = 0.99
    downside_skew: float = 0.5
    preemptive_rebalance: bool = False
    rebalance_threshold_pct: float = 5.0
    fixed_width_pct: float | None = None
    fixed_tick_width: int | None = None
    lower_width_pct: float | None = None
    upper_width_pct: float | None = None
    center_offset_pct: float = 0.0
    harvest_upward_range_fraction: float | None = None
    profit_take_return: float | None = None
    profit_take_pnl_mode: Literal["total", "fees"] = "total"
    require_profit_after_cost: bool = True
    stop_loss_return: float | None = None
    downward_range_fraction: float | None = None
    out_of_range_overshoot_fraction: float | None = None
    min_exit_swap_volume_usd: float | None = None
    exit_confirmation_swaps: int = 1
    exit_confirmation_minutes: float = 0.0
    exit_price_mode: Literal["spot", "fair_price", "qualified_pool_twap"] = "spot"
    exit_price_min_swap_volume_usd: float = 100.0
    exit_price_twap_lookback_minutes: float = 60.0
    cooldown_minutes: float = 0.0
    cooldown_blocks: int = 0
    max_active_liquidity_share: float | None = None
    entry_filters: EntryFilters = field(default_factory=EntryFilters)
    transaction_costs: TransactionCostModel = field(default_factory=TransactionCostModel)
    # Fixed guardrails
    min_tick_width: int = 50
    max_tick_width: int = 1000
    initial_capital_usd: float = 5000.0
    gas_cost_usd: float = 0.05


# Grid axes
SD_MULTIPLIERS = [0.5 + 0.25 * i for i in range(11)]  # 0.5 – 3.0
EWMA_LAMBDAS = [0.95, 0.975, 0.99, 0.999]
DOWNSIDE_SKEWS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
PREEMPTIVE = [True, False]
REBALANCE_THRESHOLDS = [1.0, 3.0, 5.0, 10.0, 15.0]
FIXED_WIDTH_PCTS = [0.0025, 0.005, 0.01, 0.015, 0.02, 0.05, 0.10]
HARVEST_RANGE_FRACTIONS = [0.04, 0.06, 0.08, 0.10, 0.12]
# H4: profit_take is inert across 0.003-0.02 at the robust operating points;
# two values retain the axis without quadrupling the grid.
PROFIT_TAKE_RETURNS = [0.005, 0.01]
STOP_LOSS_RETURNS = [-0.0025, -0.005, -0.01, -0.02]
OUT_OF_RANGE_OVERSHOOTS = [0.0, 0.02, 0.05, 0.10]


def generate_grid(gas_cost_usd: float = 0.05, initial_capital_usd: float = 5000.0) -> list[BacktestParams]:
    """11×4×6×2×5 = 2,640 parameter combinations."""
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
            initial_capital_usd=initial_capital_usd,
        )
        for sd, lam, ds, pre, rt in combos
    ]


def generate_paper_grid(gas_cost_usd: float = 0.05, initial_capital_usd: float = 5000.0) -> list[BacktestParams]:
    """Paper-style CLMM episode grid using normalized quote-price widths.

    ``fixed_width_pct`` is the full quote-price width around the selected
    center. For example, ``0.01`` creates a roughly +/-0.5% symmetric range
    before tick-spacing alignment and min/max width constraints.
    """
    combos = product(
        FIXED_WIDTH_PCTS,
        ("spot", "ewma"),
        HARVEST_RANGE_FRACTIONS,
        PROFIT_TAKE_RETURNS,
        STOP_LOSS_RETURNS,
        OUT_OF_RANGE_OVERSHOOTS,
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
            cooldown_minutes=0.0,
            gas_cost_usd=gas_cost_usd,
            initial_capital_usd=initial_capital_usd,
            min_tick_width=50,
            max_tick_width=5000,
        )
        for width, center_mode, harvest_fraction, profit_take, stop_loss, overshoot in combos
    ]
