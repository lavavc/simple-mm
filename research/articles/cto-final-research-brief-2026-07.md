# CTO Final Research Brief: cNGN Market-Layer Research

Date: 2026-07-27
Status: internal decision note; evidence reviewed or integrity-attested as labeled

## Decision headline

Do not use cross-pool directionality or the weighted-portfolio study to change
live LP, arbitrage, routing, or capital-allocation policy. The strongest result
is methodological: the pipeline can fail closed, distinguish observed updates
from carried states, and show where data freshness limits interpretation. Fund
better measurement and finish the article from the sealed evidence; do not turn
the diagnostics into a signal.

## Decisions the research was meant to inform

The work tested whether one cNGN pool adds stable information about the other,
whether that information improves a frozen LP policy, and whether multiple LP
positions can be evaluated jointly under one accounting and allocation frame.
It also reconciled Girum's independent venue analysis and measured responses at
30 seconds through 15 minutes.

## Frozen confirmatory result

At the pre-specified one-hour horizon, adding cross-pool features made mean
absolute error worse than the target-only baseline in both directions. The
BSC-to-Base MAE difference was -0.241675 bps (95% [-0.289590, -0.198156]);
Base-to-BSC was -0.587238 bps (95% [-0.734881, -0.466305]). Both directions are
contractually `inconclusive`, constrained-DTW is band-unstable, and the reviewed
publication branch is `leadership_unresolved`—not an affirmative finding that no
lead exists.

The separate frozen Base LP gate also failed to improve economics: aggregate net
return was +0.515% for the original policy and +0.310% for the gated policy
across the same 23 reset-capital windows. The reviewed economic class is
`no_net_return_improvement`.

## Post-hoc exploratory extension

The extension contains 123 BSC-to-Base shocks across 59 UTC days and 197
Base-to-BSC shocks across 70 days, with no exclusions. It uses 2,000 paired UTC
shock-day bootstrap resamples. It preserves the parent's exact 15-minute anchor
and does not change the parent conclusion.

An **unconditional response** averages every eligible shock. If the target pool
has no new observation by a horizon, its as-of state is unchanged and contributes
a valid zero response. A **conditional response** averages only the selected
subset with an observed target update. A missing update is right-censored, not a
conditional zero and not a survival estimate. **Update incidence** is the number
of observed target updates divided by eligible shocks.

### Figure 1 — Unconditional response profile

![Unconditional short-horizon response profile](../results/reports/cross_pool_short_horizon_v1/short_horizon_response.png)

The blue points are unconditional means; blue bands are pointwise 95% intervals;
amber bands are simultaneous max-z 95% intervals over the seven-horizon family.

| Direction | 3-minute mean and pointwise 95% | 15-minute mean and pointwise 95% |
| --- | ---: | ---: |
| BSC to Base | +0.204359 bps [−0.108480, +0.598811] | +0.878230 bps [−1.559177, +3.381749] |
| Base to BSC | +0.035202 bps [−0.941110, +1.156460] | +0.039554 bps [−1.886550, +2.117268] |

Every unconditional simultaneous band includes zero. The line shapes are
descriptive; the graph does not identify a directional leader.

### Figure 2 — Updates, latency, and state age

![Target update, latency, and staleness diagnostics](../results/reports/cross_pool_short_horizon_v1/short_horizon_updates.png)

This four-panel graph contains point estimates only. The upper-left panel shows
update incidence; by 15 minutes it is 39/123 (31.7%) for BSC to Base and 63/197
(32.0%) for Base to BSC. The upper-right panel shows conditional first-update
responses, +2.382769 bps and -1.237692 bps at 15 minutes. Those values describe
the selected subset that updated, not the market-wide response. The lower-left
panel shows median observed-update delay, 138 and 228 seconds. The lower-right
panel shows median target-state age, 23.5 and 35.6 minutes at the 15-minute
horizon. State age diagnoses observation freshness; it is not causal transmission
latency and does not prove the underlying market was stale. Base-to-BSC
conditional simultaneous inference is unavailable because too few UTC days have
an observed update at the shortest horizon.

### Figure 3 — Signed fee-only gap

![Fee-only cross-venue gap](../results/reports/cross_pool_short_horizon_v1/short_horizon_fee_gap.png)

