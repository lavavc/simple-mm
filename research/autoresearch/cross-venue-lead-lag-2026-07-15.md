# Cross-Venue Lead/Lag: Pools vs Quidax Top-of-Book

Date: 2026-07-16

## Scope

Test whether the three executable venue prices (uni-base pool, uni-bsc pool,
Quidax top-of-book) co-move, and whether any venue leads the others. Runs on
the engine's `price_snapshots` quote history exported from the production DB —
a quote-process study, complementary to (not a substitute for) swap-level
cross-pool analysis.

Window: 2026-04-08 → 2026-07-16 (99.0 days, 142,568 aligned minutes). Sources:
`quidax` (top-of-book mid from the ticker), `uni-base_pool` and `uni-bsc_pool`
(sqrt-price-derived pool mids), all captured every ~30–60s by the engine and
resampled to a 1-minute last-observation grid, inverted to NGN/USD.

Structural fact that shapes everything below: Quidax's mid changed in only
**161 of 142,568 minutes (0.11%)**. Any whole-series statistic involving
Quidax is dominated by zero-return minutes; event-conditioned views are the
meaningful ones for that venue.

## Method

- 1-minute log returns per venue on the aligned grid.
- Lead/lag: peak cross-correlation over lags +1..+60 minutes, both directions,
  all venue pairs.
- Significance: block-shuffle null (60-minute blocks, 100 permutations);
  p = fraction of null peaks with |corr| ≥ observed.
- Replication: the same peak search run independently on each third of the
  window. Results whose sign/lag do not persist across thirds are reported but
  not accepted.
- Event study (Quidax): for each minute with a nonzero Quidax return,
  sign-match against the cumulative pool return over the prior 15/30/60
  minutes.

## Results

| Direction | Peak corr | Lag | p | Thirds (corr@lag) |
|---|---:|---:|---:|---|
| uni-base → uni-bsc | +0.086 | +3m | 0.00 | +0.06@33m, +0.26@3m, +0.07@3m |
| uni-bsc → uni-base | +0.050 | +16m | 0.02 | −0.05@24m, +0.06@4m, +0.08@16m |
| uni-base → quidax | +0.019 | +18m | 0.11 | +0.04@58m, 0.00@9m, +0.06@18m |
| uni-bsc → quidax | −0.031 | +44m | 0.03 | +0.05@54m, −0.05@60m, −0.08@44m |
| quidax → uni-base | +0.016 | +50m | 0.19 | −0.01@20m, +0.02@58m, +0.04@50m |
| quidax → uni-bsc | +0.013 | +49m | 0.13 | −0.01@19m, −0.01@56m, +0.03@49m |

Event study — direction of Quidax quote changes vs prior pool move:

| Pool | Prior 15m | Prior 30m | Prior 60m |
|---|---:|---:|---:|
| uni-base | 9/13 (69%) | 21/32 (66%) | 44/70 (63%) |
| uni-bsc | 15/30 (50%) | 45/74 (61%) | 68/119 (57%) |

## Accepted Findings

1. **uni-base leads uni-bsc by ~3 minutes.** Peak +0.086 at +3m, p = 0.00,
   positive in all three sub-periods (and at the same 3-minute lag in two of
   three). Price discovery for cNGN/USD concentrates on the Base pool; the BSC
   pool aligns within minutes. Candidate mechanism for cross-pool LP
   performance differences: the lagging pool is adversely selected by
   arbitrageurs more often.
2. **Quidax never leads the pools.** Near-zero correlations at every lag
   (p ≥ 0.13), consistent across sub-periods. The Quidax book is a follower —
   in line with the market-structure finding in the Quidax execution EDA that
   its top-of-book is a manually managed quote stream.
3. **When Quidax re-quotes, it usually moves in the direction the Base pool
   moved over the preceding 15–60 minutes** (69% at 15m, 66% at 30m, 63% at
   60m vs a 50% null; N = 13–70 events). Modest-N directional evidence that
   manual re-quotes follow the pools with a lag of tens of minutes. This
   latency window is the mechanism behind the engine's UNI→QUIDAX CEX-DEX
   arbitrage opportunities.

