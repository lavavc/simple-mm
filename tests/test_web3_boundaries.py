"""V4 swap log decoding and event ID normalization.

Two non-obvious invariants:

1. V4 swap amounts are signed int256 values packed as two's-complement big-endian
   in 32-byte words. A negative amount0 means tokens flowed *into* the pool
   (buyer's side), positive means tokens flowed *out*. Both log-data shapes
   (bytes-like and hex-string) occur in practice.

2. event_id_from_log() must produce a stable, identical string regardless of
   whether transactionHash arrives as HexBytes or a plain hex string, and whether
   logIndex arrives as a numeric int or a hex string. The event ID is used for
   volume dedup — divergence means the same swap is counted twice or dropped.
"""
from types import SimpleNamespace

from hexbytes import HexBytes

from engine.market.dex_volume import V4_SWAP_TOPIC, event_id_from_log
from engine.venues.dex.v4 import BaseV4DexAdapter


def _word(value: int) -> bytes:
    return int(value).to_bytes(32, "big", signed=True)


def _make_adapter() -> BaseV4DexAdapter:
    adapter = object.__new__(BaseV4DexAdapter)
    adapter.config = SimpleNamespace(
        token0_address="0x0000000000000000000000000000000000000001",
        token1_address="0x0000000000000000000000000000000000000002",
    )
    return adapter


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


# =============================================================================
# event_id_from_log: stable dedup key across all Web3 log shapes
# =============================================================================


def test_event_id_stable_across_log_shapes():
    """HexBytes hash + numeric index and hex-string hash + hex-string index must
    produce the same event ID so that the same on-chain swap is never counted twice.
    """
    hexbytes_log = {
        "transactionHash": HexBytes("0xabcdef1234"),
        "logIndex": 3,
    }
    hexstr_log = {
        "transactionHash": "0xabcdef1234",
        "logIndex": "0x3",
    }

    id_from_hexbytes = event_id_from_log(hexbytes_log)
    id_from_hexstr = event_id_from_log(hexstr_log)

    assert id_from_hexbytes is not None
    assert id_from_hexbytes == id_from_hexstr
