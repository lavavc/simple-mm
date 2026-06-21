import csv
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.replay_pool_history_prices import replay_pool_history_prices

REPO_ROOT = Path(__file__).resolve().parents[1]


FIELDNAMES = [
    "block_time",
    "chain",
    "pool_id",
    "event_type",
    "tx_hash",
    "log_index",
    "block_number",
    "sqrt_price_x96",
    "tick",
    "active_liquidity",
    "fee_rate",
    "amount0",
    "amount1",
    "amount_usd",
    "cngn_usd_price",
    "token0_symbol",
    "token1_symbol",
]


def _write_rows(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def test_replay_pool_history_prices_rewrites_liquidity_rows_and_sqrt_mid_prices(tmp_path):
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    init_sqrt = str(2**96)
    swap_sqrt = str(2 * 2**96)
    common = {
        "block_time": "2026-01-01T00:00:00+00:00",
        "chain": "base",
        "pool_id": "0xpool",
        "active_liquidity": "1000",
        "fee_rate": "0.0015",
        "amount0": "0.0",
        "amount1": "0.0",
        "amount_usd": "0.0",
        "token0_symbol": "cNGN",
        "token1_symbol": "USDC",
    }
    _write_rows(
        input_path,
        [
            {
                **common,
                "event_type": "initialize",
                "tx_hash": "0x1",
                "log_index": "1",
                "block_number": "100",
                "sqrt_price_x96": init_sqrt,
                "tick": "0",
                "cngn_usd_price": "1.0",
            },
            {
                **common,
                "event_type": "mint",
                "tx_hash": "0x1",
                "log_index": "2",
                "block_number": "100",
                "sqrt_price_x96": swap_sqrt,
                "tick": "2",
                "cngn_usd_price": "4.0",
            },
            {
                **common,
                "event_type": "swap",
                "tx_hash": "0x2",
                "log_index": "3",
                "block_number": "100",
                "sqrt_price_x96": swap_sqrt,
                "tick": "2",
                "cngn_usd_price": "0.5",
            },
        ],
    )

    summary = replay_pool_history_prices(input_path, output_path, "uni-base")
    rows = list(csv.DictReader(output_path.open()))

    assert summary.row_count == 3
    assert summary.changed_price_rows == 2
    assert rows[1]["event_type"] == "mint"
    assert rows[1]["sqrt_price_x96"] == init_sqrt
    assert rows[1]["tick"] == "0"
    assert rows[1]["cngn_usd_price"] == "1.0"
    assert rows[2]["event_type"] == "swap"
    assert rows[2]["sqrt_price_x96"] == swap_sqrt
    assert rows[2]["cngn_usd_price"] == "4.0"


def test_replay_pool_history_prices_fails_when_liquidity_precedes_price_state(tmp_path):
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    _write_rows(
        input_path,
        [
            {
                "block_time": "2026-01-01T00:00:00+00:00",
                "chain": "base",
                "pool_id": "0xpool",
                "event_type": "mint",
                "tx_hash": "0x1",
                "log_index": "1",
                "block_number": "100",
                "sqrt_price_x96": str(2**96),
                "tick": "0",
                "active_liquidity": "1000",
                "fee_rate": "0.0015",
                "amount0": "0.0",
                "amount1": "0.0",
                "amount_usd": "0.0",
                "cngn_usd_price": "1.0",
                "token0_symbol": "cNGN",
                "token1_symbol": "USDC",
            }
        ],
    )

    with pytest.raises(ValueError, match="no pool price state"):
        replay_pool_history_prices(input_path, output_path, "uni-base")


def test_replay_pool_history_prices_cli_bootstraps_repo_imports(tmp_path):
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    _write_rows(
        input_path,
        [
            {
                "block_time": "2026-01-01T00:00:00+00:00",
                "chain": "base",
                "pool_id": "0xpool",
                "event_type": "initialize",
                "tx_hash": "0x1",
                "log_index": "1",
                "block_number": "100",
                "sqrt_price_x96": str(2**96),
                "tick": "0",
                "active_liquidity": "1000",
                "fee_rate": "0.0015",
                "amount0": "0.0",
                "amount1": "0.0",
                "amount_usd": "0.0",
                "cngn_usd_price": "1.0",
                "token0_symbol": "cNGN",
                "token1_symbol": "USDC",
            }
        ],
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/replay_pool_history_prices.py",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--pool",
            "uni-base",
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert output_path.exists()
