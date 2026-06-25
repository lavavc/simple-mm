"""Report calendar stress slices from causal pool feature tables."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

DEFAULT_STRESS_FIELDS = (
    "realized_volatility_cone_pct",
    "dex_premium_cone_pct",
    "active_liquidity_cone_pct",
    "active_liquidity_running_max_share_cone_pct",
    "swap_flow_imbalance_cone_pct",
    "fee_intensity_proxy_cone_pct",
    "volume_cone_pct",
)
LOW_STRESS_THRESHOLD = Decimal("0.10")
HIGH_STRESS_THRESHOLD = Decimal("0.90")


@dataclass
class StressCounts:
    low_count: int = 0
    normal_count: int = 0
    high_count: int = 0
    missing_count: int = 0

    @property
    def total_count(self) -> int:
        return self.low_count + self.normal_count + self.high_count + self.missing_count


def load_stress_slice_counts(
    features_path: Path,
    *,
    fields: Sequence[str],
) -> dict[tuple[str, str, str], StressCounts]:
    with features_path.open(newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError(f"{features_path} is missing a CSV header")
        _require_fields(reader.fieldnames, ("timestamp_ms", "pool", *fields))

        counts: dict[tuple[str, str, str], StressCounts] = {}
        for row in reader:
            date = _utc_date(row["timestamp_ms"])
            pool = row["pool"].strip()
            if pool == "":
                raise ValueError("pool must not be blank")
            for field in fields:
                key = (date, pool, field)
                bucket = _stress_bucket(row[field], field)
                field_counts = counts.setdefault(key, StressCounts())
                if bucket == "low":
                    field_counts.low_count += 1
                elif bucket == "normal":
                    field_counts.normal_count += 1
                elif bucket == "high":
                    field_counts.high_count += 1
                elif bucket == "missing":
                    field_counts.missing_count += 1
                else:
                    raise ValueError(f"unsupported stress bucket {bucket!r}")
    return counts


def render_stress_slice_markdown(
    counts: dict[tuple[str, str, str], StressCounts],
) -> str:
    lines = [
        "# Pool Feature Stress Slices",
        "",
        (
            "Buckets use causal cone percentiles: low <= 0.10, "
            "normal (0.10, 0.90), high >= 0.90."
        ),
        "",
        (
            "| date | pool | feature | low_count | normal_count | high_count | "
            "missing_count | total_count |"
        ),
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for date, pool, field in sorted(counts):
        field_counts = counts[(date, pool, field)]
        lines.append(
            f"| {date} | {pool} | {field} | {field_counts.low_count} | "
            f"{field_counts.normal_count} | {field_counts.high_count} | "
            f"{field_counts.missing_count} | {field_counts.total_count} |"
        )
    if not counts:
        lines.append("| not_available | not_available | not_available | 0 | 0 | 0 | 0 | 0 |")
    return "\n".join(lines).rstrip() + "\n"


def _require_fields(actual: Sequence[str], required: Sequence[str]) -> None:
    missing = [field for field in required if field not in actual]
    if missing:
        raise ValueError(f"feature CSV is missing required fields: {', '.join(missing)}")


def _utc_date(raw_timestamp_ms: str) -> str:
    timestamp_ms = int(raw_timestamp_ms.strip())
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).date().isoformat()


def _stress_bucket(raw: str, field: str) -> str:
    cleaned = raw.strip()
    if cleaned == "":
        return "missing"
    percentile = Decimal(cleaned)
    if percentile < 0 or percentile > 1:
        raise ValueError(f"{field} must be between 0 and 1")
    if percentile <= LOW_STRESS_THRESHOLD:
        return "low"
    if percentile >= HIGH_STRESS_THRESHOLD:
        return "high"
    return "normal"


def _parse_fields(raw: str) -> tuple[str, ...]:
    fields = tuple(field.strip() for field in raw.split(",") if field.strip())
    if not fields:
        raise ValueError("--fields must not be empty")
    return fields


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--fields",
        default=",".join(DEFAULT_STRESS_FIELDS),
        help="Comma-separated cone percentile fields to slice.",
    )
    args = parser.parse_args(argv)

    counts = load_stress_slice_counts(args.features, fields=_parse_fields(args.fields))
    markdown = render_stress_slice_markdown(counts)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(markdown)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
