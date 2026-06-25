# DEX-Only Extended Rerun Status

Date: 2026-06-25

Scope: DEX-only LP research. Quidax, DEX-premium, and Fair Value label
hypotheses remain deferred until historical Quidax coverage exists.

## Runner

Tracked script:

- `research/scripts/run_extended_rerun.sh`

The script reads replay-corrected pool histories and causal DEX-side feature
tables:

- `research/data/derived/uni_base_pool_history_replay.csv`
- `research/data/derived/uni_bsc_pool_history_replay.csv`
- `research/data/derived/uni_base_pool_features.csv`
- `research/data/derived/uni_bsc_pool_features.csv`

Regime fields are DEX-only:

- `realized_volatility_cone_pct_1h`
- `active_liquidity_cone_pct_1h`
- `active_liquidity_running_max_share_cone_pct_1h`
- `swap_flow_imbalance_cone_pct_1h`
- `fee_intensity_proxy_cone_pct_1h`
- `volume_cone_pct_1h`

The runner uses `PYTHON=${PYTHON:-python3}`, `set -euo pipefail`, configurable
`PBO_PARTITIONS`, and skips PBO when `MAX_WINDOWS < PBO_PARTITIONS` so smoke
runs cannot silently emit invalid CSCV output.

## Commands Run

Smoke:

```bash
OUT=research/results/extended_dex_only_smoke \
MAX_WINDOWS=1 \
RUN_CAPACITY=0 \
PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
zsh research/scripts/run_extended_rerun.sh
```

Minimum PBO-valid screen:

```bash
OUT=research/results/extended_dex_only_pbo8 \
MAX_WINDOWS=8 \
RUN_CAPACITY=0 \
PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
zsh research/scripts/run_extended_rerun.sh
```

`RUN_CAPACITY=0` was intentional for the screen; H12 should only run after the
full walk-forward confirms or revises the frozen winner set.

## PBO8 Screening Results

These are early-window screening results, not final full-history evidence.

| Pool | Mode | PBO | Mean OOS rank percentile | P[OOS mean < 0 selected] | Selected validation net return mean | Selected validation net return min | Max validation drawdown |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Base | EWMA | 0.243 | 0.657 | 0.829 | -0.001451 | -0.006849 | 0.005662 |
| Base | Paper | 0.257 | 0.642 | 0.643 | -0.000772 | -0.008214 | 0.004106 |
| BSC | EWMA | 0.414 | 0.587 | 0.914 | -0.000572 | -0.002160 | 0.001512 |
| BSC | Paper | 0.000 | 0.949 | 0.714 | -0.000428 | -0.002158 | 0.001512 |

Interpretation:

- No screened slice is deployable yet because all four selected-window means
  are negative after costs.
- BSC Paper has strong rank stability in the minimum PBO screen, but the
  selected OOS economics are still negative. Treat it as a candidate to inspect,
  not a winner.
- Base EWMA and Base Paper have modest PBO values but high selected-negative
  rates, so the rank signal is not enough to overcome costed economics.
- BSC EWMA is the weakest of the four on selection risk.

## Next Gate

Run the same script without `MAX_WINDOWS` and with `RUN_CAPACITY=0` first:

```bash
OUT=research/results/extended_dex_only_full \
RUN_CAPACITY=0 \
PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
zsh research/scripts/run_extended_rerun.sh
```

After the full walk-forward:

1. Re-freeze or reject the four winner configurations from full-history PBO,
   selected-window economics, and DEX-only regime stability.
2. Only then run H12 capacity curves on the accepted frozen configs.
3. Use H12 as the benchmark for dynamic sizing; dynamic sizing must beat the
   best constant policy out of sample, not just improve an opportunity screen.
