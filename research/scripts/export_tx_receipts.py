"""Export transaction receipt gas sidecars for derived LP lifecycle ledgers."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Mapping, Protocol

from web3 import Web3
from web3.middleware import geth_poa_middleware  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.v4_export import POOL_CONFIGS, ExportPoolConfig
from engine.web3_utils import as_hexstr, coerce_hex_str

RECEIPT_FIELDS = [
    "chain",
    "tx_hash",
    "block_number",
    "gas_used",
    "effective_gas_price_wei",
    "native_fee_wei",
    "tx_from",
    "tx_to",
]


class ReceiptEth(Protocol):
    def get_transaction_receipt(self, tx_hash: str) -> Mapping[str, object] | None:
        ...


class ReceiptWeb3(Protocol):
    eth: ReceiptEth


def receipt_row(chain: str, receipt: Mapping[str, object]) -> dict[str, str]:
    tx_hash = coerce_hex_str(_required(receipt, "transactionHash"))
    block_number = _int_value(_required(receipt, "blockNumber"), "blockNumber")
    gas_used = _int_value(_required(receipt, "gasUsed"), "gasUsed")
    effective_gas_price = _int_value(
        _required(receipt, "effectiveGasPrice"),
        "effectiveGasPrice",
    )
    native_fee = gas_used * effective_gas_price
    tx_from = coerce_hex_str(_required(receipt, "from"))
    tx_to_raw = receipt.get("to")
    tx_to = "" if tx_to_raw is None else coerce_hex_str(tx_to_raw)
    return {
        "chain": chain,
        "tx_hash": tx_hash,
        "block_number": str(block_number),
        "gas_used": str(gas_used),
        "effective_gas_price_wei": str(effective_gas_price),
        "native_fee_wei": str(native_fee),
        "tx_from": tx_from,
        "tx_to": tx_to,
    }


def read_unique_tx_hashes(tx_csv: Path) -> list[str]:
    seen: set[str] = set()
    tx_hashes: list[str] = []
    with tx_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "tx_hash" not in reader.fieldnames:
            raise ValueError(f"missing tx_hash column: {tx_csv}")
        for row in reader:
            raw_tx_hash = row["tx_hash"].strip()
            if not raw_tx_hash:
                raise ValueError(f"blank tx_hash in {tx_csv}")
            tx_hash = coerce_hex_str(raw_tx_hash)
            if tx_hash in seen:
                continue
            seen.add(tx_hash)
            tx_hashes.append(tx_hash)
    return tx_hashes


def export_tx_receipts(
    chain: str,
    tx_csv: Path,
    output_path: Path,
    web3_client: ReceiptWeb3,
) -> int:
    rows: list[dict[str, str]] = []
    for tx_hash in read_unique_tx_hashes(tx_csv):
        receipt = web3_client.eth.get_transaction_receipt(as_hexstr(tx_hash))
        if receipt is None:
            raise ValueError(f"missing receipt for {chain} tx_hash={tx_hash}")
        rows.append(receipt_row(chain, receipt))
    _write_receipt_rows(output_path, rows)
    return len(rows)


def build_web3(chain: str) -> Web3:
    config = _config_for_chain(chain)
    if not config.rpc_url:
        raise ValueError(f"missing RPC URL for chain={chain}")
    web3_client = Web3(Web3.HTTPProvider(config.rpc_url))
    if chain == "bsc":
        web3_client.middleware_onion.inject(geth_poa_middleware, layer=0)
    return web3_client


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    chains = sorted({config.chain for config in POOL_CONFIGS.values()})
    parser.add_argument("--chain", required=True, choices=chains)
    parser.add_argument("--tx-csv", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    count = export_tx_receipts(args.chain, args.tx_csv, args.out, build_web3(args.chain))
    print(f"wrote {count} receipt rows to {args.out}")


def _write_receipt_rows(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECEIPT_FIELDS)
        writer.writeheader()
        for row in sorted(rows, key=_receipt_sort_key):
            writer.writerow(row)


def _receipt_sort_key(row: dict[str, str]) -> tuple[int, str]:
    return int(row["block_number"]), row["tx_hash"]


def _config_for_chain(chain: str) -> ExportPoolConfig:
    matches = [config for config in POOL_CONFIGS.values() if config.chain == chain]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one pool config for chain={chain}")
    return matches[0]


def _required(receipt: Mapping[str, object], field: str) -> object:
    try:
        return receipt[field]
    except KeyError as exc:
        raise ValueError(f"receipt missing {field}") from exc


def _int_value(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"receipt field {field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError(f"receipt field {field} must be an integer")


if __name__ == "__main__":
    main()
