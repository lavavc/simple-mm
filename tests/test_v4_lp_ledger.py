import csv
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from eth_abi import encode  # type: ignore[attr-defined]
from web3 import Web3

import scripts.export_v4_lp_ledger as lp_ledger_export
from backtester.v4_event_replay import ReplayedEvent
from backtester.v4_export import POOL_CONFIGS, V4_MODIFY_LIQUIDITY_TOPIC
from backtester.v4_lp_ledger import (
    DecodedLiquidityAction,
    LedgerPositionState,
    OwnershipEvent,
    build_lp_ledger_rows,
    decode_liquidity_actions_for_tx,
    decode_ownership_events_from_receipt,
)
from engine.lp.types import (
    _V4_LP_DECREASE_LIQUIDITY,
    _V4_LP_INCREASE_LIQUIDITY,
    _V4_LP_MINT_POSITION,
    _V4_LP_SETTLE_PAIR,
    _V4_LP_TAKE_PAIR,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


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


def _build_modify_input(actions: bytes, params: list[bytes], deadline: int = 123) -> str:
    selector = Web3.keccak(text="modifyLiquidities(bytes,uint256)")[:4]
    unlock_data = encode(["bytes", "bytes[]"], [actions, params])
    calldata = selector + encode(["bytes", "uint256"], [unlock_data, deadline])
    return "0x" + calldata.hex()


def _topic_address(address: str) -> str:
    return "0x" + "00" * 12 + address[2:].lower()


def _transfer_log(
    *,
    address: str,
    from_address: str,
    to_address: str,
    token_or_amount: int,
    log_index: int,
) -> dict[str, object]:
    return {
        "address": address,
        "topics": [
            TRANSFER_TOPIC,
            _topic_address(from_address),
            _topic_address(to_address),
            "0x" + token_or_amount.to_bytes(32, "big").hex(),
        ],
        "data": hex(token_or_amount),
        "logIndex": log_index,
    }


def _erc20_transfer_log(
    *,
    token: str,
    to_address: str,
    amount_raw: int,
    log_index: int,
) -> dict[str, object]:
    return {
        "address": token,
        "topics": [
            TRANSFER_TOPIC,
            _topic_address("0x0000000000000000000000000000000000000011"),
            _topic_address(to_address),
        ],
        "data": hex(amount_raw),
        "logIndex": log_index,
    }


def _modify_liquidity_log(log_index: int) -> dict[str, object]:
    return {
        "address": POOL_CONFIGS["uni-base"].pool_manager,
        "topics": [
            V4_MODIFY_LIQUIDITY_TOPIC,
            POOL_CONFIGS["uni-base"].pool_id,
            _topic_address("0x0000000000000000000000000000000000000022"),
        ],
        "data": "0x",
        "logIndex": log_index,
    }


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


def test_decode_ownership_events_from_position_manager_transfers():
    receipt = {
        "blockNumber": 100,
        "logs": [
            _transfer_log(
                address=POOL_CONFIGS["uni-base"].position_manager,
                from_address=ZERO_ADDRESS,
                to_address="0x00000000000000000000000000000000000000AA",
                token_or_amount=42,
                log_index=7,
            ),
            _transfer_log(
                address=POOL_CONFIGS["uni-base"].token0_address,
                from_address=ZERO_ADDRESS,
                to_address="0x00000000000000000000000000000000000000AA",
                token_or_amount=999,
                log_index=8,
            ),
        ],
    }

    events = decode_ownership_events_from_receipt(
        receipt,
        POOL_CONFIGS["uni-base"].position_manager,
    )

    assert events == [
        OwnershipEvent(
            block_number=100,
            log_index=7,
            token_id=42,
            previous_owner=None,
            new_owner=Web3.to_checksum_address("0x00000000000000000000000000000000000000AA"),
        )
    ]


def test_decode_mint_action_uses_minted_token_transfer_and_pool_modify_log():
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    mint_param = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                "0x0000000000000000000000000000000000000000",
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    settle_param = encode(["address", "address"], [config.token0_address, config.token1_address])
    tx = {
        "input": _build_modify_input(bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR]), [mint_param, settle_param]),
        "hash": "0x" + "11" * 32,
        "blockNumber": 100,
    }
    receipt = {
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _modify_liquidity_log(8),
        ],
    }

    actions = decode_liquidity_actions_for_tx(tx, receipt, 1_700_000_000, config, {})

    assert len(actions) == 1
    assert actions[0].action_type == "mint"
    assert actions[0].token_id == 77
    assert actions[0].tick_lower == -120
    assert actions[0].tick_upper == 120
    assert actions[0].liquidity_delta == 999
    assert actions[0].amount0 == Decimal("1")
    assert actions[0].amount1 == Decimal("2")
    assert actions[0].amount0_raw == "1000000"
    assert actions[0].amount1_raw == "2000000"
    assert actions[0].log_index == 8
    assert actions[0].timestamp_ms == 1_700_000_000_000


