"""Report opportunity, costed backtest, and regime stability diagnostics."""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Sequence


SECONDS_PER_YEAR = Decimal("31557600")
SGHO_APY = Decimal("0.0425")
REGIME_DIAGNOSTIC_SCOPE = "retrospective validation-window"

REQUIRED_WINDOW_FIELDS = (
    "window_index",
    "window_start",
    "window_end",
    "validation_net_return",
    "validation_max_drawdown",
    "validation_rebalance_count",
    "validation_total_fees",
    "validation_total_transaction_cost",
)

DEFAULT_PARAMETER_FIELDS = (
    "strategy_mode",
    "range_mode",
    "center_mode",
    "sd_multiplier",
    "ewma_lambda",
    "downside_skew",
    "preemptive_rebalance",
    "rebalance_threshold_pct",
    "fixed_width_pct",
    "fixed_tick_width",
    "harvest_upward_range_fraction",
    "profit_take_return",
    "stop_loss_return",
    "out_of_range_overshoot_fraction",
    "min_exit_swap_volume_usd",
    "exit_confirmation_swaps",
    "exit_price_mode",
    "exit_price_min_swap_volume_usd",
    "initial_capital_usd",
    "mint_gas_usd",
    "remove_gas_usd",
    "unwind_to_cash_on_exit",
)

DEFAULT_REGIME_FIELDS = (
    "dex_premium_cone_pct",
    "swap_flow_imbalance_cone_pct",
    "active_liquidity_cone_pct",
    "active_liquidity_running_max_share_cone_pct",
    "realized_volatility_cone_pct",
    "fee_intensity_proxy_cone_pct",
    "volume_cone_pct",
)


@dataclass(frozen=True)
class BacktestSectionSummary:
    name: str
    rows: int
    mean_return_on_capital: Decimal | None
    worst_drawdown: Decimal | None
    total_rebalance_count: Decimal | None
    mean_rebalance_count: Decimal | None
    total_fees: Decimal | None
    total_transaction_cost: Decimal | None
    fee_cost_ratio: Decimal | None
    mean_apy: Decimal | None
    mean_sgho_hurdle_return: Decimal | None
    mean_sgho_excess_return: Decimal | None
    windows_above_sgho_hurdle: int | None
    capacity_status: str
    capacity_valid_rows: int | None
    capacity_invalid_rows: int | None
    mean_share_diluted: Decimal | None


@dataclass(frozen=True)
class StabilityReport:
    total_rows: int
    selected_window_count: int
    sgho_apy: Decimal
    opportunity: BacktestSectionSummary
    full_costed: BacktestSectionSummary
    regime_diagnostic_scope: str
    parameter_jumps: list[dict[str, object]]
    regime_delta_rows: list[dict[str, object]]
    unexplained_jump_count: int


@dataclass(frozen=True)
class WindowBounds:
    window_index: int
    start_ms: int
    end_ms: int


def load_window_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a CSV header")
        _require_fields(reader.fieldnames, REQUIRED_WINDOW_FIELDS, source=str(path))
        rows = [dict(row) for row in reader]

    _validate_window_rows(rows)
    return rows


