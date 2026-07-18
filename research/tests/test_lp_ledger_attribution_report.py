import csv
from decimal import Decimal, localcontext

import pytest

from research.backtester.lp_ledger_attribution import pool_attribution_orientation
from research.cross_pool.contracts import CrossPoolContractError
from research.scripts.report_lp_ledger_attribution import (
    analyze_lp_ledger_attribution,
    render_markdown,
)


def _write_rows(path, rows):
    fieldnames = [
        "chain",
        "pool_id",
        "block_number",
        "tx_hash",
        "log_index",
        "event_order",
        "event_type",
        "token_id",
        "lp_owner",
        "liquidity_delta",
        "amount0_actual",
        "amount1_actual",
        "amount0_attribution_source",
        "amount1_attribution_source",
        "amount_attribution_status",
        "cngn_usd_price_at_event",
        "timestamp_ms",
        "token0_symbol",
        "token1_symbol",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        enriched_rows = []
        for index, row in enumerate(rows):
            enriched = dict(row)
            pool = "uni-base" if row["chain"] == "base" else "uni-bsc"
            enriched.setdefault(
                "pool_id",
                pool_attribution_orientation(pool).pool_id,
            )
            enriched.setdefault("log_index", str(index + 1))
            enriched.setdefault("event_order", "0")
            enriched.setdefault("lp_owner", "0x" + "11" * 20)
            enriched.setdefault(
                "timestamp_ms",
                str(1_767_571_200_000 + index * 1_000),
            )
            enriched_rows.append(enriched)
        writer.writerows(enriched_rows)


def test_analyze_lp_ledger_attribution_counts_exact_and_ambiguous_openings(tmp_path):
    ledger = tmp_path / "ledger.csv"
    _write_rows(
        ledger,
        [
            {
                "chain": "base",
                "block_number": "42926879",
                "tx_hash": "0xexact",
                "event_type": "mint",
                "token_id": "1",
                "liquidity_delta": "1000",
                "amount0_actual": "1000",
                "amount1_actual": "2",
                "amount0_attribution_source": "erc20_transfer_to_settlement",
                "amount1_attribution_source": "erc20_transfer_to_settlement",
                "amount_attribution_status": "exact",
                "cngn_usd_price_at_event": "0.0007",
                "token0_symbol": "cNGN",
                "token1_symbol": "USDC",
            },
            {
                "chain": "base",
                "block_number": "42926880",
                "tx_hash": "0xambiguous",
                "event_type": "mint",
                "token_id": "2",
                "liquidity_delta": "500",
                "amount0_actual": "0",
                "amount1_actual": "0",
                "amount0_attribution_source": "ambiguous",
                "amount1_attribution_source": "ambiguous",
                "amount_attribution_status": "ambiguous_missing_settle_pair",
                "cngn_usd_price_at_event": "0.0007",
                "token0_symbol": "cNGN",
                "token1_symbol": "USDC",
            },
            {
                "chain": "base",
                "block_number": "42926881",
                "tx_hash": "0xclose",
                "event_type": "burn_collect",
                "token_id": "1",
                "liquidity_delta": "-1000",
                "amount0_actual": "0",
                "amount1_actual": "0",
                "amount0_attribution_source": "not_applicable",
                "amount1_attribution_source": "not_applicable",
                "amount_attribution_status": "not_applicable",
                "cngn_usd_price_at_event": "0.0008",
                "token0_symbol": "cNGN",
                "token1_symbol": "USDC",
            },
        ],
    )

    summary = analyze_lp_ledger_attribution("uni-base", ledger)

    assert summary.rows == 3
    assert summary.openings == 2
    assert summary.exact_openings == 1
    assert summary.ambiguous_openings == 1
    assert summary.exact_opening_capital_usd == Decimal("2.7000")
    assert summary.ambiguous_opening_liquidity == Decimal("500")
    assert summary.ambiguous_opening_liquidity_share == Decimal("0.3333333333333333333333333333")
    assert summary.ambiguous_samples[0].tx_hash == "0xambiguous"
    assert summary.opening_source_counts == {
        "ambiguous|ambiguous": 1,
        "erc20_transfer_to_settlement|erc20_transfer_to_settlement": 1,
    }


def test_render_markdown_includes_summary_and_ambiguous_samples(tmp_path):
    ledger = tmp_path / "ledger.csv"
    _write_rows(
        ledger,
        [
            {
                "chain": "bsc",
                "block_number": "84655203",
                "tx_hash": "0xambiguous",
                "event_type": "mint",
                "token_id": "7",
                "liquidity_delta": "10",
                "amount0_actual": "0",
                "amount1_actual": "0",
                "amount0_attribution_source": "ambiguous",
                "amount1_attribution_source": "ambiguous",
                "amount_attribution_status": "ambiguous_no_settlement_transfer",
                "cngn_usd_price_at_event": "0.0007",
                "token0_symbol": "USDT",
                "token1_symbol": "cNGN",
            },
        ],
    )
    summary = analyze_lp_ledger_attribution("uni-bsc", ledger)

    markdown = render_markdown([summary])

    assert "| `uni-bsc` | 1 | 1 | 0 | 1 |" in markdown
    assert "ambiguous_no_settlement_transfer" in markdown
    assert "0xambiguous" in markdown


def test_analyze_lp_ledger_attribution_uses_pool_config_when_symbols_are_absent(tmp_path):
    ledger = tmp_path / "ledger.csv"
    fieldnames = [
        "chain",
        "pool_id",
        "block_number",
        "tx_hash",
        "log_index",
        "event_order",
        "event_type",
        "token_id",
        "lp_owner",
        "liquidity_delta",
        "amount0_actual",
        "amount1_actual",
        "amount0_attribution_source",
        "amount1_attribution_source",
        "amount_attribution_status",
        "cngn_usd_price_at_event",
        "timestamp_ms",
    ]
    with ledger.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "chain": "base",
                "pool_id": pool_attribution_orientation("uni-base").pool_id,
                "block_number": "42926879",
                "tx_hash": "0xexact",
                "log_index": "1",
                "event_order": "0",
                "event_type": "mint",
                "token_id": "1",
                "lp_owner": "0x" + "11" * 20,
                "liquidity_delta": "1000",
                "amount0_actual": "1000",
                "amount1_actual": "2",
                "amount0_attribution_source": "erc20_transfer_to_settlement",
                "amount1_attribution_source": "erc20_transfer_to_settlement",
                "amount_attribution_status": "exact",
                "cngn_usd_price_at_event": "0.0007",
                "timestamp_ms": "1767571200000",
            }
        )

    summary = analyze_lp_ledger_attribution("uni-base", ledger)

    assert summary.exact_opening_capital_usd == Decimal("2.7000")


