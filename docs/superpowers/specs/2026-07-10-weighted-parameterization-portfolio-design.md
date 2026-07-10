# Weighted Parameterization Portfolio Backtest Design

Date: 2026-07-10

## Purpose

Run one bounded research experiment that evaluates the repository's already
declared LP parameterizations as a portfolio of virtual strategy sleeves rather
than selecting one rank-one winner. The experiment tests whether diversification
across credible parameterizations produces a more stable DEX-internal result
than winner-take-all parameter selection.

This is a diagnostic research extension. It does not establish a fair-value
label, live LP alpha, executable capacity, or permission to change production LP
behavior.

## Research Question

The primary question is:

> Given a fixed pool-local bankroll, does equal allocation across economically
> distinct parameter families that pass training-only eligibility rules improve
> costed out-of-sample stability relative to rank-one selection, equal allocation
> across every configuration, static LP, hold-cNGN, and cash?

The experiment replaces the unsupported assumption that one estimated optimum
is stable with an explicit portfolio of model uncertainty. It must not replace
one layer of parameter overfitting with an unconstrained allocation optimizer.

## Scope

### Included

- `uni-base` and `uni-bsc`, analyzed as separate portfolios.
- Existing declared EWMA, paper-style, static, frozen-family, and directional
  parameterizations.
- Existing cash, hold-cNGN, static-LP, and rank-one comparators.
- One unified run per pool that produces training, validation, sleeve, family,
  and portfolio outputs.
- Three frozen allocation rules:
  - equal weight across every eligible configuration;
  - equal weight across eligible economic families, then equal weight within
    each family;
  - a prespecified shrinkage-weight sensitivity rule.
- Costed walk-forward evaluation using only information available at each
  decision timestamp.
- Article and research-closeout updates whether the result is positive or
  negative.

### Excluded

- New fintech quote APIs.
- CBN, FMDQ, NAFEM, or other central-bank/rate acquisition.
- Renewed Bybit historical-data searches.
- A newly expanded Cartesian super-grid.
- Joint Base/BSC capital allocation.
- H12 capacity optimization.
- Optimized dynamic position sizing.
- Live engine integration or live-capital deployment.
- Treating pool-marked inventory PnL as an independent fair-value result.

External-reference hooks remain available only for genuinely new, timestamped,
overlapping data supplied in the future. Acquiring such data is no longer a task
for this branch.

## Literature Basis

The design follows four relevant findings from the literature:

1. Forecast and model combination can reduce model uncertainty, while highly
   estimated combination weights can perform poorly out of sample. See Wang et
   al., *Forecast combinations: an over 50-year review*:
   <https://arxiv.org/pdf/2205.04216>.
2. Simple `1/N` allocation is a demanding benchmark because estimation error can
   overwhelm theoretically optimal weights. See DeMiguel, Garlappi, and Uppal,
   *Optimal Versus Naive Diversification*:
   <https://academic.oup.com/rfs/article-abstract/22/5/1915/1592901>.
3. Model uncertainty can be represented as a set of statistically
   indistinguishable candidates rather than an assumed unique winner. See
   Hansen, Lunde, and Nason, *The Model Confidence Set*:
   <https://www.econometricsociety.org/publications/econometrica/2011/03/01/model-confidence-set>.
4. Searching many backtests raises the false-positive burden. Eligibility and
   allocation comparisons must therefore retain CSCV/PBO and walk-forward
   controls. See Bailey et al., *The Probability of Backtest Overfitting*:
   <https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253>.

Cover's universal-portfolio work supplies a useful sequential aggregation
analogy, but its asymptotic guarantees are not claimed for these sparse cNGN LP
windows.

## Model Universe

The universe is finite and code-defined. The experiment imports existing
configuration constructors instead of copying their axes:

- EWMA: `research.backtester.params.generate_grid`.
- Paper: `research.backtester.params.generate_paper_grid`.
- Static: the existing static configurations derived from the paper widths.
- Frozen family: `frozen_paper_configs`, `static_lp_configs`, and
  `static_lp_closed_configs` from
  `research/scripts/evaluate_frozen_family_lp.py`.
- Directional: `directional_archetype_configs` and the route-aware profiles from
  `research/scripts/evaluate_directional_paper_lp.py`.

Each sleeve identity contains:

- pool;
- economic family;
- strategy type;
- stable configuration name;
- complete parameter serialization;
- whether it is an independently simulated component or a route-aware policy;
- source constructor.

Exact duplicate parameterizations within a family are simulated once. The same
parameterization appearing in two differently named families is recorded as a
shared underlying sleeve so grid density cannot duplicate its economic weight.
Route-aware directional policies remain distinct because their entry routing is
part of the policy, not merely a `BacktestParams` value.

