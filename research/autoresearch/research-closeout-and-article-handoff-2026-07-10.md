# cNGN Research Closeout And Article Handoff

Date: 2026-07-10

## Purpose

This handoff defines the remaining research to finish before writing and
publishing the next Lava cNGN article series.

The central editorial frame is:

> Local stablecoin market making is not just a trading problem. It is a trust,
> monetary-policy, and measurement problem. The missing middle is supervised
> liquidity: enough supply and market making for users to trust cNGN, with
> enough transparency and controls for regulators to trust the market.

The next work should not try to prove a deployable LP or Fair Price strategy
from weak labels. It should finish the evidence trail, document what failed,
clean the repo, and turn the research branch history into a measured public
argument.

## July 15, 2026 Addendum

This dated handoff closes the earlier strict-QTS policy-transfer work; it does
not adjudicate cross-pool information transfer. A separate experiment is now
approved in
`docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md` to test
whether lagged BSC pool prices add information about future Base price changes,
with a reverse-direction falsification and an unconditional frozen-policy test.

The sealed run passed data and causal-alignment QA. Both one-hour directional
tests were `inconclusive`, and constrained-DTW direction was
`dtw_band_unstable`. The reviewed manifest therefore selects
`leadership_unresolved`; its independent frozen economic result is
`no_net_return_improvement`.

Review authority and the complete claim boundary are recorded in
`research/results/cross_pool_lead_lag/article_manifest.json`. The result does
not establish either directional lead, an affirmative no-lead finding, causal
price discovery, toxic flow, external-LP profitability, or deployable alpha.

## Current Research State

### Fair Price

Current guide: `research/autoresearch/fair-price.md`

Implemented:

- Quidax top-book JSON analysis through
  `research/scripts/analyze_binance_fair_price.py`.
- Binance `USDTNGN` reference fetch through
  `research/scripts/fetch_binance_reference.py`.
- Quidax/Uniswap v4 overlap analysis artifacts under `research/data/`.
- Existing fair-price capture/export/analyzer tooling for `data/cngn.db`.

Current evidence:

- Quidax history is top-of-book only. It cannot support depth-walk execution,
  fill-probability, or realized CEX PnL claims.
- Binance `USDTNGN` exists but is in `BREAK`; the latest 1m kline opens at
  `2024-03-07T02:59:00+00:00`, almost two years before the local Uniswap v4
  pool windows.
- The overlap fetch for the April-July 2026 Quidax JSON window produced zero
  Binance rows.
- Bybit P2P is the best external anchor candidate, but the local overlap is too
  short for promotion-grade claims.
- Uniswap v4 and Quidax are broadly in line over overlap windows, usually tens
  of basis points apart, but Uniswap remains DEX context rather than a clean
  Fair Price label.

Research status:

- Fair Price is closed as feed-quality and market-structure evidence, not as a
  promoted executable fair-price model.
- The correct public lesson is label discipline: do not validate a fair-value
  model against the same market surface it is meant to explain.

### DEX LP

Current guides:

- `research/autoresearch/lp.md`
- `research/autoresearch/flow-gated-cngn-lp-plan.md`

Implemented:

- Full DEX-only walk-forward rerun.
- Flow-gated LP harness.
- QTS rolling flow/markout features.
- Frozen-family LP harness.
- Directional paper LP harness with route-aware profiles.
- Fail-closed external-reference comparator hooks in
  `research/scripts/evaluate_directional_paper_lp.py`.

Current evidence:

- Full-grid rank-1 LP streams are not deployable after costed validation.
- Frozen Base strict gate is positive, but centered paper exits add no value
  over static LP and only beat pool-mark hold by roughly 0.0066 percentage
  points across five windows.
- Directional `upside_tight_v1` under `gate_strict_qts_20_25` is the strongest
  Base-only DEX-internal slice: about +1.039% across four active windows, worst
  +0.118%, and +0.228 percentage points versus pool-mark hold.
- The Base strict-QTS 20/25 directional LP policy did not transfer to BSC: it
  returned -1.272% across seven BSC windows, versus +1.039% across four Base
  windows.
- That result concerns policy transferability. It does not test whether lagged
  BSC pool prices contain incremental information about future Base price
  changes. For that policy-transfer test, BSC was a falsification pool rather
  than a tuning target.
- Promotion remains blocked because the non-pool cNGN inventory comparator has
  no usable overlapping reference series.

Research status:

- DEX LP is closed as diagnostic unless a credible external reference appears.
- The public lesson is not "we found LP alpha." It is "the first plausible
  DEX-internal signal failed the bar required for live promotion."

