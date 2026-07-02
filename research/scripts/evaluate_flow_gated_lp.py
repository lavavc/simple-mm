"""Evaluate causal flow-gated DEX LP walk-forward windows."""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

ENTRY_FEATURE_FIELDS = (
    "swap_flow_imbalance_cone_pct_1h",
    "swap_flow_imbalance",
    "realized_volatility_cone_pct_1h",
    "active_liquidity_cone_pct_1h",
    "active_liquidity_running_max_share_cone_pct_1h",
    "fee_intensity_proxy_cone_pct_1h",
    "volume_cone_pct_1h",
)

DEFAULT_PARAMETER_FIELDS = (
    "center_mode",
    "fixed_width_pct",
    "harvest_upward_range_fraction",
    "profit_take_return",
    "stop_loss_return",
    "out_of_range_overshoot_fraction",
    "exit_confirmation_swaps",
    "exit_price_mode",
    "initial_capital_usd",
    "mint_gas_usd",
    "remove_gas_usd",
)


def build_entry_states(
    feature_rows: Sequence[dict[str, str]],
    *,
    train_swaps: int,
    val_swaps: int,
    stride_swaps: int,
    flow_threshold: Decimal,
    train_return_max: Decimal,
    max_windows: int | None = None,
) -> list[dict[str, str]]:
    if train_swaps <= 0 or val_swaps <= 0 or stride_swaps <= 0:
        raise ValueError("swap counts must be positive")

    required = train_swaps + val_swaps
    rows: list[dict[str, str]] = []
    cursor = 0
    window_index = 0
    while cursor + required <= len(feature_rows):
        train_start = feature_rows[cursor]
        entry = feature_rows[cursor + train_swaps - 1]
        validation_start = feature_rows[cursor + train_swaps]
        validation_end = feature_rows[cursor + required - 1]

        train_price_return = _price_return(train_start, entry)
        validation_price_return = _price_return(validation_start, validation_end)
        entry_flow_pct = _optional_decimal(entry["swap_flow_imbalance_cone_pct_1h"])
        gate_active = (
            entry_flow_pct is not None
            and entry_flow_pct >= flow_threshold
            and train_price_return <= train_return_max
        )

        row = {
            "window_index": str(window_index),
            "train_start_timestamp_ms": _required_clean(train_start, "timestamp_ms"),
            "entry_timestamp_ms": _required_clean(entry, "timestamp_ms"),
            "validation_start_timestamp_ms": _required_clean(validation_start, "timestamp_ms"),
            "validation_end_timestamp_ms": _required_clean(validation_end, "timestamp_ms"),
            "train_price_return": _format_decimal(train_price_return),
            "validation_price_return": _format_decimal(validation_price_return),
            "gate_active": "1" if gate_active else "0",
        }
        for field in ENTRY_FEATURE_FIELDS:
            row[f"entry_{field}"] = _clean(entry.get(field, ""))
        row["entry_flow_pct"] = row["entry_swap_flow_imbalance_cone_pct_1h"]
        row["entry_flow_raw"] = row["entry_swap_flow_imbalance"]
        rows.append(row)

        window_index += 1
        if max_windows is not None and len(rows) >= max_windows:
            break
        cursor += stride_swaps
    return rows


def summarize_selected_rank1(
    window_rows: Sequence[dict[str, str]],
    entry_states: Sequence[dict[str, str]],
) -> dict[str, dict[str, str]]:
    state_by_window = _entry_state_by_window(entry_states)
    all_window_count = len(state_by_window)
    selected = []
    for row in window_rows:
        if _clean(row.get("train_rank", "")) != "1":
            continue
        window_index = _required_clean(row, "window_index")
        state = _require_entry_state(state_by_window, window_index)
        selected.append((row, state))

    return {
        "all": _summary_for_pairs(selected, all_window_count=all_window_count),
        "gated": _summary_for_pairs(
            [(row, state) for row, state in selected if state["gate_active"] == "1"],
            all_window_count=all_window_count,
        ),
        "non_gated": _summary_for_pairs(
            [(row, state) for row, state in selected if state["gate_active"] != "1"],
            all_window_count=all_window_count,
        ),
    }


