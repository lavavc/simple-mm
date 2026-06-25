"""Performance metrics for backtest results."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from research.backtester.simulator import SimResult


_SECONDS_PER_YEAR = 365.25 * 86400


def annualized_return(net_return: float, start: datetime | None, end: datetime | None) -> float:
    """Compounded APY of ``net_return`` realized over [start, end].

    Returns 0.0 when the span is empty or unknown — a config that never saw
    two distinct event times has no annualizable record. Short windows
    compound aggressively; this is a reporting metric, not a ranking input.
    """
    if start is None or end is None:
        return 0.0
    elapsed_seconds = (end - start).total_seconds()
    if elapsed_seconds <= 0:
        return 0.0
    if net_return <= -1.0:
        return -1.0
    exponent = _SECONDS_PER_YEAR / elapsed_seconds * math.log1p(net_return)
    # Spans far shorter than a year compound past float range; +/-inf marks
    # the window as too short to annualize rather than hiding it.
    if exponent > 700.0:
        return float("inf")
    return math.expm1(exponent)


def win_score(value_samples: list[tuple[datetime, float]], initial_capital: float) -> float:
    """Win-score ω of Urusov et al. (2026), Appendix A, over a mark-to-market
    equity path instead of realized position closes.

    Normalizes the positive and negative parts of the cumulative PnL path by
    the larger extreme, integrates both over time (trapezoid), and returns
    A+ / (A+ + A-). 0.5 is neutral (flat path or degenerate input); above 0.5
    the strategy spent more of the period in cumulative profit.
    """
    if initial_capital <= 0 or len(value_samples) < 2:
        return 0.5
    times = [sample[0].timestamp() for sample in value_samples]
    pnl = [sample[1] - initial_capital for sample in value_samples]
    scale = max(max(pnl), -min(pnl), 0.0)
    if scale == 0.0 or times[-1] <= times[0]:
        return 0.5
    area_pos = 0.0
    area_neg = 0.0
    for i in range(1, len(times)):
        dt = times[i] - times[i - 1]
        if dt <= 0:
            continue
        area_pos += (max(pnl[i - 1], 0.0) + max(pnl[i], 0.0)) / 2 * dt
        area_neg += (-min(pnl[i - 1], 0.0) - min(pnl[i], 0.0)) / 2 * dt
    total = area_pos + area_neg
    if total == 0.0:
        return 0.5
    return area_pos / total


def sortino_ratio(returns: list[float], benchmark: float = 0.0) -> float:
    """Annualised Sortino ratio (√365)."""
    excess = [r - benchmark for r in returns]
    if not excess:
        return 0.0
    mean_excess = sum(excess) / len(excess)
    downside = [min(r, 0.0) ** 2 for r in excess]
    dd = math.sqrt(sum(downside) / len(downside)) if downside else 0.0
    if dd == 0.0:
        return float("inf") if mean_excess > 0 else 0.0
    return (mean_excess / dd) * math.sqrt(365)


def max_drawdown(cumulative: list[float]) -> float:
    """Peak-to-trough drawdown (as a positive fraction, e.g. 0.15 = 15%)."""
    if not cumulative:
        return 0.0
    peak = cumulative[0]
    dd = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        draw = (peak - v) / peak if peak > 0 else 0.0
        if draw > dd:
            dd = draw
    return dd


def calmar_ratio(returns: list[float]) -> float:
    """Annualised return / max drawdown."""
    if not returns:
        return 0.0
    cum = _cumulative(returns)
    mdd = max_drawdown(cum)
    ann = (sum(returns) / len(returns)) * 365
    if mdd == 0.0:
        return float("inf") if ann > 0 else 0.0
    return ann / mdd


def omega_ratio(returns: list[float], threshold: float = 0.0) -> float:
    """Sum(gains above threshold) / sum(losses below threshold)."""
    gains = sum(r - threshold for r in returns if r > threshold)
    losses = sum(threshold - r for r in returns if r < threshold)
    if losses == 0.0:
        return float("inf") if gains > 0 else 1.0
    return gains / losses


def win_rate(returns: list[float]) -> float:
    if not returns:
        return 0.0
    return sum(1 for r in returns if r > 0) / len(returns)


def profit_factor(returns: list[float]) -> float:
    gross_profit = sum(r for r in returns if r > 0)
    gross_loss = abs(sum(r for r in returns if r < 0))
    if gross_loss == 0.0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def cvar(returns: list[float], alpha: float = 0.05) -> float:
    """Conditional Value at Risk (expected loss in worst α%)."""
    if not returns:
        return 0.0
    sorted_r = sorted(returns)
    n = max(1, int(len(sorted_r) * alpha))
    return abs(sum(sorted_r[:n]) / n)


def return_skew(returns: list[float]) -> float:
    if len(returns) < 3:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    m3 = sum((r - mean) ** 3 for r in returns) / n
    if m2 == 0:
        return 0.0
    return m3 / (m2 ** 1.5)


def time_in_range_pct(sim: SimResult) -> float:
    total = sim.total_swaps
    if total == 0:
        return 0.0
    return sim.in_range_swaps / total


def rebalance_cost_ratio(sim: SimResult) -> float:
    if sim.total_fees == 0.0:
        return float("inf") if sim.total_rebalance_cost > 0 else 0.0
    return sim.total_rebalance_cost / sim.total_fees


def fee_to_cost_ratio(sim: SimResult) -> float:
    if sim.total_rebalance_cost == 0.0:
        return float("inf") if sim.total_fees > 0 else 0.0
    return sim.total_fees / sim.total_rebalance_cost


def transaction_cost_ratio(sim: SimResult) -> float:
    if sim.total_fees == 0.0:
        return float("inf") if sim.total_transaction_cost > 0 else 0.0
    return sim.total_transaction_cost / sim.total_fees


def fee_to_transaction_cost_ratio(sim: SimResult) -> float:
    if sim.total_transaction_cost == 0.0:
        return float("inf") if sim.total_fees > 0 else 0.0
    return sim.total_fees / sim.total_transaction_cost


def harvest_win_rate(sim: SimResult) -> float:
    harvests = [episode for episode in sim.episodes if episode.exit_reason == "harvest_upward"]
    if not harvests:
        return 0.0
    return sum(1 for episode in harvests if episode.net_pnl > 0) / len(harvests)


def average_profitable_traversal(sim: SimResult) -> float:
    traversals = [
        episode.range_traversal_fraction
        for episode in sim.episodes
        if episode.net_pnl > 0 and episode.exit_reason != "end_of_data"
    ]
    if not traversals:
        return 0.0
    return sum(traversals) / len(traversals)


def median_episode_duration_seconds(sim: SimResult) -> float:
    durations = sorted(episode.duration_seconds for episode in sim.episodes)
    if not durations:
        return 0.0
    mid = len(durations) // 2
    if len(durations) % 2:
        return durations[mid]
    return (durations[mid - 1] + durations[mid]) / 2


def churn_rate_per_day(sim: SimResult) -> float:
    if sim.start_time is None or sim.end_time is None:
        return 0.0
    elapsed_days = max((sim.end_time - sim.start_time).total_seconds() / 86400, 1 / 86400)
    return sim.rebalance_count / elapsed_days


def average_active_liquidity_share(sim: SimResult) -> float:
    shares = [episode.active_liquidity_share for episode in sim.episodes if episode.active_liquidity_share >= 0]
    if not shares:
        return 0.0
    return sum(shares) / len(shares)


_FEE_COST_LOG_RATIO_FLOOR = -3.0
_FEE_COST_LOG_RATIO_CEIL = 2.0
_FEE_COST_EPS = 1e-9


def fee_cost_log_ratio(fees: float, tx_cost: float) -> float:
    """Signed, clamped log ratio of fees to transaction cost.

    Returns 0 when no transaction cost has been incurred (zero-activity is
    treated as neutral). Otherwise returns ln(max(fees, eps) / tx_cost),
    clamped to [-3, +2] so a single underwater config can't dominate ranking
    and so fee-heavy runs don't get unbounded credit.
    """
    if tx_cost <= _FEE_COST_EPS:
        return 0.0
    raw = math.log(max(fees, _FEE_COST_EPS) / tx_cost)
    if raw < _FEE_COST_LOG_RATIO_FLOOR:
        return _FEE_COST_LOG_RATIO_FLOOR
    if raw > _FEE_COST_LOG_RATIO_CEIL:
        return _FEE_COST_LOG_RATIO_CEIL
    return raw


def composite_objective(
    net_return: float,
    max_dd: float,
    fees: float = 0.0,
    tx_cost: float = 0.0,
    fee_cost_alpha: float = 0.001,
) -> float:
    """Risk- and structure-aware ranking score.

    Combines:
    - net_return - max_drawdown (existing risk-adjusted return)
    - + fee_cost_alpha * fee_cost_log_ratio(fees, tx_cost)
      Rewards configs whose fees structurally exceed their transaction cost;
      penalises configs underwater on fees-vs-gas. Zero-activity is neutral
      so the prior two-arg behaviour is preserved when fees and tx_cost are
      both zero.

    Default alpha=0.001 lets the log term contribute up to ±0.003 — comparable
    to a 30 bps return delta but unable to dominate when the ROI signal is
    strong.
    """
    return (net_return - max_dd) + fee_cost_alpha * fee_cost_log_ratio(fees, tx_cost)


def _cumulative(returns: list[float]) -> list[float]:
    out = []
    v = 1.0
    for r in returns:
        v *= 1.0 + r
        out.append(v)
    return out
