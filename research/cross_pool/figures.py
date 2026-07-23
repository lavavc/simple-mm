"""Deterministic statistical figures for the cross-pool evidence package."""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, cast

import matplotlib

matplotlib.use("Agg", force=True)

from matplotlib.axes import Axes  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from research.cross_pool.contracts import (  # noqa: E402
    CausalPanel,
    CrossPoolContractError,
    Direction,
    DtwStability,
    EventSummary,
    PredictiveInference,
)

_HOUR_MS = 3_600_000
_HORIZONS_MS = (900_000, _HOUR_MS, 14_400_000)
_DIRECTIONS: tuple[Direction, ...] = ("bsc_to_base", "base_to_bsc")
_DIRECTION_LABELS = {
    "bsc_to_base": "BSC → Base",
    "base_to_bsc": "Base → BSC",
}
_HORIZON_LABELS = {900_000: "15m", _HOUR_MS: "1h", 14_400_000: "4h"}
_DIRECTION_COLORS = {"bsc_to_base": "#16697A", "base_to_bsc": "#DB6400"}
_BAND_STYLES = {1: ":", 4: "-", 16: "--"}
_PNG_METADATA = {"Software": "automated-infra cross-pool research"}

FIGURE_RCPARAMS: dict[str, object] = {
    "axes.edgecolor": "#222222",
    "axes.grid": True,
    "axes.labelsize": 9.0,
    "axes.titlesize": 11.0,
    "figure.facecolor": "white",
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "font.size": 9.0,
    "grid.alpha": 0.25,
    "grid.linewidth": 0.6,
    "legend.fontsize": 8.0,
    "lines.linewidth": 1.5,
    "patch.edgecolor": "white",
    "savefig.facecolor": "white",
    "timezone": "UTC",
}
_FROZEN_RCPARAMS = matplotlib.rcParamsDefault.copy()
_FROZEN_RCPARAMS.update(cast(Any, FIGURE_RCPARAMS))


def price_gap_png(panel_1h: CausalPanel) -> bytes:
    """Plot the causal one-hour Base-minus-BSC marginal-price gap."""
    if (
        not isinstance(panel_1h, CausalPanel)
        or panel_1h.horizon_ms != _HOUR_MS
        or not panel_1h.rows
        or any(row.horizon_ms != _HOUR_MS for row in panel_1h.rows)
    ):
        raise CrossPoolContractError("price-gap figure requires a nonempty one-hour panel")
    decision_timestamps = tuple(row.timestamp_ms for row in panel_1h.rows)
    if decision_timestamps != tuple(sorted(set(decision_timestamps))) or any(
        current != previous + _HOUR_MS
        for previous, current in zip(
            decision_timestamps,
            decision_timestamps[1:],
            strict=False,
        )
    ):
        raise CrossPoolContractError(
            "price-gap figure requires a regular increasing one-hour clock"
        )
    if any(not math.isfinite(row.base_minus_bsc_gap_bps) for row in panel_1h.rows):
        raise CrossPoolContractError("price-gap figure requires finite log gaps")

    with matplotlib.rc_context(rc=_FROZEN_RCPARAMS):
        figure, axis = _new_figure()
        timestamps = tuple(_utc_datetime(row.timestamp_ms) for row in panel_1h.rows)
        gaps = tuple(row.base_minus_bsc_gap_bps for row in panel_1h.rows)
        axis.plot(
            timestamps,  # type: ignore[arg-type]
            gaps,
            color="#3C6E71",
            label="10,000 × ln(Base marginal mid / BSC marginal mid)",
        )
        axis.axhline(0.0, color="#222222", linewidth=0.8)
        axis.set_title("UTC as-of marginal-price log gap")
        axis.set_xlabel("UTC decision time")
        axis.set_ylabel("Gap (bps)")
        _format_utc_axis(axis)
        axis.legend(loc="best")
        return _render_png(figure)


def predictive_performance_png(inferences: Sequence[PredictiveInference]) -> bytes:
    """Compare baseline and cross-pool MAE across directions and horizons."""
    indexed = _index_predictive_inferences(inferences)
    with matplotlib.rc_context(rc=_FROZEN_RCPARAMS):
        figure, axis = _new_figure()
        keys = tuple(
            (direction, horizon_ms)
            for horizon_ms in _HORIZONS_MS
            for direction in _DIRECTIONS
        )
        x_positions = tuple(float(index) for index in range(len(keys)))
        baseline = tuple(indexed[key].metrics.baseline_mae_bps for key in keys)
        cross = tuple(indexed[key].metrics.cross_mae_bps for key in keys)
        width = 0.36
        axis.bar(
            tuple(position - width / 2 for position in x_positions),
            baseline,
            width=width,
            color="#9AA0A6",
            label="Target-pool-only OLS",
        )
        axis.bar(
            tuple(position + width / 2 for position in x_positions),
            cross,
            width=width,
            color="#16697A",
            label="Cross-pool-augmented OLS",
        )
        axis.set_xticks(
            x_positions,
            tuple(
                f"{_HORIZON_LABELS[horizon]}\n{_DIRECTION_LABELS[direction]}"
                for direction, horizon in keys
            ),
        )
        axis.set_title("Out-of-sample predictive error (diagnostic point estimates)")
        axis.set_ylabel("Mean absolute error (bps)")
        axis.legend(loc="best")
        figure.text(
            0.5,
            0.01,
            "Paired bootstrap uncertainty is reported in predictive_metrics.json.",
            ha="center",
            fontsize=7.5,
        )
        return _render_png(figure)


