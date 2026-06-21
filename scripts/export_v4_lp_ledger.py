"""Export Uniswap v4 LP lifecycle ledger rows."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtester.v4_event_replay import ReplayEvent, ReplayedEvent, attach_event_time_state
from backtester.v4_export import (
    POOL_CONFIGS,
    POSITION_MANAGER_ABI,
    STATE_VIEW_ABI,
    V4_INITIALIZE_TOPIC,
    V4_SWAP_TOPIC,
    _block_timestamp_from_raw,
    _candidate_modify_liquidity_tx_hashes,
    _fetch_logs_with_debug,
    _make_web3,
    _position_state_from_chain,
    _raw_get_block,
    _state_seed_before_block,
    decode_initialize_row,
    decode_swap_row,
)
from backtester.v4_lp_ledger import (
    DecodedLiquidityAction,
    LedgerPositionState,
    LPLedgerRow,
    OwnershipEvent,
    build_lp_ledger_rows,
    decode_liquidity_actions_for_tx,
    decode_ownership_events_from_receipt,
)


def export_fixture_lp_ledger(
    decoded_actions_path: Path,
    ownership_events_path: Path,
    price_events_path: Path,
    output_path: Path,
) -> int:
    rows = build_lp_ledger_rows(
        decoded_actions=[
            _decoded_action_from_json(item)
            for item in _read_json_list(decoded_actions_path)
        ],
        ownership_events=[
            OwnershipEvent(**item)
            for item in _read_json_list(ownership_events_path)
        ],
        price_events=[
            ReplayedEvent(**item)
            for item in _read_json_list(price_events_path)
        ],
    )
    _write_ledger_rows(output_path, rows)
    return len(rows)


def export_rpc_lp_ledger(
    pool: str,
    start_block: int,
    end_block: int,
    output_path: Path,
) -> int:
    config = POOL_CONFIGS[pool]
    w3 = _make_web3(config)
    decoded_actions, ownership_events = _decode_rpc_lp_inputs(
        w3,
        config,
        start_block,
        end_block,
    )
    price_events = _replayed_price_events_for_actions(
        w3,
        config,
        start_block,
        end_block,
        decoded_actions,
    )
    rows = build_lp_ledger_rows(decoded_actions, ownership_events, price_events)
    _write_ledger_rows(output_path, rows)
    return len(rows)


def _decode_rpc_lp_inputs(
    w3: Web3,
    config: Any,
    start_block: int,
    end_block: int,
) -> tuple[list[DecodedLiquidityAction], list[OwnershipEvent]]:
    position_manager = w3.eth.contract(
        address=Web3.to_checksum_address(config.position_manager),
        abi=POSITION_MANAGER_ABI,
    )
    token_state: dict[int, LedgerPositionState] = {}
    decoded_actions: list[DecodedLiquidityAction] = []
    ownership_events: list[OwnershipEvent] = []

    def resolve_position(token_id: int, block_number: int) -> LedgerPositionState | None:
        position = _position_state_from_chain(token_id, position_manager, config, block_number)
        if position is None:
            return None
        return LedgerPositionState(
            pool_id=position.pool_id,
            tick_lower=position.tick_lower,
            tick_upper=position.tick_upper,
            liquidity_after=position.liquidity,
        )

    tx_receipts = []
    for tx_hash in _candidate_modify_liquidity_tx_hashes(w3, config, start_block, end_block):
        tx = dict(w3.eth.get_transaction(tx_hash))
        receipt = dict(w3.eth.get_transaction_receipt(tx_hash))
        tx_receipts.append((tx, receipt))

    for tx, receipt in sorted(tx_receipts, key=_tx_receipt_sort_key):
        block_number = int(receipt.get("blockNumber", tx["blockNumber"]))
        block_timestamp = _block_timestamp_for_number(w3, block_number)
        ownership_events.extend(
            decode_ownership_events_from_receipt(receipt, config.position_manager)
        )
        decoded_actions.extend(
            decode_liquidity_actions_for_tx(
                tx,
                receipt,
                block_timestamp,
                config,
                token_state,
                position_resolver=resolve_position,
            )
        )
    return decoded_actions, ownership_events


def _tx_receipt_sort_key(item: tuple[dict[str, Any], dict[str, Any]]) -> tuple[int, int, str]:
    tx, receipt = item
    block_number = _int_from_rpc_value(receipt.get("blockNumber", tx["blockNumber"]))
    transaction_index = _int_from_rpc_value(receipt.get("transactionIndex", tx.get("transactionIndex", 0)))
    return block_number, transaction_index, str(tx["hash"])


def _int_from_rpc_value(value: Any) -> int:
    if isinstance(value, str):
        if value.startswith(("0x", "0X")):
            return int(value, 16)
        return int(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return int.from_bytes(bytes(value), "big")
    return int(value)


def _block_timestamp_for_number(w3: Web3, block_number: int) -> int:
    return _block_timestamp_from_raw(_raw_get_block(w3, block_number, False))


def _replayed_price_events_for_actions(
    w3: Web3,
    config: Any,
    start_block: int,
    end_block: int,
    decoded_actions: list[DecodedLiquidityAction],
) -> list[ReplayedEvent]:
    if not decoded_actions:
        return []
    state_view = w3.eth.contract(
        address=Web3.to_checksum_address(config.state_view),
        abi=STATE_VIEW_ABI,
    )
    replay_events = _pool_replay_events(w3, config, start_block, end_block)
    replay_events.extend(
        ReplayEvent(
            block_number=action.block_number,
            log_index=action.log_index,
            event_order=action.event_order,
            event_type=action.action_type,
            sqrt_price_x96=None,
            tick=None,
        )
        for action in decoded_actions
    )
    first_block = min(event.block_number for event in replay_events)
    initial_state = _state_seed_before_block(state_view, config, first_block)
    replayed_events = attach_event_time_state(replay_events, initial_state)
    action_keys = {
        (action.block_number, action.log_index, action.event_order)
        for action in decoded_actions
    }
    return [
        event
        for event in replayed_events
        if (event.block_number, event.log_index, event.event_order) in action_keys
    ]


def _pool_replay_events(
    w3: Web3,
    config: Any,
    start_block: int,
    end_block: int,
) -> list[ReplayEvent]:
    events: list[ReplayEvent] = []
    block_timestamps: dict[int, int] = {}
    for topic in (V4_INITIALIZE_TOPIC, V4_SWAP_TOPIC):
        logs = _fetch_logs_with_debug(
            w3,
            {
                "address": Web3.to_checksum_address(config.pool_manager),
                "topics": [topic, config.pool_id],
                "fromBlock": start_block,
                "toBlock": end_block,
            },
            context=f"[{config.name}] pool price logs {start_block:,}->{end_block:,}",
        )
        for log in logs:
            block_number = int(log["blockNumber"])
            block_timestamp = block_timestamps.get(block_number)
            if block_timestamp is None:
                block_timestamp = _block_timestamp_for_number(w3, block_number)
                block_timestamps[block_number] = block_timestamp
            if topic == V4_INITIALIZE_TOPIC:
                row = decode_initialize_row(log, block_timestamp, config)
            else:
                row = decode_swap_row(log, block_timestamp, config)
            events.append(
                ReplayEvent(
                    block_number=row.block_number,
                    log_index=row.log_index,
                    event_order=0,
                    event_type=row.event_type,
                    sqrt_price_x96=row.sqrt_price_x96,
                    tick=row.tick,
                )
            )
    return events


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"expected a list of objects: {path}")
    return payload


def _decoded_action_from_json(payload: dict[str, Any]) -> DecodedLiquidityAction:
    decimal_fields = {"amount0", "amount1", "collect_amount0", "collect_amount1"}
    normalized = {
        key: Decimal(str(value)) if key in decimal_fields else value
        for key, value in payload.items()
    }
    return DecodedLiquidityAction(**normalized)


def _write_ledger_rows(output_path: Path, rows: list[LPLedgerRow]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(LPLedgerRow)]
    with output_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row))


def _csv_row(row: LPLedgerRow) -> dict[str, object]:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in asdict(row).items()
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True, choices=sorted(POOL_CONFIGS))
    parser.add_argument("--start-block", required=True, type=int)
    parser.add_argument("--end-block", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--decoded-actions", type=Path)
    parser.add_argument("--ownership-events", type=Path)
    parser.add_argument("--price-events", type=Path)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    fixture_paths = (args.decoded_actions, args.ownership_events, args.price_events)
    if all(path is not None for path in fixture_paths):
        count = export_fixture_lp_ledger(
            args.decoded_actions,
            args.ownership_events,
            args.price_events,
            args.out,
        )
        print(f"wrote {count} LP ledger rows to {args.out}")
        return
    if any(path is not None for path in fixture_paths):
        raise SystemExit(
            "provide all fixture inputs together: --decoded-actions, "
            "--ownership-events, and --price-events"
        )
    count = export_rpc_lp_ledger(args.pool, args.start_block, args.end_block, args.out)
    print(f"wrote {count} LP ledger rows to {args.out}")


if __name__ == "__main__":
    main()
