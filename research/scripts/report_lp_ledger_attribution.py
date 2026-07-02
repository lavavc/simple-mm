"""Report exact vs ambiguous opening attribution in V4 LP ledgers."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.v4_export import POOL_CONFIGS


_REQUIRED_FIELDS = {
    "chain",
    "block_number",
    "tx_hash",
    "event_type",
    "token_id",
    "liquidity_delta",
    "amount0_actual",
    "amount1_actual",
    "amount0_attribution_source",
    "amount1_attribution_source",
    "amount_attribution_status",
    "cngn_usd_price_at_event",
}


@dataclass(frozen=True)
class AmbiguousOpeningSample:
    block_number: int
    tx_hash: str
    event_type: str
    token_id: int
    amount_attribution_status: str
    liquidity_delta: Decimal


@dataclass(frozen=True)
class LPAttributionSummary:
    pool: str
    path: Path
    rows: int
    openings: int
    closings: int
    zero_delta_rows: int
    exact_openings: int
    ambiguous_openings: int
    exact_opening_capital_usd: Decimal
    ambiguous_opening_liquidity: Decimal
    ambiguous_opening_liquidity_share: Decimal
    status_counts: dict[str, int]
    opening_status_counts: dict[str, int]
    opening_source_counts: dict[str, int]
    ambiguous_samples: list[AmbiguousOpeningSample]


def analyze_lp_ledger_attribution(pool: str, path: Path) -> LPAttributionSummary:
    rows = _read_rows(path)
    openings = [row for row in rows if _decimal(row["liquidity_delta"]) > 0]
    closings = [row for row in rows if _decimal(row["liquidity_delta"]) < 0]
    zero_delta_rows = [row for row in rows if _decimal(row["liquidity_delta"]) == 0]
    exact_openings = [row for row in openings if row["amount_attribution_status"] == "exact"]
    ambiguous_openings = [
        row
        for row in openings
        if row["amount_attribution_status"].startswith("ambiguous")
    ]
    exact_opening_capital_usd = sum(
        (_capital_usd(pool, row) for row in exact_openings),
        Decimal("0"),
    )
    exact_opening_liquidity = sum(
        (_decimal(row["liquidity_delta"]) for row in exact_openings),
        Decimal("0"),
    )
    ambiguous_opening_liquidity = sum(
        (_decimal(row["liquidity_delta"]) for row in ambiguous_openings),
        Decimal("0"),
    )
    total_tracked_opening_liquidity = exact_opening_liquidity + ambiguous_opening_liquidity
    if total_tracked_opening_liquidity == 0:
        ambiguous_opening_liquidity_share = Decimal("0")
    else:
        with localcontext() as context:
            context.prec = 28
            ambiguous_opening_liquidity_share = +(
                ambiguous_opening_liquidity / total_tracked_opening_liquidity
            )

    return LPAttributionSummary(
        pool=pool,
        path=path,
        rows=len(rows),
        openings=len(openings),
        closings=len(closings),
        zero_delta_rows=len(zero_delta_rows),
        exact_openings=len(exact_openings),
        ambiguous_openings=len(ambiguous_openings),
        exact_opening_capital_usd=exact_opening_capital_usd,
        ambiguous_opening_liquidity=ambiguous_opening_liquidity,
        ambiguous_opening_liquidity_share=ambiguous_opening_liquidity_share,
        status_counts=_counts(row["amount_attribution_status"] for row in rows),
        opening_status_counts=_counts(row["amount_attribution_status"] for row in openings),
        opening_source_counts=_counts(
            f"{row['amount0_attribution_source']}|{row['amount1_attribution_source']}"
            for row in openings
        ),
        ambiguous_samples=[
            AmbiguousOpeningSample(
                block_number=int(row["block_number"]),
                tx_hash=row["tx_hash"],
                event_type=row["event_type"],
                token_id=int(row["token_id"]),
                amount_attribution_status=row["amount_attribution_status"],
                liquidity_delta=_decimal(row["liquidity_delta"]),
            )
            for row in ambiguous_openings[:10]
        ],
    )


def render_markdown(summaries: Sequence[LPAttributionSummary]) -> str:
    lines = [
        "# LP Ledger Attribution QA",
        "",
        "| Pool | Rows | Openings | Exact openings | Ambiguous openings | Exact opening capital USD | Ambiguous liquidity share |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        lines.append(
            "| "
            f"`{summary.pool}` | "
            f"{summary.rows} | "
            f"{summary.openings} | "
            f"{summary.exact_openings} | "
            f"{summary.ambiguous_openings} | "
            f"{_format_decimal(summary.exact_opening_capital_usd, 2)} | "
            f"{_format_percent(summary.ambiguous_opening_liquidity_share)} |"
        )

    lines.extend(["", "## Status Counts", ""])
    for summary in summaries:
        lines.append(f"### `{summary.pool}`")
        lines.append("")
        lines.append(f"- File: `{summary.path}`")
        lines.append(f"- All statuses: `{summary.status_counts}`")
        lines.append(f"- Opening statuses: `{summary.opening_status_counts}`")
        lines.append(f"- Opening sources: `{summary.opening_source_counts}`")
        lines.append("")

    lines.extend(["## Ambiguous Opening Samples", ""])
    samples_written = False
    for summary in summaries:
        if not summary.ambiguous_samples:
            continue
        samples_written = True
        lines.append(f"### `{summary.pool}`")
        lines.append("")
        lines.append("| Block | Tx | Event | Token ID | Status | Liquidity delta |")
        lines.append("|---:|---|---|---:|---|---:|")
        for sample in summary.ambiguous_samples:
            lines.append(
                "| "
                f"{sample.block_number} | "
                f"`{sample.tx_hash}` | "
                f"{sample.event_type} | "
                f"{sample.token_id} | "
                f"{sample.amount_attribution_status} | "
                f"{sample.liquidity_delta} |"
            )
        lines.append("")
    if not samples_written:
        lines.append("No ambiguous opening rows.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty LP ledger CSV: {path}")
        missing = sorted(_REQUIRED_FIELDS.difference(reader.fieldnames))
        if missing:
            raise ValueError(f"LP ledger CSV missing fields {missing}: {path}")
        return list(reader)


def _capital_usd(pool: str, row: dict[str, str]) -> Decimal:
    amount0 = _decimal(row["amount0_actual"])
    amount1 = _decimal(row["amount1_actual"])
    price = _decimal(row["cngn_usd_price_at_event"])
    token0_symbol, token1_symbol = _token_symbols(pool, row)
    if token0_symbol == "cNGN":
        return amount0 * price + amount1
    if token1_symbol == "cNGN":
        return amount0 + amount1 * price
    raise ValueError(f"LP ledger row does not identify cNGN side: {row['tx_hash']}")


def _token_symbols(pool: str, row: dict[str, str]) -> tuple[str, str]:
    config = POOL_CONFIGS.get(pool)
    if config is not None:
        return config.token0_symbol, config.token1_symbol
    if row.get("token0_symbol") and row.get("token1_symbol"):
        return row["token0_symbol"], row["token1_symbol"]
    raise ValueError(f"unknown pool without token symbols: {pool}")


def _counts(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _decimal(value: str) -> Decimal:
    return Decimal(value)


def _format_decimal(value: Decimal, places: int) -> str:
    quantizer = Decimal("1").scaleb(-places)
    return f"{value.quantize(quantizer):,}"


def _format_percent(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.0001'))}%"


def _parse_ledger_arg(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--ledger must use POOL=PATH")
    pool, path = value.split("=", 1)
    if not pool:
        raise argparse.ArgumentTypeError("--ledger pool cannot be empty")
    if not path:
        raise argparse.ArgumentTypeError("--ledger path cannot be empty")
    return pool, Path(path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        action="append",
        required=True,
        type=_parse_ledger_arg,
        help="Ledger input as POOL=PATH. Can be repeated.",
    )
    parser.add_argument("--out", type=Path)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    summaries = [
        analyze_lp_ledger_attribution(pool, path)
        for pool, path in args.ledger
    ]
    markdown = render_markdown(summaries)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown)
    sys.stdout.write(markdown)


if __name__ == "__main__":
    main()
