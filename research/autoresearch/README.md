# Autoresearch

This folder holds the active empirical workflows for improving the engine.
Historical progress notes live in `research/autoresearch/archive/`.

## Active Pipelines

| Pipeline | Current guide | Purpose |
|---|---|---|
| Data methodology | `research/autoresearch/data-methodology-refactor.md` | Define the raw, bridged, and derived datasets needed for rigorous LP and Fair Value hypothesis tests. |
| Fair price closeout | `research/autoresearch/fair-price.md` | Close Fair Price as feed-quality and market-structure evidence because no accepted non-pool label overlaps the current windows. |
| DEX LP closeout | `research/autoresearch/lp.md` | Preserve Base/BSC diagnostic LP findings while blocking live promotion until an external comparator exists. |
| Quidax CEX execution EDA | `research/autoresearch/quidax-cex-execution-eda-2026-07-10.md` | Analyze the historical Quidax top-of-book JSON sample and its limits for CEX anchor/execution tests. |
| Quidax latency | `research/autoresearch/quidax-latency.md` | Measure execution latency so markout horizons and execution-risk assumptions are defensible. |

## Closed Handoffs

| Handoff | Status | Current replacement |
|---|---|---|
| `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md` | Historical closeout; do not execute its next-session prompt. | `research/articles/cto-final-research-brief-2026-07.md` and `research/articles/evidence-pack-2026-07-cngn-market-making.md` |

## Reviewed Cross-Pool Evidence

| Role | Source | Current interpretation |
|---|---|---|
| Parent reviewed experiment | `research/results/cross_pool_lead_lag/article_manifest.json` | Directional leadership remains unresolved; the frozen forecast gate did not improve net return. |
| Reviewed short-horizon extension | `research/results/cross_pool_short_horizon_v1/short_horizon_manifest.json` | Post-hoc timing and fee-gap diagnostics only; the parent conclusion is unchanged. |
| Historical comparator | `research/autoresearch/cross-venue-lead-lag-2026-07-15.md` | Girum's hash-bound memo is preserved for comparison, not used as the active pipeline or final conclusion. |

## Closed External-Reference Acquisition

The July 2026 branch no longer includes fintech quote APIs, CBN/FMDQ/NAFEM
rates, or renewed Bybit historical searches. External-reference hooks remain
available only for genuinely new timestamped overlapping data supplied later;
acquiring that data is not an open task.

## Rules

- Treat every strategy change as a falsifiable hypothesis.
- Use only information available at the decision timestamp.
- Prefer walk-forward validation over random splits.
- Keep DEX LP research venue-local unless a cross-subsystem dependency is explicitly justified.
- Promote research outputs into live engine behavior only after validation evidence survives the relevant out-of-sample checks.
- Do not describe a result as statistically significant unless the doc states the test that produced the claim (added 2026-07-19 after a review caught an untested significance claim).
