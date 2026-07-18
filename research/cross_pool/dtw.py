"""Weekly band-constrained dynamic-time-warping diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Sequence

from research.cross_pool.contracts import (
    DTW_BAND_STEPS,
    DTW_DAY_MS,
    DTW_POINTS_PER_WEEK,
    DTW_PRIMARY_BAND_STEPS,
    DTW_WEEK_MS,
    CrossPoolContractError,
    Direction,
    DtwConfig,
    DtwNullResult,
    DtwPath,
    DtwStability,
    DtwWeekResult,
    PanelRow,
)


@dataclass(frozen=True)
class _StandardizedWeek:
    week_start_timestamp_ms: int
    source: tuple[float, ...]
    target: tuple[float, ...]


def banded_dtw(
    source: Sequence[float],
    target: Sequence[float],
    *,
    band_steps: int,
) -> DtwPath:
    source_values = _validated_sequence(source, label="source")
    target_values = _validated_sequence(target, label="target")
    if (
        isinstance(band_steps, bool)
        or not isinstance(band_steps, int)
        or band_steps < 0
    ):
        raise CrossPoolContractError("DTW band_steps must be a nonnegative integer")
    source_length = len(source_values)
    target_length = len(target_values)
    if abs((source_length - 1) - (target_length - 1)) > band_steps:
        raise CrossPoolContractError(
            "DTW fixed terminal indices are unreachable within the requested band"
        )

    costs: dict[tuple[int, int], float] = {}
    predecessors: dict[tuple[int, int], tuple[int, int]] = {}
    for source_index in range(source_length):
        target_start = max(0, source_index - band_steps)
        target_stop = min(target_length, source_index + band_steps + 1)
        for target_index in range(target_start, target_stop):
            local_cost = _squared_cost(
                source_values[source_index],
                target_values[target_index],
            )
            cell = (source_index, target_index)
            if cell == (0, 0):
                costs[cell] = local_cost
                continue

            candidate_cells = tuple(
                candidate
                for candidate in (
                    (source_index - 1, target_index - 1),
                    (source_index - 1, target_index),
                    (source_index, target_index - 1),
                )
                if candidate in costs
            )
            if not candidate_cells:
                continue
            predecessor = candidate_cells[0]
            predecessor_cost = costs[predecessor]
            for candidate in candidate_cells[1:]:
                candidate_cost = costs[candidate]
                if candidate_cost < predecessor_cost:
                    predecessor = candidate
                    predecessor_cost = candidate_cost
            accumulated_cost = predecessor_cost + local_cost
            if not math.isfinite(accumulated_cost):
                raise CrossPoolContractError("DTW accumulated cost must be finite")
            costs[cell] = accumulated_cost
            predecessors[cell] = predecessor

    terminal = (source_length - 1, target_length - 1)
    if terminal not in costs:
        raise CrossPoolContractError(
            "DTW fixed terminal indices are unreachable within the requested band"
        )
    reversed_matches = [terminal]
    current = terminal
    while current != (0, 0):
        if current not in predecessors:
            raise CrossPoolContractError("DTW path cannot reach the fixed origin")
        current = predecessors[current]
        reversed_matches.append(current)
    matches = tuple(reversed(reversed_matches))
    total_cost = costs[terminal]
    signed_lags = sorted(target_index - source_index for source_index, target_index in matches)
    midpoint = len(signed_lags) // 2
    median_lag = (
        float(signed_lags[midpoint])
        if len(signed_lags) % 2
        else (signed_lags[midpoint - 1] + signed_lags[midpoint]) / 2.0
    )
    return DtwPath(
        source_length=source_length,
        target_length=target_length,
        band_steps=band_steps,
        matches=matches,
        total_cost=total_cost,
        normalized_cost=total_cost / len(matches),
        median_signed_lag_steps=median_lag,
    )


def evaluate_weekly_dtw(
    panel_15m: Sequence[PanelRow],
    config: DtwConfig,
    *,
    direction: Direction,
) -> tuple[DtwWeekResult, ...]:
    weeks = _complete_standardized_weeks(panel_15m, config, direction=direction)
    results: list[DtwWeekResult] = []
    for week in weeks:
        for band_steps in config.band_steps:
            path = banded_dtw(
                week.source,
                week.target,
                band_steps=band_steps,
            )
            results.append(
                DtwWeekResult(
                    week_start_timestamp_ms=week.week_start_timestamp_ms,
                    direction=direction,
                    band_steps=band_steps,
                    path_length=len(path.matches),
                    total_cost=path.total_cost,
                    normalized_cost=path.normalized_cost,
                    median_signed_lag_steps=path.median_signed_lag_steps,
                    matches=path.matches,
                )
            )
    return tuple(results)


def build_day_rotation_nulls(
    panel_15m: Sequence[PanelRow],
    config: DtwConfig,
    *,
    direction: Direction,
) -> tuple[DtwNullResult, ...]:
    weeks = _complete_standardized_weeks(panel_15m, config, direction=direction)
    steps_per_day = DTW_DAY_MS // config.grid_ms
    results: list[DtwNullResult] = []
    for week in weeks:
        for band_steps in config.band_steps:
            observed = banded_dtw(
                week.source,
                week.target,
                band_steps=band_steps,
            )
            for rotation_days in range(1, 7):
                offset = rotation_days * steps_per_day
                rotated_target = week.target[offset:] + week.target[:offset]
                rotated = banded_dtw(
                    week.source,
                    rotated_target,
                    band_steps=band_steps,
                )
                results.append(
                    DtwNullResult(
                        week_start_timestamp_ms=week.week_start_timestamp_ms,
                        direction=direction,
                        band_steps=band_steps,
                        rotation_days=rotation_days,
                        path_length=len(rotated.matches),
                        total_cost=rotated.total_cost,
                        normalized_cost=rotated.normalized_cost,
                        observed_normalized_cost=observed.normalized_cost,
                        observed_cost_improvement=(
                            rotated.normalized_cost - observed.normalized_cost
                        ),
                        median_signed_lag_steps=(
                            rotated.median_signed_lag_steps
                        ),
                        observed_median_signed_lag_steps=(
                            observed.median_signed_lag_steps
                        ),
                        observed_signed_lag_difference_steps=(
                            rotated.median_signed_lag_steps
                            - observed.median_signed_lag_steps
                        ),
                    )
                )
    return tuple(results)


def assess_dtw_stability(
    rows: Sequence[DtwWeekResult],
    config: DtwConfig,
) -> DtwStability:
    materialized = tuple(rows)
    if not materialized:
        raise CrossPoolContractError(
            "DTW stability requires at least one weekly result"
        )
    direction = materialized[0].direction
    if any(row.direction != direction for row in materialized):
        raise CrossPoolContractError("DTW stability requires one direction")
    keys = tuple(
        (row.week_start_timestamp_ms, row.band_steps) for row in materialized
    )
    if len(set(keys)) != len(keys):
        raise CrossPoolContractError("DTW stability week-band keys must be unique")
    band_order = {band_steps: index for index, band_steps in enumerate(config.band_steps)}
    expected_order = tuple(
        sorted(keys, key=lambda key: (key[0], band_order[key[1]]))
    )
    if keys != expected_order:
        raise CrossPoolContractError(
            "DTW stability rows must use canonical ordering"
        )

    by_week: dict[int, dict[int, DtwWeekResult]] = {}
    for row in materialized:
        by_week.setdefault(row.week_start_timestamp_ms, {})[row.band_steps] = row
    week_starts = tuple(sorted(by_week))
    if any(
        tuple(by_week[week_start]) != DTW_BAND_STEPS
        for week_start in week_starts
    ):
        raise CrossPoolContractError(
            "DTW stability requires a complete frozen band matrix"
        )
    if any(
        current != previous + DTW_WEEK_MS
        for previous, current in zip(
            week_starts,
            week_starts[1:],
            strict=False,
        )
    ):
        raise CrossPoolContractError(
            "DTW stability requires consecutive complete weeks"
        )

    weekly_lags = {
        band_steps: tuple(
            (
                week_start,
                by_week[week_start][band_steps].median_signed_lag_steps,
            )
            for week_start in week_starts
        )
        for band_steps in config.band_steps
    }
    aggregate_lags = {
        band_steps: float(median(lag for _, lag in weekly_lags[band_steps]))
        for band_steps in config.band_steps
    }
    primary_aggregate = aggregate_lags[DTW_PRIMARY_BAND_STEPS]
    primary_sign = _sign(primary_aggregate)
    same_sign_count = sum(
        1
        for _, lag in weekly_lags[DTW_PRIMARY_BAND_STEPS]
        if _sign(lag) != 0 and _sign(lag) == primary_sign
    )
    same_sign_share = (
        0.0 if primary_sign == 0 else same_sign_count / len(week_starts)
    )
    aggregate_signs = {_sign(lag) for lag in aggregate_lags.values()}
    band_unstable = (
        primary_sign == 0
        or (-1 in aggregate_signs and 1 in aggregate_signs)
        or same_sign_count * 3 < 2 * len(week_starts)
    )
    return DtwStability(
        direction=direction,
        aggregate_median_lag_by_band=aggregate_lags,
        weekly_median_lags_by_band=weekly_lags,
        primary_band_same_sign_week_share=same_sign_share,
        band_unstable=band_unstable,
    )


def _complete_standardized_weeks(
    panel_15m: Sequence[PanelRow],
    config: DtwConfig,
    *,
    direction: Direction,
) -> tuple[_StandardizedWeek, ...]:
    rows = _validated_panel_rows(panel_15m, config, direction=direction)
    if not rows:
        return ()
    by_week: dict[int, list[PanelRow]] = {}
    for row in rows:
        by_week.setdefault(_week_start(row.timestamp_ms), []).append(row)

    complete: list[_StandardizedWeek] = []
    for week_start_timestamp_ms in sorted(by_week):
        week_rows = tuple(by_week[week_start_timestamp_ms])
        expected_timestamps = tuple(
            week_start_timestamp_ms + index * config.grid_ms
            for index in range(DTW_POINTS_PER_WEEK)
        )
        if tuple(row.timestamp_ms for row in week_rows) != expected_timestamps:
            continue
        source, target = _direction_innovations(week_rows, direction=direction)
        complete.append(
            _StandardizedWeek(
                week_start_timestamp_ms=week_start_timestamp_ms,
                source=_standardize_weekly(
                    source,
                    label="source",
                    week_start_timestamp_ms=week_start_timestamp_ms,
                ),
                target=_standardize_weekly(
                    target,
                    label="target",
                    week_start_timestamp_ms=week_start_timestamp_ms,
                ),
            )
        )
    return tuple(complete)


def _validated_panel_rows(
    panel_15m: Sequence[PanelRow],
    config: DtwConfig,
    *,
    direction: Direction,
) -> tuple[PanelRow, ...]:
    if direction not in ("bsc_to_base", "base_to_bsc"):
        raise CrossPoolContractError("unsupported DTW direction")
    rows = tuple(panel_15m)
    previous_timestamp_ms: int | None = None
    for row in rows:
        timestamp_ms = row.timestamp_ms
        if (
            isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms <= 0
        ):
            raise CrossPoolContractError("DTW panel timestamps must be positive integers")
        if row.horizon_ms != config.grid_ms:
            raise CrossPoolContractError("DTW panel requires a 15-minute horizon")
        if timestamp_ms % config.grid_ms != 0:
            raise CrossPoolContractError("DTW panel timestamps must be epoch-aligned")
        if previous_timestamp_ms is not None:
            if timestamp_ms <= previous_timestamp_ms:
                raise CrossPoolContractError(
                    "DTW panel timestamps must be strictly increasing"
                )
            if timestamp_ms != previous_timestamp_ms + config.grid_ms:
                raise CrossPoolContractError("DTW panel timestamps must be contiguous")
        if any(
            isinstance(value, bool) or not math.isfinite(value)
            for value in (
                row.base_trailing_return_bps,
                row.bsc_trailing_return_bps,
            )
        ):
            raise CrossPoolContractError(
                "DTW panel requires finite trailing innovations"
            )
        if not (
            0 < row.base_state_timestamp_ms <= timestamp_ms
            and 0 < row.bsc_state_timestamp_ms <= timestamp_ms
        ):
            raise CrossPoolContractError("DTW panel requires causal state timestamps")
        if (
            isinstance(row.base_age_ms, bool)
            or not isinstance(row.base_age_ms, int)
            or isinstance(row.bsc_age_ms, bool)
            or not isinstance(row.bsc_age_ms, int)
            or row.base_age_ms != timestamp_ms - row.base_state_timestamp_ms
            or row.bsc_age_ms != timestamp_ms - row.bsc_state_timestamp_ms
        ):
            raise CrossPoolContractError("DTW panel state ages must match timestamps")
        previous_timestamp_ms = timestamp_ms
    return rows


def _direction_innovations(
    rows: Sequence[PanelRow],
    *,
    direction: Direction,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if direction == "bsc_to_base":
        return (
            tuple(row.bsc_trailing_return_bps for row in rows),
            tuple(row.base_trailing_return_bps for row in rows),
        )
    return (
        tuple(row.base_trailing_return_bps for row in rows),
        tuple(row.bsc_trailing_return_bps for row in rows),
    )


def _standardize_weekly(
    values: Sequence[float],
    *,
    label: str,
    week_start_timestamp_ms: int,
) -> tuple[float, ...]:
    mean = _finite_fsum(values, label=label) / len(values)
    centered = tuple(value - mean for value in values)
    if any(not math.isfinite(value) for value in centered):
        raise CrossPoolContractError(
            f"DTW {label} centered innovations must be finite"
        )
    squared = tuple(value * value for value in centered)
    variance = _finite_fsum(squared, label=label) / len(values)
    scale = math.sqrt(variance)
    if not math.isfinite(scale) or scale == 0.0:
        raise CrossPoolContractError(
            f"DTW {label} week {week_start_timestamp_ms} has zero variance"
        )
    standardized = tuple(value / scale for value in centered)
    if any(not math.isfinite(value) for value in standardized):
        raise CrossPoolContractError(
            f"DTW {label} standardized innovations must be finite"
        )
    return standardized


def _finite_fsum(values: Sequence[float], *, label: str) -> float:
    try:
        result = math.fsum(values)
    except OverflowError as exc:
        raise CrossPoolContractError(
            f"DTW {label} weekly moments must be finite"
        ) from exc
    if not math.isfinite(result):
        raise CrossPoolContractError(
            f"DTW {label} weekly moments must be finite"
        )
    return result


def _week_start(timestamp_ms: int) -> int:
    day_index = timestamp_ms // DTW_DAY_MS
    weekday = (day_index + 3) % 7
    return (day_index - weekday) * DTW_DAY_MS


def _sign(value: float) -> int:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def _validated_sequence(
    values: Sequence[float],
    *,
    label: str,
) -> tuple[float, ...]:
    if not values:
        raise CrossPoolContractError(f"DTW {label} sequence must be nonempty")
    converted: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise CrossPoolContractError(f"DTW {label} values must be finite")
        try:
            converted_value = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CrossPoolContractError(
                f"DTW {label} values must be finite"
            ) from exc
        if not math.isfinite(converted_value):
            raise CrossPoolContractError(f"DTW {label} values must be finite")
        converted.append(converted_value)
    return tuple(converted)


def _squared_cost(source_value: float, target_value: float) -> float:
    difference = source_value - target_value
    local_cost = difference * difference
    if not math.isfinite(local_cost):
        raise CrossPoolContractError("DTW local cost must be finite")
    return local_cost