## Final Research Tasks Before Writing

### Task 1: One Timeboxed External Reference Attempt

Goal: Decide whether a usable non-pool reference series exists.

Timebox: one focused pass. Do not let this reopen an indefinite data hunt.

Result: rejected 2026-07-10.

The best available candidate remains Bybit P2P, but the one-pass check did not
produce a usable historical comparator:

- Official Bybit P2P docs expose `POST /v5/p2p/item/online` as an online-ad
  endpoint, not a historical archive.
- The legacy public Bybit endpoint is reachable for current `USDT`/`NGN` ads,
  but it only gives current online-ad state.
- Local `bybit_p2p` rows in `data/cngn.db` cover only
  `2026-06-19T12:26:49.707000+00:00` through
  `2026-06-20T01:09:16.620000+00:00`.
- The Base strict-sign-cone-positive-QTS LP slice needs eight start/end edge
  marks from `2026-03-12T15:00:55+00:00` through
  `2026-06-09T16:39:09+00:00`; local Bybit covers `0/8` within the 3600-second
  max-age rule.

Conclusion: the non-pool comparator cannot be populated from currently
available data. Do not rerun this search unless genuinely new timestamped
external data arrives.

The July 2026 branch no longer includes fintech quote APIs, CBN/FMDQ/NAFEM
rates, or renewed Bybit historical searches. External-reference hooks remain
available only for genuinely new timestamped overlapping data supplied later;
acquiring that data is not an open task.

Historical attempt order (closed):

1. Authorized Bybit P2P API access for `USDT`/`NGN` ads.
   - Endpoint from Bybit docs: `POST /v5/p2p/item/online`.
   - Required fields: token, fiat currency, side, price, quantity, min/max,
     payment method, maker status, timestamp.
   - Output shape should be normalized to the reference CSV accepted by
     `evaluate_directional_paper_lp.py`: `timestamp_ms,reference_price,source`.
2. cNGN team internal quote logs or spreadsheet snapshots.
   - Only usable if source timestamps are present.
   - Do not accept screenshots without timestamps for quantitative promotion.
3. Fintech quote APIs such as Kora or Flutterwave.
   - Use for slower fiat-anchor context, not for 60-second LP labels.
4. Official daily anchors such as CBN NFEM or FMDQ/NAFEM.
   - Use only for daily regime context.

Acceptance:

- A source is usable for final LP attribution only if it covers every start and
  end timestamp of the relevant validation windows within the configured max
  age.
- A source is usable for Fair Price only if timestamp density supports the
  stated horizon. Daily bank rates must not be used for 60-600 second claims.

If accepted:

```bash
python3 research/scripts/evaluate_directional_paper_lp.py \
  --pool uni-base \
  --external-reference-csv research/data/<source>_reference.csv \
  --external-reference-source <source_name> \
  --external-reference-max-age-seconds 3600
```

Then update:

- `research/autoresearch/lp.md`
- `research/autoresearch/flow-gated-cngn-lp-plan.md`
- `research/articles/02-backtesting-the-market-layer.md`

If rejected:

- Do not search again without genuinely new data.
- Close DEX LP as a diagnostic result.
- Close Fair Price as a feed-quality and market-structure result.

### Task 2: Freeze Research Conclusions

Status: completed 2026-07-10.

Write one closeout section per pipeline.

Files:

- `research/autoresearch/fair-price.md`
- `research/autoresearch/lp.md`
- `research/autoresearch/README.md`

Required conclusions:

- Fair Price:
  - Quidax top-book data is useful for quote-cadence and managed-surface
    analysis.
  - It is not enough for depth-walk execution claims.
  - Binance does not overlap 2026 data.
  - Bybit P2P is the best forward anchor, but current overlap is too short.
  - Uniswap v4 is context, not truth.
- DEX LP:
  - Base has a sparse pool-internal directional regime worth preserving.
  - The Base strict-QTS 20/25 directional LP policy did not transfer to BSC.
  - No live LP promotion without an external reference comparator.
  - H12 capacity and dynamic sizing stay blocked unless an accepted comparator
    arrives.
- CEX execution:
  - The Quidax JSON files are top-book only.
  - The three CEX execution modes cannot be tested as executable modes from this
    data.
  - They can be discussed as design modes, not validated outcomes.

Verification:

```bash
git diff --check research/autoresearch
rg -n "promotable|deployable|truth label|depth-walk|external comparator" research/autoresearch
```