def event_response_png(summaries: Sequence[EventSummary]) -> bytes:
    """Plot mean target responses and their frozen block-bootstrap intervals."""
    indexed = _index_event_summaries(summaries)
    with matplotlib.rc_context(rc=_FROZEN_RCPARAMS):
        figure, axis = _new_figure()
        keys = tuple(
            (direction, horizon_ms)
            for horizon_ms in _HORIZONS_MS
            for direction in _DIRECTIONS
        )
        for direction_index, direction in enumerate(_DIRECTIONS):
            direction_keys = tuple(key for key in keys if key[0] == direction)
            x_positions = tuple(
                float(keys.index(key)) + (direction_index - 0.5) * 0.06
                for key in direction_keys
            )
            intervals = tuple(indexed[key].mean_response_bps for key in direction_keys)
            points = tuple(interval.point for interval in intervals)
            lower_errors = tuple(
                interval.point - interval.lower for interval in intervals
            )
            upper_errors = tuple(
                interval.upper - interval.point for interval in intervals
            )
            axis.errorbar(
                x_positions,
                points,
                yerr=(lower_errors, upper_errors),
                fmt="o",
                capsize=3,
                color=_DIRECTION_COLORS[direction],
                label=_DIRECTION_LABELS[direction],
            )
            for x_position, interval, summary in zip(
                x_positions,
                intervals,
                (indexed[key] for key in direction_keys),
                strict=True,
            ):
                axis.annotate(
                    (
                        f"agree {summary.direction_agreement.point:.0%}\n"
                        f"n={summary.event_count}, days={summary.event_day_count}"
                    ),
                    (x_position, interval.point),
                    xytext=(0, 8),
                    textcoords="offset points",
                    ha="center",
                    fontsize=6.5,
                )
        axis.axhline(0.0, color="#222222", linewidth=0.8)
        axis.set_xticks(
            tuple(float(index) for index in range(len(keys))),
            tuple(
                f"{_HORIZON_LABELS[horizon]}\n{_DIRECTION_LABELS[direction]}"
                for direction, horizon in keys
            ),
        )
        axis.set_title("Post-shock raw signed target response")
        axis.set_ylabel("Mean response (bps, 95% interval)")
        axis.legend(loc="best")
        figure.text(
            0.5,
            0.01,
            "Positive and negative source shocks are pooled. Descriptive UTC-day block "
            "bootstrap; no multiplicity adjustment.",
            ha="center",
            fontsize=7.2,
        )
        return _render_png(figure)


def dtw_lag_png(stabilities: Sequence[DtwStability]) -> bytes:
    """Plot weekly signed DTW lag across frozen bands and both directions."""
    materialized = tuple(stabilities)
    if (
        len(materialized) != len(_DIRECTIONS)
        or {row.direction for row in materialized} != set(_DIRECTIONS)
    ):
        raise CrossPoolContractError(
            "DTW lag figure requires one stability result per direction"
        )
    indexed = {row.direction: row for row in materialized}
    with matplotlib.rc_context(rc=_FROZEN_RCPARAMS):
        figure, axis = _new_figure()
        for direction in _DIRECTIONS:
            stability = indexed[direction]
            for band_steps, weekly_lags in stability.weekly_median_lags_by_band.items():
                axis.plot(
                    tuple(  # type: ignore[arg-type]
                        _utc_datetime(week_start) for week_start, _ in weekly_lags
                    ),
                    tuple(lag_steps * 15.0 for _, lag_steps in weekly_lags),
                    color=_DIRECTION_COLORS[direction],
                    linestyle=_BAND_STYLES[band_steps],
                    marker="o",
                    markersize=3,
                    label=f"{_DIRECTION_LABELS[direction]}, ±{band_steps * 15}m band",
                )
        axis.axhline(0.0, color="#222222", linewidth=0.8)
        axis.set_title("Weekly dynamic-time-warping lag")
        axis.set_xlabel("UTC week start")
        axis.set_ylabel("Signed lag (minutes)")
        _format_utc_axis(axis)
        axis.legend(loc="best", ncols=2)
        stability_note = "; ".join(
            f"{_DIRECTION_LABELS[direction]} "
            f"unstable={str(indexed[direction].band_unstable).lower()}, "
            "same-sign share="
            f"{indexed[direction].primary_band_same_sign_week_share:.2f}"
            for direction in _DIRECTIONS
        )
        figure.text(
            0.5,
            0.012,
            "Positive lag means the named source precedes its target. Exploratory; "
            "rotation-null comparison is reported separately.\n"
            f"{stability_note}",
            ha="center",
            va="bottom",
            fontsize=6.8,
            multialignment="center",
        )
        return _render_png(figure, bottom_margin=0.1)


