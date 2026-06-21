"""Report read-only quality metrics for normalized Uniswap v4 pool history CSVs."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtester.pool_price_semantics import classify_pool_price_row


_EXPECTED_TOKEN_ORDER = {
    "uni-base": ("CNGN", "USDC"),
    "uni-bsc": ("USDT", "CNGN"),
}

@dataclass(frozen=True)
class PoolHistoryQualityReport:
    pool: str
    row_count: int
    event_counts: dict[str, int]
    first_block: int | None
    last_block: int | None
    first_time: datetime | None
    last_time: datetime | None
    duplicate_events: int
    monotonic_blocks: bool
    token_order_valid: bool
    sqrt_price_mismatch_count: int
    legacy_amount_ratio_price_count: int
    unexplained_price_mismatch_count: int
    stored_price_model_counts: dict[str, int]
    legacy_amount_ratio_first_block: int | None
    legacy_amount_ratio_last_block: int | None
    sqrt_mid_first_block: int | None
    missing_active_liquidity_swaps: int
    coverage_days: float


def analyze_pool_history(csv_path: Path, pool: str) -> PoolHistoryQualityReport:
    expected_order = _EXPECTED_TOKEN_ORDER.get(pool)
    if expected_order is None:
        raise ValueError(f"Unsupported pool {pool!r}")

    row_count = 0
    event_counts: Counter[str] = Counter()
    first_block: int | None = None
    last_block: int | None = None
    first_time: datetime | None = None
    last_time: datetime | None = None
    previous_block: int | None = None
    monotonic_blocks = True
    seen_events: set[tuple[str, str]] = set()
    duplicate_events = 0
    token_order_valid = True
    sqrt_price_mismatch_count = 0
    legacy_amount_ratio_price_count = 0
    unexplained_price_mismatch_count = 0
    stored_price_model_counts: Counter[str] = Counter()
    legacy_amount_ratio_first_block: int | None = None
    legacy_amount_ratio_last_block: int | None = None
    sqrt_mid_first_block: int | None = None
    missing_active_liquidity_swaps = 0

    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_count += 1
            event_type = row["event_type"].strip().lower()
            event_counts[event_type] += 1

            block_number = int(row["block_number"])
            block_time = datetime.fromisoformat(row["block_time"])
            event_id = (
                row["tx_hash"].strip().lower(),
                row["log_index"].strip(),
            )
            if event_id in seen_events:
                duplicate_events += 1
            seen_events.add(event_id)

            if first_block is None:
                first_block = block_number
                first_time = block_time
            last_block = block_number
            last_time = block_time

            if previous_block is not None and block_number < previous_block:
                monotonic_blocks = False
            previous_block = block_number

            token_order = (
                row["token0_symbol"].strip().upper(),
                row["token1_symbol"].strip().upper(),
            )
            if token_order != expected_order:
                token_order_valid = False

            if event_type == "swap" and _missing_active_liquidity(row):
                missing_active_liquidity_swaps += 1

            price_semantics = classify_pool_price_row(row)
            stored_price_model_counts[price_semantics.stored_price_model] += 1
            if price_semantics.stored_price_model != "sqrt_mid":
                sqrt_price_mismatch_count += 1
            if price_semantics.stored_price_model == "swap_amount_ratio":
                legacy_amount_ratio_price_count += 1
                if legacy_amount_ratio_first_block is None:
                    legacy_amount_ratio_first_block = block_number
                legacy_amount_ratio_last_block = block_number
            elif price_semantics.stored_price_model == "sqrt_mid" and event_type == "swap":
                if sqrt_mid_first_block is None:
                    sqrt_mid_first_block = block_number
            elif price_semantics.stored_price_model == "unexplained":
                unexplained_price_mismatch_count += 1

    coverage_days = 0.0
    if first_time is not None and last_time is not None:
        coverage_days = (last_time - first_time).total_seconds() / 86_400

    return PoolHistoryQualityReport(
        pool=pool,
        row_count=row_count,
        event_counts=dict(event_counts),
        first_block=first_block,
        last_block=last_block,
        first_time=first_time,
        last_time=last_time,
        duplicate_events=duplicate_events,
        monotonic_blocks=monotonic_blocks,
        token_order_valid=token_order_valid,
        sqrt_price_mismatch_count=sqrt_price_mismatch_count,
        legacy_amount_ratio_price_count=legacy_amount_ratio_price_count,
        unexplained_price_mismatch_count=unexplained_price_mismatch_count,
        stored_price_model_counts=dict(stored_price_model_counts),
        legacy_amount_ratio_first_block=legacy_amount_ratio_first_block,
        legacy_amount_ratio_last_block=legacy_amount_ratio_last_block,
        sqrt_mid_first_block=sqrt_mid_first_block,
        missing_active_liquidity_swaps=missing_active_liquidity_swaps,
        coverage_days=coverage_days,
    )


def render_pool_history_quality_markdown(report: PoolHistoryQualityReport) -> str:
    lines = [
        "# Pool History Quality Report",
        "",
        "| pool | rows | first_block | last_block | monotonic_blocks | "
        "token_order_valid | duplicate_events | sqrt_price_mismatch_count | "
        "legacy_amount_ratio_price_count | unexplained_price_mismatch_count | "
        "missing_active_liquidity_swaps | coverage_days |",
        "|---|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|",
        f"| {report.pool} | {report.row_count} | {_format_optional_int(report.first_block)} | "
        f"{_format_optional_int(report.last_block)} | {report.monotonic_blocks} | "
        f"{report.token_order_valid} | {report.duplicate_events} | "
        f"{report.sqrt_price_mismatch_count} | "
        f"{report.legacy_amount_ratio_price_count} | "
        f"{report.unexplained_price_mismatch_count} | "
        f"{report.missing_active_liquidity_swaps} | {report.coverage_days:.6f} |",
        "",
        "## Stored Price Models",
        "",
        "| stored_price_model | count |",
        "|---|---:|",
    ]
    for model, count in sorted(report.stored_price_model_counts.items()):
        lines.append(f"| {model} | {count} |")
    lines.extend(
        [
            "",
            "## Stored Price Model Boundaries",
            "",
            f"Legacy amount-ratio first block: {_format_optional_int(report.legacy_amount_ratio_first_block)}",
            f"Legacy amount-ratio last block: {_format_optional_int(report.legacy_amount_ratio_last_block)}",
            f"First sqrt-mid stored swap block: {_format_optional_int(report.sqrt_mid_first_block)}",
            "",
        ]
    )
    lines.extend(
        [
            "",
            "## Event Counts",
            "",
            "| event_type | count |",
            "|---|---:|",
        ]
    )
    for event_type, count in sorted(report.event_counts.items()):
        lines.append(f"| {event_type} | {count} |")
    return "\n".join(lines).rstrip() + "\n"


def _missing_active_liquidity(row: dict[str, str]) -> bool:
    raw = row["active_liquidity"].strip()
    return raw == "" or int(raw) <= 0


def _format_optional_int(value: int | None) -> str:
    return "" if value is None else str(value)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Report read-only quality metrics for a v4 pool history CSV."
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    report = analyze_pool_history(args.csv, args.pool)
    markdown = render_pool_history_quality_markdown(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(markdown)
    if report.unexplained_price_mismatch_count:
        raise SystemExit(
            f"unexplained stored price mismatches: {report.unexplained_price_mismatch_count}"
        )


if __name__ == "__main__":
    _main()
