"""V4 swap log decoding and event ID normalization.

Two non-obvious invariants:

1. V4 swap amounts are signed int256 values packed as two's-complement big-endian
   in 32-byte words, reported from the *swapper's* perspective: the output token
   delta is positive (paid to the user), the input negative — the opposite of
   V3's pool-perspective convention. Verified against live Base receipt
   0x1e2eeea3b1416f983b204c8cdd40e9c0eff122b0368c400b5e0a42cfc9f37d34 (buy of
   cNGN=token0: amount0=+244653630964, amount1=-175457000). Both log-data
   shapes (bytes-like and hex-string) occur in practice.

2. event_id_from_log() must produce a stable, identical string regardless of
   whether transactionHash arrives as HexBytes or a plain hex string, and whether
   logIndex arrives as a numeric int or a hex string. The event ID is used for
   volume dedup — divergence means the same swap is counted twice or dropped.
"""
from types import SimpleNamespace

from hexbytes import HexBytes

import pytest

from engine.market.dex_volume import V4_SWAP_TOPIC, event_id_from_log
from engine.venues.dex.v4 import BaseV4DexAdapter
from engine.web3_utils import iter_block_chunks, log_scan_chunk_size


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
        _word(15),
        _word(-21),
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
        _word(-10),
        _word(22),
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


def test_parse_swap_output_raw_live_base_buy_receipt() -> None:
    """Regression for the 2026-07-10 half-open: sign convention verified against
    the live Base receipt of buy tx 0x1e2eee… ($175.457 USDC → 244,653.63 cNGN).
    The old parser expected the output delta to be negative and returned None on
    every V4 swap, so a fee-blind estimate silently replaced the actual fill."""
    adapter = _make_adapter()
    payload = b"".join([
        _word(244653630964),   # amount0: +cNGN received (token0, output)
        _word(-175457000),     # amount1: -USDC paid (token1, input)
        bytes(32),
        bytes(32),
        bytes(32),
        bytes(32),
    ])
    receipt = {"logs": [{"topics": [HexBytes(V4_SWAP_TOPIC)], "data": payload}]}

    assert BaseV4DexAdapter._parse_swap_output_raw(
        adapter, receipt, adapter.config.token0_address
    ) == 244653630964
    # The input token must never be mistaken for output.
    assert BaseV4DexAdapter._parse_swap_output_raw(
        adapter, receipt, adapter.config.token1_address
    ) is None


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


# =============================================================================
# iter_block_chunks: bounded log-scan windows that fully cover the range
# =============================================================================
#
# eth_getLogs over fromBlock 0 -> latest pulls an unbounded response into one
# Python list and has OOM-ed the host. iter_block_chunks must split the range so
# each request stays bounded while still covering every block exactly once.


def test_iter_block_chunks_ascending_covers_range_without_gaps():
    chunks = list(iter_block_chunks(0, 12, 5))
    assert chunks == [(0, 4), (5, 9), (10, 12)]
    # contiguous, no gaps or overlaps, every block covered once
    assert chunks[0][0] == 0 and chunks[-1][1] == 12
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert next_start == prev_end + 1


def test_iter_block_chunks_descending_yields_newest_first():
    chunks = list(iter_block_chunks(0, 12, 5, descending=True))
    assert chunks == [(8, 12), (3, 7), (0, 2)]
    assert chunks[0][1] == 12 and chunks[-1][0] == 0


def test_iter_block_chunks_exact_and_empty_ranges():
    assert list(iter_block_chunks(100, 100, 50)) == [(100, 100)]
    assert list(iter_block_chunks(0, 9, 5)) == [(0, 4), (5, 9)]  # exact multiple
    assert list(iter_block_chunks(10, 5, 50)) == []  # from > to


def test_iter_block_chunks_rejects_nonpositive_chunk():
    with pytest.raises(ValueError):
        list(iter_block_chunks(0, 10, 0))


def test_log_scan_chunk_size_is_chain_aware():
    assert log_scan_chunk_size(56) == 5_000  # BSC mainnet
    assert log_scan_chunk_size(97) == 5_000  # BSC testnet
    assert log_scan_chunk_size(8453) == 50_000  # Base