## Reported but Not Accepted

- **uni-bsc → uni-base at +16m** (+0.050, p = 0.02): sign flips across thirds
  (negative in the first), lag unstable; likely reflects bidirectional
  co-alignment rather than a genuine reverse lead.
- **uni-bsc → quidax at +44m** (−0.031, p = 0.03): sign inconsistent across
  thirds and economically unmotivated (a negative lead has no mechanism here);
  treated as a multiple-comparisons artifact.

## Limitations

- Forward-filled quote grids: Quidax's series is 99.9% zero returns, so
  whole-series correlations involving it are structurally attenuated — the
  event-conditioned view is the informative one, and it has N = 13–119
  depending on the window/pool.
- Peak-picking over 60 lags inflates nominal significance; the block-shuffle
  null mitigates but does not eliminate this.
- Quote mids, not trades — addressed by the swap-level validation below, which
  reproduces the lead/lag structure on actual pool trades.

## Swap-Level Lead/Lag Validation

Date: 2026-07-19

To address the forward-filled-quote limitation, the lead/lag question was
re-run on actual pool trades: every V4 Swap event in both pools over the same
window (1,280 Base swaps, 3,777 BSC-block-stamped swaps of 3,799 fetched),
each with its exact block timestamp and post-swap marginal price. Two
independent implementations were run (`research/scripts/run_lead_lag_analysis.py`
and a separate cross-check with a de-clustered event design); their results
agree, and all figures below reproduce from the raw data.

### Method

- Trigger: a swap moving its pool's marginal price by ≥ 10 bps versus the
  price immediately before it.
- Response: the other pool's return from just before the trigger to
  trigger + horizon (3 and 30 minutes), signed in the trigger's direction.
  Reported both as a mean response in bps and as an OLS slope (beta) of
  follower return on trigger return.
- Continuation: the trigger pool's own subsequent return over the same
  horizon — positive = the move sticks (information), negative = the move
  reverts (noise).
- Own-trade exclusion: every swap transaction's sender was fetched from the
  chain (`data/tx_senders_cache.json`, 100% coverage) and matched against the
  engine's five wallet addresses, plus tx hashes recorded in the production
  `arb_attempts`. The two filters agree exactly: 84 Base and 102 BSC swaps
  are ours (arb legs, LP ratio-swaps, and manual script trades).

### Results

Per-swap triggers (implementation A):

| Scenario | Horizon | Base→BSC beta | BSC→Base beta | Base continuation | BSC continuation |
|---|---|---:|---:|---:|---:|
| All trades (N=89 / 144) | 3 min | +26.5% | +1.5% | +2.9% | −12.5% |
| | 30 min | +57.1% | +3.3% | +12.4% | −39.5% |
| Excluding our trades (N=90 / 146) | 3 min | +24.0% | +0.6% | +3.9% | −9.7% |
| | 30 min | +55.1% | +2.2% | +13.4% | −37.3% |

De-clustered cross-check (implementation B; one trigger per 10-minute window,
so burst episodes count once — N=68 Base / 87 BSC events, excluding our
trades N=69 / 88):

| Direction | Horizon | Mean response | Beta | Trigger-pool continuation |
|---|---|---:|---:|---:|
| Base→BSC | 3 min | +3.4 bp | +20.9% | +1.1 bp |
| Base→BSC | 30 min | +9.4 bp | +49.0% | +3.8 bp |
| BSC→Base | 3 min | +0.4 bp | +0.9% | −0.6 bp |
| BSC→Base | 30 min | +1.3 bp | +3.8% | −11.5 bp |

Scenario-B note: excluding swaps changes the return series itself (removing a
swap can merge two adjacent sub-threshold moves into a new ≥10 bps trigger),
which is why the excluded scenario has one or two *more* triggers, not fewer.
The exclusion comparison is therefore "same procedure on the organic-only
tape", not "same events minus ours".

