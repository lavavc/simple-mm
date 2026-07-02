# LP Research Implementation Status

## Completed

This repo now has a first concrete implementation of a paper-informed LP research and policy layer.

### 1. Richer persisted LP snapshots

The persisted `lp_position_json` payload now includes additional fields needed for paper-style analysis:

- `tick_lower`
- `tick_upper`
- `current_price`
- `price_position_fraction`

These were wired through:

- [engine/types.py](/Users/johnbeecher/Desktop/automated-infra/engine/types.py:64)
- [engine/lp/types.py](/Users/johnbeecher/Desktop/automated-infra/engine/lp/types.py:28)
- [engine/lp/uniswap_v4.py](/Users/johnbeecher/Desktop/automated-infra/engine/lp/uniswap_v4.py:628)
- [engine/venues/dex/base.py](/Users/johnbeecher/Desktop/automated-infra/engine/venues/dex/base.py:300)

This gives the persisted data enough information to classify where price sat relative to the LP range at each snapshot.

### 2. DB read helpers for LP reconstruction

The DB layer now exposes query helpers to pull:

- LP actions in a time window
- LP position snapshots in a time window

This was added in:

- [engine/db/queries/actions.py](/Users/johnbeecher/Desktop/automated-infra/engine/db/queries/actions.py:84)
- [engine/db/queries/positions.py](/Users/johnbeecher/Desktop/automated-infra/engine/db/queries/positions.py:37)
- [engine/db/backend.py](/Users/johnbeecher/Desktop/automated-infra/engine/db/backend.py:41)
- [engine/db/repository.py](/Users/johnbeecher/Desktop/automated-infra/engine/db/repository.py:56)

### 3. LP episode reconstruction and research analytics

A new module reconstructs LP episodes from persisted `actions` and `position_snapshots`:

- [engine/lp/research.py](/Users/johnbeecher/Desktop/automated-infra/engine/lp/research.py:1)

Current outputs include:

- grouping snapshots by `token_id`
- identifying closed vs open episodes using confirmed removal actions
- start and end price bucket classification:
  - `below`
  - `in_range`
  - `above`
- derived `position_type` strings such as `in_range_to_in_range`
- per-episode:
  - start/end value
  - PnL
  - PnL return
  - duration
  - delta traversed
  - average active share
- venue-level summary:
  - total episodes
  - closed episodes
  - profitable share
  - total PnL
  - average return
  - win-score
  - dominant position type

### 4. Finite-state LP policy

A new finite-state LP policy module was added:

- [engine/lp/policy.py](/Users/johnbeecher/Desktop/automated-infra/engine/lp/policy.py:1)

Current states/actions:

- States:
  - `idle`
  - `accumulate`
  - `active`
  - `harvesting`
  - `repositioning`
  - `defensive`
- Actions:
  - `enter`
  - `hold`
  - `harvest`
  - `reset`
  - `defend`

Current decision rules are simple and deterministic:

- `ENTER` when there is no active LP position
- `HARVEST` when open-episode return and in-range traversal exceed configured thresholds
- `RESET` when price materially overshoots the selected range
- `DEFEND` when stop-loss or weak historical win-score conditions are hit
- `HOLD` otherwise

### 5. Rebalancer integration

The LP rebalancer now consults the research-driven policy before falling back to the old in-range topup flow:

- [engine/lp/rebalancer.py](/Users/johnbeecher/Desktop/automated-infra/engine/lp/rebalancer.py:1)
- [engine/scheduler/core.py](/Users/johnbeecher/Desktop/automated-infra/engine/scheduler/core.py:120)

Behavioral change:

- out-of-range logic still works as before
- in-range positions can now be recentered early for:
  - profit harvest
  - defensive exit
  - policy reset

### 6. CLI analysis script

A new script can run LP analysis directly against the SQLite DB:

- [research/scripts/analyze_lp_strategy.py](/Users/johnbeecher/Desktop/automated-infra/research/scripts/analyze_lp_strategy.py:1)

Example:

```bash
python research/scripts/analyze_lp_strategy.py --db ./data/cngn.db --venue uni-base
```

### 7. Tests

Added focused tests for:

- LP episode reconstruction
- policy transitions
- compatibility with existing LP tests

Files:

- [tests/test_lp_research.py](/Users/johnbeecher/Desktop/automated-infra/tests/test_lp_research.py:1)

Validated with:

```bash
pytest -q tests/test_lp_research.py
pytest -q tests/test_lp_strategy.py
pytest -q tests/test_database.py
pytest -q tests/test_lp_e2e.py
pytest -q tests/test_lp_ratio.py
```

## Current Limitations

### 1. Historical backfill is only partially faithful

Older `position_snapshots` do not contain the new `current_price` and `price_position_fraction` fields.

Implication:

- new analysis quality is much better for data captured after this change
- older DB history can still be analyzed, but classification may be incomplete or degraded

### 2. Episode reconstruction is snapshot-based, not transaction-native

The paper reconstructs capital cycles from mint/burn/collect transaction data.

Current implementation reconstructs episodes from:

- confirmed LP lifecycle actions
- periodic position mark-to-market snapshots

Implication:

- this is directionally useful
- it is not yet a full on-chain-equivalent accounting model
- fee PnL, inventory PnL, and transaction-cost attribution are not yet separated cleanly

### 3. Position taxonomy is simplified

The paper uses a richer 15-type taxonomy.

Current implementation uses a compressed form based on start/end location relative to range:

- `below_to_below`
- `below_to_in_range`
- `in_range_to_in_range`
- `in_range_to_above`
- etc.

Implication:

- enough for early policy development
- not yet a full replication of the paper’s taxonomy

### 4. Win-score is approximate

The current win-score implementation uses closed episodes and their durations as an approximation of the cumulative PnL path.

Implication:

- useful as a ranking signal
- not yet a full reproduction of the paper’s exact path-area construction

### 5. Policy thresholds are hard-coded defaults

The finite-state policy currently uses fixed defaults in code.

Implication:

- easy to test
- not yet operator-configurable or venue-specific

## Remaining Work

### 1. Add a first-class derived research table

Create persistent derived tables such as:

- `lp_events`
- `lp_position_episodes`
- `lp_episode_features`
- possibly `lp_strategy_summaries`

This should replace repeated on-demand reconstruction and make the analytics queryable by API and dashboard.

### 2. Persist more LP lifecycle detail at action time

Add richer structured metadata to LP actions:

- token amounts in normalized units
- price at action time
- range bounds in price space
- active share
- current position value
- fair-price deviation
- portfolio delta snapshot
- gas used / gas USD

This is needed for better episode accounting and attribution.

### 3. Replicate the paper’s capital accounting more faithfully

Implement a true episode accounting model closer to the paper:

- explicit episode open capital
- explicit realized close capital
- capital-weighted close price
- matched add/remove legs
- treatment of topups and partial removals
- fee PnL separated from inventory PnL
- rebalance costs separated from gross LP outcome

### 4. Replicate the paper’s full position taxonomy

Upgrade the simplified bucket model to a richer taxonomy analogous to the paper’s 15 position types.

This should include:

- classification by start/end relative to range
- midpoint placement metrics
- lower/upper boundary-relative metrics
- signed traversal metrics
- overshoot metrics

### 5. Implement the paper’s path-based win-score exactly

Build the exact cumulative-PnL-path area metric rather than the current episode-duration approximation.

This likely requires:

- explicit cumulative realized PnL event series
- normalization by max positive / negative excursion
- area-above / area-below integration across the observation window

### 6. Make policy parameters configurable per venue

Add settings or persisted venue config for:

- profit take threshold
- stop loss threshold
- traversal threshold
- max overshoot threshold
- minimum acceptable historical win-score

This should live alongside existing `DexParams` or in a parallel policy config model.

### 7. Expand the finite-state machine

The current FSM is only the first version.

Still needed:

- explicit state transition persistence
- cooldown state after harvest/reset
- “stand down” state when market quality is poor
- “inventory override” state tied to global portfolio delta
- venue-specific transition logic

### 8. Use portfolio and fair-price context more deeply

The current policy only lightly consumes current range and optional strategy fair price.

Still needed:

- integrate portfolio delta directly into state transitions
- integrate fair-price deviation into early exit logic
- use swap-flow imbalance in policy decisions
- detect when LP inventory conflicts with arb inventory needs

### 9. Backtester integration

The backtester should be upgraded to evaluate the new policy directly.

Needed outputs:

- episode-level reports
- win-score
- PnL by position type
- harvest vs reset attribution
- traversal before profitable exit
- comparison of:
  - range-exit-only policy
  - profit-target-only policy
  - hybrid policy

### 10. API and dashboard exposure

Expose LP research summaries and episode views through the API and dashboard.

Useful endpoints/views:

- per-venue LP research summary
- open episode diagnostics
- recent closed episodes
- distribution of position types
- win-score over time
- policy action history

### 11. Migration / backfill tooling

Add a backfill command that:

- reads historical DB state
- reconstructs research episodes
- populates derived tables
- flags incomplete historical records where new snapshot fields are missing

## Practical Next Step

The next highest-value implementation step is:

1. add persistent `lp_episode_features` storage
2. promote the current analysis output into a queryable API surface
3. make policy thresholds configurable per venue
4. integrate the new policy into the backtester for controlled comparison against the existing range-exit-only logic

That would move this from “working research scaffold” to “closed empirical loop for strategy iteration.”
