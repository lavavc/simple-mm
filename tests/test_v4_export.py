import csv
import json
from decimal import Decimal

from eth_abi import encode  # type: ignore[attr-defined]

from backtester.v4_export import (
    POOL_CONFIGS,
    _candidate_modify_liquidity_tx_hashes,
    _amounts_from_liquidity,
    build_liquidity_rows_for_tx,
    _decode_position_info,
    _decode_take_pair_param,
    _extract_take_pair_amounts,
    _decode_burn_param,
    _decode_increase_or_decrease_param,
    _int_from_rpc,
    _pool_id_prefix_matches,
    _read_existing_export_metadata,
    _read_export_checkpoint,
    _resolve_resume_start_block,
    _write_export_checkpoint,
    _decode_mint_param,
    decode_initialize_row,
    decode_modify_liquidity_row,
    decode_modify_liquidities_payload,
    decode_swap_row,
    derive_cngn_price,
)
from engine.lp.types import _V4_LP_BURN_POSITION, _V4_LP_INCREASE_LIQUIDITY, _V4_LP_MINT_POSITION, _V4_LP_TAKE_PAIR
from web3 import Web3


def _build_modify_input(actions: bytes, params: list[bytes], deadline: int = 123) -> str:
    selector = Web3.keccak(text="modifyLiquidities(bytes,uint256)")[:4]
    unlock_data = encode(["bytes", "bytes[]"], [actions, params])
    calldata = selector + encode(["bytes", "uint256"], [unlock_data, deadline])
    return "0x" + calldata.hex()


def _encode_position_info(pool_id: str, tick_lower: int, tick_upper: int, has_subscriber: int = 0) -> int:
    pool_prefix = int(pool_id, 16) >> 56
    tick_lower_bits = tick_lower & ((1 << 24) - 1)
    tick_upper_bits = tick_upper & ((1 << 24) - 1)
    return (pool_prefix << 56) | (tick_upper_bits << 32) | (tick_lower_bits << 8) | has_subscriber


