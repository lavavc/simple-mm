# Weighted Portfolio Execution Finalization Protocol Amendment

**Date frozen:** 2026-07-24

**Protocol:** `2026-07-24`

**Artifact schema:** `weighted-portfolio-artifacts/v3`
**Status:** binding amendment; fresh frozen execution pending

This amendment replaces only the entry-scaling, terminal-settlement, diagnostic,
checkpoint-compatibility, and source-freeze clauses of the July 23 weighted LP
portfolio correction contract. All other July 10 and July 23 constraints remain
binding.

## Preserved Methodology

- The primary experiment uses the 10% aggregate synthetic-liquidity-share cap.
  A 15% cap is a separately labelled sensitivity and cannot rescue a failed
  primary result.
- The 10% sleeve cap, 35% family cap, causal windows, catalog, allocation rules,
  comparators, residual cash, removal tests, and both reset-matrix PBO analyses
  remain unchanged.
- Base and BSC remain separate experiments. Base cNGN is token0; BSC cNGN is
  token1. Execution accounting uses economic direction rather than token index.
- Historical pool liquidity and price paths remain exogenous. Synthetic LP
  positions affect only portfolio accounting and the aggregate-liquidity cap.

## Entry Execution

An entry candidate is evaluated once at full scale. Admission, range, sizing,
expected fee APR, event state, and the full fixed transaction-cost breakdown are
immutable in an `EntryIntent`. Scale trials may not rerun any of those decisions.

The fixed entry cost is paid once and is never scaled. The remaining funded
wallet is scaled on the finite lattice
`{256/256, 255/256, ..., 1/256}`.

Every lattice point is tested in descending order because joint affordability
need not be monotone after inventory netting and variable costs. The first
feasible point is selected. Only `JointActionAffordabilityError` means that a
lower point may be tried; malformed state and all other accounting failures
propagate immediately. If no positive lattice point is affordable, the entire
entry batch becomes a zero-scale no-op and incurs no fixed cost.

Every batch with at least one frozen intent produces an ordered
`EntryScaleEvent` containing its event identity, intended entry count, executed
entry count, and exact lattice scale. A zero-scale no-op is retained with zero
executed entries. Published scalar counts and the minimum scale must reconcile
to this trace.

## Terminal Settlement

Terminal liquidation is one atomic portfolio operation:

1. Freeze a removal intent for every open position.
2. Remove all synthetic liquidity and charge one fixed removal cost per open
   position. Loose cNGN has no per-sleeve fixed cost.
3. Pool the resulting wallets, including residual cash. When aggregate cNGN is
   positive, sell it once through an exact-input `cngn_to_stable` swap; otherwise
   record zero terminal inventory swaps and zero inventory-swap cost.
4. Price a positive sale against historical active liquidity after all synthetic
   liquidity has been removed. Charge one additional swap fixed cost and one
   aggregate variable cost only when that sale occurs.
5. Allocate the sale economics pro rata by each sleeve's marked cNGN input.
6. Fund sleeve shortfalls deterministically from the pooled portfolio wallet.
   Signed `terminal_funding_transfer_usd` rows must sum to zero within
   `1e-10 * max(1, opening capital)`. The published cash row must reconcile its
   opening and closing values to its transfer within
   `1e-8 * max(1, absolute expected cash)`.

The canonical settlement is recomputed and validated before any sleeve runtime
is mutated. It must conserve both assets, contain exactly the funded sleeve set,
leave no open position or cNGN inventory, and reconcile fixed, variable, marked
notional, input, output, and funding-transfer identities. Aggregate insolvency
or malformed state fails closed; individual underfunding does not fail an
otherwise solvent portfolio.

## Evidence Contract

The v3 artifact set remains exactly 14 files per pool with the July 23 row-count
and matrix-completeness requirements. It adds the following binding evidence:

- typed `failure_type` and bounded `failure_reason` diagnostics on invalid rows;
  valid rows require both fields to be blank;
- blocked, carried, removal, comparator, concentration, stability, and PBO rows
  repeat the originating failure exactly;
- entry batch count, scaled batch count, and minimum execution scale;
- terminal position, loose-cNGN, and zero-settlement counts;
- terminal inventory-swap count, fixed cost, variable cost, and externally
  marked notional;
- signed terminal funding transfers in joint attribution.

Checkpoints persist the complete entry-scale trace and failure provenance.
Canonical serialization and deterministic ordering remain mandatory.

## Checkpoint and Source Provenance

The checkpoint container remains `weighted-portfolio-checkpoint/v1`, while its
run identity must bind protocol `2026-07-24` and artifact schema
`weighted-portfolio-artifacts/v3`. A v2 checkpoint or result package is never
resumed, upgraded, overwritten, or validated as v3. Raw histories, derived
features, QTS inputs, and correctly frozen catalogs may be reused only when their
bytes are hashed into the new run identity.

Source provenance uses a two-stage freeze:

1. Commit the complete source closure as immutable commit `X`.
2. In a follow-on commit outside that closure, pin the validator to `X` and run
   final attestation with worktree closure bytes identical to `X`.

Until this sequence and the full v3 run both pass, there is no validated v3
package and no authorization for article performance claims.
