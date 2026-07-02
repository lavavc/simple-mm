# Automated LP Strategy Research Guide

Use this guide when constructing, testing, and backtesting hypotheses for the
cNGN CLMM LP strategy. The goal is to move quickly without losing scientific
discipline: isolate one mechanism, test it against the right baselines, and
report whether it survives validation rather than only whether it wins one
window.

## Core Principles

- Treat every strategy change as a falsifiable hypothesis, not a tweak.
- Prefer swap-count walk-forward windows over calendar windows when event
  density is uneven.
- Keep `uni-base` and `uni-bsc` separate. Their token order, liquidity depth,
  fee tier, and event quality differ enough that pooled conclusions are risky.
- Always compare against the current EWMA baseline, paper-style strategy, hold
  50/50 inventory, and static LP allocation when those benchmark paths are
  available.
- Rank mostly by validation net return, but penalize train/validation mismatch,
  drawdown, churn, and transaction cost.
- Do not trust one-window winners. A candidate that wins once but never repeats
  is an idea to investigate, not a deployable strategy.

## Non-Negotiable Data Checks

Before any serious run:

1. Confirm v4 price is derived from pool state, not swap amount ratios.
   Use `sqrt_price_x96` at the event/block and convert to cNGN/USD.
2. Confirm token direction:
   - `uni-base`: token0 = cNGN, token1 = USDC, higher tick means higher
     cNGN/USD.
   - `uni-bsc`: token0 = USDT, token1 = cNGN, higher tick means lower
     cNGN/USD, so cNGN/USD movement is inverted versus raw pool price.
3. Confirm LP holdings use CLMM liquidity math with current `sqrt_price_x96`,
   lower tick sqrt price, and upper tick sqrt price.
4. Check active liquidity is present on swap rows if transaction-cost or
   price-impact tests are involved.
5. Inspect extreme returns manually. Large returns on cNGN pairs are usually
   a sign of bad price sourcing, dust events, token-decimal mistakes, or an
   unintended unwind assumption.

Relevant tests:

```bash
pytest -q research/tests/test_backtester.py research/tests/test_v4_export.py tests/test_params_validation.py tests/test_price_math.py tests/test_lp_ratio.py
```

## Hypothesis Template

Write the hypothesis before running the grid.

```text
Hypothesis:
  <One sentence describing the proposed edge.>

Mechanism:
  <Why this should improve return, drawdown, fee capture, or churn.>

Expected Signature:
  <What should appear in windows/episodes if the hypothesis is true.>

Variant Set:
  <The smallest grid needed to isolate the mechanism.>

Controls:
  <Baseline strategy, unchanged params, benchmark portfolios.>

Success Criteria:
  <Validation return, consistency, drawdown, fee/cost, min window count.>

Failure Criteria:
  <Conditions that would reject or narrow the hypothesis.>
```

Good hypotheses are narrow:

- "Favorable out-of-range exits should be classified as profit-taking when
  traversal and PnL gates pass."
- "BSC needs wider ranges because active liquidity is thinner and transaction
  costs dominate narrow churn."
- "A minimum exit swap volume filter should reduce dust-driven defensive exits."

Weak hypotheses are broad:

- "Try more parameters."
- "Make stop-loss looser."
- "Use fair price."

## Strategy Mechanics To Preserve

The paper-style strategy is episode-based. Preserve these semantics unless the
hypothesis explicitly changes them:

- `harvest_upward_range_fraction`: favorable price traversal trigger measured
  as a fraction of the selected range width.
- `profit_take_return`: PnL gate that must pass before a harvest is allowed.
- `stop_loss_return`: PnL-based defensive exit.
- `out_of_range_overshoot_fraction`: tolerance past the range boundary before
  a geometry-based exit.
- Favorable out-of-range movement can be profit-taking if harvest and profit
  gates pass.
- Adverse out-of-range movement is defensive and should be tracked separately
  from PnL-based stop-loss where possible.

For BSC, never infer "upward" from raw tick direction. Use normalized cNGN/USD
direction or helpers that account for `cngn_is_token0`.

## Efficient Experiment Workflow

1. Start with a minimal parameter family.
   Do not expand width, center, harvest, profit, stop, fair-price, and exit
   filters all at once.

2. Run a smoke test or one-pool subset first.
   Confirm output rows, no impossible returns, and plausible fee/cost ratios.

3. Run full-grid for both methodologies on the relevant pool.

4. Run swap-count walk-forward validation.
   Use event-count windows that produce enough validation windows without
   making each validation slice too sparse.

5. Compare:
   - selected top candidate per window
   - aggregate parameter performance
   - full-period best rows
   - benchmark portfolios
   - episode exit reason distribution

6. Only then expand the grid.

## Standard Commands

Base full-grid:

```bash
python -m research.backtester.run --csv research/data/uni_base_pool_history.csv --dataset-format v4 --pool uni-base --strategy-mode ewma --initial-capital-usd 1200 --output research/results/uni_base_ewma_full_grid.csv
python -m research.backtester.run --csv research/data/uni_base_pool_history.csv --dataset-format v4 --pool uni-base --strategy-mode paper --initial-capital-usd 1200 --output research/results/uni_base_paper_full_grid.csv
```

Base swap-count walk-forward:

