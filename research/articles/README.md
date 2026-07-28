# cNGN Market-Making Article Plan

```text
CPL_EDITORIAL_STATUS: EVIDENCE_REVIEWED
CPL_PRIMARY_CLASS: inconclusive
CPL_REVERSE_CLASS: inconclusive
CPL_ARTICLE_BRANCH: leadership_unresolved
CPL_ECONOMIC_CLASS: no_net_return_improvement
CPL_ROBUSTNESS_STATUS: complete
CPL_ROBUSTNESS_FLAGS: dtw_band_unstable
CPL_SOURCE_MANIFEST: research/results/cross_pool_lead_lag/article_manifest.json
```

```text
CSH_EDITORIAL_STATUS: EVIDENCE_REVIEWED
CSH_RESEARCH_ROLE: post_hoc_exploratory
CSH_PARENT_DECISION: leadership_unresolved
CSH_PARENT_DECISION_UNCHANGED: true
CSH_SOURCE_MANIFEST: research/results/cross_pool_short_horizon_v1/short_horizon_manifest.json
CSH_MANIFEST_SHA256: 9145738bb6dd4aa84512b3f62625d779e6e4ef223f5b614e1f9facc502ab7509
```

The reviewed short-horizon extension adds response, update-incidence, latency,
staleness, and fee-only gap diagnostics. It does not change the parent
leadership conclusion or add an economic claim.

Active July 2026 sequence:

1. `01-supervised-liquidity.md`
   - Policy-facing article on supervised cNGN liquidity, central-bank trust, and
     why shallow domestic liquidity can push users toward foreign-stablecoin
     substitutes.
2. `02-backtesting-the-market-layer.md`
   - Empirical article on the Fair Price, DEX LP, and CEX execution-mode
     experiments, plus the reviewed cross-pool experiment.
   - The reviewed cross-pool conclusion is `leadership_unresolved`; the frozen
     economic result is `no_net_return_improvement`. Both remain diagnostic
     research rather than live promotion.
   - The final weighted-portfolio packages passed integrity attestation, but
     both failed the pre-specified claim gate. The article records an
     integrity-attested non-result and reports no portfolio performance.

Supporting evidence:

- `evidence-pack-2026-07-cngn-market-making.md`
- `cto-final-research-brief-2026-07.md`
- `docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md`
- `../results/cross_pool_short_horizon_v1/short_horizon_manifest.json`
- `../results/reports/cross_pool_short_horizon_v1/short_horizon_manifest.json`

The parent run's generated manifest and reports remain local under
`../results/cross_pool_lead_lag/`; the evidence pack and CTO brief bind the
reviewed manifest by SHA-256. The parent manifest is too large for the
publication mirror because it contains the full coverage witness. It remains
ignored research output; `.gitignore` is unchanged.

Tracked publication artifacts:

- `../results/reports/cross_pool_lead_lag/`: the three parent figures cited by
  the CTO brief.
- `../results/reports/cross_pool_short_horizon_v1/`: the reviewed extension
  manifest and three cited figures.

The CTO brief is the compact engineering handoff. It explains all completed
hypotheses, exact outcomes, statistical terms, figures, and failed or unfinished
work. The evidence pack remains the durable claim ledger.

Publication boundary: the reviewed manifest permits only the aggregate claim
that directional leadership remains unresolved and the independent economic
classification `no_net_return_improvement`. It forbids claims of either
directional lead, bidirectional incremental evidence, an affirmative no-lead
result, causal price discovery, toxic flow, external-LP profitability, or
deployable alpha. Implementation-specific parameters and execution details stay
out of the article. The weighted-portfolio integrity attestation does not
authorize a performance, comparator, economic, or promotion conclusion.

Archived source material:

- `archive/open-source-market-maker-source-outline.md`
- `archive/dex-lp-live-equity-curves-notes.md`

The archived files are useful for later systems or live-accounting pieces, but
they are not part of the current two-article publication sequence.
