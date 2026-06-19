# DEX LP Strategy Research Log

Historical companion to `autoresearch/lp.md`. Each entry follows the LP
autoresearch reporting standard that was active when the experiment was run.

Pool definitions (engine/venues/dex/uniswap_*.py):

| Pool | token0 / token1 | Fee | Capital |
|---|---|--:|--:|
| uni-base | cNGN / USDC | 15 bps | $1,200 |
| uni-bsc  | USDT / cNGN (invert_price=True) | 12 bps | $450 |

Dataset: 2026-03-04 → 2026-05-12 (sqrt-derived prices).
True price bands: uni-base 4.05%, uni-bsc 6.12% peak-to-trough.

---

## H7 — Verify gas-cost defaults against on-chain data

**Hypothesis.** Backtester defaults `gas_cost_usd = $0.05` (Base) and `$0.20` (BSC) may not match real mint/burn/collect transaction costs. If real BSC gas is materially lower than $0.20, every BSC strategy's break-even shifts.

**Mechanism.** Cost recalibration. Each rebalance currently charges $0.40 (BSC) against fee accrual that often runs $0.04-$0.74 per 75-swap window.

**Method.** Queried public BSC RPC and Alchemy Base RPC for `eth_getTransactionReceipt` on the 12 BSC + 80 Base liquidity-event tx hashes in `data/uni_bsc_pool_history.csv` and `data/uni_base_pool_history.csv`. Computed `gas_used × effectiveGasPrice × native_usd / 1e18`. Used Coinbase spot at the time of analysis: BNB = $652.16, ETH = $2,267.92.

**Saved:** `backtester/results/h7/h7_base_gas_costs.csv`.

**Results (median per-event USD cost):**

| Pool | Event | Real cost | Default | Ratio default ÷ real |
|---|---|--:|--:|--:|
| BSC | mint    | $0.0146 | $0.20 | **13.7× too high** |
| BSC | burn    | $0.0063 | $0.20 | **31.5× too high** |
| BSC | collect | $0.0079 | $0.20 | 25.3× too high |
| BSC | full cycle (mint+burn) | $0.021 | $0.40 | **19× too high** |
| Base | mint    | $0.073  | $0.05 | 0.68× (default 32% too low) |
| Base | burn    | $0.0071 | $0.05 | 7.0× too high |
| Base | full cycle | $0.080 | $0.10 | close, but mix is wrong |

**Walk-forward impact (calibrated gas: BSC mint=$0.015, remove=$0.015; Base mint=$0.073, remove=$0.022):**

| Pool / Mode | Baseline | Max mean val net | Eligible | Mean tx_cost | Mean fees | fee/cost ratio (cfg median) |
|---|---|--:|--:|--:|--:|--:|
| BSC Paper | default gas | +0.088% | 28 | $0.67 | $0.18 | underwater |
| BSC Paper | calibrated  | **+0.316%** | 69 | $0.38 | $0.30 | underwater |
| BSC EWMA  | default     | +0.106% | 25 | $1.34 | $0.38 | underwater |
| BSC EWMA  | calibrated  | **+0.313%** | 88 | $0.39 | $0.38 | underwater |

**Saved outputs:**
- `backtester/results/h7/uni_bsc_paper_full_grid_calibrated.csv`
- `backtester/results/h7/uni_bsc_{paper,ewma}_swapwf_calibrated_{aggregate,windows,summary}.csv`

**Verdict: KEEP, large effect on BSC.** Gas was the dominant explanation for BSC negative-EV. Permanent change: backtester defaults should be calibrated per pool per environment, not hard-coded. Recommended values in the table above. Codify next.

---

## H6 — Fee/cost-aware composite ranking + eligibility gate

**Hypothesis.** Selecting parameters by `composite = net_return − max_drawdown` alone permits configs that look good only because the training period was quiet (low rebalance count by accident). A `+ α · clamped_log(fees / tx_cost)` term should reward structural break-even and demote configs that are net positive only because they happened not to rebalance.

**Mechanism.** Same composite used everywhere (training selection at run.py:350, aggregate ranking at run.py:526 via `robust_validation_score`). One change cascades through the pipeline.

**Implementation:**
- `backtester/metrics.py`: extended `composite_objective(net_return, max_dd, fees=0, tx_cost=0, fee_cost_alpha=0.001)`. Added `fee_cost_log_ratio` helper clamped to [-3, +2].
- `backtester/run.py`: `_compute_metrics` passes `sim.total_fees`, `sim.total_transaction_cost`. Aggregator emits `mean_validation_fee_to_tx_cost_ratio` column. Eligibility filter adds `mean_fee_cost_ratio >= min_fee_cost_ratio` (default 1.0).
- `tests/test_backtester.py`: 9 new tests (composite bonus/penalty/clamping, eligibility gate). All 72 tests pass.

