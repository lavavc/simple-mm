import csv
from dataclasses import fields
from decimal import Decimal

from backtester.v4_lp_ledger import LPLedgerRow
from scripts.export_lp_paper_episodes import export_paper_episodes


def _ledger_row(event_type: str, liquidity_delta: str, timestamp_ms: int) -> dict[str, str]:
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
        "lp_owner": "0xlp",
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
        "amount_attribution_status": "fixture_exact" if Decimal(liquidity_delta) > 0 else "not_applicable",
        "amount0_raw": "10",
        "amount1_raw": "10",
        "collect_amount0": "15" if Decimal(liquidity_delta) < 0 else "0",
        "collect_amount1": "15" if Decimal(liquidity_delta) < 0 else "0",
        "sqrt_price_x96_at_event": str(2**96),
        "tick_at_event": "0",
        "cngn_usd_price_at_event": "1",
        "timestamp_ms": str(timestamp_ms),
    }
    return {field.name: row[field.name] for field in fields(LPLedgerRow)}


def test_export_paper_episodes_writes_reconstructed_episode_csv(tmp_path):
    ledger = tmp_path / "ledger.csv"
    output = tmp_path / "episodes.csv"
    fieldnames = [field.name for field in fields(LPLedgerRow)]
    with ledger.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(_ledger_row("mint", "100", 1000))
        writer.writerow(_ledger_row("burn_collect", "-100", 3000))

    count = export_paper_episodes(ledger, output)

    rows = list(csv.DictReader(output.open()))
    assert count == 1
    assert rows[0]["pool"] == "uni-base"
    assert rows[0]["opening_capital"] == "20"
    assert rows[0]["closing_capital"] == "30"
    assert rows[0]["pnl"] == "10"
