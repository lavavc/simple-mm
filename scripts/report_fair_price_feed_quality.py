"""Report raw fair-price feed quality from persisted price snapshots."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path

from scripts.export_fair_price_markouts import (
    PriceSnapshot,
    _quidax_executable_prices,
    load_price_snapshots,
)


@dataclass(frozen=True)
class SourceFeedQuality:
    source: str
    observations: int
    first_timestamp_ms: int | None
    last_timestamp_ms: int | None
    median_gap_ms: Decimal | None
    max_gap_ms: Decimal | None
    metadata_observations: int
    missing_metadata_count: int
    executable_depth_observations: int
    missing_executable_depth_count: int
    unique_mid_count: int


@dataclass(frozen=True)
class FeedQualityReport:
    target_usd: Decimal
    sources: dict[str, SourceFeedQuality]


def analyze_feed_quality(
    snapshots: list[PriceSnapshot],
    *,
    target_usd: Decimal,
) -> FeedQualityReport:
    source_names = sorted({snapshot.source for snapshot in snapshots})
    return FeedQualityReport(
        target_usd=target_usd,
        sources={
            source: _source_quality(
                [snapshot for snapshot in snapshots if snapshot.source == source],
                target_usd=target_usd,
            )
            for source in source_names
        },
    )


def render_feed_quality_markdown(report: FeedQualityReport) -> str:
    lines = [
        "# Fair Price Feed Quality Report",
        "",
        f"Target USD: {_format_decimal(report.target_usd)}",
        "",
        "Use this report before estimator comparison.",
        "",
        "| source | observations | first_ts | last_ts | median_gap_ms | max_gap_ms | "
        "metadata_obs | missing_metadata | executable_depth_obs | "
        "missing_executable_depth | unique_mid_count |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for source in sorted(report.sources):
        quality = report.sources[source]
        lines.append(
            f"| {quality.source} | {quality.observations} | "
            f"{_format_optional_int(quality.first_timestamp_ms)} | "
            f"{_format_optional_int(quality.last_timestamp_ms)} | "
            f"{_format_decimal(quality.median_gap_ms)} | "
            f"{_format_decimal(quality.max_gap_ms)} | "
            f"{quality.metadata_observations} | "
            f"{quality.missing_metadata_count} | "
            f"{quality.executable_depth_observations} | "
            f"{quality.missing_executable_depth_count} | "
            f"{quality.unique_mid_count} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def _source_quality(
    snapshots: list[PriceSnapshot],
    *,
    target_usd: Decimal,
) -> SourceFeedQuality:
    ordered = sorted(snapshots, key=lambda snapshot: snapshot.timestamp_ms)
    timestamps = [snapshot.timestamp_ms for snapshot in ordered]
    gaps = [
        Decimal(str(current - previous))
        for previous, current in zip(timestamps, timestamps[1:])
    ]
    metadata_count = sum(1 for snapshot in ordered if snapshot.metadata is not None)
    executable_count = sum(
        1
        for snapshot in ordered
        if snapshot.source == "quidax"
        and _quidax_executable_prices(snapshot, target_usd=target_usd).executable_mid
        is not None
    )
    return SourceFeedQuality(
        source=ordered[0].source if ordered else "",
        observations=len(ordered),
        first_timestamp_ms=timestamps[0] if timestamps else None,
        last_timestamp_ms=timestamps[-1] if timestamps else None,
        median_gap_ms=_median(sorted(gaps)) if gaps else None,
        max_gap_ms=max(gaps) if gaps else None,
        metadata_observations=metadata_count,
        missing_metadata_count=len(ordered) - metadata_count,
        executable_depth_observations=executable_count,
        missing_executable_depth_count=(
            len(ordered) - executable_count if ordered and ordered[0].source == "quidax" else 0
        ),
        unique_mid_count=len({snapshot.mid for snapshot in ordered}),
    )


def _median(values: list[Decimal]) -> Decimal:
    midpoint = len(values) // 2
    if len(values) % 2 == 1:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) / Decimal("2")


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    with localcontext() as context:
        context.prec = 28
        rounded = +value
    return format(rounded.normalize(), "f")


def _format_optional_int(value: int | None) -> str:
    return "" if value is None else str(value)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Report raw fair-price feed quality.")
    parser.add_argument("--db", required=True, help="Path to engine SQLite DB")
    parser.add_argument("--out", required=True, help="Output markdown path, or '-' for stdout")
    parser.add_argument("--from-ts", type=int, default=None)
    parser.add_argument("--to-ts", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--target-usd", default="100")
    args = parser.parse_args()

    target_usd = Decimal(str(args.target_usd))
    if target_usd <= 0:
        raise ValueError("--target-usd must be positive")

    snapshots = await load_price_snapshots(
        args.db,
        from_ts=args.from_ts,
        to_ts=args.to_ts,
        limit=args.limit,
    )
    markdown = render_feed_quality_markdown(
        analyze_feed_quality(snapshots, target_usd=target_usd)
    )

    if args.out == "-":
        print(markdown, end="")
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown)


if __name__ == "__main__":
    asyncio.run(_main())
