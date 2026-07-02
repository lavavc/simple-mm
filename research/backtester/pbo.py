"""Probability of Backtest Overfitting via CSCV (Bailey, Borwein, López de Prado, Zhu).

Operates on a configs × windows performance matrix produced by
``backtester.run --matrix-output``: every grid config evaluated on every
disjoint validation window, with no top-n selection in between.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from itertools import combinations


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    combination_count: int
    logits: list[float]
    mean_logit: float
    median_logit: float
    mean_oos_rank_percentile: float
    oos_loss_probability: float
    degradation_slope: float
    degradation_intercept: float
    config_count: int
    window_count: int
    partitions: int


def contiguous_partitions(window_count: int, partitions: int) -> list[list[int]]:
    """Split window indexes 0..window_count-1 into ``partitions`` contiguous,
    near-equal blocks (earlier blocks absorb the remainder).

    Blocks stay contiguous so serial correlation between adjacent windows is
    not split across train and test more than necessary.
    """
    if partitions < 2 or partitions % 2 != 0:
        raise ValueError("partitions must be an even integer >= 2")
    if window_count < partitions:
        raise ValueError(f"need at least {partitions} windows, have {window_count}")
    base, remainder = divmod(window_count, partitions)
    blocks: list[list[int]] = []
    start = 0
    for i in range(partitions):
        size = base + (1 if i < remainder else 0)
        blocks.append(list(range(start, start + size)))
        start += size
    return blocks


def _mean_over(row: list[float], indexes: list[int]) -> float:
    return sum(row[i] for i in indexes) / len(indexes)


def compute_pbo(matrix: list[list[float]], partitions: int = 8) -> PBOResult:
    """CSCV PBO over ``matrix[config][window]`` performance values.

    For every balanced train/test split of the window blocks, the config with
    the best in-sample mean is selected and its out-of-sample relative rank
    omega = rank/(N+1) is turned into a logit. PBO is the fraction of splits
    whose logit is <= 0, i.e. the in-sample winner lands in the bottom half
    out of sample.
    """
    if not matrix:
        raise ValueError("matrix must contain at least one config row")
    window_count = len(matrix[0])
    if any(len(row) != window_count for row in matrix):
        raise ValueError("all config rows must cover the same windows")
    config_count = len(matrix)
    if config_count < 2:
        raise ValueError("PBO needs at least two configs to rank against")

    blocks = contiguous_partitions(window_count, partitions)
    logits: list[float] = []
    rank_percentiles: list[float] = []
    oos_losses = 0
    selected_pairs: list[tuple[float, float]] = []

    for train_blocks in combinations(range(partitions), partitions // 2):
        train_idx = sorted(i for b in train_blocks for i in blocks[b])
        test_idx = sorted(
            i for b in range(partitions) if b not in train_blocks for i in blocks[b]
        )
        is_perf = [_mean_over(row, train_idx) for row in matrix]
        oos_perf = [_mean_over(row, test_idx) for row in matrix]
        best = max(range(config_count), key=lambda c: is_perf[c])
        worse = sum(1 for p in oos_perf if p < oos_perf[best])
        ties = sum(1 for p in oos_perf if p == oos_perf[best]) - 1
        omega = (worse + 0.5 * ties + 1) / (config_count + 1)
        logit = math.log(omega / (1 - omega))
        logits.append(logit)
        rank_percentiles.append(omega)
        if oos_perf[best] < 0:
            oos_losses += 1
        selected_pairs.append((is_perf[best], oos_perf[best]))

    slope, intercept = _ols(selected_pairs)
    return PBOResult(
        pbo=sum(1 for lam in logits if lam <= 0) / len(logits),
        combination_count=len(logits),
        logits=logits,
        mean_logit=statistics.fmean(logits),
        median_logit=statistics.median(logits),
        mean_oos_rank_percentile=statistics.fmean(rank_percentiles),
        oos_loss_probability=oos_losses / len(logits),
        degradation_slope=slope,
        degradation_intercept=intercept,
        config_count=config_count,
        window_count=window_count,
        partitions=partitions,
    )


def _ols(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """Slope and intercept of OOS-on-IS performance of the selected configs.

    A strongly negative slope means harder in-sample optimization buys worse
    out-of-sample results (Bailey et al.'s performance degradation plot).
    """
    n = len(pairs)
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    var_x = sum((x - mean_x) ** 2 for x, _ in pairs)
    if var_x == 0:
        return 0.0, mean_y
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    slope = cov / var_x
    return slope, mean_y - slope * mean_x
