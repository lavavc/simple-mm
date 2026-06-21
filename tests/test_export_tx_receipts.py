import csv
from pathlib import Path
from typing import Mapping

import pytest

from scripts.export_tx_receipts import (
    export_tx_receipts,
    read_unique_tx_hashes,
    receipt_row,
)


class FakeEth:
    def __init__(self, receipts: Mapping[str, Mapping[str, object] | None]) -> None:
        self.receipts = receipts
        self.calls: list[str] = []

    def get_transaction_receipt(self, tx_hash: str) -> Mapping[str, object] | None:
        self.calls.append(tx_hash)
        return self.receipts[tx_hash]


class FakeWeb3:
    def __init__(self, receipts: Mapping[str, Mapping[str, object] | None]) -> None:
        self.eth = FakeEth(receipts)


def test_receipt_row_computes_native_fee() -> None:
    row = receipt_row(
        "base",
        {
            "transactionHash": "0x1",
            "blockNumber": 10,
            "gasUsed": 21_000,
            "effectiveGasPrice": 2_000_000_000,
            "from": "0xfrom",
            "to": "0xto",
        },
    )

    assert row["native_fee_wei"] == "42000000000000"
    assert row["tx_hash"] == "0x1"


def test_read_unique_tx_hashes_deduplicates_in_csv_order(tmp_path: Path) -> None:
    tx_csv = tmp_path / "ledger.csv"
    tx_csv.write_text(
        "chain,tx_hash,block_number\n"
        "base,0xA,10\n"
        "base,0xa,10\n"
        "base,0xB,11\n"
    )

    assert read_unique_tx_hashes(tx_csv) == ["0xa", "0xb"]


def test_export_tx_receipts_deduplicates_calls_and_writes_sorted_rows(tmp_path: Path) -> None:
    tx_csv = tmp_path / "ledger.csv"
    output = tmp_path / "receipts.csv"
    tx_csv.write_text(
        "chain,tx_hash,block_number\n"
        "base,0xb,11\n"
        "base,0xa,10\n"
        "base,0xA,10\n"
    )
    fake_web3 = FakeWeb3(
        {
            "0xb": {
                "transactionHash": "0xb",
                "blockNumber": 11,
                "gasUsed": 3,
                "effectiveGasPrice": 5,
                "from": "0xfromb",
                "to": "0xtob",
            },
            "0xa": {
                "transactionHash": "0xa",
                "blockNumber": 10,
                "gasUsed": 2,
                "effectiveGasPrice": 7,
                "from": "0xfroma",
                "to": "0xtoa",
            },
        }
    )

    count = export_tx_receipts("base", tx_csv, output, fake_web3)

    assert count == 2
    assert fake_web3.eth.calls == ["0xb", "0xa"]
    rows = list(csv.DictReader(output.open()))
    assert [row["tx_hash"] for row in rows] == ["0xa", "0xb"]
    assert rows[0]["native_fee_wei"] == "14"
    assert rows[1]["native_fee_wei"] == "15"


def test_export_tx_receipts_fails_when_receipt_is_missing(tmp_path: Path) -> None:
    tx_csv = tmp_path / "ledger.csv"
    output = tmp_path / "receipts.csv"
    tx_csv.write_text("chain,tx_hash,block_number\nbase,0xa,10\n")
    fake_web3 = FakeWeb3({"0xa": None})

    with pytest.raises(ValueError, match="missing receipt"):
        export_tx_receipts("base", tx_csv, output, fake_web3)
