from types import SimpleNamespace

from hexbytes import HexBytes

from engine.market.dex_volume import V4_SWAP_TOPIC
from engine.venues.dex.v4 import BaseV4DexAdapter
from engine.web3_utils import as_hexstr, coerce_hex_bytes, coerce_hex_str


def _word(value: int) -> bytes:
    return int(value).to_bytes(32, "big", signed=True)


def _make_adapter() -> BaseV4DexAdapter:
    adapter = object.__new__(BaseV4DexAdapter)
    adapter.config = SimpleNamespace(
        token0_address="0x0000000000000000000000000000000000000001",
        token1_address="0x0000000000000000000000000000000000000002",
    )
    return adapter


def test_hex_helpers_normalize_bytes_and_strings():
    assert coerce_hex_str(HexBytes("0x1234")) == "0x1234"
    assert coerce_hex_str(b"\x12\x34") == "0x1234"
    assert coerce_hex_bytes("0x1234") == b"\x12\x34"
    assert coerce_hex_bytes(HexBytes("0x1234")) == b"\x12\x34"
    assert as_hexstr("1234") == "0x1234"


def test_parse_swap_output_raw_handles_bytes_like_log_data() -> None:
    adapter = _make_adapter()
    payload = b"".join([
        _word(-15),
        _word(21),
        bytes(32),
        bytes(32),
        bytes(32),
        bytes(32),
    ])
    receipt = {
        "logs": [
            {
                "topics": [HexBytes(V4_SWAP_TOPIC)],
                "data": payload,
            }
        ]
    }

    output_raw = BaseV4DexAdapter._parse_swap_output_raw(
        adapter,
        receipt,
        adapter.config.token0_address,
    )
    assert output_raw == 15


def test_parse_swap_output_raw_handles_hex_string_log_data() -> None:
    adapter = _make_adapter()
    payload = b"".join([
        _word(10),
        _word(-22),
        bytes(32),
        bytes(32),
        bytes(32),
        bytes(32),
    ])
    receipt = {
        "logs": [
            {
                "topics": [bytes.fromhex(V4_SWAP_TOPIC[2:])],
                "data": "0x" + payload.hex(),
            }
        ]
    }

    output_raw = BaseV4DexAdapter._parse_swap_output_raw(
        adapter,
        receipt,
        adapter.config.token1_address,
    )
    assert output_raw == 22
