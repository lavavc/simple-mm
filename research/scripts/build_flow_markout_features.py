"""Build causal QTS flow and markout features from DEX swap history."""

from __future__ import annotations

import argparse
import csv
from collections import deque
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Sequence

from research.backtester.pool_features import derive_swap_flow

DEFAULT_TAUS = (20, 50, 100)
DEFAULT_HORIZONS = (10, 25, 50)


def build_flow_markout_rows(
    *,
    history_rows: Sequence[dict[str, str]],
    feature_rows: Sequence[dict[str, str]],
    taus: Sequence[int],
    horizons: Sequence[int],
    ew_lambda: Decimal,
) -> list[dict[str, str]]:
    _validate_positive_ints(taus, "taus")
    _validate_positive_ints(horizons, "horizons")
    if ew_lambda < 0 or ew_lambda >= 1:
        raise ValueError("ew_lambda must be in [0, 1)")

    merged_rows = _merged_swap_rows(history_rows, feature_rows)
    prices = [_required_decimal(row, "raw_sqrt_mid") for row in merged_rows]
    signed_cngn: list[Decimal] = []
    signed_usd: list[Decimal] = []
    for row in merged_rows:
        flow = derive_swap_flow(row)
        signed_cngn.append(flow.signed_cngn_amount)
        signed_usd.append(flow.signed_usd_notional)

    flow_cngn = {tau: _rolling_pretrade_sum(signed_cngn, tau) for tau in taus}
    flow_usd = {tau: _rolling_pretrade_sum(signed_usd, tau) for tau in taus}
    flow_usd_z = {tau: _expanding_z_scores(flow_usd[tau]) for tau in taus}
    markouts = {horizon: _forward_markouts(prices, horizon) for horizon in horizons}
    betas, predicted = _adaptive_betas(
        flow_usd=flow_usd,
        markouts=markouts,
        taus=taus,
        horizons=horizons,
        ew_lambda=ew_lambda,
    )

    output: list[dict[str, str]] = []
    for index, row in enumerate(merged_rows):
        out = {
            "timestamp_ms": row["timestamp_ms"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "raw_sqrt_mid": row["raw_sqrt_mid"],
            "signed_cngn_amount": _format_fixed(signed_cngn[index]),
            "signed_usd_notional": _format_fixed(signed_usd[index]),
        }
        for tau in taus:
            out[f"F_{tau}_signed_cngn"] = _format_fixed(flow_cngn[tau][index])
            out[f"F_{tau}_signed_usd"] = _format_fixed(flow_usd[tau][index])
            out[f"F_{tau}_signed_usd_z"] = _format_optional(flow_usd_z[tau][index])
        for horizon in horizons:
            out[f"markout_{horizon}_raw_sqrt_mid_return"] = _format_optional(
                markouts[horizon][index]
            )
        for tau in taus:
            for horizon in horizons:
                out[f"beta_{tau}_{horizon}_ew"] = _format_optional(betas[(tau, horizon)][index])
                out[f"predicted_markout_{tau}_{horizon}"] = _format_optional(
                    predicted[(tau, horizon)][index]
                )
        output.append(out)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    rows = build_flow_markout_rows(
        history_rows=_read_csv(Path(args.history)),
        feature_rows=_read_csv(Path(args.features)),
        taus=tuple(_parse_int_list(args.taus)),
        horizons=tuple(_parse_int_list(args.horizons)),
        ew_lambda=Decimal(args.ew_lambda),
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(out_path, rows)
    print(f"wrote {len(rows)} rows to {out_path}")
    return 0


def _merged_swap_rows(
    history_rows: Sequence[dict[str, str]],
    feature_rows: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    feature_by_key = {_row_key(row): row for row in feature_rows}
    if len(feature_by_key) != len(feature_rows):
        raise ValueError("duplicate feature row key")

    merged: list[dict[str, str]] = []
    for history_row in history_rows:
        if history_row.get("event_type") != "swap":
            continue
        key = _row_key(history_row)
        feature_row = feature_by_key.get(key)
        if feature_row is None:
            raise ValueError(f"missing feature row for swap {key}")
        merged.append({**history_row, **feature_row})
    return merged


def _row_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (
        _required_clean(row, "tx_hash"),
        _required_clean(row, "log_index"),
        _required_clean(row, "block_number"),
    )


def _rolling_pretrade_sum(values: Sequence[Decimal], lookback: int) -> list[Decimal]:
    result: list[Decimal] = []
    total = Decimal("0")
    window: deque[Decimal] = deque()
    for value in values:
        result.append(total)
        window.append(value)
        total += value
        if len(window) > lookback:
            total -= window.popleft()
    return result


def _expanding_z_scores(values: Sequence[Decimal]) -> list[Decimal | None]:
    result: list[Decimal | None] = []
    seen: list[Decimal] = []
    for value in values:
        if len(seen) < 2:
            result.append(None)
        else:
            mean = sum(seen, Decimal("0")) / Decimal(len(seen))
            variance = sum((item - mean) * (item - mean) for item in seen) / Decimal(len(seen))
            if variance == 0:
                result.append(None)
            else:
                with localcontext() as context:
                    context.prec = 60
                    result.append((value - mean) / variance.sqrt())
        seen.append(value)
    return result


def _forward_markouts(prices: Sequence[Decimal], horizon: int) -> list[Decimal | None]:
    result: list[Decimal | None] = []
    for index, price in enumerate(prices):
        target_index = index + horizon
        if target_index >= len(prices):
            result.append(None)
            continue
        target = prices[target_index]
        if price <= 0 or target <= 0:
            raise ValueError("raw_sqrt_mid must be positive")
        with localcontext() as context:
            context.prec = 60
            result.append(target / price - Decimal("1"))
    return result


def _adaptive_betas(
    *,
    flow_usd: dict[int, list[Decimal]],
    markouts: dict[int, list[Decimal | None]],
    taus: Sequence[int],
    horizons: Sequence[int],
    ew_lambda: Decimal,
) -> tuple[
    dict[tuple[int, int], list[Decimal | None]],
    dict[tuple[int, int], list[Decimal | None]],
]:
    betas: dict[tuple[int, int], list[Decimal | None]] = {}
    predicted: dict[tuple[int, int], list[Decimal | None]] = {}
    row_count = len(next(iter(flow_usd.values()))) if flow_usd else 0

    for tau in taus:
        for horizon in horizons:
            key = (tau, horizon)
            numerator = Decimal("0")
            denominator = Decimal("0")
            beta_rows: list[Decimal | None] = []
            predicted_rows: list[Decimal | None] = []
            flows = flow_usd[tau]
            labels = markouts[horizon]
            for index in range(row_count):
                matured_index = index - horizon
                if matured_index >= 0 and labels[matured_index] is not None:
                    matured_flow = flows[matured_index]
                    matured_markout = labels[matured_index]
                    assert matured_markout is not None
                    numerator = ew_lambda * numerator + matured_flow * matured_markout
                    denominator = ew_lambda * denominator + matured_flow * matured_flow

                if denominator == 0:
                    beta_rows.append(None)
                    predicted_rows.append(None)
                else:
                    beta = numerator / denominator
                    beta_rows.append(beta)
                    predicted_rows.append(beta * flows[index])
            betas[key] = beta_rows
            predicted[key] = predicted_rows
    return betas, predicted


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


def _required_clean(row: dict[str, str], field: str) -> str:
    value = str(row.get(field, "")).strip()
    if value == "":
        raise ValueError(f"missing required field {field}")
    return value


def _required_decimal(row: dict[str, str], field: str) -> Decimal:
    value = _required_clean(row, field)
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal field {field}: {value}") from exc


def _format_fixed(value: Decimal) -> str:
    return f"{value:.6f}"


def _format_optional(value: Decimal | None) -> str:
    if value is None:
        return ""
    return _format_fixed(value)


def _validate_positive_ints(values: Sequence[int], name: str) -> None:
    if not values:
        raise ValueError(f"{name} must not be empty")
    if any(value <= 0 for value in values):
        raise ValueError(f"{name} must be positive")


def _parse_int_list(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--taus", default=",".join(str(value) for value in DEFAULT_TAUS))
    parser.add_argument("--horizons", default=",".join(str(value) for value in DEFAULT_HORIZONS))
    parser.add_argument("--ew-lambda", default="0.94")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
