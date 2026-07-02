import subprocess
import sys
from pathlib import Path


def test_report_pool_feature_stress_slices_groups_calendar_buckets(
    tmp_path: Path,
) -> None:
    features_path = tmp_path / "features.csv"
    out_path = tmp_path / "stress.md"
    features_path.write_text(
        "timestamp_ms,pool,realized_volatility_cone_pct,dex_premium_cone_pct_1h\n"
        "1767225600000,uni-base,0.05,0.95\n"
        "1767229200000,uni-base,0.50,\n"
        "1767312000000,uni-base,0.91,0.10\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[2]
                / "research/scripts/report_pool_feature_stress_slices.py",
            ),
            "--features",
            str(features_path),
            "--out",
            str(out_path),
            "--fields",
            "realized_volatility_cone_pct,dex_premium_cone_pct_1h",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    markdown = out_path.read_text()

    assert "# Pool Feature Stress Slices" in markdown
    assert (
        "| 2026-01-01 | uni-base | realized_volatility_cone_pct | "
        "1 | 1 | 0 | 0 | 2 |"
    ) in markdown
    assert "| 2026-01-01 | uni-base | dex_premium_cone_pct_1h | 0 | 0 | 1 | 1 | 2 |" in markdown
    assert (
        "| 2026-01-02 | uni-base | realized_volatility_cone_pct | "
        "0 | 0 | 1 | 0 | 1 |"
    ) in markdown
    assert "| 2026-01-02 | uni-base | dex_premium_cone_pct_1h | 1 | 0 | 0 | 0 | 1 |" in markdown


def test_report_pool_feature_stress_slices_fails_on_missing_fields(
    tmp_path: Path,
) -> None:
    features_path = tmp_path / "features.csv"
    out_path = tmp_path / "stress.md"
    features_path.write_text("timestamp_ms,pool,realized_volatility_cone_pct\n")

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[2]
                / "research/scripts/report_pool_feature_stress_slices.py",
            ),
            "--features",
            str(features_path),
            "--out",
            str(out_path),
            "--fields",
            "missing_cone_pct",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "missing_cone_pct" in result.stderr
