"""Fetch historical cNGN token balance snapshots for LP account addresses.

Scans ERC-20 Transfer event logs on Base and BSC for the given LP account
addresses and reconstructs a time-series of net cNGN holdings.

Usage:
    python research/scripts/fetch_cngn_balance_history.py \
        --base-addr 0xB25dB46588634D1153c058407D08361AbC6323fE \
        --bsc-addr  0x71A39D4663d52FFEb2EC78CAa3FC73d4Cc7E9302 \
        --out       research/data/cngn_balance_history.csv \
        [--start-block-base <N>] \
        [--start-block-bsc  <N>]

Output CSV columns:
    chain, block_number, block_time, tx_hash, delta_raw, balance_raw, balance_cngn
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import geth_poa_middleware  # type: ignore[attr-defined]

load_dotenv()

# cNGN token addresses (checksummed)
CNGN_BASE = Web3.to_checksum_address("0x46C85152bFe9f96829aA94755D9f915F9B10EF5F")
CNGN_BSC  = Web3.to_checksum_address("0xa8aea66b361a8d53e8865c62d142167af28af058")

# ERC-20 Transfer topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Decimals
DECIMALS_BASE = 6   # cNGN on Base is 6 decimals
DECIMALS_BSC  = 18  # cNGN on BSC is 18 decimals

# Block chunk size for getLogs (stay well inside RPC limits)
CHUNK = 50_000


def _rpc_url(chain: str) -> str:
    alchemy_key = os.getenv("ALCHEMY_KEY", "")
    if chain == "base":
        if alchemy_key:
            return f"https://base-mainnet.g.alchemy.com/v2/{alchemy_key}"
        return os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
    else:
        if alchemy_key:
            return f"https://bnb-mainnet.g.alchemy.com/v2/{alchemy_key}"
        return os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org")


def _pad_address(addr: str) -> str:
    """Pad an address to 32 bytes for use as a topic filter."""
    return "0x" + addr[2:].lower().zfill(64)


def _build_web3(chain: str) -> Web3:
    """Build a Web3 client with chain-specific middleware."""
    w3 = Web3(Web3.HTTPProvider(_rpc_url(chain)))
    if chain == "bsc":
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    return w3


def _fetch_transfers(
    w3: Web3,
    token_addr: str,
    account_addr: str,
    from_block: int,
    to_block: int,
    decimals: int,
    chain: str,
) -> list[dict]:
    """Fetch all Transfer events where account is sender or receiver."""
    padded = _pad_address(account_addr)
    rows: list[dict] = []

    for start in range(from_block, to_block + 1, CHUNK):
        end = min(start + CHUNK - 1, to_block)
        for is_receiver in (True, False):
            # Build topic list: Transfer(from, to) — filter by either position
            topics: list = (
                [TRANSFER_TOPIC, None, padded] if is_receiver
                else [TRANSFER_TOPIC, padded]
            )
            logs = w3.eth.get_logs({  # type: ignore[arg-type]
                "address": Web3.to_checksum_address(token_addr),
                "fromBlock": start,
                "toBlock": end,
                "topics": topics,
            })
            for log in logs:
                raw_amt = int(log["data"], 16) if isinstance(log["data"], str) else int(log["data"].hex(), 16)
                signed_delta = raw_amt if is_receiver else -raw_amt
                rows.append({
                    "chain": chain,
                    "block_number": log["blockNumber"],
                    "tx_hash": log["transactionHash"].hex(),
                    "log_index": log["logIndex"],
                    "delta_raw": signed_delta,
                    "decimals": decimals,
                })

        time.sleep(0.1)  # rate-limit courtesy pause

    # Sort by block then log index
    rows.sort(key=lambda r: (r["block_number"], r["log_index"]))
    return rows


def _enrich_with_timestamps(w3: Web3, rows: list[dict]) -> list[dict]:
    """Add block_time by fetching each unique block header (cached)."""
    block_cache: dict[int, int] = {}
    for row in rows:
        bn = row["block_number"]
        if bn not in block_cache:
            blk = w3.eth.get_block(bn)
            block_cache[bn] = blk.get("timestamp", 0)  # type: ignore[typeddict-item]
        row["block_time"] = datetime.fromtimestamp(block_cache[bn], tz=timezone.utc).isoformat()
    return rows


def fetch_chain(chain: str, token_addr: str, account_addr: str, start_block: int, decimals: int) -> list[dict]:
    w3 = _build_web3(chain)
    if not w3.is_connected():
        print(f"[{chain}] Cannot connect to RPC — skipping.", file=sys.stderr)
        return []

    current = w3.eth.block_number
    print(f"[{chain}] Current block: {current:,}. Scanning {start_block:,}→{current:,} "
          f"({(current - start_block):,} blocks) for {account_addr[:10]}…")

    rows = _fetch_transfers(w3, token_addr, account_addr, start_block, current, decimals, chain)
    rows = _enrich_with_timestamps(w3, rows)

    # Compute running balance
    balance = 0
    for row in rows:
        balance += row["delta_raw"]
        row["balance_raw"] = balance
        row["balance_cngn"] = balance / (10 ** row["decimals"])

    print(f"[{chain}] Found {len(rows)} transfer events. Final balance: {balance / 10**decimals:,.2f} cNGN")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-addr", required=True, help="Base chain LP account address")
    ap.add_argument("--bsc-addr",  required=True, help="BSC LP account address")
    ap.add_argument("--out",       default="research/data/cngn_balance_history.csv", help="Output CSV path")
    ap.add_argument("--start-block-base", type=int, default=42_914_900,
                    help="First Base block to scan (default ~early 2025)")
    ap.add_argument("--start-block-bsc",  type=int, default=84_606_863,
                    help="First BSC block to scan (default ~early 2025)")
    args = ap.parse_args()

    all_rows: list[dict] = []

    all_rows += fetch_chain(
        "base", CNGN_BASE,
        Web3.to_checksum_address(args.base_addr),
        args.start_block_base, DECIMALS_BASE,
    )
    all_rows += fetch_chain(
        "bsc", CNGN_BSC,
        Web3.to_checksum_address(args.bsc_addr),
        args.start_block_bsc, DECIMALS_BSC,
    )

    if not all_rows:
        print("No transfers found. Check RPC connectivity and addresses.")
        sys.exit(1)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["chain", "block_number", "block_time", "tx_hash", "delta_raw", "balance_raw", "balance_cngn"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Written {len(all_rows)} rows → {out_path}")


if __name__ == "__main__":
    main()
