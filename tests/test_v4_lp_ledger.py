import csv
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from backtester.v4_event_replay import ReplayedEvent
from backtester.v4_lp_ledger import DecodedLiquidityAction, OwnershipEvent, build_lp_ledger_rows

REPO_ROOT = Path(__file__).resolve().parents[1]


def _price_event(
    event_type: str,
    block_number: int,
    log_index: int,
    event_order: int,
    sqrt_price_x96: int,
    tick: int,
) -> ReplayedEvent:
    return ReplayedEvent(
        block_number=block_number,
        log_index=log_index,
        event_order=event_order,
        event_type=event_type,
        sqrt_price_x96=None,
        tick=None,
        event_time_sqrt_price_x96=sqrt_price_x96,
        event_time_tick=tick,
        event_time_state_source="same_block_prior_event",
    )


def test_collect_row_carries_token_id_owner_range_and_event_price():
    rows = build_lp_ledger_rows(
        decoded_actions=[
            DecodedLiquidityAction("mint", 100, 10, 0, 42, "0xowner", -120, 120, 1000, 0, 1, 2, chain="base"),
            DecodedLiquidityAction(
                "collect",
                100,
                20,
                1,
                42,
                "0xowner",
                -120,
                120,
                0,
                amount0=0,
                amount1=0,
                collect_amount0=3,
                collect_amount1=4,
                chain="base",
            ),
        ],
        ownership_events=[OwnershipEvent(100, 9, 42, None, "0xowner")],
        price_events=[
            _price_event("mint", 100, 10, 0, 111, 1),
            _price_event("collect", 100, 20, 1, 222, 2),
        ],
    )

    assert rows[1].token_id == 42
    assert rows[1].lp_owner == "0xowner"
    assert rows[1].owner_source == "action"
    assert rows[1].tick_lower == -120
    assert rows[1].tick_upper == 120
    assert rows[1].collect_amount0 == Decimal("3")
    assert rows[1].collect_amount1 == Decimal("4")
    assert rows[1].sqrt_price_x96_at_event == 222
    assert rows[1].tick_at_event == 2


def test_owner_lookup_uses_latest_transfer_at_or_before_action():
    rows = build_lp_ledger_rows(
        decoded_actions=[
            DecodedLiquidityAction("mint", 100, 10, 0, 42, None, -120, 120, 1000, 0, 1, 2, chain="base"),
            DecodedLiquidityAction("burn", 100, 30, 0, 42, None, -120, 120, -400, 0, 0, 0, chain="base"),
        ],
        ownership_events=[
            OwnershipEvent(100, 9, 42, None, "0xfirst"),
            OwnershipEvent(100, 25, 42, "0xfirst", "0xsecond"),
        ],
        price_events=[
            _price_event("mint", 100, 10, 0, 111, 1),
            _price_event("burn", 100, 30, 0, 222, 2),
        ],
    )

    assert rows[0].lp_owner == "0xfirst"
    assert rows[0].owner_source == "transfer"
    assert rows[0].liquidity_after == 1000
    assert rows[1].lp_owner == "0xsecond"
    assert rows[1].owner_source == "transfer"
    assert rows[1].liquidity_after == 600


def test_export_v4_lp_ledger_cli_writes_fixture_rows(tmp_path):
    decoded_actions = tmp_path / "decoded_actions.json"
    ownership_events = tmp_path / "ownership_events.json"
    price_events = tmp_path / "price_events.json"
    output = tmp_path / "ledger.csv"
    decoded_actions.write_text(json.dumps([
        {
            "action_type": "mint",
            "block_number": 100,
            "log_index": 10,
            "event_order": 0,
            "token_id": 42,
            "lp_owner": None,
            "tick_lower": -120,
            "tick_upper": 120,
            "liquidity_delta": 1000,
            "amount0": "1",
            "amount1": "2",
            "collect_amount0": "0",
            "collect_amount1": "0",
            "chain": "base",
            "pool_id": "0xpool",
            "block_time": "2026-01-01T00:00:00+00:00",
            "tx_hash": "0xabc",
            "position_manager": "0xpm",
            "amount0_raw": "1",
            "amount1_raw": "2",
            "timestamp_ms": 1000,
        }
    ]))
    ownership_events.write_text(json.dumps([
        {
            "block_number": 100,
            "log_index": 9,
            "event_order": 0,
            "token_id": 42,
            "previous_owner": None,
            "new_owner": "0xowner",
        }
    ]))
    price_events.write_text(json.dumps([
        {
            "block_number": 100,
            "log_index": 10,
            "event_order": 0,
            "event_type": "mint",
            "sqrt_price_x96": None,
            "tick": None,
            "event_time_sqrt_price_x96": 222,
            "event_time_tick": 2,
            "event_time_state_source": "prior_event",
        }
    ]))

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_v4_lp_ledger.py",
            "--pool",
            "uni-base",
            "--start-block",
            "100",
            "--end-block",
            "100",
            "--decoded-actions",
            str(decoded_actions),
            "--ownership-events",
            str(ownership_events),
            "--price-events",
            str(price_events),
            "--out",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    rows = list(csv.DictReader(output.open()))
    assert len(rows) == 1
    assert rows[0]["token_id"] == "42"
    assert rows[0]["lp_owner"] == "0xowner"
    assert rows[0]["sqrt_price_x96_at_event"] == "222"
