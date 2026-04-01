- /dashboard/docs is the canonical reference for how the system operates and what problems it must solve. Reference every change against this, update the docs whenever making a change, and do not add anything that goes against the docs without first clarifying.
- Make documentation as succinct as possible. Avoid adding code blocks: just explain the underlying concepts in plain terms.
- Add the least code possible to address a request.
- Avoid code complexity and bloat at every opportunity.

## Execution flow invariants

These are the non-obvious invariants that must hold across every arb path. Each one was learned from a real bug. Reviewers must check all of them when any execution path changes.

**Routing produces a route decision, not execution inputs.** `select_route()` outputs a size and a direction. Any amounts an execution path needs (cNGN estimates, expected outputs) must be derived locally at execution time from the pool state cache. Carrying computed amounts from routing to execution via `SelectedRoute` fields creates a stale-data class of bugs — the pool can move between routing and execution, and those fields become a second source of truth that diverges from reality.

**DEX sell amount uses the preflight estimate; CEX sell uses the actual buy result.** For a DEX sell leg, execution uses the same cNGN estimate that was validated in the preflight simulation (`sell_cngn_amount`), not `buy_trade.amount`. The preflight confirmed the pool can absorb that exact amount, so reusing it avoids a second simulation at execution time. For a CEX sell leg, `buy_trade.amount` is correct — the CEX order should reflect exactly what was received, since no on-chain simulation is needed and using the estimate could over- or under-sell relative to actual delivery.

**Null-safe fallbacks on Optional DB fields.** When reading an Optional field from a DB-loaded model and falling back to another field, always use `x if x is not None else y`, never `x or y`. The `or` form silently skips a stored zero, re-opening the bug the field was introduced to fix.

**No hardcoded gas limits for swaps.** Every swap transaction must call `eth_estimateGas` and apply a fixed multiplier (currently 1.2×) as the gas limit. A hardcoded constant cannot account for varying pool tick ranges, hook complexity, or calldata length and will cause out-of-gas reverts on atypical trades. Approval transactions are exempt since their gas is stable and predictable.

**Detection and execution must use the same adapter.** Before adding a new venue or renaming one, verify that the venue key used in the arb signal (`cex_dex.py`/`dex_dex.py`) exactly matches the key registered in `engine/main.py` and the executor's venue map. Mismatches silently route execution through the wrong adapter.

**Trade size must be capped against both legs before execution.** The router (`engine/core/arbitrage/router.py`) caps against buy-side stablecoin and sell-side cNGN. Any change to execution flow must preserve both caps. This assumes all arb directions follow the buy-cNGN/sell-cNGN pattern — if a new direction is added where the sell leg consumes stablecoin instead of cNGN, the inventory model in `router.py` and `inventory.py` must be extended to handle it.

**DB migrations: never use `try/except` around `execute()`.** Use `PRAGMA table_info(table)` to check for existing columns before ALTER TABLE. Caught execute() exceptions leave aiosqlite in a dirty state that hangs pytest.

## Price confidence invariants

**Confidence is capped at 90% — always.** The NGN price is inherently uncertain; 100% confidence is never appropriate. The ceiling is `min(0.9, ...)` in `BlendedPriceCalculator._compute_confidence`. Do not raise it.

**Confidence degrades by 20% per missing venue, flooring at 0.** With all venues reporting: 90%. One missing: 70%. Two missing: 50%, and so on.