def test_decode_decrease_take_pair_combines_burn_and_collect_amounts():
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    decrease_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [55, 400, 0, 0, b""])
    take_pair_param = encode(["address", "address", "address"], [config.token0_address, config.token1_address, recipient])
    tx = {
        "input": _build_modify_input(bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]), [decrease_param, take_pair_param]),
        "hash": "0x" + "22" * 32,
        "blockNumber": 100,
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(8),
            _erc20_transfer_log(token=config.token0_address, to_address=recipient, amount_raw=2_500_000, log_index=9),
            _erc20_transfer_log(token=config.token1_address, to_address=recipient, amount_raw=1_250_000, log_index=10),
        ],
    }
    token_state = {
        55: LedgerPositionState(
            pool_id=config.pool_id,
            tick_lower=-120,
            tick_upper=120,
            liquidity_after=1_000,
        )
    }

    actions = decode_liquidity_actions_for_tx(tx, receipt, 1_700_000_000, config, token_state)

    assert len(actions) == 1
    assert actions[0].action_type == "burn_collect"
    assert actions[0].token_id == 55
    assert actions[0].tick_lower == -120
    assert actions[0].tick_upper == 120
    assert actions[0].liquidity_delta == -400
    assert actions[0].collect_amount0 == Decimal("2.5")
    assert actions[0].collect_amount1 == Decimal("1.25")
    assert actions[0].log_index == 8
    assert token_state[55].liquidity_after == 600


def test_decode_decrease_resolves_preexisting_position_before_action_block():
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    decrease_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [55, 400, 0, 0, b""])
    take_pair_param = encode(["address", "address", "address"], [config.token0_address, config.token1_address, recipient])
    tx = {
        "input": _build_modify_input(bytes([_V4_LP_DECREASE_LIQUIDITY, _V4_LP_TAKE_PAIR]), [decrease_param, take_pair_param]),
        "hash": "0x" + "23" * 32,
        "blockNumber": 100,
    }
    receipt = {
        "logs": [
            _modify_liquidity_log(8),
            _erc20_transfer_log(token=config.token0_address, to_address=recipient, amount_raw=2_500_000, log_index=9),
        ],
    }
    calls = []

    def resolver(token_id: int, block_number: int) -> LedgerPositionState | None:
        calls.append((token_id, block_number))
        return LedgerPositionState(config.pool_id, -120, 120, 1_000)

    actions = decode_liquidity_actions_for_tx(
        tx,
        receipt,
        1_700_000_000,
        config,
        {},
        position_resolver=resolver,
    )

    assert calls == [(55, 99)]
    assert len(actions) == 1
    assert actions[0].action_type == "burn_collect"
    assert actions[0].liquidity_delta == -400


