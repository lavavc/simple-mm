# Autoresearch

This folder holds the active empirical workflows for improving the engine.
Historical progress notes live in `autoresearch/archive/`.

## Active Pipelines

| Pipeline | Current guide | Purpose |
|---|---|---|
| Data methodology | `autoresearch/data-methodology-refactor.md` | Define the raw, bridged, and derived datasets needed for rigorous LP and Fair Value hypothesis tests. |
| Fair price | `autoresearch/fair-price.md` | Validate CEX-led executable fair-price estimators and short-run imbalance signals. |
| DEX LP | `autoresearch/lp.md` | Validate LP range, sizing, and rebalance policy hypotheses with walk-forward backtests. |
| Quidax latency | `autoresearch/quidax-latency.md` | Measure execution latency so markout horizons and execution-risk assumptions are defensible. |

## Rules

- Treat every strategy change as a falsifiable hypothesis.
- Use only information available at the decision timestamp.
- Prefer walk-forward validation over random splits.
- Keep DEX LP research venue-local unless a cross-subsystem dependency is explicitly justified.
- Promote research outputs into live engine behavior only after validation evidence survives the relevant out-of-sample checks.
