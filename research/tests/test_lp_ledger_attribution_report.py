import csv
from decimal import Decimal

from research.scripts.report_lp_ledger_attribution import (
    analyze_lp_ledger_attribution,
    render_markdown,
)


def _write_rows(path, rows):
    fieldnames = [
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
        "token0_symbol",
        "token1_symbol",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_analyze_lp_ledger_attribution_counts_exact_and_ambiguous_openings(tmp_path):
    ledger = tmp_path / "ledger.csv"
    _write_rows(
        ledger,
        [
            {
                "chain": "base",
                "block_number": "100",
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
                "block_number": "101",
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
                "block_number": "102",
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


def test_render_markdown_includes_summary_and_ambiguous_samples(tmp_path):
    ledger = tmp_path / "ledger.csv"
    _write_rows(
        ledger,
        [
            {
                "chain": "bsc",
                "block_number": "200",
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
    ]
    with ledger.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "chain": "base",
                "block_number": "100",
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
            }
        )

    summary = analyze_lp_ledger_attribution("uni-base", ledger)

    assert summary.exact_opening_capital_usd == Decimal("2.7000")