def test_export_rpc_lp_ledger_writes_rows_from_rpc_inputs(tmp_path, monkeypatch):
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    mint_param = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                "0x0000000000000000000000000000000000000000",
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    settle_param = encode(["address", "address"], [config.token0_address, config.token1_address])
    tx_hash = "0x" + "24" * 32
    tx = {
        "input": _build_modify_input(bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR]), [mint_param, settle_param]),
        "hash": tx_hash,
        "blockNumber": 100,
    }
    receipt = {
        "blockNumber": 100,
        "logs": [
            _transfer_log(
                address=config.position_manager,
                from_address=ZERO_ADDRESS,
                to_address=recipient,
                token_or_amount=77,
                log_index=5,
            ),
            _modify_liquidity_log(8),
        ],
    }
    fake_w3 = SimpleNamespace(
        eth=SimpleNamespace(
            contract=lambda **_kwargs: object(),
            get_transaction=lambda requested_hash: tx,
            get_transaction_receipt=lambda requested_hash: receipt,
        )
    )
    output = tmp_path / "ledger.csv"

    monkeypatch.setattr(lp_ledger_export, "_make_web3", lambda _config: fake_w3)
    monkeypatch.setattr(lp_ledger_export, "_candidate_modify_liquidity_tx_hashes", lambda *_args: [tx_hash])
    monkeypatch.setattr(lp_ledger_export, "_block_timestamp_for_number", lambda *_args: 1_700_000_000)
    monkeypatch.setattr(
        lp_ledger_export,
        "_replayed_price_events_for_actions",
        lambda *_args: [_price_event("mint", 100, 8, 0, 2**96, 0)],
    )

    count = lp_ledger_export.export_rpc_lp_ledger("uni-base", 100, 100, output)

    rows = list(csv.DictReader(output.open()))
    assert count == 1
    assert rows[0]["token_id"] == "77"
    assert rows[0]["lp_owner"] == Web3.to_checksum_address(recipient)
    assert rows[0]["event_type"] == "mint"


def test_export_rpc_lp_ledger_decodes_candidate_transactions_chronologically(tmp_path, monkeypatch):
    config = POOL_CONFIGS["uni-base"]
    recipient = "0x00000000000000000000000000000000000000AA"
    mint_param = encode(
        ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [
            (
                config.token0_address,
                config.token1_address,
                1500,
                30,
                "0x0000000000000000000000000000000000000000",
            ),
            -120,
            120,
            999,
            1_000_000,
            2_000_000,
            recipient,
            b"",
        ],
    )
    settle_param = encode(["address", "address"], [config.token0_address, config.token1_address])
    increase_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [77, 111, 500_000, 1_000_000, b""])
    mint_hash = "0x" + "34" * 32
    increase_hash = "0x" + "12" * 32
    txs = {
        mint_hash: {
            "input": _build_modify_input(bytes([_V4_LP_MINT_POSITION, _V4_LP_SETTLE_PAIR]), [mint_param, settle_param]),
            "hash": mint_hash,
            "blockNumber": 100,
            "transactionIndex": 5,
        },
        increase_hash: {
            "input": _build_modify_input(bytes([_V4_LP_INCREASE_LIQUIDITY, _V4_LP_SETTLE_PAIR]), [increase_param, settle_param]),
            "hash": increase_hash,
            "blockNumber": 101,
            "transactionIndex": 0,
        },
    }
    receipts = {
        mint_hash: {
            "blockNumber": 100,
            "transactionIndex": 5,
            "logs": [
                _transfer_log(
                    address=config.position_manager,
                    from_address=ZERO_ADDRESS,
                    to_address=recipient,
                    token_or_amount=77,
                    log_index=5,
                ),
                _modify_liquidity_log(8),
            ],
        },
        increase_hash: {
            "blockNumber": 101,
            "transactionIndex": 0,
            "logs": [_modify_liquidity_log(12)],
        },
    }
    fake_w3 = SimpleNamespace(
        eth=SimpleNamespace(
            contract=lambda **_kwargs: object(),
            get_transaction=lambda requested_hash: txs[requested_hash],
            get_transaction_receipt=lambda requested_hash: receipts[requested_hash],
        )
    )
    output = tmp_path / "ledger.csv"

    monkeypatch.setattr(lp_ledger_export, "_make_web3", lambda _config: fake_w3)
    monkeypatch.setattr(
        lp_ledger_export,
        "_candidate_modify_liquidity_tx_hashes",
        lambda *_args: [increase_hash, mint_hash],
    )
    monkeypatch.setattr(lp_ledger_export, "_block_timestamp_for_number", lambda *_args: 1_700_000_000)
    monkeypatch.setattr(
        lp_ledger_export,
        "_replayed_price_events_for_actions",
        lambda _w3, _config, _start, _end, actions: [
            _price_event(action.action_type, action.block_number, action.log_index, action.event_order, 2**96, 0)
            for action in actions
        ],
    )

    count = lp_ledger_export.export_rpc_lp_ledger("uni-base", 100, 101, output)

    rows = list(csv.DictReader(output.open()))
    assert count == 2
    assert [row["event_type"] for row in rows] == ["mint", "mint"]
    assert rows[1]["liquidity_delta"] == "111"


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
