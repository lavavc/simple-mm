# Supervised Liquidity For The Naira Internet

## Working Title

Supervised Liquidity: How cNGN Can Earn Market Trust Without Losing Central
Bank Trust

## One-Sentence Thesis

The future of cNGN is not a choice between no liquidity and uncontrolled
liquidity. The missing middle is supervised liquidity: enough supply and market
making for users to trust the asset, with enough transparency and controls for
the central bank to trust the market.

## Why This Fits Lava

This should be the policy-aware sequel to Lava's first cNGN market-making
article, not a victory lap for a trading bot. The first piece argued that local
stablecoins need local market makers and that open-source infrastructure can
make those markets more competitive and legible. This piece should answer the
next question: how can cNGN liquidity grow without asking the Central Bank of
Nigeria to trust an opaque, unrestricted offshore-style market?

The editorial posture should be cooperative with regulators. The article should
argue that shallow domestic liquidity does not make monetary risks disappear; it
can push real users toward USDT P2P, informal OTC desks, and foreign-stablecoin
substitutes. A supervised cNGN market gives regulators more visibility, not
less, if it is paired with reserve discipline, redemption standards, market-maker
reporting, DEX exposure limits, and circuit breakers.

## Source Frame

- Lava, "Making Markets For The Rest Of The World":
  `https://lavavc.io/research/making-markets-for-the-rest-of-the-world`
  - Use this as the article's direct predecessor. It frames the repo as a public
    market-making primitive for emerging-market stablecoins.
- BIS/CPMI, *Investigating the impact of global stablecoins*:
  `https://www.bis.org/cpmi/publ/d187.pdf`
  - Use this to show why central banks should care about integrating stablecoin
    rails into supervised payment systems. Stablecoins can improve speed, cost,
    access, and cross-border usability only if legal certainty, governance,
    consumer protection, financial integrity, operational resilience, and market
    integrity are handled.
  - Use its monetary-sovereignty framing to make the domestic-liquidity
    argument: if credible local stablecoin rails do not exist, foreign
    stablecoins become the more liquid payment and savings layer.
  - Pair the argument with technology-neutral supervision and "same activity,
    same risk, same regulation."
- IMF Working Paper, *Macro-Financial Impacts of Foreign Digital Money*:
  `https://www.elibrary.imf.org/view/journals/001/2023/249/article-A001-en.xml`
  - Use this carefully. The paper models foreign stablecoins as liquid assets
    that can hedge domestic inflation or depreciation, which can amplify
    currency substitution, reduce bank intermediation, and weaken monetary
    policy transmission.
  - Do not flatten the paper into "capital controls are bad." Its relevant point
    here is that capital flow measures can increase foreign-stablecoin
    circumvention when the stablecoin channel remains open.
  - The constructive implication is domestic: supervised cNGN liquidity first,
    then future compliant naira-denominated hedging or yield rails such as
    tokenized government-bill exposure or regulated lending markets. Do not
    imply the IMF endorses those product designs directly.
- Binance Square cNGN post:
  `https://www.binance.com/en/square/post/339419841352033`
  - Use only as a narrative signal that market attention has already moved to
    cNGN's liquidity and supply constraints. Do not treat it as an authoritative
    source.

## Narrative Progression

### 1. Users route around weak regulated rails

Open with the practical market problem. A local stablecoin can be regulated,
redeemable, and onchain while still being hard to use if users cannot enter or
exit at believable prices. If cNGN is too shallow, people who need liquidity,
inflation hedging, or USD/NGN referenceability will route to USDT P2P and
informal OTC channels.

The point is not that users are trying to defy the central bank. The point is
that liquidity is a form of product reliability. When the local instrument is
not reliable, users choose the instrument that is.

### 2. The central bank's concerns are legitimate

Take monetary sovereignty, capital controls, financial integrity, and data
visibility seriously. The article should not sound like a crypto industry demand
for permissionless growth. It should say plainly that foreign-stablecoin
substitution can weaken domestic-policy transmission and move activity away from
regulated institutions.

This section should use BIS and IMF to show that the regulator's concern is not
anti-innovation. The policy question is whether stablecoin liquidity develops in
a supervised domestic perimeter or leaks into harder-to-observe offshore rails.

### 3. Thin domestic liquidity does not solve the policy problem

Low cNGN supply and manually constrained DEX liquidity may reduce the headline
size of public onchain markets, but they do not remove user demand for liquid
digital naira exposure. If the domestic asset is not liquid enough, the most
liquid alternative becomes the de facto hedge and settlement asset.

This is the pivot from "more DEX liquidity" to "supervised liquidity." The
article should not advocate flooding DEXs. It should argue that cNGN liquidity
can scale in stages, with supervision improving alongside market depth.

### 4. Supervised liquidity is the missing middle

Describe a market design package that a central bank could trust:

- reserve backing and public reserve attestations
- redemption service-level agreements
- issuer reporting on supply, redemptions, and large flows
- market-maker reporting on inventory, spreads, venues, and abnormal conditions
- DEX liquidity caps that scale with transparency and operational controls
- circuit breakers for abnormal premiums, discounts, rapid flow changes, or stale
  reference prices
- source-age disclosure for every displayed quote or market-making decision

The policy ask is not "let the market do anything." It is "let the domestic
stablecoin market grow through observable, auditable rails."

### 5. Market making becomes monetary infrastructure

Use the repo's research results as a measurement example. Quidax and Uniswap v4
cNGN prices were broadly in line over the observed overlap, usually within tens
of basis points. That is useful evidence that the venues were not wildly
disconnected, but it is not enough to declare either venue the universal truth.

This distinction matters for the central-bank trust argument. Good market making
is not just tighter spreads; it is a discipline for saying which prices are
fresh, which venues are executable, which inventory is local, and when the
engine should stop.

### 6. Domestic hedging rails are a future policy design question

Close by pointing beyond spot liquidity without overselling it. If users are
using foreign stablecoins to hedge inflation or depreciation, domestic naira
markets need credible alternatives over time. Tokenized government bills,
inflation-linked products, or regulated lending markets could eventually let
users manage naira risk without exiting the naira perimeter.

Frame this as a design direction, not a current claim. The article should say
that spot cNGN liquidity and trustworthy market making are the prerequisite.

## Likely Structure

1. The market does not believe a stablecoin until it can trade it.
2. Why the central bank is right to care.
3. Why thin domestic liquidity pushes users offshore.
4. The supervised-liquidity model.
5. What the cNGN market data shows and does not show.
6. From stablecoin rails to domestic hedging rails.

## Evidence And Repo Anchors

- `research/articles/evidence-pack-2026-07-cngn-market-making.md`
- `research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md`
- `research/autoresearch/fair-price.md`
- `research/autoresearch/quidax-cex-execution-eda-2026-07-10.md`
- `research/data/quidax_uniswap_v4_overlap_report.md`
- `engine/market/fair_price.py`
- `dashboard/docs/arbitrage/market-data.md`

## Claims To Avoid

- Do not claim the Central Bank of Nigeria is irrational or anti-innovation.
- Do not claim cNGN should flood DEXs with unrestricted liquidity.
- Do not say capital controls are simply bad.
- Do not treat the Binance Square post as authoritative evidence.
- Do not imply the IMF paper endorses tokenized T-bills, decentralized lending,
  or any particular product design.
- Do not claim Quidax, Uniswap v4, or Bybit is a clean fair-value truth label.