**Calibrated gas + H6 ranking results (top_n=20):**

| Pool / Mode | A=default+old | B=calib+old | C=calib+H6 |
|---|---|---|---|
| BSC Paper eligible / max val | 28 / +0.088% | 69 / +0.316% | **40** / +0.316% |
| BSC EWMA  eligible / max val | 25 / +0.106% | 88 / +0.313% | **50** / +0.313% |
| Base Paper eligible / max val | 208 / +0.406% | n/a | **131** / +0.404% |
| Base EWMA  eligible / max val | 186 / +0.310% | n/a | **133** / +0.308% |

**Saved outputs:** `backtester/results/h7_h6/uni_{bsc,base}_{paper,ewma}_swapwf_{aggregate,windows,summary}.csv`.

**Verdict: KEEP.** Returns barely shift because gas calibration already did the heavy lifting; H6's value is in eligibility quality:
- Top BSC paper config: fee/cost ratio rises from ~0.27 (default gas) to **5.07×** (calibrated + H6 attributing).
- Base eligibility set shrinks ~30%, filtering out marginal configs.
- Composite tweak is small (α = 0.001) relative to net_return signal — does not dominate the ranking.

---

## Methodology fix — top_n=20 → top_n=100

**Problem identified.** All top configs in H6 results reported `valid_window_count = 1`. With 17–19 walk-forward windows and `top_n=20`, each window's top 20 collects mostly distinct configs, so no config gets validated more than once. The +0.32% BSC paper headline was a single-window fluke.

**Method.** Re-ran all four WFs with `--top-n 100` (calibrated gas, H6 ranking).

**Saved outputs:** `backtester/results/h7_h6_topn100/uni_{bsc,base}_{paper,ewma}_swapwf_*.csv`.

**Robust winners (configs winning training in ≥5 windows):**

| Pool / Mode | Config | Windows | Pos rate | Mean val | Median | fee/cost | Rebalances/window |
|---|---|--:|--:|--:|--:|--:|--:|
| BSC Paper | width=0.5%, ewma, profit=0.01, overshoot=0 | 5 | **60%** | **+0.069%** | +0.009% | 1.76× | 0.4 |
| BSC EWMA  | sd=2.0, λ=0.975, skew=0.6, thresh=15% | 6 | 50% | **+0.084%** | +0.070% | 1.78× | 1.2 |
| Base EWMA | sd=2.5, λ=0.95, skew=0.5, **preemptive**, thresh=5% | 5 | **100%** | **+0.135%** | +0.086% | 1.80× | 1.2 |
| Base Paper | (no config wins ≥5 windows) | – | – | – | – | – | – |

**Single-window headline vs robust:**
- BSC Paper: +0.32% (1 window) → **+0.07% (5 windows, 60% positive)**
- BSC EWMA:  +0.31% (1 window) → **+0.08% (6 windows, 50% positive)**
- Base EWMA: +0.31% (1 window) → **+0.14% (5 windows, 100% positive)**

**Verdict: KEEP. Real returns are modest but actually validated.** BSC moved from structurally underwater (~−0.1% per window) to marginally positive (~+0.07%). Base EWMA has a credible robust winner with 100% positive rate over 5 windows. Base Paper still picks different winners per window — no robust config emerges.

---

## H4 — Base Paper fine-grid (width, profit_take)

**Hypothesis (refined).** Coarse paper grid stepped width by 2× (`{0.01, 0.02, 0.05, 0.10}`) and profit_take by ≥2× (`{0.005, 0.01}`). Top single-window winners (+0.40%) all had 0 rebalances, suggesting "static range + occasional harvest" is the actual mechanism. Question: is the cluster around width=0.02 / profit=0.01 an edge-of-grid artifact?

**Mechanism.** Time-in-range × pool fee tier × position share dominates. Narrower → less in-range but higher fee share per swap. Wider → more in-range but lower fee share. The optimum should sit between coarse grid points.

**Variant set.** 108 configs:
- `fixed_width_pct ∈ {0.01, 0.0125, 0.015, 0.0175, 0.02, 0.025, 0.03, 0.04, 0.05}`
- `center_mode ∈ {spot, ewma}`
- `profit_take_return ∈ {0.003, 0.005, 0.0075, 0.01, 0.015, 0.02}`
- Fixed: harvest=0.04, stop=−0.01, overshoot=0.05 (no-ops at 0-rebalance)

**Command:** `python scripts/h4_base_paper_fine_grid.py` (calibrated gas, top_n=100, swap-count WF 200/50/50)