def load_feature_by_window(
    path: Path,
    window_rows: list[dict[str, str]],
    *,
    regime_fields: Sequence[str],
) -> dict[int, dict[str, float]]:
    selected_rows = _selected_window_rows(window_rows)
    bounds = [
        WindowBounds(
            window_index=_parse_int_field(row, "window_index"),
            start_ms=_parse_timestamp_ms(row["window_start"]),
            end_ms=_parse_timestamp_ms(row["window_end"]),
        )
        for row in selected_rows
    ]
    if not bounds:
        return {}

    with path.open(newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a CSV header")
        _require_fields(
            reader.fieldnames,
            ("timestamp_ms", *regime_fields),
            source=str(path),
        )

        feature_values: dict[int, dict[str, list[Decimal]]] = {
            bound.window_index: {field: [] for field in regime_fields}
            for bound in bounds
        }
        for row in reader:
            timestamp_ms = _parse_int(row["timestamp_ms"], "timestamp_ms")
            bound = _window_for_timestamp(bounds, timestamp_ms)
            if bound is None:
                continue
            for field in regime_fields:
                if _clean_value(row[field]) == "":
                    continue
                value = _parse_decimal(row[field], field)
                feature_values[bound.window_index][field].append(value)

    result: dict[int, dict[str, float]] = {}
    for window_index, values_by_field in feature_values.items():
        averaged: dict[str, float] = {}
        for field, values in values_by_field.items():
            if values:
                averaged[field] = float(_mean(values))
        if averaged:
            result[window_index] = averaged
    return result


def parameter_jump_rows(
    window_rows: list[dict[str, str]],
    feature_by_window: dict[int, dict[str, float]],
    parameter_fields: list[str],
    regime_fields: list[str],
    min_regime_delta: float,
) -> list[dict[str, object]]:
    selected_rows = _selected_window_rows(window_rows)
    jumps: list[dict[str, object]] = []
    for previous, current in zip(selected_rows, selected_rows[1:]):
        previous_index = _parse_int_field(previous, "window_index")
        current_index = _parse_int_field(current, "window_index")
        regime_deltas = _regime_deltas(
            feature_by_window,
            previous_index=previous_index,
            current_index=current_index,
            regime_fields=regime_fields,
        )
        max_regime_field, max_regime_delta = _max_regime_delta(regime_deltas)

        for field in parameter_fields:
            _require_row_field(previous, field)
            _require_row_field(current, field)
            previous_value = _clean_value(previous[field])
            current_value = _clean_value(current[field])
            if previous_value == current_value:
                continue
            jumps.append(
                {
                    "from_window_index": previous_index,
                    "to_window_index": current_index,
                    "parameter": field,
                    "from_value": _format_parameter_value(previous_value),
                    "to_value": _format_parameter_value(current_value),
                    "parameter_delta": _parameter_delta(previous_value, current_value),
                    "regime_deltas": regime_deltas,
                    "max_regime_field": max_regime_field,
                    "max_regime_delta": max_regime_delta,
                    "unexplained_jump": max_regime_delta < min_regime_delta,
                    "regime_diagnostic_scope": REGIME_DIAGNOSTIC_SCOPE,
                }
            )
    return jumps


def build_stability_report(
    window_rows: list[dict[str, str]],
    feature_by_window: dict[int, dict[str, float]],
    *,
    parameter_fields: Sequence[str] = DEFAULT_PARAMETER_FIELDS,
    regime_fields: Sequence[str] = DEFAULT_REGIME_FIELDS,
    min_regime_delta: float = 0.20,
    sgho_apy: Decimal = SGHO_APY,
) -> StabilityReport:
    _validate_window_rows(window_rows)
    eligible_rows = [
        row for row in window_rows if _clean_value(row.get("skipped_reason", "")) == ""
    ]
    selected_rows = _selected_window_rows(eligible_rows)
    opportunity_rows = [
        row
        for row in selected_rows
        if _parse_decimal_field(row, "validation_total_transaction_cost") == 0
    ]
    full_costed_rows = [
        row
        for row in selected_rows
        if _parse_decimal_field(row, "validation_total_transaction_cost") != 0
    ]
    jumps = parameter_jump_rows(
        selected_rows,
        feature_by_window,
        list(parameter_fields),
        list(regime_fields),
        min_regime_delta,
    )
    regime_rows = _neighbor_regime_delta_rows(
        selected_rows,
        feature_by_window,
        regime_fields=list(regime_fields),
    )
    return StabilityReport(
        total_rows=len(window_rows),
        selected_window_count=len(selected_rows),
        sgho_apy=sgho_apy,
        opportunity=_section_summary(
            "Opportunity Screen",
            opportunity_rows,
            sgho_apy=sgho_apy,
        ),
        full_costed=_section_summary(
            "Full Costed Backtest",
            full_costed_rows,
            sgho_apy=sgho_apy,
        ),
        regime_diagnostic_scope=REGIME_DIAGNOSTIC_SCOPE,
        parameter_jumps=jumps,
        regime_delta_rows=regime_rows,
        unexplained_jump_count=sum(1 for jump in jumps if jump["unexplained_jump"]),
    )


def render_stability_markdown(report: StabilityReport) -> str:
    lines = [
        "# Backtest Regime Stability Report",
        "",
        f"Rows analyzed: {report.total_rows}",
        f"Selected walk-forward windows: {report.selected_window_count}",
        f"sGHO APY benchmark: {_format_percent(report.sgho_apy)}",
        "",
    ]
    lines.extend(_section_lines(report.opportunity))
    lines.append("")
    lines.extend(_section_lines(report.full_costed))
    lines.extend(
        [
            "",
            "## Parameter Jump Stability",
            "",
            (
                "Regime diagnostics are retrospective validation-window summaries; "
                "use them for post-run stability audit, not ex-ante parameter selection."
            ),
            "",
            f"Unexplained jump count: {report.unexplained_jump_count}",
            "",
            (
                "| from_window | to_window | parameter | from_value | to_value | "
                "max_regime_delta | unexplained_jump |"
            ),
            "|---:|---:|---|---:|---:|---:|---|",
        ]
    )
    if report.parameter_jumps:
        for jump in report.parameter_jumps:
            lines.append(
                f"| {jump['from_window_index']} | {jump['to_window_index']} | "
                f"{jump['parameter']} | {jump['from_value']} | {jump['to_value']} | "
                f"{_format_number(jump['max_regime_delta'])} | "
                f"{jump['unexplained_jump']} |"
            )
    else:
        lines.append(
            "| not_available | not_available | not_available | not_available | "
            "not_available | not_available | not_available |"
        )

    lines.extend(
        [
            "",
            "Regime-feature deltas across neighboring windows:",
            "",
            "| from_window | to_window | regime_field | delta |",
            "|---:|---:|---|---:|",
        ]
    )
    if report.regime_delta_rows:
        for row in report.regime_delta_rows:
            lines.append(
                f"| {row['from_window_index']} | {row['to_window_index']} | "
                f"{row['regime_field']} | {_format_number(row['delta'])} |"
            )
    else:
        lines.append("| not_available | not_available | not_available | not_available |")
    return "\n".join(lines).rstrip() + "\n"


def _section_lines(summary: BacktestSectionSummary) -> list[str]:
    lines = [
        f"## {summary.name}",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| rows | {summary.rows if summary.rows else 'not_available'} |",
        f"| return_on_capital | {_format_decimal(summary.mean_return_on_capital)} |",
        f"| worst_drawdown | {_format_decimal(summary.worst_drawdown)} |",
        f"| total_rebalance_count | {_format_decimal(summary.total_rebalance_count)} |",
        f"| mean_rebalance_count | {_format_decimal(summary.mean_rebalance_count)} |",
        f"| total_fees | {_format_decimal(summary.total_fees)} |",
        f"| total_transaction_cost | {_format_decimal(summary.total_transaction_cost)} |",
        f"| fee_cost_ratio | {_format_decimal(summary.fee_cost_ratio)} |",
        f"| mean_apy | {_format_decimal(summary.mean_apy)} |",
        f"| mean_sgho_hurdle_return | {_format_decimal(summary.mean_sgho_hurdle_return)} |",
        f"| mean_sgho_excess_return | {_format_decimal(summary.mean_sgho_excess_return)} |",
        (
            "| windows_above_sgho_hurdle | "
            f"{_format_optional_int(summary.windows_above_sgho_hurdle)} |"
        ),
        "",
        f"Capacity/share validity: {summary.capacity_status}",
    ]
    if summary.capacity_status != "not_available":
        lines.extend(
            [
                f"Capacity valid rows: {_format_optional_int(summary.capacity_valid_rows)}",
                f"Capacity invalid rows: {_format_optional_int(summary.capacity_invalid_rows)}",
                f"Mean diluted share: {_format_decimal(summary.mean_share_diluted)}",
            ]
        )
    return lines


def _section_summary(
    name: str,
    rows: list[dict[str, str]],
    *,
    sgho_apy: Decimal,
) -> BacktestSectionSummary:
    if not rows:
        return BacktestSectionSummary(
            name=name,
            rows=0,
            mean_return_on_capital=None,
            worst_drawdown=None,
            total_rebalance_count=None,
            mean_rebalance_count=None,
            total_fees=None,
            total_transaction_cost=None,
            fee_cost_ratio=None,
            mean_apy=None,
            mean_sgho_hurdle_return=None,
            mean_sgho_excess_return=None,
            windows_above_sgho_hurdle=None,
            capacity_status="not_available",
            capacity_valid_rows=None,
            capacity_invalid_rows=None,
            mean_share_diluted=None,
        )

    returns = [_parse_decimal_field(row, "validation_net_return") for row in rows]
    drawdowns = [_parse_decimal_field(row, "validation_max_drawdown") for row in rows]
    rebalances = [
        _parse_decimal_field(row, "validation_rebalance_count") for row in rows
    ]
    total_fees = sum(
        (_parse_decimal_field(row, "validation_total_fees") for row in rows),
        Decimal("0"),
    )
    total_cost = sum(
        (
            _parse_decimal_field(row, "validation_total_transaction_cost")
            for row in rows
        ),
        Decimal("0"),
    )
    apys = [_row_apy(row) for row in rows]
    sgho_hurdles = [_row_sgho_hurdle_return(row, sgho_apy) for row in rows]
    sgho_excess_returns = [
        net_return - hurdle for net_return, hurdle in zip(returns, sgho_hurdles)
    ]
    capacity = _capacity_summary(rows)

    return BacktestSectionSummary(
        name=name,
        rows=len(rows),
        mean_return_on_capital=_mean(returns),
        worst_drawdown=max(drawdowns),
        total_rebalance_count=sum(rebalances, Decimal("0")),
        mean_rebalance_count=_mean(rebalances),
        total_fees=total_fees,
        total_transaction_cost=total_cost,
        fee_cost_ratio=(total_fees / total_cost if total_cost != 0 else None),
        mean_apy=_mean(apys),
        mean_sgho_hurdle_return=_mean(sgho_hurdles),
        mean_sgho_excess_return=_mean(sgho_excess_returns),
        windows_above_sgho_hurdle=sum(
            1 for net_return, hurdle in zip(returns, sgho_hurdles) if net_return > hurdle
        ),
        capacity_status=capacity[0],
        capacity_valid_rows=capacity[1],
        capacity_invalid_rows=capacity[2],
        mean_share_diluted=capacity[3],
    )


def _capacity_summary(
    rows: list[dict[str, str]],
) -> tuple[str, int | None, int | None, Decimal | None]:
    if not all("share_validity_ok" in row for row in rows):
        return "not_available", None, None, None

    valid_rows = 0
    invalid_rows = 0
    shares: list[Decimal] = []
    for row in rows:
        if _parse_bool(row["share_validity_ok"], "share_validity_ok"):
            valid_rows += 1
        else:
            invalid_rows += 1
        if "mean_share_diluted" in row and _clean_value(row["mean_share_diluted"]) != "":
            shares.append(_parse_decimal(row["mean_share_diluted"], "mean_share_diluted"))
    status = "valid" if invalid_rows == 0 else "invalid_rows_present"
    return status, valid_rows, invalid_rows, _mean(shares) if shares else None


def _neighbor_regime_delta_rows(
    window_rows: list[dict[str, str]],
    feature_by_window: dict[int, dict[str, float]],
    *,
    regime_fields: list[str],
) -> list[dict[str, object]]:
    selected_rows = _selected_window_rows(window_rows)
    rows: list[dict[str, object]] = []
    for previous, current in zip(selected_rows, selected_rows[1:]):
        previous_index = _parse_int_field(previous, "window_index")
        current_index = _parse_int_field(current, "window_index")
        deltas = _regime_deltas(
            feature_by_window,
            previous_index=previous_index,
            current_index=current_index,
            regime_fields=regime_fields,
        )
        for field, delta in deltas.items():
            rows.append(
                {
                    "from_window_index": previous_index,
                    "to_window_index": current_index,
                    "regime_field": field,
                    "delta": delta,
                    "regime_diagnostic_scope": REGIME_DIAGNOSTIC_SCOPE,
                }
            )
    return rows


def _regime_deltas(
    feature_by_window: dict[int, dict[str, float]],
    *,
    previous_index: int,
    current_index: int,
    regime_fields: Sequence[str],
) -> dict[str, float]:
    previous_features = _features_for_window(feature_by_window, previous_index)
    current_features = _features_for_window(feature_by_window, current_index)
    deltas: dict[str, float] = {}
    for field in regime_fields:
        if field not in previous_features or field not in current_features:
            continue
        deltas[field] = abs(float(current_features[field]) - float(previous_features[field]))
    if regime_fields and not deltas:
        raise ValueError(
            f"no common usable regime fields for windows {previous_index} and {current_index}"
        )
    return deltas


def _features_for_window(
    feature_by_window: dict[int, dict[str, float]],
    window_index: int,
) -> dict[str, float]:
    if window_index not in feature_by_window:
        raise ValueError(f"missing feature rows for window {window_index}")
    return feature_by_window[window_index]


def _max_regime_delta(deltas: dict[str, float]) -> tuple[str | None, float]:
    if not deltas:
        return None, 0.0
    field = max(deltas, key=lambda item: deltas[item])
    return field, deltas[field]


def _selected_window_rows(window_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[int, list[tuple[int, dict[str, str]]]] = {}
    for position, row in enumerate(window_rows):
        if _clean_value(row.get("skipped_reason", "")) != "":
            continue
        window_index = _parse_int_field(row, "window_index")
        grouped.setdefault(window_index, []).append((position, row))

    selected: list[dict[str, str]] = []
    for window_index in sorted(grouped):
        candidates = grouped[window_index]
        _, row = min(candidates, key=lambda item: _selection_key(item[0], item[1]))
        selected.append(row)
    return selected


def _selection_key(position: int, row: dict[str, str]) -> tuple[int, int, int]:
    return (
        _optional_int(row.get("train_rank")),
        _optional_int(row.get("validation_rank")),
        position,
    )


def _window_for_timestamp(
    bounds: list[WindowBounds],
    timestamp_ms: int,
) -> WindowBounds | None:
    for bound in bounds:
        # Feature tables add a millisecond sequence to rows sharing a block_time second.
        if bound.start_ms <= timestamp_ms <= bound.end_ms + 999:
            return bound
    return None


def _validate_window_rows(rows: list[dict[str, str]]) -> None:
    for index, row in enumerate(rows, start=1):
        for field in REQUIRED_WINDOW_FIELDS:
            if field not in row:
                raise ValueError(f"row {index} is missing required field {field}")
        for field in ("window_index", "window_start", "window_end"):
            if _clean_value(row[field]) == "":
                raise ValueError(f"row {index} has blank required field {field}")
        _parse_int_field(row, "window_index")
        _parse_timestamp_ms(row["window_start"])
        _parse_timestamp_ms(row["window_end"])
        if _clean_value(row.get("skipped_reason", "")) != "":
            continue
        for field in (
            "validation_net_return",
            "validation_max_drawdown",
            "validation_rebalance_count",
            "validation_total_fees",
            "validation_total_transaction_cost",
        ):
            if _clean_value(row[field]) == "":
                raise ValueError(f"row {index} has blank required field {field}")
        _parse_decimal_field(row, "validation_net_return")
        _parse_decimal_field(row, "validation_max_drawdown")
        _parse_decimal_field(row, "validation_rebalance_count")
        _parse_decimal_field(row, "validation_total_fees")
        _parse_decimal_field(row, "validation_total_transaction_cost")


def _require_fields(
    actual: Sequence[str],
    required: Sequence[str],
    *,
    source: str,
) -> None:
    missing = [field for field in required if field not in actual]
    if missing:
        raise ValueError(f"{source} is missing required fields: {', '.join(missing)}")


def _require_row_field(row: dict[str, str], field: str) -> None:
    if field not in row:
        raise ValueError(f"window row is missing parameter field {field}")


def _parse_decimal_field(row: dict[str, str], field: str) -> Decimal:
    _require_row_field(row, field)
    return _parse_decimal(row[field], field)


def _parse_decimal(raw: str, field: str) -> Decimal:
    value = _clean_value(raw)
    if value == "":
        raise ValueError(f"{field} must not be blank")
    return Decimal(value)


def _parse_int_field(row: dict[str, str], field: str) -> int:
    _require_row_field(row, field)
    return _parse_int(row[field], field)


def _parse_int(raw: str, field: str) -> int:
    value = _clean_value(raw)
    if value == "":
        raise ValueError(f"{field} must not be blank")
    return int(value)


def _parse_bool(raw: str, field: str) -> bool:
    value = _clean_value(raw)
    if value == "True":
        return True
    if value == "False":
        return False
    raise ValueError(f"{field} must be True or False")


def _optional_int(raw: str | None) -> int:
    if raw is None or _clean_value(raw) == "":
        return 1_000_000_000
    return int(raw)


def _optional_decimal(raw: str) -> Decimal | None:
    value = _clean_value(raw)
    if value == "":
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _parameter_delta(previous_value: str, current_value: str) -> float | None:
    previous_decimal = _optional_decimal(previous_value)
    current_decimal = _optional_decimal(current_value)
    if previous_decimal is None or current_decimal is None:
        return None
    return float((current_decimal - previous_decimal).copy_abs())


def _format_parameter_value(value: str) -> str:
    decimal_value = _optional_decimal(value)
    if decimal_value is None:
        return value
    return _format_decimal(decimal_value)


def _parse_timestamp_ms(raw: str) -> int:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("window timestamps must include timezone")
    return int(parsed.timestamp() * 1000)


def _row_apy(row: dict[str, str]) -> Decimal:
    if "validation_apy" in row and _clean_value(row["validation_apy"]) != "":
        return _parse_decimal(row["validation_apy"], "validation_apy")
    net_return = _parse_decimal_field(row, "validation_net_return")
    elapsed_seconds = _row_elapsed_seconds(row)
    if elapsed_seconds <= 0:
        raise ValueError("window_end must be after window_start")
    if net_return <= Decimal("-1"):
        return Decimal("-1")
    exponent = float(SECONDS_PER_YEAR / elapsed_seconds) * math.log1p(float(net_return))
    if exponent > 700:
        return Decimal("Infinity")
    return Decimal(str(math.expm1(exponent)))


def _row_sgho_hurdle_return(row: dict[str, str], sgho_apy: Decimal) -> Decimal:
    elapsed_seconds = _row_elapsed_seconds(row)
    if elapsed_seconds <= 0:
        raise ValueError("window_end must be after window_start")
    exponent = float(elapsed_seconds / SECONDS_PER_YEAR) * math.log1p(float(sgho_apy))
    return Decimal(str(math.expm1(exponent)))


def _row_elapsed_seconds(row: dict[str, str]) -> Decimal:
    start_ms = _parse_timestamp_ms(row["window_start"])
    end_ms = _parse_timestamp_ms(row["window_end"])
    return Decimal(end_ms - start_ms) / Decimal("1000")


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("cannot compute mean of empty values")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _clean_value(raw: object) -> str:
    return "" if raw is None else str(raw).strip()


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "not_available"
    if value.is_infinite():
        return "Infinity"
    with localcontext() as context:
        context.prec = 28
        rounded = +value
    return format(rounded.normalize(), "f")


def _format_number(value: object) -> str:
    if value is None:
        return "not_available"
    if isinstance(value, Decimal):
        return _format_decimal(value)
    if isinstance(value, float):
        return _format_decimal(Decimal(str(value)))
    if isinstance(value, int):
        return str(value)
    return str(value)


def _format_percent(value: Decimal) -> str:
    return f"{_format_decimal(value * Decimal('100'))}%"


def _format_optional_int(value: int | None) -> str:
    return "not_available" if value is None else str(value)


def _parse_csv_list(value: str) -> list[str]:
    fields = [field.strip() for field in value.split(",") if field.strip()]
    if not fields:
        raise ValueError("field list must not be empty")
    return fields


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", required=True, type=Path)
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--parameter-fields",
        default=",".join(DEFAULT_PARAMETER_FIELDS),
        help="Comma-separated selected parameter fields to compare between windows.",
    )
    parser.add_argument(
        "--regime-fields",
        default=",".join(DEFAULT_REGIME_FIELDS),
        help="Comma-separated feature fields used to explain parameter jumps.",
    )
    parser.add_argument("--min-regime-delta", type=float, default=0.20)
    parser.add_argument("--sgho-apy", default=str(SGHO_APY))
    args = parser.parse_args(argv)

    parameter_fields = _parse_csv_list(args.parameter_fields)
    regime_fields = _parse_csv_list(args.regime_fields)
    sgho_apy = Decimal(str(args.sgho_apy))
    if sgho_apy < 0:
        raise ValueError("--sgho-apy must not be negative")

    window_rows = load_window_rows(args.windows)
    feature_by_window = load_feature_by_window(
        args.features,
        window_rows,
        regime_fields=regime_fields,
    )
    report = build_stability_report(
        window_rows,
        feature_by_window,
        parameter_fields=parameter_fields,
        regime_fields=regime_fields,
        min_regime_delta=args.min_regime_delta,
        sgho_apy=sgho_apy,
    )
    markdown = render_stability_markdown(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(markdown)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