## Economic Families

Allocation happens hierarchically so a family with a larger grid cannot obtain
more capital merely by declaring more parameter points. The top-level families
are:

1. EWMA adaptive ranges.
2. Symmetric paper-style ranges.
3. Static LP ranges.
4. Frozen reduced-family ranges.
5. Directional route-aware policies.

Directional component archetypes are attribution records, not independently
funded sleeves when they are already combined by a route-aware policy. This
prevents counting both a policy and its internal components as separate capital.

## Walk-Forward Data Flow

For each valid pool-local walk-forward window:

1. Build the pool state at the training/validation boundary using the existing
   event-order replay.
2. Simulate every unique sleeve on the training interval.
3. Apply eligibility using training data only.
4. Freeze weights before the validation interval begins.
5. Simulate each eligible sleeve once on the validation interval from the same
   causal boundary state.
6. Preserve each sleeve's costed value or return path, not only its terminal
   summary metrics.
7. Aggregate the frozen sleeve paths into family and portfolio paths.
8. Report sleeve, family, portfolio, and comparator results for the same
   validation interval.

No validation metric may influence eligibility, weight selection, family caps,
or shrinkage constants in that window.

## Capital Accounting

Each pool has one fixed total bankroll. A sleeve with weight `w` is simulated
with `w * bankroll`, and its transaction costs, price impact, gas, slippage, and
failed-transaction assumptions are applied to that sleeve's actual allocated
capital.

Portfolio value is the sum of sleeve values plus cash. Reported portfolio
returns must never be computed by averaging independently simulated full-bankroll
returns when fixed dollar costs or nonlinear price impact make that operation
invalid.

The initial experiment treats sleeves as virtual independent positions sharing
a bankroll but not changing the historical pool state. It must disclose this
partial-equilibrium assumption. Aggregate active-liquidity share is checked
against existing per-position limits; a window fails closed if simultaneous
virtual sleeves would violate a declared aggregate limit.

Reallocation occurs only at walk-forward boundaries. When weights change, the
experiment records the cost of closing the old sleeve allocation and opening the
new allocation using existing transaction-cost semantics. There is no event-level
weight chasing.

## Eligibility

Eligibility is prespecified and training-only. A sleeve is eligible when:

- accounting is valid and finite;
- it has sufficient activity for the window under existing minimum-window
  rules;
- its training net return is positive after modeled costs;
- its training fee-to-transaction-cost ratio meets the existing economic
  admissibility rule used by the relevant harness;
- it does not violate existing liquidity-share or tick-width guards.

The implementation must reuse existing thresholds where they are already
defined. It must not invent a permissive fallback when a family lacks a required
metric; that family fails closed until its eligibility contract is explicit.

PBO is not used as a per-window look-ahead filter. It is reported across the
completed validation matrix at configuration, family, and allocation-rule level.
Parameter-jump and regime-stability reports remain post-run audits rather than
ex-ante selectors.

## Allocation Rules

### A. Equal Eligible Configurations

Every eligible unique sleeve receives equal weight. This is the naive
configuration-level diversification benchmark and exposes the effect of grid
density.

### B. Equal Eligible Families — Primary

Capital is divided equally across families containing at least one eligible
sleeve, subject to a 35% family cap. Capital inside each family is divided
equally across its eligible unique sleeves, subject to a 10% per-sleeve cap.
Capital left over after caps remains cash; it is not redistributed through an
optimizer.

If no sleeve is eligible, the portfolio is 100% cash. This is a valid result.

### C. Frozen Shrinkage Sensitivity

The sensitivity rule starts from allocation B and applies a bounded exponential
tilt based on a training-only costed risk score:

`tilted_weight_i = base_weight_i * exp(eta * clipped_score_i)`

The constants `eta`, score clipping bounds, family cap, and sleeve cap are fixed
in the implementation specification before results are run. They are not
searched. The tilted allocation is shrunk back toward allocation B by a fixed
coefficient, then any unused weight remains cash.

This rule is secondary. A positive conclusion may not depend solely on the
shrinkage rule outperforming after its additional estimation burden.

## Comparators

Every validation report includes:

- idle cash;
- hold-cNGN at the existing pool mark;
- the existing pool-routed hold comparator where available;
- static LP;
- rank-one EWMA;
- rank-one paper;
- frozen-family policy;
- directional policy;
- equal eligible configurations;
- equal eligible families;
- frozen shrinkage sensitivity.

Rank-one comparators are selected on training data only. Base and BSC tables are
never pooled into a single performance claim.

## Metrics and Diagnostics

At minimum, report:

- costed validation return;
- cumulative portfolio value;
- maximum drawdown;
- worst validation window;
- positive-window rate;
- total fees and total transaction cost;
- fee-to-transaction-cost ratio;
- rebalance count and allocation-boundary turnover;
- cash weight and deployed weight;
- return and drawdown contribution by family;
- sleeve and family return correlations where the sample permits;
- effective number of sleeves, `1 / sum(weight_i ** 2)`;
- PBO/CSCV for comparable complete matrices;
- sensitivity to removing the best ex-post sleeve;
- performance relative to static LP and pool-mark hold.

Correlations based on too few observations are labeled insufficient rather than
filled with zero. Ragged matrices fail closed for PBO, matching the current
implementation.

## Success and Interpretation

The experiment is informative whether it passes or fails. The primary
equal-family portfolio is considered more stable than rank-one selection only if
it:

- improves or preserves costed validation return;
- does not worsen maximum drawdown or the worst window materially;
- is not dependent on one sleeve or one family;
- retains the result when the best ex-post sleeve is removed;
- does not rely on BSC losses being hidden by Base gains;
- remains economically meaningful after allocation turnover costs.

No fixed numeric promotion threshold is introduced because the experiment is
not a live-promotion gate. The conclusion must report the full comparison rather
than convert a mixed result into a pass/fail slogan.

Even a strong result remains DEX-internal. Without an independent cNGN inventory
mark, it may support only the claim that parameter diversification stabilizes a
pool-marked simulation—not that it creates deployable alpha.

## Outputs

Generated results remain under ignored `research/results/**` and include:

- configuration catalog;
- training eligibility table;
- frozen weight table by window;
- sleeve validation matrix;
- family validation matrix;
- portfolio validation matrix;
- comparator summary;
- contribution and concentration report;
- PBO inputs and report;
- Markdown research summary.

Durable source, tests, and documentation are committed. Generated matrices and
plots are not committed unless deliberately promoted later.

## Code Boundaries

The implementation should keep four responsibilities separate:

1. A catalog module builds stable sleeve identities from existing constructors.
2. An allocation module implements pure eligibility and frozen-weight rules.
3. A portfolio evaluator composes costed sleeve paths under shared-bankroll
   accounting.
4. A CLI orchestrates pool runs and writes reports.

Pure allocation logic must not import `engine/api/`, concrete venue adapters, or
live LP managers. Existing backtester and research-harness functions remain the
simulation source of truth. Duplicated strategy grids or directional routing
rules are prohibited.

## Testing and Failure Behavior

Tests must cover:

- stable catalog identities and exact deduplication;
- no double funding of directional policies and their component archetypes;
- training-only eligibility and frozen validation weights;
- equal-family weighting independent of family grid size;
- 35% family and 10% sleeve caps with residual cash;
- 100% cash when no sleeve qualifies;
- shared-bankroll arithmetic with fixed dollar costs;
- boundary reallocation costs;
- aggregate liquidity-limit failure;
- Optional zero values preserved as meaningful values;
- Base/BSC output separation;
- ragged PBO matrices rejected;
- best-sleeve removal sensitivity;
- one normal path, one no-eligible-sleeve path, and one incompatible-family
  integration path.

Missing metrics, duplicate unstable identities, invalid weights, weights above
one, negative capital, and non-finite returns fail loudly. There are no silent
defaults.

## Research-Branch Integration

After the run, update:

- `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md`;
- `research/autoresearch/lp.md`;
- `research/autoresearch/README.md`;
- `research/autoresearch/fair-price.md` to state that fintech and central-bank
  rate acquisition is abandoned for this branch;
- `research/articles/evidence-pack-2026-07-cngn-market-making.md`;
- `research/articles/02-backtesting-the-market-layer.md`.

Article 2 gains a research-improvements/future-considerations section explaining
parameterizations as a portfolio of hypotheses, the difference between simple
diversification and a second-stage optimizer, and the requirements for eventual
shadow and live allocation. It must disclose the same pool-mark and comparator
limitations as the rest of the article.

The final closeout sequence is:

1. Implement and test the bounded portfolio harness.
2. Run it once across the declared model universe for both pools.
3. Interpret all allocation rules and comparators.
4. Update the closeout and article evidence.
5. Complete repository cleanup and verification.
6. Stop this research branch.

H12, live dynamic sizing, and engine integration remain outside the branch even
if the diagnostic portfolio result is positive.

## Live-Trading Translation

Any later live use would separate a virtual model portfolio from physical LP
execution. Virtual sleeves would propose desired exposure; an execution
consolidator would combine overlapping tick ranges into a small number of
positions while preserving attribution back to sleeves.

The required progression is shadow allocation, fixed small risk budget,
scheduled weight updates, family and sleeve caps, cash reserve, live
cost/inventory attribution, and automatic suspension when realized behavior
leaves the backtested envelope. This is future architecture, not part of the
current implementation.