**Saved outputs:** `backtester/results/h4/uni_base_paper_fine_grid_{aggregate,windows}.csv`.

**Robust winners (valid_window_count ≥ 10):**

| width | center | profit | windows | pos rate | mean val | median | fee/cost | rebal/win |
|--:|---|--:|--:|--:|--:|--:|--:|--:|
| **0.015** | spot | 0.005 | **16/19** | **75%** | **+0.083%** | +0.096% | 1.60× | 0.0 |
| 0.010 | ewma | 0.005–0.020 | 15/19 | **80%** | +0.064% | +0.108% | 1.91× | 0.13 |
| 0.020 | spot | 0.005 | 18/19 | 67% | +0.061% | +0.040% | 1.26× | 0.0 |
| 0.0175 | ewma | 0.003–0.020 | 19/19 | 63% | +0.051% | +0.045% | 1.48× | 0.0 |
| 0.0125 | spot | any | 19/19 | 68% | +0.045% | +0.045% | 1.56× | 0.05 |
| 0.030 | spot | 0.005 | 17/19 | 65% | +0.050% | +0.043% | 0.87× | 0.0 |
| 0.040 | spot | 0.005 | 17/19 | 59% | +0.024% | +0.014% | 0.63× | 0.0 |
| 0.050 | ewma | 0.010 | 16/19 | 44% | −0.003% | −0.006% | 0.56× | 0.0 |

**Pattern observations:**
1. Optimum is **interior to the coarse grid**: width=0.015 spot (between the old 0.01/0.02 points) wins on mean validation AND 75% positive rate AND 16/19 windows.
2. `profit_take_return` does not differentiate at narrow rebalance counts — same width × center produces identical aggregates across profit ∈ {0.005, 0.0075, 0.01, 0.015, 0.02}. The strategy rarely actually harvests; profit_take fires < once per window on average.
3. Beyond width=0.03, fee/cost ratio drops below 1.0 — position is too wide to capture meaningful fee share. Even though uptime is high, fees-per-dollar collapse.
4. The previous "+0.40%" headline was a single-window selection artifact. None of those configs reappear in the robust set. The strategy that's actually robust earns ~+0.08% per 50-swap window.

**Verdict: KEEP. The grid had a hole; width=0.015 spot fills it.**
- New Base Paper headline: **+0.083% mean validation, 75% positive_window_rate, 16/19 windows** at width=0.015 spot, profit_take=0.005.
- Production grid (params.py:76 `FIXED_WIDTH_PCTS`) should add 0.015 between 0.01 and 0.02.
- profit_take_return grid can be **collapsed** to 1–2 values for Base Paper without information loss; the parameter is mostly inert at the operating point.

---

## Infrastructure changes — 2026-06-10

Codified between research rounds; all prior absolute fee/return numbers
above are NOT comparable to future runs:

1. **Gas defaults calibrated permanently.** `run.py` now defaults V4 pools to
   the H7 medians (Base mint=$0.073/remove=$0.022, BSC mint=$0.015/remove=$0.015);
   CLI flags still override. (Closes the "permanent gas calibration" item.)
2. **Grid updated.** width=0.015 added (H4 interior optimum);
   `PROFIT_TAKE_RETURNS` collapsed to {0.005, 0.01} (inert axis per H4).
   Paper grid: 3,840 → 2,240 configs.
3. **Fee-share dilution fix (material).** Fee share is now
   `L_ours / (L_pool + L_ours)`; it was `L_ours / L_pool`, which could exceed
   100%. Measured share ~9% (Base, $1.2k @ 1.5% width) and ~23% (BSC, $450 @
   0.5% width) — BSC fee income in all entries above is overstated ~30%,
   Base ~9%. Expect headline returns to drop on re-run; BSC's marginal
   positive EV may not survive.
4. **New per-config metrics.** `apy` (compounded annualization per window;
   mean/median aggregated) for the 4.25% sGHO hurdle comparison, and
   `win_score` (Urusov et al. omega over the mark-to-market equity path).
5. **PBO tooling.** `--matrix-output` writes the full configs × windows
   validation matrix; `scripts/compute_pbo.py` computes CSCV PBO
   (default metric `validation_composite`, partitions=8).

---

## H11 — Swap-clock estimation (filed, not yet executed)

**Hypothesis.** Re-parameterizing EWMA decay and width estimation in
swap-event time instead of calendar time reduces the train/validation gap.

**Mechanism.** Validation windows are already swap-count based; estimation
remains calendar-based. In thin pools with bursty activity, per-swap
volatility is more stationary than per-hour volatility, so a swap-clock
half-life should transfer better across windows with different event density.

