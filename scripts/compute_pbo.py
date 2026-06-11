"""CSCV probability-of-backtest-overfitting report for a walk-forward matrix CSV.

Usage:
    python scripts/compute_pbo.py --matrix backtester/results/<...>_matrix.csv \
        [--metric validation_composite] [--partitions 8] [--output report.json]

The matrix CSV comes from `python -m backtester.run ... --walkforward
--matrix-output <path>` and holds every grid config evaluated on every valid
validation window.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtester.pbo import compute_pbo

WINDOW_FIELDS = {"window_index", "window_start", "window_end", "val_swap_count"}


def load_matrix(path: str, metric: str) -> tuple[list[list[float]], list[tuple], list[int]]:
    """Pivot the long-format matrix CSV into matrix[config][window] for ``metric``.

    Fails loudly if any config is missing any window — a ragged matrix means
    the export was interrupted or mixed runs were concatenated.
    """
    by_config: dict[tuple, dict[int, float]] = defaultdict(dict)
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or metric not in reader.fieldnames:
            raise SystemExit(f"column {metric!r} not found in {path}")
        param_fields = [
            name
            for name in reader.fieldnames
            if name not in WINDOW_FIELDS and not name.startswith("validation_")
        ]
        for row in reader:
            key = tuple(row[name] for name in param_fields)
            window_index = int(row["window_index"])
            if window_index in by_config[key]:
                raise SystemExit(
                    f"duplicate (config, window={window_index}) row — mixed runs in {path}?"
                )
            by_config[key][window_index] = float(row[metric])

    window_sets = {frozenset(windows) for windows in by_config.values()}
    if len(window_sets) != 1:
        raise SystemExit("ragged matrix: configs cover different window sets")
    window_order = sorted(next(iter(window_sets)))
    config_keys = sorted(by_config)
    matrix = [[by_config[key][w] for w in window_order] for key in config_keys]
    return matrix, config_keys, window_order


def main() -> None:
    parser = argparse.ArgumentParser(description="CSCV PBO over a configs x windows matrix")
    parser.add_argument("--matrix", required=True)
    parser.add_argument(
        "--metric",
        default="validation_composite",
        help="matrix column to rank configs by (default mirrors training selection)",
    )
    parser.add_argument("--partitions", type=int, default=8)
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()

    matrix, _, window_order = load_matrix(args.matrix, args.metric)
    result = compute_pbo(matrix, partitions=args.partitions)

    print(f"matrix: {result.config_count} configs x {result.window_count} windows ({args.metric})")
    print(f"window indexes: {window_order[0]}..{window_order[-1]}")
    print(f"partitions: {result.partitions} -> {result.combination_count} train/test splits")
    print(f"PBO (P[logit <= 0]):        {result.pbo:.3f}")
    print(f"mean / median logit:        {result.mean_logit:.3f} / {result.median_logit:.3f}")
    print(f"mean OOS rank percentile:   {result.mean_oos_rank_percentile:.3f}")
    print(f"P[OOS mean < 0 | selected]: {result.oos_loss_probability:.3f}")
    print(f"degradation OOS~IS slope:   {result.degradation_slope:.3f} (intercept {result.degradation_intercept:.5f})")

    if args.output:
        report = {**asdict(result), "metric": args.metric, "matrix": args.matrix}
        del report["logits"]
        Path(args.output).write_text(json.dumps(report, indent=2))
        print(f"report written to {args.output}")


if __name__ == "__main__":
    main()