### Findings

1. **Base leads, BSC follows — on real trades.** A ≥10 bps Base move is
   followed by BSC recovering roughly a fifth to a quarter of it within 3
   minutes and about half within 30 (consistent across both implementations
   and with the quote-level result). Significance was initially untested at
   the swap level; Iteration 1 below adds a circular-shift permutation test
   (Base→BSC p < 0.002).
2. **BSC's moves are mostly noise.** The reverse direction transmits only
   2–5% at 30 minutes, and the trigger pool's own continuation splits sharply:
   Base moves continue (+3.8 bp / +13% beta at 30m) while BSC moves revert
   (−11.5 bp / −37% beta). Base-initiated moves carry information; the bulk of
   BSC-initiated moves are transient dislocations that get arbitraged back.
3. **Not our engine's artifact.** With all 186 of our swaps removed, every
   number above is essentially unchanged. The lead/lag structure is organic
   market behavior; our arbitrage accounts for only a small share of the
   correction flow.

## Autoresearch Iterations (2026-07-19)

Three self-directed follow-up iterations off the Next list, run and written
back in one loop.

### Iteration 1 — swap-level significance test

Circular time-shift null: the follower pool's entire tape is rotated by a
random offset (≥3 days, 500 draws), preserving its internal structure while
destroying alignment with triggers; the de-clustered mean response is
recomputed per draw.

| Direction | Horizon | Observed | Null mean ± sd | p |
|---|---|---:|---:|---:|
| Base→BSC | 3 min | +3.4 bp | 0.0 ± 0.3 | < 0.002 |
| Base→BSC | 30 min | +9.4 bp | 0.0 ± 0.6 | < 0.002 |
| BSC→Base | 3 min | +0.4 bp | 0.0 ± 0.1 | 0.022 |
| BSC→Base | 30 min | +1.3 bp | 0.0 ± 0.4 | 0.006 |

Refinement to Finding 2: BSC→Base transmission is statistically detectable,
not zero — but economically small (~4% of the move vs ~50% in the other
direction). "Base ignores BSC" becomes "BSC transmits weakly but measurably."

### Iteration 2 — Quidax re-quote latency, per episode

Event-aligned: each Quidax re-quote ≥5 bps is traced back (≤24 h) to the
onset of the preceding same-direction 30-minute Base move reaching half the
re-quote's size. 12 of 56 re-quotes match. The lag distribution is bimodal:
a fast cluster (~19–48 min) and a slow cluster (6–22 h) — a single "median
latency" number is not meaningful. The slow cluster's re-quotes concentrate
in morning hours, which motivated:

### Iteration 3 — re-quotes follow the Lagos workday

Hour-of-day distribution of all 161 quote changes: **78% fall between 08:00
and 16:59 Lagos time** (uniform expectation 37.5%; z = 10.7), peaking
10:00–13:00. The Quidax book is maintained on an office schedule.
Consequence: the manual re-quote latency is regime-dependent —
minutes-to-hours during the Lagos workday, overnight-or-longer otherwise —
so pool moves outside working hours leave the longest-lived UNI→QUIDAX
dislocations. This is a direct timing input for CEX-DEX arbitrage and for
any future Quidax ladder policy.

## Next

1. [x] Cross-check the Base→BSC lead against swap-level pool data
   (completed 2026-07-19; see validation above).
2. [x] Swap-level significance test (completed 2026-07-19; Iteration 1).
3. [x] Per-episode Quidax re-quote latency (completed 2026-07-19;
   Iterations 2–3 — latency is bimodal and workday-gated, not a constant).
4. Quantify UNI→QUIDAX opportunity size and duration conditional on Lagos
   working hours vs off-hours (follow-up to Iteration 3).
5. Use the confirmed Base-leads-BSC structure to inform LP adverse-selection
   models and CEX-DEX arbitrage timing assumptions.
