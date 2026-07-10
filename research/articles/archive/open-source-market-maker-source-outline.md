# The Open-Source Market Maker for African Stablecoins

Archived source outline. This is not part of the July 2026 two-article sequence.
Keep it as source material for a later systems article after repo cleanup and
live-execution evidence are ready.

## Working Title

The Open-Source Market Maker for African Stablecoins

## One-Sentence Thesis

If local stablecoins are going to become public financial infrastructure, the machinery that keeps their venues aligned should be inspectable, falsifiable, and reusable.

## Why This Fits LAVA

This piece should be the most "builders and systems" oriented article in the series. LAVA writing often turns an abstract infrastructure claim into a concrete mechanism: escrow layers, routing layers, settlement layers, compliance at the edges, market actors, and failure modes. This outline should do the same for CEX-DEX market making.

The article should not present the repo as a magic profit bot. It should present it as a reference implementation that helps developers reason about price discovery, venue-local inventory, execution risk, and open research.

## Narrative Progression

### 1. Start with the invisible work behind tight spreads

Open with a practical scene: a fintech, OTC desk, or treasury wants to move between cNGN and USD stablecoins. The visible product experience is a quote. Underneath the quote is a live system answering several questions:

- Which venue has the best executable price right now?
- How much size can actually trade before slippage destroys the spread?
- Which inventory bucket is being consumed?
- What happens if one leg fills and the other fails?
- When should the engine skip a trade even if the headline spread looks profitable?

The article should make the point that liquidity quality comes from these unglamorous details.

### 2. Explain the engine as five layers

Use the repo's documented arbitrage pipeline:

**Market Data -> Signal -> Risk -> Execution -> Post-Trade**

Then translate each layer for readers:

- Market data: Quidax depth, Uniswap V4 pool state, Bybit P2P references, gas, portfolio exposure.
- Signal: CEX-DEX and DEX-DEX opportunity detection.
- Risk: venue-local inventory caps, rolling volume, delta limits, circuit breakers.
- Execution: preflight on-chain legs, CEX REST exceptions, route execution, live amount derivation.
- Post-trade: persistence, half-open recovery, alerting, and auditability.

This is the most important design explanation in the piece because it shows the system as infrastructure rather than a single trading loop.

### 3. Design choice: route registry as market grammar

Highlight `engine/arb/routing/route_registry.py` as the source of truth for route direction, venue names, pipeline, leg type, and cNGN effect.

Editorial framing: in a thin market, a duplicated or ambiguous route definition is not a style issue; it is a financial risk. The route registry gives the system a common grammar for saying "buy here, sell there, inventory moves this way."

Potential explainer table:

| Family | Example | What it means |
|---|---|---|
| CEX-DEX | Quidax to Uniswap BSC | Buy cNGN on Quidax, sell on-chain |
| CEX-DEX | Uniswap Base to Quidax | Buy on-chain, sell into Quidax depth |
| DEX-DEX | Base to BSC delta-balance | Buy on one chain, sell from existing inventory on the other |

### 4. Design choice: inventory is venue-local

This should be a major section. The engine refuses to treat inventory as globally fungible across Quidax, Base, and BSC. A cNGN fill on one venue does not magically appear on another venue.

Tie this to African stablecoin reality: liquidity fragmentation is not just about price; it is about where the assets are, which rail can settle, and how expensive it is to rebalance. The engine's route scoring penalizes trades that make future rebalancing harder.

### 5. Design choice: execution distrusts stale route amounts

Explain a subtle but important invariant:

- `select_route()` decides ranking, adjusted size, and expected profit.
- Execution derives live token amounts locally at execution time.
- DEX sell legs use preflight cNGN estimates, not stale routing fields.
- Recovery uses persisted `buy_amount_cngn`, not a fresh wallet query.

This makes the system more publishable because it shows where live market uncertainty enters the stack.

### 6. Design choice: half-open recovery is first-class

Explain half-open trades: buy succeeded, sell failed. The engine immediately persists the buy hash, buy amount, executed size/status, and trips the circuit breaker.

The article should make this accessible: a market maker's scariest bug is not missing a trade; it is thinking a trade is complete when only one leg happened.

### 7. The open-source analogy to Flashbots

Frame the release as an invitation, not a victory lap. Flashbots did not just publish code; it gave searchers, validators, researchers, and protocols a shared way to talk about a hidden market. This repo can help do the same for CEX-DEX liquidity around African stablecoins.

The article should encourage:

- publishing route logic and assumptions
- publishing data methodology
- separating live policy from research experiments
- standardizing markout labels for local stablecoin markets
- treating failed hypotheses as useful public infrastructure

Use the live LP EDA as the concrete proof of why this discipline matters. The
same V4 pools can look healthy in aggregate while our own LP addresses end close
to flat or slightly negative. Owner-level accounting changes the story: Base had
8 owners in the exported ledger, BSC had only 2, and the apparent aggregate BSC
profit came from another large owner rather than our in-house address. That is
exactly the kind of distinction open-source market-making infrastructure should
make legible.

### 8. Results slot for the finished article

When live or simulated execution evidence is ready, add:

- count of detected CEX-DEX and DEX-DEX opportunities by direction
- opportunity size distribution after inventory caps
- skipped trades by reason: no profit, insufficient buy-side stablecoin, insufficient sell-side cNGN, circuit breaker, preflight failure
- half-open/recovery incident count and examples
- realized versus expected profit by route family
- gas and rebalancing penalty contribution to route selection
- live LP owner-level accounting: aggregate pool ledger versus in-house LP
  addresses, average/median owner PnL, and tail outcomes

## Likely Structure

1. The quote is the easy part.
2. A five-layer engine for local stablecoin liquidity.
3. Routes are grammar, not constants.
4. Inventory is local, not wishful.
5. Execution is where stale assumptions go to die.
6. Recovery is part of the product.
7. Accounting is part of the product.
8. Why this should be open-source.

## Evidence And Repo Anchors

- `dashboard/docs/arbitrage/overview.md`
- `dashboard/docs/arbitrage/signal.md`
- `dashboard/docs/arbitrage/risk.md`
- `dashboard/docs/arbitrage/execution.md`
- `engine/arb/routing/route_registry.py`
- `engine/arb/routing/router.py`
- `engine/arb/execution/route_execution.py`
- `engine/arb/execution/preflight.py`
- `engine/arb/execution/recovery.py`
- `engine/arb/risk/inventory.py`
- `research/articles/archive/dex-lp-live-equity-curves-notes.md`
- `tests/test_router.py`
- `tests/test_cex_dex_execution.py`
- `tests/test_dex_dex_execution.py`
- `tests/test_arb_persistence.py`

## Claims To Avoid Until Proven

- Do not frame the repo as a fully generalized market maker for every African stablecoin.
- Do not imply bridge-based DEX-DEX execution; the current DEX-DEX model is delta-balance with existing venue inventory.
- Do not hide the operational risk around API latency, bank-hour redemption, gas, inventory drift, or half-open trades.
- Do not present open-sourcing as sufficient by itself; the useful artifact is code plus methodology plus data discipline.
- Do not present aggregate pool PnL as our own LP performance without
  owner-level attribution.