This graph applies both pool fees under USDC/USDT parity. It is not net
executable profit: gas, slippage, latency, inventory constraints, fill risk, and
stablecoin basis are absent. At 15 minutes, the mean BSC-to-Base signed gap moves
from -14.774555 to -19.003638 bps; Base-to-BSC moves from -18.949015 to
-20.881117 bps. The red start-minus-end values are +4.229084 and +1.932103 bps.
Both gaps remain negative and become more negative, so a positive red value does
not mean the gap closed toward zero, became executable, or produced profit.

Girum's de-clustered three-minute means were +3.4 bps Base to BSC and +0.4 bps
BSC to Base. This extension reports +0.035202 and +0.204359 bps. Signs agree,
but magnitude ordering reverses. Different shocks, samples, clustering, and
inference make this a descriptive comparison, not corroboration of Base
leadership.

## Weighted-portfolio result

The joint study completed 26 Base windows and 37 BSC windows over 4,895 canonical
economic units per pool. The candidate reset matrices were complete, but the
allocation-rule reset matrices were invalid and incomplete; carried paths were
path-dependent and not applicable to that PBO test. Artifact integrity passed
for both packages, but the default claim gate failed for both. Therefore no
weighted-portfolio performance result is reportable. Do not substitute selected
candidate, allocation, comparator, return, or drawdown figures for the failed
portfolio claim.

A separate pool-local policy-transfer diagnostic remains publishable only with
its qualification: +1.039% across four active Base windows and -1.272% across
seven BSC windows. The policy did not transfer across pools and is not a live LP
recommendation.

## Recommended decisions

1. **No deployment decision:** make no live LP, routing, arbitrage, or allocation
   change from these studies. Keep LP accounting pool-separated, and do not turn
   the signed fee-only gap into a route without contemporaneous gas, slippage,
   fillability, inventory, latency, and basis.
2. **Fund measurement, not a signal:** capture venue-native update time,
   endpoint availability, state age, stablecoin basis, executable depth, and
   realized fills. The evidence identifies an observability gap, not a tradeable
   lag.
3. **Keep portfolio research quarantined:** preserve the failed claim gate and
   do not report private performance cells. Reopen it only after correcting the
   allocation-rule matrix contract under independent review.
4. **Finish the article from the sealed evidence:** publish the disciplined
   non-result and its measurement lessons without operational details. Reopen
   directionality only with materially fresher overlapping data or a new
   pre-registered mechanism.

## Limitations and prohibited inferences

The evidence does not establish causal price discovery, does not establish a
unique directional leader, does not establish deployable alpha, does not
establish toxic-flow attribution, does not establish external-LP profitability,
does not establish net executable profit, and does not establish an affirmative
no-lead result. USDC/USDT parity remains an assumption. Recorded state age is not
the same thing as latent market inactivity.

## Evidence ledger

- Parent reviewed manifest:
  `research/results/cross_pool_lead_lag/article_manifest.json`, SHA-256
  `adf4fd71fd33604cee70fcba7aa90d2b7763715d3cba68a868d26347c599abfe`.
- Reviewed short-horizon manifest:
  `research/results/cross_pool_short_horizon_v1/short_horizon_manifest.json`,
  SHA-256
  `9145738bb6dd4aa84512b3f62625d779e6e4ef223f5b614e1f9facc502ab7509`.
- Tracked response, update, and fee-gap figures: SHA-256
  `2ef9ec2b7da282dccc2bd0ac4dc6f706b393298389620deca3b2f9ef26d5f99a`,
  `7efeae4b654f55137de04186640b7d3b5674f0663fbd9d3205526bbc59172433`,
  and
  `ff2fe442578bd04bfbd6cb099cb2d01d19f6c118cba1e921c65e64be57b2e09c`.
- Base and BSC portfolio manifests: SHA-256
  `6ce68fa8c0a05cb6339d223e9558aa9f5a425a5205de6d5b8bdddce252241ac4`
  and
  `81c3f01c496f137dcaf1eb7c25b425b05eb5c93adc08fd20f5397cedcf893dc7`.
- Sealed article evidence pack:
  `research/articles/evidence-pack-2026-07-cngn-market-making.md`, SHA-256
  `50737e3c513b100f6d1907777f2da6fa71df485793fdaf1694ed3f61b55fe8bf`.