def test_report_is_independent_of_the_callers_decimal_context(tmp_path):
    ledger = tmp_path / "ledger.csv"
    _write_rows(
        ledger,
        [
            {
                "chain": "base",
                "block_number": "42926879",
                "tx_hash": "0xexact",
                "event_type": "mint",
                "token_id": "1",
                "liquidity_delta": "1000",
                "amount0_actual": "123456789.123456789123456789",
                "amount1_actual": "2.123456789123456789",
                "amount0_attribution_source": "source0",
                "amount1_attribution_source": "source1",
                "amount_attribution_status": "exact",
                "cngn_usd_price_at_event": "0.0007",
                "token0_symbol": "cNGN",
                "token1_symbol": "USDC",
            }
        ],
    )

    with localcontext() as context:
        context.prec = 5
        low = render_markdown([analyze_lp_ledger_attribution("uni-base", ledger)])
    with localcontext() as context:
        context.prec = 50
        high = render_markdown([analyze_lp_ledger_attribution("uni-base", ledger)])

    assert low == high


def test_report_fails_closed_without_tracked_opening_liquidity(tmp_path):
    ledger = tmp_path / "ledger.csv"
    _write_rows(
        ledger,
        [
            {
                "chain": "base",
                "block_number": "42926879",
                "tx_hash": "0xclose",
                "event_type": "burn_collect",
                "token_id": "1",
                "liquidity_delta": "-1",
                "amount0_actual": "0",
                "amount1_actual": "0",
                "amount0_attribution_source": "not_applicable",
                "amount1_attribution_source": "not_applicable",
                "amount_attribution_status": "not_applicable",
                "cngn_usd_price_at_event": "0",
                "token0_symbol": "cNGN",
                "token1_symbol": "USDC",
            }
        ],
    )

    with pytest.raises(CrossPoolContractError, match="tracked opening liquidity"):
        analyze_lp_ledger_attribution("uni-base", ledger)
