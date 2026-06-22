import csv
from dataclasses import fields
from decimal import Decimal

from backtester.v4_lp_ledger import LPLedgerRow
from scripts.export_lp_episode_features import export_lp_episode_features


def _ledger_row(
    event_type: str,
    tx_hash: str,
    liquidity_delta: str,
    timestamp_ms: int,
) -> dict[str, str]:
    opening = Decimal(liquidity_delta) > 0
    row = {
        "chain": "base",
        "pool_id": "",
        "block_number": str(timestamp_ms),
        "block_time": "2026-01-01T00:00:00+00:00",
        "tx_hash": tx_hash,
        "log_index": "0",
        "event_order": "0",
        "event_type": event_type,
        "position_manager": "0xpm",
        "token_id": "1",
        "lp_owner": "0xlp",
        "owner_source": "fixture",
        "tick_lower": "-100",
        "tick_upper": "100",
        "liquidity_delta": liquidity_delta,
        "liquidity_after": "100" if opening else "0",
        "amount0": "10" if opening else "0",
        "amount1": "10" if opening else "0",
        "amount0_actual": "10" if opening else "0",
        "amount1_actual": "10" if opening else "0",
        "amount0_attribution_source": "fixture",
        "amount1_attribution_source": "fixture",
        "amount_attribution_status": "fixture_exact" if opening else "not_applicable",
        "amount0_raw": "10",
        "amount1_raw": "10",
        "collect_amount0": "16" if not opening else "0",
        "collect_amount1": "16" if not opening else "0",
        "sqrt_price_x96_at_event": str(2**96),
        "tick_at_event": "0",
        "cngn_usd_price_at_event": "1",
        "timestamp_ms": str(timestamp_ms),
    }
    return {field.name: row[field.name] for field in fields(LPLedgerRow)}


def test_export_lp_episode_features_writes_gas_adjusted_rows(tmp_path):
    ledger = tmp_path / "ledger.csv"
    receipts = tmp_path / "receipts.csv"
    output = tmp_path / "features.csv"
    ledger_fields = [field.name for field in fields(LPLedgerRow)]
    with ledger.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ledger_fields)
        writer.writeheader()
        writer.writerow(_ledger_row("mint", "0xaaa", "100", 1000))
        writer.writerow(_ledger_row("burn_collect", "0xbbb", "-100", 3000))
    receipts.write_text(
        "chain,tx_hash,block_number,gas_used,effective_gas_price_wei,"
        "native_fee_wei,tx_from,tx_to\n"
        "base,0xaaa,1,1,1,2000000000000000,0xfrom,0xto\n"
        "base,0xbbb,2,1,1,3000000000000000,0xfrom,0xto\n"
    )

    count = export_lp_episode_features(
        ledger,
        receipts,
        output,
        native_token_usd=Decimal("2000"),
    )

    rows = list(csv.DictReader(output.open()))
    assert count == 1
    assert rows[0]["gross_pnl"] == "12"
    assert rows[0]["gas_native_fee_wei"] == "5000000000000000"
    assert rows[0]["gas_cost_usd"] == "10.000"
    assert rows[0]["net_pnl_after_gas"] == "2.000"
    assert rows[0]["gas_attribution_status"] == "exact_open_close"
