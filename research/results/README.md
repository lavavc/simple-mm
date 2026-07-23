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

Replay profiles are append-only provenance contracts. New replay inspection and
LP-ledger exports use `pool-history-replay-v2`; sealed v1 sidecars and reviewed
manifests remain valid through the immutable v1 registry entry. A fresh v2
replay cannot bind a v1 ledger sidecar, and a v1 exporter checkpoint cannot be
resumed as v2. The reviewed v1 evidence does not need to be rerun.

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

## Frozen weighted-portfolio evidence

The corrected Base and BSC portfolio study runs each pool independently and
publishes exactly 14 artifacts per pool only after every primary and removal
window is durable. Checkpoints are pool-local and resumable; final output
directories are created by atomic replacement.

```bash
PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  research/scripts/evaluate_parameter_portfolio.py \
  --pool uni-base \
  --out-dir research/results/parameter_portfolio \
  --checkpoint-dir research/results/parameter_portfolio_checkpoints \
  --full-run

PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  research/scripts/evaluate_parameter_portfolio.py \
  --pool uni-bsc \
  --out-dir research/results/parameter_portfolio \
  --checkpoint-dir research/results/parameter_portfolio_checkpoints \
  --full-run
```

The July 23 frozen source closure is bound to commit
`b331b432bf612ed21413d54a0fd6c0eb76b7c38f`. Validate both packages from that
unchanged closure before using them in publication:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  research/scripts/validate_parameter_portfolio_publication.py \
  --publication-root research/results/parameter_portfolio \
  --repo-root . \
  --pool all
```

The validator independently checks canonical bytes, source and input identity,
the catalog, eligibility, frozen allocation rules, comparator selection, row
coverage, carried-capital continuity, joint attribution, removal evidence, and
both reset-matrix PBO calculations. Exit zero is a publication gate: candidate
and allocation PBO must both be computed, and every required carried,
comparator, and removal path must remain valid. Structurally valid fail-closed
artifacts are preserved, but the validator emits `FAIL code=CLAIM_GATE` instead
of authorizing an article result. Add `--integrity-only` only when attesting a
diagnostic package: that mode may return `PASS` with
`evidence_status=not_publishable`, but it does not authorize article claims.