def summarize_candidate_family(
    matrix_rows: Sequence[dict[str, str]],
    entry_states: Sequence[dict[str, str]],
    *,
    parameter_fields: Sequence[str],
    gated: bool,
) -> list[dict[str, str]]:
    state_by_window = _entry_state_by_window(entry_states)
    total_window_count = len(state_by_window)
    grouped: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    key_values: dict[tuple[str, ...], dict[str, str]] = {}

    for row in matrix_rows:
        window_index = _required_clean(row, "window_index")
        state = _require_entry_state(state_by_window, window_index)
        key = tuple(_clean(row.get(field, "")) for field in parameter_fields)
        key_values[key] = {field: key[index] for index, field in enumerate(parameter_fields)}
        if gated and state["gate_active"] != "1":
            continue
        grouped[key].append(row)

    rows: list[dict[str, str]] = []
    for key, values in grouped.items():
        summary = _summary_for_rows(values, all_window_count=total_window_count)
        rows.append({**key_values[key], **summary})

    rows.sort(
        key=lambda row: (
            _sort_decimal(row["sum_active_return"]),
            _sort_decimal(row["worst_active_return"]),
        ),
        reverse=True,
    )
    return rows


def render_markdown(
    *,
    pool: str,
    entry_states: Sequence[dict[str, str]],
    selected_summary: dict[str, dict[str, str]],
    gated_family_rows: Sequence[dict[str, str]],
    ungated_family_rows: Sequence[dict[str, str]],
) -> str:
    active_windows = [row["window_index"] for row in entry_states if row["gate_active"] == "1"]
    lines = [
        f"# Flow-Gated LP Audit: {pool}",
        "",
        "## Entry Gate",
        "",
        f"- windows: {len(entry_states)}",
        f"- active gate windows: {len(active_windows)}",
        f"- active window indexes: {', '.join(active_windows) if active_windows else 'none'}",
        "",
        "## Selected Rank-1 Stream",
        "",
        "| section | windows | sum_return | mean_return | worst_return | "
        "positive_window_rate | fee_cost_ratio | total_rebalances |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("all", "gated", "non_gated"):
        summary = selected_summary[name]
        lines.append(
            "| "
            + " | ".join(
                [
                    name,
                    summary["windows"],
                    summary["sum_return"],
                    summary["mean_return"],
                    summary["worst_return"],
                    summary["positive_window_rate"],
                    summary["fee_cost_ratio"],
                    summary["total_rebalances"],
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Top Gated Fixed-Family Rows",
            "",
            _family_table(gated_family_rows[:10]),
            "",
            "## Top Ungated Fixed-Family Rows",
            "",
            _family_table(ungated_family_rows[:10]),
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    feature_rows = _read_csv(Path(args.features))
    window_rows = _read_csv(Path(args.windows))
    matrix_rows = _read_csv(Path(args.matrix))
    parameter_fields = tuple(_split_fields(args.parameter_fields))

    entry_states = build_entry_states(
        feature_rows,
        train_swaps=args.train_swaps,
        val_swaps=args.val_swaps,
        stride_swaps=args.stride_swaps,
        flow_threshold=Decimal(args.flow_threshold),
        train_return_max=Decimal(args.train_return_max),
        max_windows=args.max_windows,
    )
    selected_summary = summarize_selected_rank1(window_rows, entry_states)
    ungated_family = summarize_candidate_family(
        matrix_rows,
        entry_states,
        parameter_fields=parameter_fields,
        gated=False,
    )
    gated_family = summarize_candidate_family(
        matrix_rows,
        entry_states,
        parameter_fields=parameter_fields,
        gated=True,
    )

    _write_csv(out_dir / "entry_state_windows.csv", entry_states)
    _write_csv(
        out_dir / "selected_rank1_gate_summary.csv",
        [{"section": section, **summary} for section, summary in selected_summary.items()],
    )
    _write_csv(
        out_dir / "fixed_family_gate_summary.csv",
        [{"gate_mode": "gated", **row} for row in gated_family]
        + [{"gate_mode": "ungated", **row} for row in ungated_family],
    )
    report = render_markdown(
        pool=args.pool,
        entry_states=entry_states,
        selected_summary=selected_summary,
        gated_family_rows=gated_family,
        ungated_family_rows=ungated_family,
    )
    (out_dir / "flow_gated_lp_report.md").write_text(report)
    print(f"wrote {out_dir}")
    return 0


def _summary_for_pairs(
    pairs: Sequence[tuple[dict[str, str], dict[str, str]]],
    *,
    all_window_count: int,
) -> dict[str, str]:
    return _summary_for_rows([row for row, _state in pairs], all_window_count=all_window_count)


def _summary_for_rows(rows: Sequence[dict[str, str]], *, all_window_count: int) -> dict[str, str]:
    returns = [_required_decimal(row, "validation_net_return") for row in rows]
    fees = [_required_decimal(row, "validation_total_fees") for row in rows]
    tx_costs = [_required_decimal(row, "validation_total_transaction_cost") for row in rows]
    rebalances = [_required_decimal(row, "validation_rebalance_count") for row in rows]
    drawdowns = [_required_decimal(row, "validation_max_drawdown") for row in rows]

    active_windows = len(rows)
    total_return = sum(returns, Decimal("0"))
    total_fees = sum(fees, Decimal("0"))
    total_tx_cost = sum(tx_costs, Decimal("0"))
    total_rebalances = sum(rebalances, Decimal("0"))
    return {
        "active_windows": str(active_windows),
        "windows": str(active_windows),
        "sum_active_return": _format_fixed(total_return),
        "sum_return": _format_fixed(total_return),
        "mean_active_return": _format_fixed(_mean(returns)),
        "mean_return": _format_fixed(_mean(returns)),
        "median_return": _format_fixed(_median(returns)),
        "worst_active_return": _format_fixed(min(returns) if returns else None),
        "worst_return": _format_fixed(min(returns) if returns else None),
        "positive_active_rate": _format_fixed(_positive_rate(returns)),
        "positive_window_rate": _format_fixed(_positive_rate(returns)),
        "mean_all_window_return": _format_fixed(
            total_return / Decimal(all_window_count) if all_window_count > 0 else None
        ),
        "total_fees": _format_fixed(total_fees),
        "total_transaction_cost": _format_fixed(total_tx_cost),
        "fee_cost_ratio": _format_fixed(
            total_fees / total_tx_cost if total_tx_cost != 0 else None
        ),
        "total_rebalances": _format_fixed(total_rebalances),
        "worst_drawdown": _format_fixed(max(drawdowns) if drawdowns else None),
    }


def _family_table(rows: Sequence[dict[str, str]]) -> str:
    if not rows:
        return "none"
    fields = [
        "center_mode",
        "fixed_width_pct",
        "active_windows",
        "sum_active_return",
        "mean_active_return",
        "worst_active_return",
        "positive_active_rate",
        "fee_cost_ratio",
        "total_rebalances",
    ]
    lines = [
        "| " + " | ".join(fields) + " |",
        "| " + " | ".join("---" for _ in fields) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row.get(field, "") for field in fields) + " |")
    return "\n".join(lines)


def _entry_state_by_window(entry_states: Sequence[dict[str, str]]) -> dict[str, dict[str, str]]:
    states = {_required_clean(row, "window_index"): row for row in entry_states}
    if len(states) != len(entry_states):
        raise ValueError("duplicate entry state window_index")
    return states


def _require_entry_state(
    state_by_window: dict[str, dict[str, str]],
    window_index: str,
) -> dict[str, str]:
    state = state_by_window.get(window_index)
    if state is None:
        raise ValueError(f"missing entry state for window {window_index}")
    return state


def _price_return(start_row: dict[str, str], end_row: dict[str, str]) -> Decimal:
    start = _required_decimal(start_row, "raw_sqrt_mid")
    end = _required_decimal(end_row, "raw_sqrt_mid")
    if start <= 0 or end <= 0:
        raise ValueError("raw_sqrt_mid must be positive")
    return end / start - Decimal("1")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a CSV header")
        return [dict(row) for row in reader]


def _write_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _split_fields(value: str) -> list[str]:
    fields = [_clean(field) for field in value.split(",")]
    return [field for field in fields if field]


def _required_clean(row: dict[str, str], field: str) -> str:
    value = _clean(row.get(field, ""))
    if value == "":
        raise ValueError(f"missing required field {field}")
    return value


def _clean(value: str | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _required_decimal(row: dict[str, str], field: str) -> Decimal:
    value = _optional_decimal(row.get(field, ""))
    if value is None:
        raise ValueError(f"missing required decimal field {field}")
    return value


def _optional_decimal(value: str | None) -> Decimal | None:
    cleaned = _clean(value)
    if cleaned == "":
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value {cleaned}") from exc


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(str(statistics.median(values)))


def _positive_rate(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(sum(1 for value in values if value > 0)) / Decimal(len(values))


def _format_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _format_fixed(value: Decimal | None) -> str:
    if value is None:
        return "not_available"
    return f"{value:.6f}"


def _sort_decimal(value: str) -> Decimal:
    if value == "not_available":
        return Decimal("-Infinity")
    return Decimal(value)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", required=True)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--train-swaps", required=True, type=int)
    parser.add_argument("--val-swaps", required=True, type=int)
    parser.add_argument("--stride-swaps", required=True, type=int)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--flow-threshold", default="0.90")
    parser.add_argument("--train-return-max", default="0")
    parser.add_argument("--max-windows", type=int)
    parser.add_argument("--parameter-fields", default=",".join(DEFAULT_PARAMETER_FIELDS))
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
