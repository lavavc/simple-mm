#!/bin/zsh
# Extended-dataset re-run (through 2026-06-09): four swap-count walk-forwards
# with full-grid validation matrices, CSCV PBO per pool/mode, and the H12
# capital sweep. Gas defaults are the H7-calibrated per-pool values (run.py).
set -e
cd "$(dirname "$0")/.."
OUT=backtester/results/extended_20260609
mkdir -p "$OUT"

run_wf() {
  local pool=$1 mode=$2 capital=$3 train=$4 val=$5
  echo "=== $pool $mode walk-forward ==="
  python -m backtester.run \
    --csv "data/${pool//-/_}_pool_history.csv" --dataset-format v4 --pool "$pool" \
    --strategy-mode "$mode" --initial-capital-usd "$capital" \
    --walkforward --window-mode swap_count \
    --train-swaps "$train" --val-swaps "$val" --stride-swaps "$val" \
    --min-train-swaps "$train" --min-train-liquidity-events 0 --min-val-swaps "$val" \
    --top-n 100 \
    --matrix-output "$OUT/${pool//-/_}_${mode}_matrix.csv" \
    --output "$OUT/${pool//-/_}_${mode}_swapwf.csv"
  python scripts/compute_pbo.py \
    --matrix "$OUT/${pool//-/_}_${mode}_matrix.csv" \
    --output "$OUT/${pool//-/_}_${mode}_pbo.json" \
    | tee "$OUT/${pool//-/_}_${mode}_pbo.txt"
}

run_wf uni-base ewma  1200 200 50
run_wf uni-base paper 1200 200 50
run_wf uni-bsc  ewma  450 300 75
run_wf uni-bsc  paper 450 300 75

echo "=== H12 capital sweep (frozen winners; re-freeze if the WF revises them) ==="
python scripts/h12_capital_sweep.py --output "$OUT/capacity_curve.csv"
python scripts/h12_capacity_analysis.py --artifact "$OUT/capacity_curve.csv" | tee "$OUT/h12_analysis.txt"

echo "done: $OUT"
