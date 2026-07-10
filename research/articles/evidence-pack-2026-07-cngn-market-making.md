# Evidence Pack: July 2026 cNGN Market Making

Date: 2026-07-10

Purpose: collect the source-backed claims that can support the next two Lava
cNGN articles without implying live promotion of Fair Price or DEX LP research.

## Source Map

External sources:

- Lava, "Market making for the rest of the world":
  `https://lavavc.io/research/making-markets-for-the-rest-of-the-world`
- BIS/CPMI, *Investigating the impact of global stablecoins*:
  `https://www.bis.org/cpmi/publ/d187.pdf`
- IMF Working Paper, *Macro-Financial Impacts of Foreign Digital Money*:
  `https://www.elibrary.imf.org/view/journals/001/2023/249/article-A001-en.xml`
- Binance Square cNGN post, narrative signal only:
  `https://www.binance.com/en/square/post/339419841352033`
- Bybit P2P online ad docs:
  `https://github.com/bybit-exchange/docs/blob/master/docs/p2p/ad/online-ad-list.mdx`

Local artifacts:

- `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md`
- `research/autoresearch/fair-price.md`
- `research/autoresearch/lp.md`
- `research/autoresearch/flow-gated-cngn-lp-plan.md`
- `research/autoresearch/quidax-cex-execution-eda-2026-07-10.md`
- `research/data/binance_fair_price_report.md`
- `research/data/quidax_uniswap_v4_overlap_report.md`
- `research/results/flow_gated_lp/uni_base/directional_paper_gate_summary.csv`
- `research/results/flow_gated_lp/uni_bsc/directional_paper_gate_summary.csv`

## What The First Lava Article Argued

The first article set up the market-making story as an ecosystem public-good
problem:

- USD stablecoin market making became sophisticated because simple public
  infrastructure created a competitive loop of arbitrage, LP, searching, and
  block-building.
- Emerging-market stablecoins need local market makers because local fiat access
  is a structural advantage that global firms cannot easily replicate.
- cNGN is the concrete example: the repo should fetch relevant prices, detect
  CEX/DEX arbitrage, and provide liquidity on Quidax and Uniswap v4.
- The article promised follow-up writing on pricing, backtesting, and lessons
  from the research branch.
- The closing public-good claim was that transparent, open-source market-making
  infrastructure can make onchain local-currency markets more efficient, cheaper,
  and easier to regulate.

## What The Repo Now Proves

Fair Price and Quidax:

- The Quidax JSON sample has 255,846 dense rows but only 102 compressed quote
  states over `2026-04-08T11:33:53.112Z` to `2026-07-03T08:27:23.088Z`.
- The sample is top-book only. It has no depth ladder, fills, order sizes, or
  independent Binance reference marks.
- The cNGN team manually manages Quidax best bid and offer from Binance mid, so
  Quidax is a managed quote surface rather than an independent truth label.
- State-based inference finds no statistically reliable midpoint directional
  edge at 10-600 second horizons; all midpoint confidence intervals include
  zero.
- Crossing either side of the Quidax top book has reliably negative future-mid
  markout at every tested horizon. This is spread cost, not alpha.

External reference availability:

- Binance `USDTNGN` exists but is in `BREAK`; the latest 1m kline opens at
  `2024-03-07T02:59:00+00:00`, close `1518.40000000`.
- The Binance overlap fetch for the Quidax JSON window produced zero Binance
  reference observations: 255,846 Quidax rows, 0 Binance-reference rows.
- Official Bybit P2P docs expose online ads, not a historical archive.
- Local `bybit_p2p` rows cover only 171 snapshots from
  `2026-06-19T12:26:49.707000+00:00` through
  `2026-06-20T01:09:16.620000+00:00`.
- Bybit covers `0/8` Base strict-sign-cone-positive-QTS start/end marks within
  the 3600-second external-reference max-age rule.

Quidax versus Uniswap v4:

| Pool | Joined rows <=15m | Median DEX-Quidax bps | p10 bps | p90 bps | Median abs bps | Within 50 bps | Within 100 bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Base | 714 | -10.7 | -48.3 | +51.3 | 25.7 | 80.4% | 95.7% |
| BSC | 2,934 | -7.7 | -40.0 | +49.2 | 21.1 | 88.3% | 99.4% |

Interpretation: Quidax and Uniswap v4 are broadly in line over their overlapping
timestamps, usually tens of basis points apart. The tails still reach roughly
160-174 bps, so Uniswap is useful as DEX context and LP inventory mark, not as a
clean Fair Price label.

