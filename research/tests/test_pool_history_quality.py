import subprocess
import sys
from pathlib import Path

import pytest

from research.scripts.report_pool_history_quality import analyze_pool_history

CSV_HEADER = (
    "block_time,chain,pool_id,event_type,tx_hash,log_index,block_number,"
    "sqrt_price_x96,tick,active_liquidity,fee_rate,amount0,amount1,"
    "amount_usd,cngn_usd_price,token0_symbol,token1_symbol\n"
)


def _write_rows(csv_path: Path, rows: list[str]) -> None:
    csv_path.write_text(CSV_HEADER + "".join(rows))


def test_quality_report_counts_events_and_blocks(tmp_path: Path) -> None:
    csv_path = tmp_path / "pool.csv"
    _write_rows(
        csv_path,
        [
            "2026-01-01T00:00:00+00:00,base,0xpool,swap,0x1,1,10,"
            "79228162514264337593543950336,0,100,0.0015,-1,1,1,1,cNGN,USDC\n",
            "2026-01-01T00:01:00+00:00,base,0xpool,mint,0x2,2,11,"
            "79228162514264337593543950336,0,100,0.0015,0,0,0,1,cNGN,USDC\n",
        ],
    )

    report = analyze_pool_history(csv_path, "uni-base")

    assert report.row_count == 2
    assert report.event_counts == {"swap": 1, "mint": 1}
    assert report.first_block == 10
    assert report.last_block == 11
    assert report.duplicate_events == 0
    assert report.monotonic_blocks is True
    assert report.token_order_valid is True
    assert report.stored_price_model_counts == {"sqrt_mid": 2}
    assert report.legacy_amount_ratio_price_count == 0
    assert report.unexplained_price_mismatch_count == 0
    assert report.coverage_days == pytest.approx(1 / 1440)


def test_quality_report_flags_duplicate_nonmonotonic_token_and_price_anomalies(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "pool.csv"
    _write_rows(
        csv_path,
        [
            "2026-01-01T00:00:00+00:00,base,0xpool,swap,0xdup,1,12,"
            "79228162514264337593543950336,0,100,0.0015,-1,1,1,1,cNGN,USDC\n",
            "2026-01-01T00:01:00+00:00,base,0xpool,swap,0xdup,1,11,"
            "79228162514264337593543950336,0,100,0.0015,-1,1,1,2,USDC,cNGN\n",
        ],
    )

    report = analyze_pool_history(csv_path, "uni-base")

    assert report.duplicate_events == 1
    assert report.monotonic_blocks is False
    assert report.sqrt_price_mismatch_count == 1
    assert report.unexplained_price_mismatch_count == 1
    assert report.token_order_valid is False


def test_quality_report_classifies_legacy_amount_ratio_price_without_unexplained_alert(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "pool.csv"
    _write_rows(
        csv_path,
        [
            "2026-01-01T00:00:00+00:00,base,0xpool,swap,0xlegacy,1,10,"
            "79228162514264337593543950336,0,100,0.0015,-1000,998.5,998.5,0.9985,cNGN,USDC\n",
            "2026-01-01T00:01:00+00:00,base,0xpool,swap,0xsqrt,2,11,"
            "79228162514264337593543950336,0,100,0.0015,-1000,998.5,998.5,1,cNGN,USDC\n",
        ],
    )

    report = analyze_pool_history(csv_path, "uni-base")

    assert report.sqrt_price_mismatch_count == 1
    assert report.legacy_amount_ratio_price_count == 1
    assert report.unexplained_price_mismatch_count == 0
    assert report.stored_price_model_counts == {
        "sqrt_mid": 1,
        "swap_amount_ratio": 1,
    }
    assert report.legacy_amount_ratio_last_block == 10
    assert report.sqrt_mid_first_block == 11


def test_quality_report_counts_swaps_missing_active_liquidity(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "pool.csv"
    _write_rows(
        csv_path,
        [
            "2026-01-01T00:00:00+00:00,base,0xpool,swap,0x1,1,10,"
            "79228162514264337593543950336,0,,0.0015,-1,1,1,1,cNGN,USDC\n",
            "2026-01-01T00:01:00+00:00,base,0xpool,swap,0x2,2,11,"
            "79228162514264337593543950336,0,0,0.0015,-1,1,1,1,cNGN,USDC\n",
            "2026-01-01T00:02:00+00:00,base,0xpool,mint,0x3,3,12,"
            "79228162514264337593543950336,0,0,0.0015,0,0,0,1,cNGN,USDC\n",
        ],
    )

    report = analyze_pool_history(csv_path, "uni-base")

    assert report.missing_active_liquidity_swaps == 2


def test_quality_report_rejects_unknown_pool(tmp_path: Path) -> None:
    csv_path = tmp_path / "pool.csv"
    csv_path.write_text(CSV_HEADER)

    with pytest.raises(ValueError, match="Unsupported pool"):
        analyze_pool_history(csv_path, "unknown")


def test_cli_writes_markdown_report(tmp_path: Path) -> None:
    csv_path = tmp_path / "pool.csv"
    out_path = tmp_path / "quality.md"
    _write_rows(
        csv_path,
        [
            "2026-01-01T00:00:00+00:00,base,0xpool,swap,0x1,1,10,"
            "79228162514264337593543950336,0,100,0.0015,-1,1,1,1,cNGN,USDC\n",
        ],
    )

    result = subprocess.run(
        [
            sys.executable,
            "research/scripts/report_pool_history_quality.py",
            "--csv",
            str(csv_path),
            "--pool",
            "uni-base",
            "--out",
            str(out_path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    markdown = out_path.read_text()
    assert "# Pool History Quality Report" in markdown
    assert "| uni-base | 1 | 10 | 10 | True | True | 0 | 0 | 0 | 0 | 0 |" in markdown
    assert "| sqrt_mid | 1 |" in markdown
