# Directional Paper LP Experiment Design

## Goal

Test whether DEX-only cNGN LP returns can be improved by allowing directional
Uniswap v4 ranges instead of centered paper ranges. The experiment should answer
whether Base has a causal, costed LP policy that beats centered static LP and
pool-mark hold, especially in windows where cNGN price moves down or mean
reverts.

## Current Evidence

The frozen paper family is not promotable as-is:

- Base strict-gate frozen paper equals mark-only static LP in every active
  window, so the tested active exits add no value.
- Base strict-gate frozen paper beats pool-mark hold by only `0.0066`
  percentage points across five active windows.
- BSC paper remains negative under every tested gate and should be treated as a
  falsification pool, not as the target.
- With hindsight, Base's best frozen-paper config per window beats pool-mark
  hold over all 26 windows (`+2.087%` vs `+1.643%`) and is profitable in 5 of
  11 down-price windows. That is an upper bound, not a deployable result, but it
  indicates that causal regime selection and range shape may matter.

## Strategy Additions

Add a reduced `directional_paper_lp` experiment family with three directional
range archetypes:

- `upside_capture`: shift the range above spot or widen the upper side when
  DEX-only features imply positive cNGN markout. This mode should reduce the
  current centered-LP failure of selling too much cNGN during upside windows.
- `dip_accumulator`: shift the range below spot or widen the lower side when
  train price is flat/down and flow or QTS features imply mean reversion. This
  mode intentionally profits from down moves only when fee capture and rebound
  offset inventory loss.
- `fee_box`: use a tight near-spot box only when fee intensity and volume are
  high enough to justify churn after entry and exit costs.

The strategy may choose `no_position` when features do not map cleanly to one of
the three archetypes. Skipping positive-markout windows is acceptable if the
available LP range would predictably underperform pool-mark hold.

## Data Flow

Use existing DEX-only feature sources:

- `research/data/derived/uni_base_pool_features.csv`
- `research/data/derived/uni_bsc_pool_features.csv`
- `research/data/derived/uni_base_flow_markout_features.csv`
- `research/data/derived/uni_bsc_flow_markout_features.csv`

The entry join remains causal: only train-window features, entry-row features,
and QTS predictions available at the entry event may route the archetype.

The simulator already exposes the range controls needed for the first pass:

- `center_offset_pct`
- `lower_width_pct`
- `upper_width_pct`
- `profit_take_pnl_mode="fees"`
- `downward_range_fraction`

The first implementation should use these existing parameters before adding new
simulator semantics.

## Experiment Harness

Create a research harness parallel to the frozen-family script. It should:

1. Build the same entry-state windows and causal gates.
2. Generate a small archetype grid for Base and BSC.
3. Route each window to one archetype or no-position using simple, inspectable
   rules.
4. Simulate each routed policy and its component archetypes.
5. Emit window-level attribution and gate summaries.

Suggested output files under `research/results/flow_gated_lp/<pool>/`:

- `directional_paper_window_results.csv`
- `directional_paper_gate_summary.csv`
- `directional_paper_attribution.csv`
- `directional_paper_report.md`

## Initial Rule Candidates

Keep the first rule set intentionally small:

- `upside_capture`: strict sign-cone active and positive `predicted_markout_20_25`
  or `predicted_markout_100_25`.
- `dip_accumulator`: train price flat/down, high flow or high fee-intensity
  percentile, and non-negative short QTS markout.
- `fee_box`: high fee-intensity and high volume percentile with near-flat QTS
  markout.
- `no_position`: anything else.

These are candidates to test, not final deployment rules. The harness should
report each rule's active windows so brittle one-window discoveries are obvious.

## Success Criteria

Base directional LP is only promotable if it satisfies all of the following:

- Beats no-position after costs.
- Beats the best centered static LP baseline after costs.
- Beats pool-mark hold in positive-markout windows or skips those windows.
- Improves down-price window economics versus centered LP and hold.
- Has positive leave-one-active-window-out return under the promoted gate.
- Does not rely on a single active window for most of its return.
- Does not improve Base by making BSC look equally attractive; BSC should remain
  a stress/falsification pool unless it independently clears the same bar.

H12 capacity curves remain blocked until these criteria are met.

## How Down-Price Profit Is Allowed

Directional LP can profit when cNGN price goes down only through fee carry plus
controlled inventory exposure. The experiment should distinguish:

- healthy down-window profit: fees exceed inventory/divergence loss and the
  position exits before the down move persists;
- unhealthy down-window loss masking: routed hold looks bad only because same
  pool entry/exit costs are punitive;
- directional inventory bet: a below-spot range intentionally accumulates cNGN
  and should be judged against hold-mark exposure.

Down-price profit is not evidence of bearish alpha unless the LP position has a
documented exposure shape that benefits from the path, not just the final mark.

## Testing

Add tests before implementation for:

- directional archetype config generation, including asymmetric width and
  center offset fields;
- causal rule routing from synthetic entry states;
- `no_position` fallback when no rule matches;
- attribution rows that separate centered static, directional LP, hold-mark, and
  down-window deltas;
- missing comparator failures.

Then run focused tests and lint for the new harness and modified helper code.

## Scope Boundaries

This experiment does not unblock CEX fair-price research, does not add external
cNGN price series, and does not run H12 as a promotion step. It also does not
broaden the full grid. The first pass is a reduced, interpretable directional
family using current DEX-only data and existing simulator controls.
