"""Performance metrics for backtest results."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backtester.simulator import SimResult


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


def composite_objective(sortino_val: float, max_dd: float, tir: float) -> float:
    """sortino × (1 - max_dd) × time_in_range."""
    return sortino_val * (1.0 - max_dd) * tir


def _cumulative(returns: list[float]) -> list[float]:
    out = []
    v = 1.0
    for r in returns:
        v *= 1.0 + r
        out.append(v)
    return out