DEX LP:

| Pool | Gate | Active windows | Directional sum | Worst directional | Positive directional | Directional minus hold | Best static sum |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Base | `gate_strict_qts_20_25` | 4 | +1.039% | +0.118% | 100.0% | +0.228 pp | +0.796% |
| BSC | `gate_strict_qts_20_25` | 7 | -1.272% | -0.842% | 14.3% | -1.602 pp | +0.400% |

Interpretation: Base has a sparse DEX-internal directional regime worth
preserving as a diagnostic. BSC rejects cross-pool generalization. No live LP
promotion is justified without a non-pool inventory comparator.

## What The Repo Does Not Prove

- It does not prove a live Fair Price model.
- It does not prove depth-walk Quidax execution, fill probability, or realized
  CEX PnL.
- It does not prove that Uniswap v4 is an independent truth label.
- It does not prove DEX LP alpha.
- It does not prove that the Base strict-QTS result is large, durable, or
  transferable.
- It does not prove that Bybit P2P can be used as a historical comparator for
  the current LP windows.

## Central Bank Trust Framing

The BIS/CPMI source supports a regulator-friendly frame:

- Stablecoins can improve payment speed, cost, and inclusion, but only if legal,
  governance, operational, market-integrity, consumer-protection, and financial
  integrity risks are addressed.
- Global or foreign stablecoins can create monetary-policy, financial-stability,
  international-monetary-system, competition, and monetary-sovereignty risks.
- The article should therefore advocate technology-neutral, functions-based
  supervision: same activity, same risk, same regulation.

The IMF working paper supports a sharper macro frame:

- Foreign stablecoins can serve as liquidity instruments and hedges against
  domestic inflation or depreciation.
- That can amplify currency substitution, reduce bank intermediation, and weaken
  monetary policy transmission in a small developing economy.
- Capital flow measures can increase foreign-stablecoin circumvention when the
  stablecoin channel remains open.
- A complete ban can work inside the model, but the paper itself notes practical
  enforcement difficulty because decentralized and informal use can remain.

Article implication: do not write "capital controls are bad." Write that weak
domestic rails can push users toward offshore stablecoin substitutes, and that a
supervised domestic stablecoin market can keep more activity visible to the
central bank.

## cNGN Supply And Supervised Liquidity Argument

Public line:

> The choice is not no liquidity versus unrestricted DEX liquidity. The missing
> middle is supervised liquidity: enough cNGN supply and market making for users
> to trust the asset, with enough transparency and controls for regulators to
> trust the market.

Mechanism:

- If cNGN is too shallow, users who need liquidity, inflation hedging, or USD/NGN
  referenceability will route toward USDT P2P and informal OTC channels.
- If cNGN liquidity is expanded without reporting, redemption discipline, market
  surveillance, and DEX limits, central-bank concerns become more serious.
- A measured path is to scale liquidity alongside issuer reporting, market-maker
  reporting, redemption SLAs, liquidity caps that scale with transparency, and
  circuit breakers for abnormal premiums, discounts, or rapid flow changes.
- Future domestic hedging rails can be discussed as policy design questions:
  tokenized government-bill exposure, inflation-protected instruments, or
  regulated lending markets. Do not imply current research has validated those
  products or that the IMF paper endorses them directly.

## Tables And Figures Available For Publication

- Source availability matrix:
  Quidax top-book available; Binance historical overlap unavailable; Bybit
  current ads reachable but no historical coverage; bank/official rates useful
  only for slower context.
- Fair Price label matrix:
  Quidax managed top book is diagnostic; Uniswap v4 is DEX context; no accepted
  independent promotion label.
- Quidax/Uniswap overlap summary:
  Base and BSC basis-point table above.
- CEX execution-mode feasibility:
  `quidax` anchor testable against future Quidax top book; `dex_vwap` and
  `blended` only proxy-testable with stale/as-of DEX context from this sample.
- DEX LP summary:
  Base strict QTS slice versus BSC rejection table above.
- Claims allowed versus claims rejected:
  use this pack as the checklist before drafting.

## Claims To Avoid

- "Fair Price is solved."
- "The LP strategy works."
- "The Base slice is live LP alpha."
- "Quidax proves execution quality."
- "Uniswap v4 is the fair-value truth label."
- "Bybit is a direct executable label."
- "The central bank should give up capital controls."
- "cNGN should flood DEXs with unrestricted supply."
- "The IMF paper endorses decentralized lending or tokenized T-bills as the
  policy answer."
