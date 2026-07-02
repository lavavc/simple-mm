#!/bin/zsh
# DEX-only extended re-run: four swap-count walk-forwards with full-grid
# validation matrices, CSCV PBO per pool/mode, DEX-side regime diagnostics,
# and the H12 capital sweep. Gas defaults are the H7-calibrated per-pool
# values in research.backtester.run.
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=${OUT:-research/results/extended_dex_only}
PYTHON=${PYTHON:-python3}
PBO_PARTITIONS=${PBO_PARTITIONS:-8}
RUN_CAPACITY=${RUN_CAPACITY:-1}
mkdir -p "$OUT"

REGIME_FIELDS=realized_volatility_cone_pct_1h,active_liquidity_cone_pct_1h,active_liquidity_running_max_share_cone_pct_1h,swap_flow_imbalance_cone_pct_1h,fee_intensity_proxy_cone_pct_1h,volume_cone_pct_1h
MAX_WINDOWS_ARGS=()
if [[ -n "${MAX_WINDOWS:-}" ]]; then
  MAX_WINDOWS_ARGS=(--max-windows "$MAX_WINDOWS")
fi

run_wf() {
  local pool=$1 mode=$2 capital=$3 train=$4 val=$5
  local prefix=${pool//-/_}
  echo "=== $pool $mode walk-forward ==="
  "$PYTHON" -m research.backtester.run \
    --csv "research/data/derived/${prefix}_pool_history_replay.csv" \
    --dataset-format v4 --pool "$pool" \
    --strategy-mode "$mode" --initial-capital-usd "$capital" \
    --walkforward --window-mode swap_count \
    --train-swaps "$train" --val-swaps "$val" --stride-swaps "$val" \
    --min-train-swaps "$train" --min-train-liquidity-events 0 --min-val-swaps "$val" \
    --top-n 100 \
    --matrix-output "$OUT/${prefix}_${mode}_matrix.csv" \
    --output "$OUT/${prefix}_${mode}_swapwf.csv" \
    "${MAX_WINDOWS_ARGS[@]}"
  if [[ -n "${MAX_WINDOWS:-}" && "$MAX_WINDOWS" -lt "$PBO_PARTITIONS" ]]; then
    echo "skipping PBO for $pool $mode: MAX_WINDOWS=$MAX_WINDOWS < PBO_PARTITIONS=$PBO_PARTITIONS" \
      | tee "$OUT/${prefix}_${mode}_pbo.txt"
  else
    "$PYTHON" research/scripts/compute_pbo.py \
      --matrix "$OUT/${prefix}_${mode}_matrix.csv" \
      --partitions "$PBO_PARTITIONS" \
      --output "$OUT/${prefix}_${mode}_pbo.json" \
      | tee "$OUT/${prefix}_${mode}_pbo.txt"
  fi
  "$PYTHON" research/scripts/report_backtest_regime_stability.py \
    --windows "$OUT/${prefix}_${mode}_swapwf_windows.csv" \
    --features "research/data/derived/${prefix}_pool_features.csv" \
    --out "$OUT/${prefix}_${mode}_regime_stability.md" \
    --regime-fields "$REGIME_FIELDS"
}

run_wf uni-base ewma  1200 200 50
run_wf uni-base paper 1200 200 50
run_wf uni-bsc  ewma  450 300 75
run_wf uni-bsc  paper 450 300 75

if [[ "$RUN_CAPACITY" == "1" ]]; then
  echo "=== H12 capital sweep (frozen winners; re-freeze if the WF revises them) ==="
  "$PYTHON" research/scripts/h12_capital_sweep.py --output "$OUT/capacity_curve.csv"
  "$PYTHON" research/scripts/h12_capacity_analysis.py --artifact "$OUT/capacity_curve.csv" | tee "$OUT/h12_analysis.txt"
else
  echo "skipping H12 capacity sweep: RUN_CAPACITY=$RUN_CAPACITY"
fi

echo "done: $OUT"