**Expected signature.** Lower |mean_train − mean_validation| return gap at
comparable mean validation return; lambda selected more consistently across
windows.

**Variant set.** EWMA lambda interpreted per swap (current behavior already
updates per swap event — the hypothesis is about *width*: per-swap sigma ×
sqrt(expected swaps-to-rebalance) instead of per-interval sigma).

**Controls.** Current EWMA winners per pool; identical windows, gas, capital.

**Success criteria.** Gap shrinks on both pools without mean validation
return degrading; PBO does not increase.

---

## H12 — Capital response curve (pre-registered, awaiting extended data)

**Hypothesis.** Per-pool net value of the active position is concave in
deployed capital with an interior optimum well below the $5,000 cap on
uni-base, and below the $100 floor on uni-bsc at current volume.

**Mechanism.** Fee income saturates as share C/(C+D) of the recorded depth D;
gas is fixed per rebalance (favors size); entry/exit price impact is convex
in size; the idle remainder earns the 4.25% sGHO hurdle.

**Expected signature.** Paired per-window marginal APR declines monotonically
with capital and crosses zero (hurdle-adjusted) at the optimum; the two
pools' marginal curves approximately collapse when plotted against realized
diluted share; analytic C* = sqrt(pot_rate·D/hurdle) − D from per-window pot/D
estimates agrees with the simulated argmax within the jackknife range.

**Variant set.** FixedDeployment ∈ {100, 250, 500, 1000, 2000, 3500, 5000}
against a fixed $5,000 bankroll, idle_apr=0.0425, for the four frozen
robust winner configs only (no grid search, no selection → no PBO needed).
Diluted share > 30% is excluded from inference as replay fiction.

**Controls.** $5,000 fully in sGHO (excess ≡ 0 by construction); identical
windows/events across capital levels (paired differences); drop-one-window
jackknife on marginal means.

**Success criteria.** A per-pool C* with a jackknife-stable sign pattern in
the marginal curve. Model-vs-sim agreement is a separate finding either way.

**Tooling (committed).** `backtester/sizing.py` (SizingPolicy protocol with
causal EntryContext; DeployFullWallet preserves status quo, test-pinned),
bankroll/deployment split + report-only `idle_hurdle_credit` in the
simulator, `scripts/h12_capital_sweep.py` (capacity-curve artifact),
`scripts/h12_capacity_analysis.py` (paired marginals, share collapse,
analytic C*).

**Plumbing check (NOT inference — pre-extension data).** A smoke run on the
2026-05 snapshot produced sane shapes: Base curves roll over at $500–$1,000
(marginal APR turns negative past ~10–15% share); BSC loses most windows to
the 30% share ceiling above $1,000; analytic C* (~$8–15k) far exceeds the
simulated argmax — the fee-only model ignores impact and adverse inventory
moves, so expect the structural model to need a cost term. Real numbers come
from the extended dataset.

---

## Open items / next hypotheses

- **Re-run all four walk-forwards on the extended dataset** (through
  2026-06-09) with the fee-share fix, calibrated gas, top_n=100, and
  `--matrix-output`; compute PBO; check whether the four robust winners hold.
- **Run H12 on the extended dataset** (after the re-run confirms or revises
  the frozen winner set in `scripts/h12_capital_sweep.py`).
- **H13 (sketch) — dynamic sizing.** Apply the H12-validated structural rule
  to rolling pot/D estimates; ≤2 free parameters; must beat the best
  *constant* policy selected in-sample, out-of-sample.
- **H14 (sketch) — cross-pool shared bankroll.** One allocator over both
  pools' capacity curves vs independent per-pool sizing. Later: same
  capacity-curve artifact emitted from arb/CEX history → LP-vs-arb
  marginal-dollar comparison per venue (respect venue-local inventory).
- **H1 — BSC wide-passive subgrid.** Now that gas is calibrated, the wide-passive baseline can be compared to the narrow-rebalancing winner that emerged from top_n=100.
- **H11 — Swap-clock estimation.** Filed above.
- **H_new — Tighten on BSC EWMA / Base EWMA robust winners' neighborhoods.** Search around `(sd=2.0, λ=0.975, skew=0.6, thresh=15%)` BSC EWMA and `(sd=2.5, λ=0.95, skew=0.5, preemptive, thresh=5%)` Base EWMA.
- **Hygiene — fix buggy CSV `cngn_usd_price` column.** Currently derived from amount0/amount1 ratio at swap events; sqrt-derived value should overwrite it. Simulator already ignores it, but downstream tooling consumes it. (Not yet filed as a hypothesis entry.)