def lp_performance_png(
    rows: Sequence[tuple[str, float, float]],
) -> bytes:
    """Compare aggregate return and worst isolated-window drawdown by strategy."""
    materialized = tuple(rows)
    if len(materialized) != 5:
        raise CrossPoolContractError(
            "LP performance figure requires all five frozen strategies"
        )
    labels = tuple(row[0] for row in materialized)
    if len(set(labels)) != len(labels) or any(not label for label in labels):
        raise CrossPoolContractError(
            "LP performance figure strategy labels must be unique"
        )
    if any(
        not math.isfinite(value)
        for _label, aggregate_return, drawdown in materialized
        for value in (aggregate_return, drawdown)
    ) or any(drawdown < 0.0 for _label, _return, drawdown in materialized):
        raise CrossPoolContractError(
            "LP performance figure metrics must be finite with nonnegative drawdown"
        )

    with matplotlib.rc_context(rc=_FROZEN_RCPARAMS):
        figure, axis = _new_figure()
        x_positions = tuple(float(index) for index in range(len(materialized)))
        width = 0.36
        axis.bar(
            tuple(position - width / 2 for position in x_positions),
            tuple(row[1] for row in materialized),
            width=width,
            color="#16697A",
            label="Sum of isolated-window net returns",
        )
        axis.bar(
            tuple(position + width / 2 for position in x_positions),
            tuple(row[2] for row in materialized),
            width=width,
            color="#DB6400",
            label="Worst within-window drawdown magnitude",
        )
        axis.axhline(0.0, color="#222222", linewidth=0.8)
        axis.set_xticks(x_positions, labels)
        axis.set_title("Frozen Base LP economic comparison")
        axis.set_ylabel("Fraction of initial capital")
        axis.legend(loc="best")
        figure.text(
            0.5,
            0.01,
            "Window returns are summed; drawdowns are never stitched across reset windows.",
            ha="center",
            fontsize=7.2,
        )
        return _render_png(figure)


def _index_predictive_inferences(
    rows: Sequence[PredictiveInference],
) -> dict[tuple[Direction, int], PredictiveInference]:
    materialized = tuple(rows)
    keys = tuple((row.metrics.direction, row.metrics.horizon_ms) for row in materialized)
    expected = {
        (direction, horizon_ms)
        for direction in _DIRECTIONS
        for horizon_ms in _HORIZONS_MS
    }
    if len(keys) != len(expected) or set(keys) != expected:
        raise CrossPoolContractError(
            "predictive figure requires both directions at every frozen horizon"
        )
    return dict(zip(keys, materialized, strict=True))


def _index_event_summaries(
    rows: Sequence[EventSummary],
) -> dict[tuple[Direction, int], EventSummary]:
    materialized = tuple(rows)
    keys = tuple((row.direction, row.horizon_ms) for row in materialized)
    expected = {
        (direction, horizon_ms)
        for direction in _DIRECTIONS
        for horizon_ms in _HORIZONS_MS
    }
    if len(keys) != len(expected) or set(keys) != expected:
        raise CrossPoolContractError(
            "event figure requires both directions at every frozen horizon"
        )
    return dict(zip(keys, materialized, strict=True))


def _new_figure() -> tuple[Figure, Axes]:
    figure = Figure(figsize=(8.0, 4.8), dpi=144)
    return figure, figure.add_subplot()


def _format_utc_axis(axis: Axes) -> None:
    locator = AutoDateLocator(tz=timezone.utc)  # type: ignore[no-untyped-call]
    axis.xaxis.set_major_locator(locator)
    axis.xaxis.set_major_formatter(
        ConciseDateFormatter(  # type: ignore[no-untyped-call]
            locator,
            tz=timezone.utc,
        )
    )


def _utc_datetime(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)


def _render_png(figure: Figure, *, bottom_margin: float = 0.06) -> bytes:
    figure.tight_layout(rect=(0.0, bottom_margin, 1.0, 1.0), pad=1.0)
    buffer = io.BytesIO()
    FigureCanvasAgg(figure).print_png(  # type: ignore[no-untyped-call]
        buffer,
        metadata=_PNG_METADATA,
    )
    rendered = buffer.getvalue()
    if not rendered:
        raise CrossPoolContractError("figure rendering produced no bytes")
    return rendered
