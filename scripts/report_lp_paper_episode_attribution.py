"""Report close-side attribution quality for paper LP episodes."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtester.lp_paper_episodes import (  # noqa: E402
    CLOSE_ATTRIBUTION_EXACT_COLLECT,
    CLOSE_ATTRIBUTION_INTERIM_COLLECT,
    CLOSE_ATTRIBUTION_MIXED,
    CLOSE_ATTRIBUTION_SAME_TX_COLLECT,
    CLOSE_ATTRIBUTION_ZERO_COLLECT,
    PaperLPEpisode,
    analyze_paper_episode_attribution,
)
from scripts.export_lp_paper_episodes import _read_ledger_rows  # noqa: E402

STRICT_SAMPLE = "strict"
PAPER_COMPATIBLE_SAMPLE = "paper_compatible"
LOWER_BOUND_SAMPLE = "lower_bound"
ZERO_COLLECT_EXCLUDED_SAMPLE = "zero_collect_excluded"
_STRICT_CLOSE_STATUSES = {
    CLOSE_ATTRIBUTION_EXACT_COLLECT,
    CLOSE_ATTRIBUTION_SAME_TX_COLLECT,
}
_PAPER_COMPATIBLE_CLOSE_STATUSES = {
    CLOSE_ATTRIBUTION_EXACT_COLLECT,
    CLOSE_ATTRIBUTION_SAME_TX_COLLECT,
    CLOSE_ATTRIBUTION_INTERIM_COLLECT,
    CLOSE_ATTRIBUTION_MIXED,
}
_SAMPLE_ORDER = (
    STRICT_SAMPLE,
    PAPER_COMPATIBLE_SAMPLE,
    LOWER_BOUND_SAMPLE,
    ZERO_COLLECT_EXCLUDED_SAMPLE,
)


@dataclass(frozen=True)
class PnLSampleSummary:
    name: str
    episodes: int
    opening_capital: Decimal
    closing_capital: Decimal
    pnl: Decimal
    return_on_capital: Decimal | None


@dataclass(frozen=True)
class PoolEpisodeAttributionSummary:
    pool: str
    path: Path
    episodes: int
    status_counts: dict[str, int]
    source_counts: dict[str, int]
    zero_collect_closes: int
    unmatched_collect_rows: int
    unmatched_collect_capital: Decimal
    sample_summaries: dict[str, PnLSampleSummary]


def analyze_pool_episode_attribution(pool: str, path: Path) -> PoolEpisodeAttributionSummary:
    attribution = analyze_paper_episode_attribution(_read_ledger_rows(path))
    status_counts = _counts(episode.close_attribution_status for episode in attribution.episodes)
    source_counts = _source_counts(
        episode.close_attribution_source
        for episode in attribution.episodes
    )
    return PoolEpisodeAttributionSummary(
        pool=pool,
        path=path,
        episodes=len(attribution.episodes),
        status_counts=status_counts,
        source_counts=source_counts,
        zero_collect_closes=status_counts.get(CLOSE_ATTRIBUTION_ZERO_COLLECT, 0),
        unmatched_collect_rows=attribution.unmatched_collect_rows,
        unmatched_collect_capital=attribution.unmatched_collect_capital,
        sample_summaries=_sample_summaries(attribution.episodes),
    )


def render_markdown(summaries: Sequence[PoolEpisodeAttributionSummary]) -> str:
    lines = [
        "# LP Paper Episode Close Attribution QA",
        "",
        "| Pool | Episodes | Exact collect | Same-tx collect | Interim collect | "
        "Zero-collect close | Unmatched collect rows | Unmatched collect capital |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        lines.append(
            "| "
            f"`{summary.pool}` | "
            f"{summary.episodes} | "
            f"{summary.source_counts.get(CLOSE_ATTRIBUTION_EXACT_COLLECT, 0)} | "
            f"{summary.source_counts.get(CLOSE_ATTRIBUTION_SAME_TX_COLLECT, 0)} | "
            f"{summary.source_counts.get(CLOSE_ATTRIBUTION_INTERIM_COLLECT, 0)} | "
            f"{summary.zero_collect_closes} | "
            f"{summary.unmatched_collect_rows} | "
            f"{_format_decimal(summary.unmatched_collect_capital)} |"
        )

    lines.extend(
        [
            "",
            "## PnL Sample Tiers",
            "",
            "| Pool | Sample | Episodes | Opening capital | Closing capital | "
            "PnL | Return on capital |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for summary in summaries:
        for sample_name in _SAMPLE_ORDER:
            sample = summary.sample_summaries[sample_name]
            lines.append(
                "| "
                f"`{summary.pool}` | "
                f"`{sample.name}` | "
                f"{sample.episodes} | "
                f"{_format_decimal(sample.opening_capital)} | "
                f"{_format_decimal(sample.closing_capital)} | "
                f"{_format_decimal(sample.pnl)} | "
                f"{_format_percent(sample.return_on_capital)} |"
            )

    lines.extend(["", "## Status Counts", ""])
    for summary in summaries:
        lines.append(f"### `{summary.pool}`")
        lines.append("")
        lines.append(f"- File: `{summary.path}`")
        lines.append(f"- Close statuses: `{summary.status_counts}`")
        lines.append(f"- Close sources: `{summary.source_counts}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        action="append",
        required=True,
        help="Pool ledger in NAME=PATH form. May be passed multiple times.",
    )
    parser.add_argument("--out", required=True, type=Path)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    summaries = [
        analyze_pool_episode_attribution(pool, path)
        for pool, path in (_parse_ledger_arg(value) for value in args.ledger)
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_markdown(summaries))
    print(f"wrote close attribution QA for {len(summaries)} pool(s) to {args.out}")


def _parse_ledger_arg(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"--ledger must be NAME=PATH, got {value!r}")
    pool, path = value.split("=", 1)
    if not pool:
        raise ValueError(f"--ledger missing pool name: {value!r}")
    if not path:
        raise ValueError(f"--ledger missing path: {value!r}")
    return pool, Path(path)


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _sample_summaries(episodes: Sequence[PaperLPEpisode]) -> dict[str, PnLSampleSummary]:
    return {
        STRICT_SAMPLE: _pnl_summary(
            STRICT_SAMPLE,
            [
                episode
                for episode in episodes
                if episode.close_attribution_status in _STRICT_CLOSE_STATUSES
            ],
        ),
        PAPER_COMPATIBLE_SAMPLE: _pnl_summary(
            PAPER_COMPATIBLE_SAMPLE,
            [
                episode
                for episode in episodes
                if episode.close_attribution_status in _PAPER_COMPATIBLE_CLOSE_STATUSES
            ],
        ),
        LOWER_BOUND_SAMPLE: _pnl_summary(LOWER_BOUND_SAMPLE, episodes),
        ZERO_COLLECT_EXCLUDED_SAMPLE: _pnl_summary(
            ZERO_COLLECT_EXCLUDED_SAMPLE,
            [
                episode
                for episode in episodes
                if episode.close_attribution_status == CLOSE_ATTRIBUTION_ZERO_COLLECT
            ],
        ),
    }


def _pnl_summary(name: str, episodes: Sequence[PaperLPEpisode]) -> PnLSampleSummary:
    opening_capital = sum((episode.opening_capital for episode in episodes), Decimal("0"))
    closing_capital = sum((episode.closing_capital for episode in episodes), Decimal("0"))
    pnl = sum((episode.pnl for episode in episodes), Decimal("0"))
    return_on_capital = None if opening_capital == 0 else pnl / opening_capital
    return PnLSampleSummary(
        name=name,
        episodes=len(episodes),
        opening_capital=opening_capital,
        closing_capital=closing_capital,
        pnl=pnl,
        return_on_capital=return_on_capital,
    )


def _source_counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        for source in value.split("|"):
            if source == "none":
                continue
            counts[source] = counts.get(source, 0) + 1
    return counts


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _format_percent(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    percent = (value * Decimal("100")).quantize(Decimal("0.0001"))
    if percent == 0:
        return "0%"
    return f"{format(percent.normalize(), 'f')}%"


if __name__ == "__main__":
    main()