class TestV4Export:
    def test_derive_cngn_price_base(self):
        price = derive_cngn_price(1_500_000_000, -1_000_000, POOL_CONFIGS["uni-base"])
        assert price == Decimal("0.0006666666666666666666666666667")

    def test_decode_swap_row_base(self):
        amount0 = int(-1_500 * 10**6).to_bytes(32, "big", signed=True)
        amount1 = int(1 * 10**6).to_bytes(32, "big", signed=True)
        sqrt_p = (2**96).to_bytes(32, "big")
        liquidity = (1_000_000).to_bytes(32, "big")
        tick = (10).to_bytes(32, "big", signed=True)
        fee = (1500).to_bytes(32, "big")
        log = {
            "data": "0x" + (amount0 + amount1 + sqrt_p + liquidity + tick + fee).hex(),
            "transactionHash": "0x" + "11" * 32,
            "logIndex": 7,
            "blockNumber": 123,
        }
        row = decode_swap_row(log, 1_700_000_000, POOL_CONFIGS["uni-base"])
        assert row.event_type == "swap"
        assert row.active_liquidity == 1_000_000
        assert row.amount_usd == 1.0
        assert row.cngn_usd_price == 1.0
        assert row.event_source == "pool_manager_swap"
        assert row.amount0_raw == str(-1_500 * 10**6)

    def test_decode_initialize_row_base(self):
        fee = (1500).to_bytes(32, "big")
        tick_spacing = (30).to_bytes(32, "big", signed=True)
        hooks = bytes.fromhex("00" * 12 + "12" * 20)
        sqrt_p = (2**96).to_bytes(32, "big")
        tick = (0).to_bytes(32, "big", signed=True)
        log = {
            "data": "0x" + (fee + tick_spacing + hooks + sqrt_p + tick).hex(),
            "topics": [
                "0x" + "aa" * 32,
                POOL_CONFIGS["uni-base"].pool_id,
                "0x" + "00" * 12 + POOL_CONFIGS["uni-base"].token0_address[2:],
                "0x" + "00" * 12 + POOL_CONFIGS["uni-base"].token1_address[2:],
            ],
            "transactionHash": "0x" + "12" * 32,
            "logIndex": 1,
            "blockNumber": 123,
        }
        row = decode_initialize_row(log, 1_700_000_000, POOL_CONFIGS["uni-base"])
        assert row.event_type == "initialize"
        assert row.event_source == "pool_manager_initialize"
        assert row.fee_rate == 0.0015
        assert row.tick_spacing == 30
        assert row.sqrt_price_x96 == 2**96

    def test_decode_modify_liquidity_row_classifies_delta(self):
        tick_lower = (-120).to_bytes(32, "big", signed=True)
        tick_upper = (120).to_bytes(32, "big", signed=True)
        liquidity_delta = (-999).to_bytes(32, "big", signed=True)
        salt = bytes.fromhex("34" * 32)
        log = {
            "data": "0x" + (tick_lower + tick_upper + liquidity_delta + salt).hex(),
            "topics": [
                "0x" + "bb" * 32,
                POOL_CONFIGS["uni-base"].pool_id,
                "0x" + "00" * 12 + "11" * 20,
            ],
            "transactionHash": "0x" + "13" * 32,
            "logIndex": 2,
            "blockNumber": 124,
        }
        row = decode_modify_liquidity_row(log, 1_700_000_000, (2**96, 0, 1_000_000, 1.0), POOL_CONFIGS["uni-base"])
        assert row.event_type == "burn"
        assert row.event_source == "pool_manager_modify_liquidity"
        assert row.tick_lower == -120
        assert row.tick_upper == 120
        assert row.liquidity_delta == -999
        assert row.salt == "0x" + "34" * 32

    def test_decode_modify_liquidities_payload(self):
        mint_param = encode(
            ["(address,address,uint24,int24,address)", "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
            [
                (
                    POOL_CONFIGS["uni-base"].token0_address,
                    POOL_CONFIGS["uni-base"].token1_address,
                    1500,
                    30,
                    "0x0000000000000000000000000000000000000000",
                ),
                -120,
                120,
                999,
                1_000_000,
                2_000_000,
                "0x0000000000000000000000000000000000000001",
                b"",
            ],
        )
        inc_param = encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [55, 111, 0, 0, b""])
        burn_param = encode(["uint256", "uint128", "uint128", "bytes"], [55, 0, 0, b""])
        input_data = _build_modify_input(
            bytes([_V4_LP_MINT_POSITION, _V4_LP_INCREASE_LIQUIDITY, _V4_LP_BURN_POSITION]),
            [mint_param, inc_param, burn_param],
        )
        actions, params = decode_modify_liquidities_payload(input_data)
        assert list(actions) == [_V4_LP_MINT_POSITION, _V4_LP_INCREASE_LIQUIDITY, _V4_LP_BURN_POSITION]
        pool_key, tick_lower, tick_upper, liquidity, amount0_max, amount1_max = _decode_mint_param(params[0])
        assert int(pool_key[2]) == 1500
        assert tick_lower == -120
        assert tick_upper == 120
        assert liquidity == 999
        assert amount0_max == 1_000_000
        assert amount1_max == 2_000_000
        token_id, liquidity_delta = _decode_increase_or_decrease_param(params[1])
        assert token_id == 55
        assert liquidity_delta == 111
        assert _decode_burn_param(params[2]) == 55

    def test_amounts_from_liquidity_returns_positive_amounts(self):
        amount0, amount1 = _amounts_from_liquidity(
            liquidity=1_000_000,
            tick_lower=-120,
            tick_upper=120,
            sqrt_price_x96=2**96,
            config=POOL_CONFIGS["uni-base"],
        )
        assert amount0 > 0
        assert amount1 > 0

    def test_decode_take_pair_and_extract_amounts(self):
        recipient = "0x00000000000000000000000000000000000000AA"
        take_pair_param = encode(
            ["address", "address", "address"],
            [
                POOL_CONFIGS["uni-base"].token0_address,
                POOL_CONFIGS["uni-base"].token1_address,
                recipient,
            ],
        )
        currency0, currency1, decoded_recipient = _decode_take_pair_param(take_pair_param)
        assert Web3.to_checksum_address(currency0) == Web3.to_checksum_address(POOL_CONFIGS["uni-base"].token0_address)
        assert Web3.to_checksum_address(currency1) == Web3.to_checksum_address(POOL_CONFIGS["uni-base"].token1_address)
        assert decoded_recipient == Web3.to_checksum_address(recipient)

        transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)").hex()
        receipt = {
            "logs": [
                {
                    "address": POOL_CONFIGS["uni-base"].token0_address,
                    "topics": [
                        transfer_topic,
                        "0x" + "00" * 12 + "11" * 20,
                        "0x" + "00" * 12 + recipient[2:],
                    ],
                    "data": hex(2_500_000),
                },
                {
                    "address": POOL_CONFIGS["uni-base"].token1_address,
                    "topics": [
                        transfer_topic,
                        "0x" + "00" * 12 + "22" * 20,
                        "0x" + "00" * 12 + recipient[2:],
                    ],
                    "data": hex(1_250_000),
                },
                {
                    "address": POOL_CONFIGS["uni-base"].token1_address,
                    "topics": [
                        transfer_topic,
                        "0x" + "00" * 12 + "22" * 20,
                        "0x" + "00" * 12 + "bb" * 20,
                    ],
                    "data": hex(999_999),
                },
            ]
        }

        amount0, amount1 = _extract_take_pair_amounts(receipt, recipient, POOL_CONFIGS["uni-base"])
        assert amount0 == Decimal("2.5")
        assert amount1 == Decimal("1.25")

    def test_int_from_rpc_handles_bytes_like_values(self):
        assert _int_from_rpc(b"\x00\x00\x00\x05") == 5

    def test_decode_position_info_uses_pool_prefix_and_signed_ticks(self):
        info = _encode_position_info(POOL_CONFIGS["uni-base"].pool_id, -120, 120, 1)

        pool_prefix, tick_lower, tick_upper = _decode_position_info(info)

        assert _pool_id_prefix_matches(pool_prefix, POOL_CONFIGS["uni-base"])
        assert tick_lower == -120
        assert tick_upper == 120

    def test_read_existing_export_metadata_counts_rows_and_max_block(self, tmp_path):
        path = tmp_path / "history.csv"
        fieldnames = ["event_type", "block_number"]
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({"event_type": "initialize", "block_number": "100"})
            writer.writerow({"event_type": "swap", "block_number": "125"})
            writer.writerow({"event_type": "collect", "block_number": "120"})

        metadata = _read_existing_export_metadata(str(path))

        assert metadata.row_count == 3
        assert metadata.max_block == 125
        assert metadata.initialize_found is True

    def test_export_checkpoint_roundtrip(self, tmp_path):
        path = tmp_path / "uni-base.checkpoint.json"

        _write_export_checkpoint(str(path), "uni-base", 456)

        assert _read_export_checkpoint(str(path), "uni-base") == 456
        assert json.loads(path.read_text()) == {
            "pool": "uni-base",
            "last_scanned_block": 456,
        }

    def test_export_checkpoint_rejects_corrupt_or_wrong_pool(self, tmp_path):
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text('{"pool":"uni-base"}')
        wrong_pool = tmp_path / "wrong-pool.json"
        wrong_pool.write_text('{"pool":"uni-bsc","last_scanned_block":456}')

        for path in (corrupt, wrong_pool):
            try:
                _read_export_checkpoint(str(path), "uni-base")
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected invalid checkpoint to fail: {path}")

    def test_resume_start_uses_furthest_durable_progress(self):
        metadata = type("Metadata", (), {"max_block": 125})()

        assert _resolve_resume_start_block(100, metadata, checkpoint_block=150) == 151
        assert _resolve_resume_start_block(140, metadata, checkpoint_block=120) == 140

    def test_candidate_modify_liquidity_tx_hashes_dedupes_logs(self, monkeypatch):
        fake_logs = [
            {"transactionHash": "0x" + "11" * 32},
            {"transactionHash": "0x" + "22" * 32},
            {"transactionHash": "0x" + "11" * 32},
        ]

        def _fake_fetch_logs(_w3, _params, context=None):
            return fake_logs

        monkeypatch.setattr("backtester.v4_export._fetch_logs_with_debug", _fake_fetch_logs)
        tx_hashes = _candidate_modify_liquidity_tx_hashes(object(), POOL_CONFIGS["uni-base"], 1, 100)
        assert tx_hashes == ["0x" + "11" * 32, "0x" + "22" * 32]

    def test_collect_is_skipped_when_tx_was_not_tied_to_target_pool(self):
        take_pair_param = encode(
            ["address", "address", "address"],
            [
                POOL_CONFIGS["uni-base"].token0_address,
                POOL_CONFIGS["uni-base"].token1_address,
                "0x00000000000000000000000000000000000000AA",
            ],
        )
        tx = {
            "input": _build_modify_input(bytes([_V4_LP_TAKE_PAIR]), [take_pair_param]),
            "hash": "0x" + "33" * 32,
            "blockNumber": 123,
        }
        receipt = {"logs": []}

        class _FakeCall:
            def __init__(self, result):
                self._result = result

            def call(self, block_identifier=None):
                return self._result

        class _FakeFunctions:
            def getSlot0(self, pool_id_bytes):
                return _FakeCall((2**96, 0, 0, 1500))

            def getLiquidity(self, pool_id_bytes):
                return _FakeCall(1_000_000)

        class _FakeStateView:
            functions = _FakeFunctions()

        rows = build_liquidity_rows_for_tx(
            tx,
            receipt,
            1_700_000_000,
            _FakeStateView(),
            object(),
            POOL_CONFIGS["uni-base"],
            {},
        )
        assert rows == []

    def test_collect_emits_when_burn_token_id_resolves_to_target_pool(self):
        burn_param = encode(["uint256", "uint128", "uint128", "bytes"], [55, 0, 0, b""])
        take_pair_param = encode(
            ["address", "address", "address"],
            [
                POOL_CONFIGS["uni-base"].token0_address,
                POOL_CONFIGS["uni-base"].token1_address,
                "0x00000000000000000000000000000000000000AA",
            ],
        )
        tx = {
            "input": _build_modify_input(bytes([_V4_LP_BURN_POSITION, _V4_LP_TAKE_PAIR]), [burn_param, take_pair_param]),
            "hash": "0x" + "44" * 32,
            "blockNumber": 123,
        }
        recipient = "0x00000000000000000000000000000000000000AA"
        transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)").hex()
        receipt = {
            "logs": [
                {
                    "address": POOL_CONFIGS["uni-base"].token1_address,
                    "topics": [
                        transfer_topic,
                        "0x" + "00" * 12 + "22" * 20,
                        "0x" + "00" * 12 + recipient[2:],
                    ],
                    "data": hex(1_250_000),
                    "logIndex": 1,
                },
            ]
        }

        class _FakeCall:
            def __init__(self, result):
                self._result = result

            def call(self, block_identifier=None):
                return self._result

        class _FakeStateViewFunctions:
            def getSlot0(self, pool_id_bytes):
                return _FakeCall((2**96, 0, 0, 1500))

            def getLiquidity(self, pool_id_bytes):
                return _FakeCall(1_000_000)

        class _FakeStateView:
            functions = _FakeStateViewFunctions()

        class _FakePositionManagerFunctions:
            def getPoolAndPositionInfo(self, token_id):
                return _FakeCall((
                    (
                        POOL_CONFIGS["uni-base"].token0_address,
                        POOL_CONFIGS["uni-base"].token1_address,
                        1500,
                        30,
                        "0x0000000000000000000000000000000000000000",
                    ),
                    _encode_position_info(POOL_CONFIGS["uni-base"].pool_id, -120, 120),
                ))

        class _FakePositionManager:
            functions = _FakePositionManagerFunctions()

        rows = build_liquidity_rows_for_tx(
            tx,
            receipt,
            1_700_000_000,
            _FakeStateView(),
            _FakePositionManager(),
            POOL_CONFIGS["uni-base"],
            {},
        )
        assert len(rows) == 1
        assert rows[0].event_type == "collect"
