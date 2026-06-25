import csv
from dataclasses import fields
from decimal import Decimal

from research.backtester.v4_lp_ledger import LPLedgerRow
from research.scripts.report_lp_paper_episode_attribution import (
    analyze_pool_episode_attribution,
    render_markdown,
)


def _ledger_row(
    event_type: str,
    liquidity_delta: str,
    timestamp_ms: int,
    *,
    owner: str = "0xlp",
    collect_amount0: str = "0",
    collect_amount1: str = "0",
) -> dict[str, str]:
    row = {
        "chain": "base",
        "pool_id": "",
        "block_number": str(timestamp_ms),
        "block_time": "2026-01-01T00:00:00+00:00",
        "tx_hash": f"0x{timestamp_ms}",
        "log_index": "0",
        "event_order": "0",
        "event_type": event_type,
        "position_manager": "0xpm",
        "token_id": "1",
        "lp_owner": owner,
        "owner_source": "fixture",
        "tick_lower": "-100",
        "tick_upper": "100",
        "liquidity_delta": liquidity_delta,
        "liquidity_after": "100" if Decimal(liquidity_delta) > 0 else "0",
        "amount0": "10" if Decimal(liquidity_delta) > 0 else "0",
        "amount1": "10" if Decimal(liquidity_delta) > 0 else "0",
        "amount0_actual": "10" if Decimal(liquidity_delta) > 0 else "0",
        "amount1_actual": "10" if Decimal(liquidity_delta) > 0 else "0",
        "amount0_attribution_source": "fixture",
        "amount1_attribution_source": "fixture",
        "amount_attribution_status": (
            "fixture_exact" if Decimal(liquidity_delta) > 0 else "not_applicable"
        ),
        "amount0_raw": "10",
        "amount1_raw": "10",
        "collect_amount0": collect_amount0,
        "collect_amount1": collect_amount1,
        "sqrt_price_x96_at_event": str(2**96),
        "tick_at_event": "0",
        "cngn_usd_price_at_event": "1",
        "timestamp_ms": str(timestamp_ms),
    }
    return {field.name: row[field.name] for field in fields(LPLedgerRow)}


def test_pool_episode_attribution_report_counts_sources_and_unmatched_collects(tmp_path):
    ledger = tmp_path / "ledger.csv"
    fieldnames = [field.name for field in fields(LPLedgerRow)]
    with ledger.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(_ledger_row("mint", "100", 1000))
        writer.writerow(_ledger_row("collect", "0", 2000, collect_amount0="5", collect_amount1="5"))
        writer.writerow(_ledger_row("burn_collect", "-100", 3000))
        writer.writerow(_ledger_row("collect", "0", 4000, collect_amount0="3", collect_amount1="3"))

    summary = analyze_pool_episode_attribution("uni-base", ledger)
    markdown = render_markdown([summary])

    assert summary.episodes == 1
    assert summary.source_counts == {"interim_collect": 1}
    assert summary.status_counts == {"interim_collect": 1}
    assert summary.zero_collect_closes == 0
    assert summary.unmatched_collect_rows == 1
    assert summary.unmatched_collect_capital == Decimal("6")
    assert "| `uni-base` | 1 | 0 | 0 | 1 | 0 | 1 | 6 |" in markdown


def test_pool_episode_attribution_report_includes_pnl_sample_tiers(tmp_path):
    ledger = tmp_path / "ledger.csv"
    fieldnames = [field.name for field in fields(LPLedgerRow)]
    with ledger.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(_ledger_row("mint", "100", 1000, owner="0xstrict"))
        writer.writerow(
            _ledger_row(
                "burn_collect",
                "-100",
                2000,
                owner="0xstrict",
                collect_amount0="5",
                collect_amount1="5",
            )
        )
        writer.writerow(_ledger_row("mint", "100", 3000, owner="0xpaper"))
        writer.writerow(
            _ledger_row(
                "collect",
                "0",
                3500,
                owner="0xpaper",
                collect_amount0="5",
                collect_amount1="5",
            )
        )
        writer.writerow(_ledger_row("burn_collect", "-100", 4000, owner="0xpaper"))
        writer.writerow(_ledger_row("mint", "100", 5000, owner="0xlower"))
        writer.writerow(_ledger_row("burn_collect", "-100", 6000, owner="0xlower"))

    summary = analyze_pool_episode_attribution("uni-base", ledger)
    markdown = render_markdown([summary])

    assert summary.sample_summaries["strict"].episodes == 1
    assert summary.sample_summaries["strict"].opening_capital == Decimal("20")
    assert summary.sample_summaries["strict"].pnl == Decimal("-10")
    assert summary.sample_summaries["paper_compatible"].episodes == 2
    assert summary.sample_summaries["paper_compatible"].opening_capital == Decimal("40")
    assert summary.sample_summaries["paper_compatible"].pnl == Decimal("-20")
    assert summary.sample_summaries["lower_bound"].episodes == 3
    assert summary.sample_summaries["lower_bound"].opening_capital == Decimal("60")
    assert summary.sample_summaries["lower_bound"].pnl == Decimal("-40")
    assert summary.sample_summaries["zero_collect_excluded"].episodes == 1
    assert "| `uni-base` | `paper_compatible` | 2 | 40 | 20 | -20 | -50% |" in markdown
    assert "| `uni-base` | `lower_bound` | 3 | 60 | 20 | -40 | -66.6667% |" in markdown
