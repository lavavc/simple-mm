# Research Results

Generated backtest outputs, plots, markout exports, and experiment artifacts belong here.

This directory is ignored by default. Check in only small files that summarize a result well
enough for review, preferably under `research/results/reports/`, plus README files that explain
how an ignored artifact was produced.

## Frozen cross-pool evidence

The July 15 Base/BSC study publishes one sealed statistical directory and then
atomically augments that same directory with the frozen Base economic test.
Run the statistical stage only from a clean committed source tree:

```bash
python3 research/scripts/run_cross_pool_lead_lag.py \
  --base-features research/data/derived/uni_base_pool_features.csv \
  --bsc-features research/data/derived/uni_bsc_pool_features.csv \
  --base-replay research/data/derived/uni_base_pool_history_replay.csv \
  --bsc-replay research/data/derived/uni_bsc_pool_history_replay.csv \
  --base-ledger research/data/derived/uni_base_lp_ledger.csv \
  --bsc-ledger research/data/derived/uni_bsc_lp_ledger.csv \
  --out-dir research/results/cross_pool_lead_lag
```

After that stage produces a QA-pass manifest with economics pending, run:

```bash
python3 research/scripts/evaluate_cross_pool_economic_lp.py \
  --predictions research/results/cross_pool_lead_lag/predictive_predictions.csv \
  --out-dir research/results/cross_pool_lead_lag
```

The economic stage accepts no pool, window, route, parameter, gas, capital, or
truncation overrides. It requires the same clean source commit and deterministic
runtime as the sealed statistical run, then analyzes private snapshots of the
prediction, Base feature, Base flow-markout feature, and Base replay inputs. It
binds the first, second, and fourth hashes to the statistical manifest, records
the flow-markout hash in the frozen economic contract, verifies both frozen
economic fingerprints and the explicit 5 bps forecast gate, and writes five
economic artifacts through an atomic directory exchange. A contract-valid bad
forecast or economic input is recorded as
`not_adjudicable_qa` without fabricated performance rows. Filesystem and
publication failures leave the pending statistical evidence unchanged.
