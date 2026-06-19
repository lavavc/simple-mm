"""Analyze fair-price estimator markouts from the exported markout CSV."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Callable

EstimatorFn = Callable[[dict[str, str]], Decimal | None]


@dataclass(frozen=True)
class EstimatorSpec:
    name: str
    value: EstimatorFn


@dataclass(frozen=True)
class EstimatorMetrics:
    observations: int
    missing_estimate_count: int
    mae: Decimal | None
    median_abs_error: Decimal | None
    signed_bias: Decimal | None
    rmse: Decimal | None
    direction_hits: int
    direction_observations: int
    direction_hit_rate: Decimal | None


@dataclass(frozen=True)
class HorizonReport:
    horizon_seconds: int
    label_count: int
    missing_label_count: int
    label_lag_observations: int
    median_label_lag_ms: Decimal | None
    max_label_lag_ms: Decimal | None
    estimators: dict[str, EstimatorMetrics]
    side_estimators: dict[str, EstimatorMetrics]


@dataclass(frozen=True)
class DatasetQuality:
    median_row_gap_ms: Decimal | None
    max_row_gap_ms: Decimal | None
    current_executable_observations: int
    missing_current_executable_count: int
    unique_current_executable_mid_count: int
    bybit_observations: int
    median_bybit_age_ms: Decimal | None
    max_bybit_age_ms: Decimal | None


@dataclass(frozen=True)
class MarkoutReport:
    total_rows: int
    first_timestamp_ms: int | None
    last_timestamp_ms: int | None
    quality: DatasetQuality
    horizons: dict[int, HorizonReport]


MID_ESTIMATORS: tuple[EstimatorSpec, ...] = (
    EstimatorSpec("quidax_ticker_mid", lambda row: _decimal(row.get("quidax_ticker_mid"))),
    EstimatorSpec("quidax_top_mid", lambda row: _decimal(row.get("quidax_top_mid"))),
    EstimatorSpec("quidax_executable_mid", lambda row: _decimal(row.get("quidax_executable_mid"))),
    EstimatorSpec("bybit_p2p_mid", lambda row: _inverted_decimal(row.get("bybit_mid"))),
)

SIDE_ESTIMATORS: tuple[tuple[str, str, str], ...] = (
    ("quidax_buy_cngn_usd", "quidax_buy_cngn_usd", "buy_cngn_usd"),
    ("quidax_sell_cngn_usd", "quidax_sell_cngn_usd", "sell_cngn_usd"),
)


def load_markout_rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="") as file:
        return [dict(row) for row in csv.DictReader(file)]


def analyze_markouts(
    rows: list[dict[str, str]],
    *,
    horizons_seconds: list[int],
) -> MarkoutReport:
    timestamps = [
        int(row["timestamp_ms"])
        for row in rows
        if row.get("timestamp_ms") and row["timestamp_ms"].isdigit()
    ]
    horizon_reports: dict[int, HorizonReport] = {}
    for horizon in horizons_seconds:
        label_key = f"label_{horizon}s_executable_mid"
        labels = [_decimal(row.get(label_key)) for row in rows]
        label_count = sum(1 for label in labels if label is not None)
        label_lags = [
            lag
            for row in rows
            if _decimal(row.get(label_key)) is not None
            for lag in [_nonnegative_decimal(row.get(f"label_{horizon}s_lag_ms"))]
            if lag is not None
        ]
        estimator_metrics = {
            spec.name: _metrics_for_estimator(
                rows,
                label_key=label_key,
                estimator=spec.value,
            )
            for spec in MID_ESTIMATORS
        }
        side_metrics = {
            name: _metrics_for_estimator(
                rows,
                label_key=f"label_{horizon}s_{label_suffix}",
                estimator=_field_estimator(field),
            )
            for name, field, label_suffix in SIDE_ESTIMATORS
        }
        horizon_reports[horizon] = HorizonReport(
            horizon_seconds=horizon,
            label_count=label_count,
            missing_label_count=len(rows) - label_count,
            label_lag_observations=len(label_lags),
            median_label_lag_ms=_median(sorted(label_lags)) if label_lags else None,
            max_label_lag_ms=max(label_lags) if label_lags else None,
            estimators=estimator_metrics,
            side_estimators=side_metrics,
        )

    return MarkoutReport(
        total_rows=len(rows),
        first_timestamp_ms=min(timestamps) if timestamps else None,
        last_timestamp_ms=max(timestamps) if timestamps else None,
        quality=_dataset_quality(rows, timestamps),
        horizons=horizon_reports,
    )


def infer_horizons(rows: list[dict[str, str]]) -> list[int]:
    horizons: set[int] = set()
    for row in rows:
        for key in row:
            if key.startswith("label_") and key.endswith("s_executable_mid"):
                raw = key.removeprefix("label_").removesuffix("s_executable_mid")
                if raw.isdigit():
                    horizons.add(int(raw))
    return sorted(horizons)


def render_markdown_report(report: MarkoutReport) -> str:
    lines = [
        "# Fair Price Markout Report",
        "",
        f"Rows analyzed: {report.total_rows}",
        f"First timestamp ms: {_format_optional_int(report.first_timestamp_ms)}",
        f"Last timestamp ms: {_format_optional_int(report.last_timestamp_ms)}",
        "",
        "Label: future Quidax depth-walk executable midpoint.",
        "",
        "## Markout Dataset Quality",
        "",
        f"Median row gap ms: {_format_decimal(report.quality.median_row_gap_ms)}",
        f"Max row gap ms: {_format_decimal(report.quality.max_row_gap_ms)}",
        f"Current executable observations: {report.quality.current_executable_observations}",
        "Missing current executable observations: "
        f"{report.quality.missing_current_executable_count}",
        f"Unique current executable mids: {report.quality.unique_current_executable_mid_count}",
        f"Bybit observations: {report.quality.bybit_observations}",
        f"Median Bybit age ms: {_format_decimal(report.quality.median_bybit_age_ms)}",
        f"Max Bybit age ms: {_format_decimal(report.quality.max_bybit_age_ms)}",
        "",
    ]
    for horizon in sorted(report.horizons):
        horizon_report = report.horizons[horizon]
        lines.extend(
            [
                f"## Horizon {horizon}s",
                "",
                f"Rows with label: {horizon_report.label_count}",
                f"Rows missing label: {horizon_report.missing_label_count}",
                f"Median label lag ms: {_format_decimal(horizon_report.median_label_lag_ms)}",
                f"Max label lag ms: {_format_decimal(horizon_report.max_label_lag_ms)}",
                "",
                "| estimator | observations | mae | signed_bias | rmse | direction_hits |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for name, metrics in horizon_report.estimators.items():
            lines.append(_metric_table_row(name, metrics))
        lines.extend(
            [
                "",
                "Side-specific execution error:",
                "",
                "| side | observations | mae | signed_bias | rmse | direction_hits |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for name, metrics in horizon_report.side_estimators.items():
            lines.append(_metric_table_row(name, metrics))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _dataset_quality(rows: list[dict[str, str]], timestamps: list[int]) -> DatasetQuality:
    timestamp_gaps = [
        Decimal(str(current - previous))
        for previous, current in zip(sorted(timestamps), sorted(timestamps)[1:])
    ]
    executable_mids = [
        value
        for row in rows
        for value in [_decimal(row.get("quidax_executable_mid"))]
        if value is not None
    ]
    bybit_ages = [
        value
        for row in rows
        if _decimal(row.get("bybit_mid")) is not None
        for value in [_nonnegative_decimal(row.get("bybit_age_ms"))]
        if value is not None
    ]
    return DatasetQuality(
        median_row_gap_ms=_median(sorted(timestamp_gaps)) if timestamp_gaps else None,
        max_row_gap_ms=max(timestamp_gaps) if timestamp_gaps else None,
        current_executable_observations=len(executable_mids),
        missing_current_executable_count=len(rows) - len(executable_mids),
        unique_current_executable_mid_count=len(set(executable_mids)),
        bybit_observations=len(bybit_ages),
        median_bybit_age_ms=_median(sorted(bybit_ages)) if bybit_ages else None,
        max_bybit_age_ms=max(bybit_ages) if bybit_ages else None,
    )


def _metrics_for_estimator(
    rows: list[dict[str, str]],
    *,
    label_key: str,
    estimator: EstimatorFn,
) -> EstimatorMetrics:
    errors: list[Decimal] = []
    direction_hits = 0
    direction_observations = 0
    missing_estimate_count = 0

    for row in rows:
        label = _decimal(row.get(label_key))
        if label is None:
            continue
        estimate = estimator(row)
        if estimate is None:
            missing_estimate_count += 1
            continue
        errors.append(estimate - label)

        current = _current_reference(row, label_key)
        if current is None:
            continue
        predicted_direction = _sign(estimate - current)
        actual_direction = _sign(label - current)
        if predicted_direction == 0 or actual_direction == 0:
            continue
        direction_observations += 1
        if predicted_direction == actual_direction:
            direction_hits += 1

    if not errors:
        return EstimatorMetrics(
            observations=0,
            missing_estimate_count=missing_estimate_count,
            mae=None,
            median_abs_error=None,
            signed_bias=None,
            rmse=None,
            direction_hits=direction_hits,
            direction_observations=direction_observations,
            direction_hit_rate=None,
        )

    abs_errors = sorted(abs(error) for error in errors)
    mae = _mean(abs_errors)
    signed_bias = _mean(errors)
    rmse = _sqrt(_mean([error * error for error in errors]))
    direction_hit_rate = (
        Decimal(direction_hits) / Decimal(direction_observations)
        if direction_observations > 0
        else None
    )
    return EstimatorMetrics(
        observations=len(errors),
        missing_estimate_count=missing_estimate_count,
        mae=mae,
        median_abs_error=_median(abs_errors),
        signed_bias=signed_bias,
        rmse=rmse,
        direction_hits=direction_hits,
        direction_observations=direction_observations,
        direction_hit_rate=direction_hit_rate,
    )


def _current_reference(row: dict[str, str], label_key: str) -> Decimal | None:
    if label_key.endswith("_buy_cngn_usd"):
        return _decimal(row.get("quidax_buy_cngn_usd"))
    if label_key.endswith("_sell_cngn_usd"):
        return _decimal(row.get("quidax_sell_cngn_usd"))
    return _decimal(row.get("quidax_executable_mid"))


def _metric_table_row(name: str, metrics: EstimatorMetrics) -> str:
    direction = (
        ""
        if metrics.direction_observations == 0
        else f"{metrics.direction_hits} / {metrics.direction_observations}"
    )
    return (
        f"| {name} | {metrics.observations} | {_format_decimal(metrics.mae)} | "
        f"{_format_decimal(metrics.signed_bias)} | {_format_decimal(metrics.rmse)} | "
        f"{direction} |"
    )


def _field_estimator(field: str) -> EstimatorFn:
    return lambda row: _decimal(row.get(field))


def _decimal(raw: str | None) -> Decimal | None:
    if raw is None or raw == "":
        return None
    value = Decimal(str(raw))
    if value <= 0:
        return None
    return value


def _nonnegative_decimal(raw: str | None) -> Decimal | None:
    if raw is None or raw == "":
        return None
    value = Decimal(str(raw))
    if value < 0:
        return None
    return value


def _inverted_decimal(raw: str | None) -> Decimal | None:
    value = _decimal(raw)
    if value is None:
        return None
    return Decimal("1") / value


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _median(values: list[Decimal]) -> Decimal:
    midpoint = len(values) // 2
    if len(values) % 2 == 1:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) / Decimal("2")


def _sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.sqrt()


def _sign(value: Decimal) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    with localcontext() as context:
        context.prec = 28
        rounded = +value
    return format(rounded.normalize(), "f")


def _format_optional_int(value: int | None) -> str:
    return "" if value is None else str(value)


def _parse_horizons(raw: str) -> list[int]:
    horizons = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not horizons:
        raise ValueError("At least one horizon is required")
    if any(horizon <= 0 for horizon in horizons):
        raise ValueError("Horizons must be positive seconds")
    return horizons


def _main() -> None:
    parser = argparse.ArgumentParser(description="Analyze fair-price markout CSV rows.")
    parser.add_argument("--csv", required=True, help="Input markout CSV")
    parser.add_argument("--out", required=True, help="Output markdown path, or '-' for stdout")
    parser.add_argument(
        "--horizons",
        default="",
        help="Comma-separated horizons in seconds. Defaults to horizons present in the CSV.",
    )
    args = parser.parse_args()

    rows = load_markout_rows(args.csv)
    horizons = _parse_horizons(args.horizons) if args.horizons else infer_horizons(rows)
    report = analyze_markouts(rows, horizons_seconds=horizons)
    markdown = render_markdown_report(report)

    if args.out == "-":
        print(markdown, end="")
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown)


if __name__ == "__main__":
    _main()