### Task 3: Repo Cleanup

Goal: separate durable source/docs/tests from local/generated artifacts.

Keep or commit:

- `research/scripts/analyze_binance_fair_price.py`
- `research/scripts/fetch_binance_reference.py`
- `research/scripts/evaluate_directional_paper_lp.py`
- matching tests under `research/tests/`
- closeout docs under `research/autoresearch/`
- article outlines under `research/articles/`

Do not commit unless deliberately promoted:

- `research/results/**`
- generated `research/data/**` outputs
- `.firecrawl/`
- `__pycache__/`
- local JSON inputs such as `quidax_cngn_usdt.json`, unless the repo has a
  clear data policy for them

Checks:

```bash
git status --short
find research -name "__pycache__" -type d
pytest research/tests/test_fetch_binance_reference.py \
  research/tests/test_analyze_binance_fair_price.py \
  research/tests/test_evaluate_directional_paper_lp.py \
  research/tests/test_evaluate_flow_gated_lp.py \
  research/tests/test_evaluate_frozen_family_lp.py -q
python3 -m py_compile \
  research/scripts/fetch_binance_reference.py \
  research/scripts/analyze_binance_fair_price.py \
  research/scripts/evaluate_directional_paper_lp.py
git diff --check
```

### Task 4: Article Evidence Pack

Status: completed 2026-07-10.

Create a short evidence pack before drafting prose.

Suggested file:

- `research/articles/evidence-pack-2026-07-cngn-market-making.md`

Sections:

1. What the first Lava article argued.
2. What the repo now proves.
3. What the repo explicitly does not prove.
4. Central bank trust framing.
5. cNGN supply and supervised liquidity argument.
6. Tables and figures available for publication.
7. Claims to avoid.

Must include:

- Binance `USDTNGN` cutoff and no-overlap result.
- Quidax top-book-only caveat.
- Quidax/Uniswap overlap basis-point summary.
- Base directional LP result and failed strict-QTS policy transfer to BSC.
- Bybit P2P as best forward reference, but not enough current data.
- The article's regulatory line: "supervised liquidity," not "unrestricted
  DEX liquidity."

## Article Series Plan

The active article outlines live under `research/articles/`:

- `01-supervised-liquidity.md`
- `02-backtesting-the-market-layer.md`

The next public writing should be a two-article sequence. The older
open-source market-maker outline and live LP side notes remain archived source
material under `research/articles/archive/`, but they are not part of this
publication sequence.

### Article 1: Supervised Liquidity For The Naira Internet

Working title:

> Supervised Liquidity: How cNGN Can Earn Market Trust Without Losing Central
> Bank Trust

Thesis:

> The future of cNGN is not a choice between no liquidity and uncontrolled
> liquidity. The missing middle is supervised liquidity: enough supply and
> market making for users to trust the asset, with enough transparency and
> controls for the central bank to trust the market.

Audience:

- Lava readers.
- cNGN ecosystem participants.
- Policy-aware crypto operators.
- Regulators who are skeptical but reachable.

Policy sources to use in the Article 1 description:

- BIS/CPMI, *Investigating the impact of global stablecoins*:
  `https://www.bis.org/cpmi/publ/d187.pdf`
  - Use this to show that stablecoins can improve payments only when legal
    certainty, governance, financial integrity, consumer protection, market
    integrity, and operational resilience are addressed.
  - Use its monetary-sovereignty framing to argue that central banks should want
    transparent domestic stablecoin rails before foreign stablecoins become the
    default hedge and payment asset.
  - Pair this with "same activity, same risk, same regulation" and
    technology-neutral supervision rather than a permissionless-growth pitch.
- IMF Working Paper, *Macro-Financial Impacts of Foreign Digital Money*:
  `https://www.elibrary.imf.org/view/journals/001/2023/249/article-A001-en.xml`
  - Use this carefully: the paper models foreign stablecoins as inflation and
    depreciation hedges that can amplify currency substitution, reduce bank
    intermediation, and weaken monetary policy transmission.
  - Its capital-control result is not "capital controls are bad." It is that
    capital flow measures can increase foreign-stablecoin circumvention when
    the stablecoin channel remains open.
  - The article's constructive move is therefore domestic: build supervised cNGN
    liquidity and eventually compliant naira-denominated hedging or yield rails,
    such as tokenized government-bill exposure or regulated lending markets,
    so users do not need to leave the naira perimeter to manage inflation risk.

Main argument:

1. Users route around weak regulated rails.
   - Use the Binance Square post as a narrative signal, not as authoritative
     evidence.
   - Pair it with IMF-style framing: stablecoin demand responds to FX,
     remittance, inflation, and payment frictions.
2. The CBN's concerns are legitimate.
   - Monetary sovereignty, financial integrity, capital controls, and data
     visibility are real policy concerns.
3. Low cNGN supply and shallow liquidity do not solve those concerns.
   - They may push flow toward USDT P2P and informal OTC channels.
   - The IMF foreign-digital-money paper supports this mechanism: if the only
     liquid hedge is a foreign stablecoin, users have stronger reason to exit
     domestic deposits and regulated rails.
4. The better path is supervised liquidity.
   - Reserve backing.
   - Redemption SLAs.
   - issuer and market-maker reporting.
   - DEX liquidity caps that scale with transparency.
   - circuit breakers for abnormal premiums, discounts, and rapid flow changes.
   - Domestic naira-denominated hedging products should be framed as a future
     supervised-market design question, not as an immediate promise.
5. Market making becomes public monetary infrastructure.
   - Good market makers keep naira activity visible.
   - Bad or absent liquidity lets activity leave the regulated perimeter.

Tone:

- Cooperative with CBN.
- Precise about risks.
- Avoids claiming regulators are irrational.
- Avoids saying cNGN is intentionally suppressing DEX liquidity.
- Avoids telling cNGN to "flood DEXs."

Claims to avoid:

- "CBN caused the market to move offshore."
- "cNGN must increase supply immediately."
- "DEX liquidity is inherently good."
- "Binance was right."
- "The central bank should give up capital controls."

### Article 2: Backtesting The Market Layer

Working title:

> Backtesting The Market Layer: What We Tried To Prove Before Scaling cNGN
> Liquidity

Thesis:

> The honest way to build market infrastructure for local stablecoins is to turn
> every trading belief into a timestamped hypothesis, then publish the tests that
> survive contact with venue-specific data.

Audience:

- builders and researchers.
- market makers.
- policy readers who want evidence rather than slogans.

Main argument:

1. The wrong truth label can make any strategy look smart.
   - DEX mid cannot be the label for a model trying to explain DEX stress.
   - Quidax top-book cannot stand in for depth-walk execution.
2. The repo tested three tracks.
   - Fair Price.
   - DEX LP.
   - CEX execution mode feasibility.
3. The research mostly produced disciplined non-results.
   - Binance no overlap.
   - Quidax top-book only.
   - Bybit too short.
   - Base LP slice promising but sparse.
   - The strict-QTS policy transfer to BSC failed.
4. These failures are the point.
   - They show what evidence is required before live market-making policy
     changes.
5. Public research infrastructure helps the ecosystem.
   - Labels, backtests, stale-source reporting, and comparator discipline are
     reusable even when a specific hypothesis fails.

Tables to publish:

- Source availability matrix.
- Fair Price label matrix.
- Quidax/Uniswap overlap summary.
- DEX LP summary: Base promising slice and failed strict-QTS policy transfer to
  BSC.
- Claims allowed versus claims rejected.

Claims to avoid:

- "The LP strategy works."
- "Fair Price is solved."
- "Quidax proves execution quality."
- "Bybit is a direct executable label."

## Publication Readiness Checklist

Research is ready for article drafting when:

- The external reference attempt is accepted or explicitly closed.
- Fair Price docs state what is and is not testable.
- DEX LP docs state promotion remains blocked unless external comparator data
  arrives.
- Repo status clearly separates source/docs/tests from local artifacts.
- The evidence pack exists.
- Article outlines no longer promise result slots that the research cannot fill.

Article drafting is ready when:

- Article 1 has a policy-safe thesis and avoids adversarial claims about CBN or
  cNGN.
- Article 2 has exact result tables and avoids promotion language.
- Every empirical claim links back to a local artifact or named external source.
- The "where this breaks" section is kept in each article.

## Suggested Next Session Prompt

Use this prompt for the next research session:

> Continue from
> `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md`.
> First, run the one-pass external reference attempt and decide whether the
> non-pool comparator can be populated. If not, close Fair Price and DEX LP as
> diagnostic research results, update the docs, clean generated artifacts, and
> prepare `research/articles/evidence-pack-2026-07-cngn-market-making.md`.

Suggested skills:

- `superpowers:using-superpowers`
- `superpowers:brainstorming`
- `superpowers:test-driven-development`
- `superpowers:verification-before-completion`
- `quant-analyst`
- `firecrawl-search` or `firecrawl-scrape` for source checks