```bash
python -m research.backtester.run --csv research/data/uni_base_pool_history.csv --dataset-format v4 --pool uni-base --strategy-mode ewma --initial-capital-usd 1200 --walkforward --window-mode swap_count --train-swaps 200 --val-swaps 50 --stride-swaps 50 --min-train-swaps 200 --min-train-liquidity-events 0 --min-val-swaps 50 --top-n 20 --output research/results/uni_base_ewma_swapwf.csv
python -m research.backtester.run --csv research/data/uni_base_pool_history.csv --dataset-format v4 --pool uni-base --strategy-mode paper --initial-capital-usd 1200 --walkforward --window-mode swap_count --train-swaps 200 --val-swaps 50 --stride-swaps 50 --min-train-swaps 200 --min-train-liquidity-events 0 --min-val-swaps 50 --top-n 20 --output research/results/uni_base_paper_swapwf.csv
```

BSC full-grid:

```bash
python -m research.backtester.run --csv research/data/uni_bsc_pool_history.csv --dataset-format v4 --pool uni-bsc --strategy-mode ewma --initial-capital-usd 450 --output research/results/uni_bsc_ewma_full_grid.csv
python -m research.backtester.run --csv research/data/uni_bsc_pool_history.csv --dataset-format v4 --pool uni-bsc --strategy-mode paper --initial-capital-usd 450 --output research/results/uni_bsc_paper_full_grid.csv
```

BSC swap-count walk-forward:

```bash
python -m research.backtester.run --csv research/data/uni_bsc_pool_history.csv --dataset-format v4 --pool uni-bsc --strategy-mode ewma --initial-capital-usd 450 --walkforward --window-mode swap_count --train-swaps 300 --val-swaps 75 --stride-swaps 75 --min-train-swaps 300 --min-train-liquidity-events 0 --min-val-swaps 75 --top-n 20 --output research/results/uni_bsc_ewma_swapwf.csv
python -m research.backtester.run --csv research/data/uni_bsc_pool_history.csv --dataset-format v4 --pool uni-bsc --strategy-mode paper --initial-capital-usd 450 --walkforward --window-mode swap_count --train-swaps 300 --val-swaps 75 --stride-swaps 75 --min-train-swaps 300 --min-train-liquidity-events 0 --min-val-swaps 75 --top-n 20 --output research/results/uni_bsc_paper_swapwf.csv
```

Adjust capital and window sizes only with a written reason.

## Ranking Candidates

The implemented pipeline (research/backtester/run.py, research/backtester/metrics.py) ranks by:

```text
composite = net_return - max_drawdown + 0.001 * clamp(ln(fees / tx_cost), -3, +2)
```

Training selection, validation ranking, and the aggregate
`robust_validation_score` (median validation composite - worst window
drawdown + worst window divergent loss) all use this composite, so one
definition cascades through the pipeline. Aggregate eligibility additionally
requires: enough valid windows, positive median validation return, mean
fee/tx-cost ratio >= 1.0, drawdown and divergent-loss limits.

Per-config APY (compounded annualization of each validation window's net
return; mean and median reported) is tracked for comparison against the
4.25% sGHO opportunity-cost benchmark. Win-score omega (Urusov et al.) is
tracked to catch configs that end positive but spend most of the window
underwater.

Overfitting control: `--matrix-output` exports the full configs x windows
validation matrix (no top-n selection) and `research/scripts/compute_pbo.py` computes
CSCV PBO over it. Report PBO alongside any headline; a winner with PBO near
0.5 is indistinguishable from selection noise.

Then apply guardrails:

- Prefer configs observed in multiple validation windows.
- Reject or flag candidates with negative median validation return.
- Reject or flag candidates whose validation return is mostly one outlier.
- Penalize high churn when fee/cost ratio is weak.
- Penalize high drawdown unless the hypothesis specifically targets return
  with accepted drawdown.
- Report full-period winners separately from walk-forward winners.

Minimum table for each method/pool:

```text
rank
range_mode
center_mode
width / sd / lambda / skew
harvest fraction
profit target
stop-loss
overshoot
mean train return
mean validation return
median validation return
min validation return
train-validation gap
positive-window rate
max drawdown
fees
transaction costs
rebalance count
valid window count
```

## Episode-Level EDA

When a hypothesis succeeds or fails, inspect episodes before drawing a
conclusion.

Break down by:

- exit reason
- favorable vs adverse traversal
- in-range duration
- range width
- fee earned before exit
- transaction cost by component
- active liquidity at entry and exit
- swap volume around exit
- cNGN/USD move from entry to exit
- divergence versus hold inventory

For paper-inspired behavior, specifically check:

- profitable closes after in-range upward movement
- profitable favorable out-of-range closes
- adverse out-of-range loss profile
- stop-losses triggered by dust or low-notional swaps
- whether profit comes from fees, inventory move, or both

## When To Add Tests

Add or update tests whenever changing:

- price conversion
- token direction or inversion
- LP amount/valuation math
- swap fee accrual
- transaction cost or price impact
- exit reason priority
- walk-forward window construction
- ranking or aggregation logic

Tests should include both Base and BSC direction where sign matters.

## Reporting Standard

Every research result should include:

1. Hypothesis and mechanism.
2. Exact commands or script used.
3. Dataset, capital, and window settings.
4. Top rows by validation-aware score.
5. Percentile distribution of selected validation windows.
6. Baseline comparison.
7. Episode-level explanation for material wins/losses.
8. Clear verdict:
   - keep and expand
   - keep but narrow
   - reject
   - inconclusive due to sparse windows

Do not present a single best return as the conclusion. The conclusion must be
based on validation behavior and the mechanism observed in episodes.
