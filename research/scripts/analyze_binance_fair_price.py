"""Analyze Binance-reference fair-price policy versus Quidax top-of-book."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence


DEFAULT_HORIZONS_SECONDS = (60, 120, 300, 600)
REFERENCE_PRICE_FIELDS = (
    "reference_price",
    "price",
    "mid",
    "binance_reference",
    "ngn_per_usdt",
    "usdt_ngn",
)
TIMESTAMP_FIELDS = ("timestamp_ms", "ts")


@dataclass(frozen=True)
class QuidaxTopBookPoint:
    timestamp_ms: int
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal
    spread_bps: Decimal | None


@dataclass(frozen=True)
class BinanceReferencePoint:
    timestamp_ms: int
    price: Decimal


@dataclass(frozen=True)
class EstimatorMetrics:
    observations: int
    mae: Decimal | None
    signed_bias: Decimal | None
    rmse: Decimal | None


@dataclass(frozen=True)
class HorizonPolicyReport:
    horizon_seconds: int
    label_count: int
    missing_label_count: int
    median_label_lag_ms: Decimal | None
    max_label_lag_ms: Decimal | None
    estimators: dict[str, EstimatorMetrics]


@dataclass(frozen=True)
class BinanceFairPricePolicyReport:
    total_rows: int
    reference_observations: int
    missing_reference_count: int
    median_reference_age_ms: Decimal | None
    max_reference_age_ms: Decimal | None
    median_abs_mid_offset_bps: Decimal | None
    median_spread_bps: Decimal | None
    horizons: dict[int, HorizonPolicyReport]


def load_quidax_top_book_json(path: Path) -> list[QuidaxTopBookPoint]:
    raw_rows = json.loads(path.read_text())
    if not isinstance(raw_rows, list):
        raise ValueError(f"{path} must contain a JSON list")
    points = [_quidax_point_from_mapping(row, path=path) for row in raw_rows]
    return sorted(points, key=lambda point: point.timestamp_ms)


def load_reference_price_file(path: Path) -> list[BinanceReferencePoint]:
    if path.suffix.lower() == ".json":
        raw_rows = json.loads(path.read_text())
        if not isinstance(raw_rows, list):
            raise ValueError(f"{path} must contain a JSON list")
        points = [_reference_point_from_mapping(row, path=path) for row in raw_rows]
    else:
        with path.open(newline="") as handle:
            points = [
                _reference_point_from_mapping(row, path=path)
                for row in csv.DictReader(handle)
            ]
    return sorted(points, key=lambda point: point.timestamp_ms)


def build_policy_rows(
    quidax_rows: Sequence[QuidaxTopBookPoint],
    reference_rows: Sequence[BinanceReferencePoint],
    *,
    horizons_seconds: Sequence[int],
    max_reference_age_ms: int,
    max_label_lag_ms: int,
) -> list[dict[str, str]]:
    if max_reference_age_ms < 0 or max_label_lag_ms < 0:
        raise ValueError("max ages must be non-negative")

    quidax = sorted(quidax_rows, key=lambda point: point.timestamp_ms)
    reference = sorted(reference_rows, key=lambda point: point.timestamp_ms)
    quidax_timestamps = [point.timestamp_ms for point in quidax]
    reference_timestamps = [point.timestamp_ms for point in reference]
    rows: list[dict[str, str]] = []

    for point in quidax:
        reference_point = _previous_reference(
            reference,
            reference_timestamps,
            timestamp_ms=point.timestamp_ms,
            max_age_ms=max_reference_age_ms,
        )
        row = {
            "timestamp_ms": str(point.timestamp_ms),
            "quidax_bid": _format_price(point.bid),
            "quidax_ask": _format_price(point.ask),
            "quidax_mid": _format_price(point.mid),
            "quidax_spread_bps": _format_bps(_spread_bps(point)),
            "binance_reference_price": _format_price(
                reference_point.price if reference_point is not None else None
            ),
            "binance_reference_age_ms": str(point.timestamp_ms - reference_point.timestamp_ms)
            if reference_point is not None
            else "",
            "quidax_mid_minus_binance_bps": _format_bps(
                _relative_bps(point.mid, reference_point.price)
                if reference_point is not None
                else None
            ),
            "quidax_bid_offset_bps": _format_bps(
                _relative_bps(point.bid, reference_point.price)
                if reference_point is not None
                else None
            ),
            "quidax_ask_offset_bps": _format_bps(
                _relative_bps(point.ask, reference_point.price)
                if reference_point is not None
                else None
            ),
        }
        for horizon in horizons_seconds:
            label = _future_quidax_label(
                quidax,
                quidax_timestamps,
                target_timestamp_ms=point.timestamp_ms + horizon * 1000,
                max_label_lag_ms=max_label_lag_ms,
            )
            prefix = f"label_{horizon}s"
            row[f"{prefix}_quidax_mid"] = _format_price(label.mid if label else None)
            row[f"{prefix}_lag_ms"] = str(
                label.timestamp_ms - (point.timestamp_ms + horizon * 1000)
            ) if label else ""
            row[f"{prefix}_quidax_mid_return"] = _format_bps(
                _relative_bps(label.mid, point.mid) if label else None
            )
        rows.append(row)

    return rows


def analyze_policy_rows(
    rows: Sequence[dict[str, str]],
    *,
    horizons_seconds: Sequence[int],
) -> BinanceFairPricePolicyReport:
    reference_ages = [
        age
        for row in rows
        for age in [_decimal(row.get("binance_reference_age_ms"))]
        if age is not None
    ]
    mid_offsets = [
        abs(offset)
        for row in rows
        for offset in [_decimal(row.get("quidax_mid_minus_binance_bps"))]
        if offset is not None
    ]
    spreads = [
        spread
        for row in rows
        for spread in [_decimal(row.get("quidax_spread_bps"))]
        if spread is not None
    ]
    horizon_reports: dict[int, HorizonPolicyReport] = {}
    for horizon in horizons_seconds:
        label_key = f"label_{horizon}s_quidax_mid"
        lag_key = f"label_{horizon}s_lag_ms"
        labels = [_decimal(row.get(label_key)) for row in rows]
        label_lags = [
            lag
            for row in rows
            if _decimal(row.get(label_key)) is not None
            for lag in [_decimal(row.get(lag_key))]
            if lag is not None
        ]
        horizon_reports[horizon] = HorizonPolicyReport(
            horizon_seconds=horizon,
            label_count=sum(1 for label in labels if label is not None),
            missing_label_count=sum(1 for label in labels if label is None),
            median_label_lag_ms=_median(label_lags),
            max_label_lag_ms=max(label_lags) if label_lags else None,
            estimators={
                "current_quidax_mid": _estimator_metrics(
                    rows,
                    label_key=label_key,
                    estimator_key="quidax_mid",
                ),
                "binance_reference": _estimator_metrics(
                    rows,
                    label_key=label_key,
                    estimator_key="binance_reference_price",
                ),
            },
        )

    return BinanceFairPricePolicyReport(
        total_rows=len(rows),
        reference_observations=len(reference_ages),
        missing_reference_count=len(rows) - len(reference_ages),
        median_reference_age_ms=_median(reference_ages),
        max_reference_age_ms=max(reference_ages) if reference_ages else None,
        median_abs_mid_offset_bps=_median(mid_offsets),
        median_spread_bps=_median(spreads),
        horizons=horizon_reports,
    )


def render_markdown_report(report: BinanceFairPricePolicyReport) -> str:
    lines = [
        "# Binance Fair Price Policy Report",
        "",
        "Label: future Quidax managed top-book midpoint.",
        "Depth-walk execution is not tested by this report.",
        "",
        f"Rows analyzed: {report.total_rows}",
        f"Rows with Binance reference: {report.reference_observations}",
        f"Rows missing Binance reference: {report.missing_reference_count}",
        f"Median Binance reference age ms: {_format_metric(report.median_reference_age_ms)}",
        f"Max Binance reference age ms: {_format_metric(report.max_reference_age_ms)}",
        f"Median abs Quidax midpoint offset bps: {_format_metric(report.median_abs_mid_offset_bps)}",
        f"Median Quidax spread bps: {_format_metric(report.median_spread_bps)}",
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
                f"Median label lag ms: {_format_metric(horizon_report.median_label_lag_ms)}",
                f"Max label lag ms: {_format_metric(horizon_report.max_label_lag_ms)}",
                "",
                "| estimator | observations | mae | rmse | signed_bias |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for name, metrics in horizon_report.estimators.items():
            lines.append(_estimator_row(name, metrics))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_policy_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _quidax_point_from_mapping(row: Any, *, path: Path) -> QuidaxTopBookPoint:
    if not isinstance(row, dict):
        raise ValueError(f"{path} contains a non-object Quidax row")
    timestamp = _required_int(_first_present(row, TIMESTAMP_FIELDS), path=path)
    mid = _required_decimal(_first_present(row, ("mid",)), path=path)
    if mid <= 0:
        raise ValueError(f"{path} contains a non-positive Quidax mid")
    return QuidaxTopBookPoint(
        timestamp_ms=timestamp,
        bid=_optional_decimal(_first_present(row, ("bid",))),
        ask=_optional_decimal(_first_present(row, ("ask",))),
        mid=mid,
        spread_bps=_optional_decimal(_first_present(row, ("spread_bps",))),
    )


def _reference_point_from_mapping(row: Any, *, path: Path) -> BinanceReferencePoint:
    if not isinstance(row, dict):
        raise ValueError(f"{path} contains a non-object reference row")
    timestamp = _required_int(_first_present(row, TIMESTAMP_FIELDS), path=path)
    price = _required_decimal(_first_present(row, REFERENCE_PRICE_FIELDS), path=path)
    if price <= 0:
        raise ValueError(f"{path} contains a non-positive reference price")
    return BinanceReferencePoint(timestamp_ms=timestamp, price=price)


def _previous_reference(
    reference: Sequence[BinanceReferencePoint],
    timestamps: Sequence[int],
    *,
    timestamp_ms: int,
    max_age_ms: int,
) -> BinanceReferencePoint | None:
    index = bisect_right(timestamps, timestamp_ms) - 1
    if index < 0:
        return None
    point = reference[index]
    if timestamp_ms - point.timestamp_ms > max_age_ms:
        return None
    return point


def _future_quidax_label(
    points: Sequence[QuidaxTopBookPoint],
    timestamps: Sequence[int],
    *,
    target_timestamp_ms: int,
    max_label_lag_ms: int,
) -> QuidaxTopBookPoint | None:
    index = bisect_left(timestamps, target_timestamp_ms)
    if index >= len(points):
        return None
    point = points[index]
    if point.timestamp_ms - target_timestamp_ms > max_label_lag_ms:
        return None
    return point


def _estimator_metrics(
    rows: Sequence[dict[str, str]],
    *,
    label_key: str,
    estimator_key: str,
) -> EstimatorMetrics:
    errors: list[Decimal] = []
    for row in rows:
        label = _decimal(row.get(label_key))
        estimate = _decimal(row.get(estimator_key))
        if label is None or estimate is None:
            continue
        errors.append(estimate - label)
    if not errors:
        return EstimatorMetrics(0, None, None, None)
    abs_errors = [abs(error) for error in errors]
    square_mean = sum(error * error for error in errors) / Decimal(len(errors))
    rmse = Decimal(str(math.sqrt(float(square_mean))))
    return EstimatorMetrics(
        observations=len(errors),
        mae=sum(abs_errors, Decimal("0")) / Decimal(len(abs_errors)),
        signed_bias=sum(errors, Decimal("0")) / Decimal(len(errors)),
        rmse=rmse,
    )


def _spread_bps(point: QuidaxTopBookPoint) -> Decimal | None:
    if point.spread_bps is not None:
        return point.spread_bps
    if point.bid is None or point.ask is None:
        return None
    return (point.ask - point.bid) / point.mid * Decimal("10000")


def _relative_bps(value: Decimal | None, reference: Decimal | None) -> Decimal | None:
    if value is None or reference is None:
        return None
    if reference <= 0:
        raise ValueError("reference price must be positive")
    return (value / reference - Decimal("1")) * Decimal("10000")


def _first_present(row: dict[str, Any], fields: Sequence[str]) -> str:
    for field in fields:
        value = row.get(field)
        if value not in (None, ""):
            return str(value)
    return ""


def _required_int(value: str, *, path: Path) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{path} contains an invalid or missing timestamp") from exc


def _required_decimal(value: str, *, path: Path) -> Decimal:
    parsed = _optional_decimal(value)
    if parsed is None:
        raise ValueError(f"{path} contains an invalid or missing decimal")
    return parsed


def _optional_decimal(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value {value}") from exc


def _decimal(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(str(statistics.median(values)))


def _format_price(value: Decimal | None) -> str:
    if value is None:
        return ""
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _format_bps(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.6f}"


def _format_metric(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.6f}"


def _estimator_row(name: str, metrics: EstimatorMetrics) -> str:
    return (
        f"| {name} | {metrics.observations} | {_format_metric(metrics.mae)} | "
        f"{_format_metric(metrics.rmse)} | {_format_metric(metrics.signed_bias)} |"
    )


def _parse_horizons(raw: str) -> list[int]:
    horizons = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not horizons or any(horizon <= 0 for horizon in horizons):
        raise ValueError("--horizons must contain positive integers")
    return horizons


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quidax-json", required=True)
    parser.add_argument("--binance-reference", required=True)
    parser.add_argument("--out-csv", required=True)
    parser.add_argument("--out-report", required=True)
    parser.add_argument(
        "--horizons",
        default=",".join(str(horizon) for horizon in DEFAULT_HORIZONS_SECONDS),
    )
    parser.add_argument("--max-reference-age-seconds", type=int, default=3600)
    parser.add_argument("--max-label-lag-seconds", type=int, default=60)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    horizons = _parse_horizons(args.horizons)
    rows = build_policy_rows(
        load_quidax_top_book_json(Path(args.quidax_json)),
        load_reference_price_file(Path(args.binance_reference)),
        horizons_seconds=horizons,
        max_reference_age_ms=args.max_reference_age_seconds * 1000,
        max_label_lag_ms=args.max_label_lag_seconds * 1000,
    )
    write_policy_csv(Path(args.out_csv), rows)
    Path(args.out_report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_report).write_text(
        render_markdown_report(analyze_policy_rows(rows, horizons_seconds=horizons))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
