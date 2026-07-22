"""Export only Swap events for a v4 pool.

Swaps-only variant of export_v4_pool_history.py: one topic-filtered
eth_getLogs scan over the PoolManager, no PositionManager transaction
filtering, so it covers the full pool history in minutes instead of hours.
Output rows use the same schema as the full exporter.
"""

from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web3 import Web3

from research.backtester.v4_export import (
    POOL_CONFIGS,
    V4_SWAP_TOPIC,
    ExportRow,
    _fetch_logs_with_debug,
    _log,
    _make_web3,
    _raw_get_block,
    _block_timestamp_from_raw,
    decode_swap_row,
)

SCAN_CHUNK_BLOCKS = 50_000


def _fetch_swap_logs(w3: Web3, config, start_block: int, end_block: int) -> list:
    logs: list = []
    chunk = SCAN_CHUNK_BLOCKS
    block = start_block
    while block <= end_block:
        to_block = min(block + chunk - 1, end_block)
        try:
            batch = _fetch_logs_with_debug(
                w3,
                {
                    "address": Web3.to_checksum_address(config.pool_manager),
                    "topics": [V4_SWAP_TOPIC, config.pool_id],
                    "fromBlock": block,
                    "toBlock": to_block,
                },
                context=f"[{config.name}] swap logs {block:,}->{to_block:,}",
            )
        except Exception:
            if chunk <= 2_000:
                raise
            chunk //= 4
            _log(f"[{config.name}] range too large, retrying with chunk={chunk:,}")
            continue
        logs.extend(batch)
        _log(f"[{config.name}] scanned {block:,}->{to_block:,}: {len(batch)} swaps ({len(logs)} total)")
        block = to_block + 1
    return logs


def main() -> None:
    parser = argparse.ArgumentParser(description="Export v4 Swap events to CSV")
    parser.add_argument("--pool", choices=sorted(POOL_CONFIGS), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--end-block", type=int)
    args = parser.parse_args()

    config = POOL_CONFIGS[args.pool]
    w3 = _make_web3(config)
    start_block = args.start_block if args.start_block is not None else config.default_start_block
    end_block = args.end_block if args.end_block is not None else w3.eth.block_number

    logs = _fetch_swap_logs(w3, config, start_block, end_block)
    logs.sort(key=lambda log: (int(log["blockNumber"]), int(log["logIndex"])))

    unique_blocks = sorted({int(log["blockNumber"]) for log in logs})
    _log(f"[{config.name}] fetching timestamps for {len(unique_blocks)} blocks")
    with ThreadPoolExecutor(max_workers=8) as pool:
        timestamps = dict(
            zip(
                unique_blocks,
                pool.map(
                    lambda block: _block_timestamp_from_raw(_raw_get_block(w3, block, False)),
                    unique_blocks,
                ),
            )
        )

    rows = [decode_swap_row(log, timestamps[int(log["blockNumber"])], config) for log in logs]

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[field.name for field in fields(ExportRow)])
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    print(f"wrote {len(rows)} swap rows to {output_path}")


if __name__ == "__main__":
    main()
