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

Full walk-forward:

```bash
OUT=research/results/extended_dex_only_full \
RUN_CAPACITY=0 \
PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
zsh research/scripts/run_extended_rerun.sh
```

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

## Full Walk-Forward Results

Full run window counts:

- Base: `26` valid swap-count windows per style.
- BSC: `37` valid swap-count windows per style.

`RUN_CAPACITY=0` skipped H12 as planned.

| Pool | Mode | PBO | Mean OOS rank pct | P[OOS mean < 0 selected] | Costed return on capital | Mean APY | Fee/cost ratio | Windows above sGHO | Unexplained jumps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Base | EWMA | 0.000 | 0.962 | 1.000 | -0.000483 | -2.28% | 1.140 | 13/26 | 67 |
| Base | Paper | 0.429 | 0.513 | 1.000 | -0.000177 | 1.08% | 1.213 | 11/26 | 41 |
| BSC | EWMA | 0.186 | 0.668 | 1.000 | -0.000474 | -7.58% | 0.858 | 9/37 | 64 |
| BSC | Paper | 0.000 | 0.955 | 1.000 | -0.000564 | -0.32% | 0.779 | 10/37 | 56 |

Interpretation:

- None of the four slices should be promoted to capital allocation. The rank
  stability in Base EWMA and BSC Paper is real, but the selected OOS economics
  are negative in every CSCV split.
- Base Paper has the least negative costed return and much lower churn than
  EWMA, but its PBO and jump diagnostics are not acceptable.
- Mean APY can look less bad than return on capital for short windows because
  annualization is nonlinear. The costed return, sGHO excess, and selected OOS
  loss probability are the gating metrics.
- H12 should not be run as a promotion step yet. Capacity curves are still
  useful diagnostically, but the current winner set is not frozen.

## Selected Configuration Synthesis

The tables below summarize the rank-1 training winner per validation window.
The broader top-100 validation candidate sets are used only to judge whether the
rank-1 selections are isolated or part of a shared cluster.

### EWMA Style

| Metric | Base EWMA | BSC EWMA |
| --- | ---: | ---: |
| Rank-1 unique configs | 26/26 | 25/37 |
| Exact rank-1 overlap across pools | 2 configs | 2 configs |
| Top-100 unique configs | 1131/2600 | 1358/3700 |
| Top-100 overlap across pools | 818 configs | 818 configs |
| `sd_multiplier >= 2.0` | 18/26 | 18/37 |
| `sd_multiplier <= 0.75` | 5/26 | 13/37 |
| `ewma_lambda == 0.95` | 15/26 | 27/37 |
| `rebalance_threshold_pct >= 10` | 20/26 | 17/37 |

EWMA story:

- Both pools use volatility ranges around an EWMA center and often prefer the
  fastest tested memory, `ewma_lambda=0.95`.
- Exact deployable choices are not consistent. Base selects a different rank-1
  config in every window; BSC repeats more, but mostly through a narrow/tight
  cluster and a wide/sticky cluster.
- Base leans wider and more patient. BSC is bimodal: many ultra-tight `0.5`
  sigma choices, plus several wide `2.75` to `3.0` sigma choices.
- This looks like regime-fitting, not a stable shared EWMA policy. The top-100
  overlap says the same broad search space is relevant to both pools, but the
  rank-1 surface is too jagged to codify directly.

### Paper Style

| Metric | Base Paper | BSC Paper |
| --- | ---: | ---: |
| Rank-1 unique configs | 19/26 | 19/37 |
| Exact rank-1 overlap across pools | 5 configs | 5 configs |
| Top-100 unique configs | 1317/2600 | 1101/3700 |
| Top-100 overlap across pools | 740 configs | 740 configs |
| Spot-centered rank-1 configs | 20/26 | 26/37 |
| Width `<= 0.5%` | 8/26 | 29/37 |
| Width `1%` to `2%` | 16/26 | 4/37 |
| Profit take `0.5%` | 13/26 | 29/37 |
| Stop loss `-0.25%` | 19/26 | 29/37 |
| Zero overshoot | 23/26 | 26/37 |

Paper-style story:

- The exit discipline is highly consistent across pools: spot-centered ranges
  are common, `harvest_upward_range_fraction=0.04` wins every rank-1 window,
  exit confirmation is always one swap, exit price mode is always spot, and the
  selected exits usually use tight stops with little or no overshoot.
- Width is not shared. Base mostly wants mid-width `1%` to `2%` ranges; BSC
  mostly wants narrow `0.25%` to `0.5%` ranges.
- The most defensible next hypothesis is therefore not one global paper config.
  It is a shared paper-style exit family with pool-specific width and possibly
  pool-specific profit-take tightness.

## Revised Next Gate

1. Reject the current four full-grid rank-1 winner streams as deployable
   strategies.
2. Collapse the search into low-dimensional hypotheses:
   - Paper-style exit family with shared exit discipline and pool-specific width.
   - EWMA family only if width/threshold choices can be tied to DEX-only stress
     features ex ante.
3. Re-run a reduced-grid or frozen-family validation with explicit stress
   buckets and cost decomposition before H12.
4. Run H12 only on accepted frozen configs, or run it explicitly as a diagnostic
   capacity curve with no deployment interpretation.
5. Dynamic sizing should be tested against the best constant policy from the
   reduced/frozen family, not against the unstable full-grid rank-1 stream.
