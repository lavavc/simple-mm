# V4 LP Ledger Checkpoint And Progress Design

**Date:** 2026-07-21

**Status:** Approved design, amended 2026-07-22; implementation in progress

**Scope:** Full-RPC `rpc_verified` Base and BSC LP-ledger exports

## Problem

The full-RPC LP-ledger exporter currently materializes every intermediate in
memory and publishes only after the complete run. That publication boundary is
correct: a partial ledger must never be mistaken for verified market-structure
evidence. However, a host restart also discards completed candidate scans,
transaction and receipt reads, decoding work, and price-event scans. The
exporter exposes no durable phase or unit counters, so an operator cannot tell
whether a quiet process is advancing, blocked, or dead.

The Base and BSC exports were both terminated by a host reboot after several
hours. Their absent canonical ledger and coverage pairs correctly failed
closed, but all completed RPC work was lost. A subsequent live benchmark also
exposed a specification error: treating every PositionManager `Transfer`
transaction as a bundle candidate would fetch and decode roughly 804,000
unrelated positions. The replacement must preserve complete target-pool action
and ownership evidence while making expensive internal work resumable,
observable, and scoped to the research population.

## Goals

1. Resume a compatible full-RPC export automatically after interruption.
2. Preserve completed network work at bounded, explicit transaction
   boundaries.
3. Report each Base and BSC run's current phase, durable counts, rate, and ETA
   without requiring access to a live terminal.
4. Produce final ledger and coverage bytes deterministically from staged data.
5. Preserve the existing fail-closed final-pair publication semantics while
   adding reboot-durable directory synchronization and exact checkpoint-aware
   reconciliation.
6. Reject corrupt, incompatible, reorged, or ambiguous checkpoint state rather
   than guessing how to recover it.
7. Ensure RPC credentials and endpoint URLs cannot enter checkpoint, progress,
   status, or error artifacts.
8. Attest the complete PositionManager `Transfer` log scan while fetching
   transaction bundles only for target-pool actions and ownership transfers of
   token IDs derived from those actions.
9. Reuse the frozen pool-history replay artifacts by exact byte digest instead
   of rerunning their already completed exports.

## Non-Goals

- A partial checkpoint is not an LP ledger, coverage sidecar, frozen result, or
  publication artifact.
- This change does not alter the July 15 statistical estimand, event-time price
  methodology, portfolio rules, or article evidence policy. It corrects the
  acquisition contract so unrelated PositionManager positions are scan
  evidence rather than transaction-bundle candidates.
- This change does not create a generic workflow or checkpoint framework for
  unrelated research scripts.
- Candidate-list and fixture exports do not become verified or resumable merely
  because they emit progress.
- The initial implementation adds no parallel RPC fan-out. Its performance
  correction comes from ordering action discovery before transfer filtering and
  eliminating unrelated bundle reads. A configured parent range may be queried
  through bounded sequential binary subdivision after three retryable
  transport, rate-limit, or provider-range failures. Children are transient
  query units; the fixed parent remains the only durable attestation unit and is
  committed only after every child succeeds. A non-retryable or malformed
  response, explicit truncation signal, or failure at a single-block leaf stops
  the run without committing the parent.

Alchemy documents either a 10,000-log cap for an arbitrary block range or an
uncapped log count for a range of at most 2,000 inclusive blocks, subject to a
150 MB response limit. Every transient `eth_getLogs` query is therefore
proactively bounded to at most 2,000 inclusive blocks before the retry and
subdivision policy applies. This bound is an acquisition invariant, not a
performance hint.

## Existing Evidence Boundary

The July 15 design requires each verified ledger to bind exact CSV bytes to a
full inclusive scan from pool inception through the requested end block. The
amended evidence population is:

1. every target-pool PoolManager `ModifyLiquidity` witness;
2. the canonical token-ID set derived from complete, reconciled action
   decoding;
3. every PositionManager ERC-721 `Transfer` witness whose indexed token ID is
   in that frozen set; and
4. the complete count and canonical digest of all PositionManager `Transfer`
   logs observed while producing that relevant subset.

Transactions and receipts must agree on identity and location for every fetched
action or relevant ownership candidate. Endpoint headers must match before and
after all reads, and the final CSV and coverage sidecar must publish as one
validated pair. The complete unfiltered transfer scan is attested but its
unrelated transactions are never fetched.

The existing precoverage ledgers are regression oracles only. Their 36 Base and
13 BSC rows prove exact attribution for the rows present, not completeness. In
particular, all target-pool action witnesses must be reconciled before the token
set freezes; wrapped or indirect PositionManager calls cannot be silently
omitted merely because the top-level transaction input is not a direct
`modifyLiquidities` call.

Those rules remain authoritative. Checkpointing operates strictly before the
final pair-publication boundary. A completed checkpoint can reconstruct a
proposed pair, but only successful coverage construction, staged validation,
and final pair publication create canonical evidence.

This evidence is provider-conditioned: complete normalization and durable
attestation prove what the configured RPC returned under the bounded query
contract, but cannot disprove an undisclosed provider omission. A stronger
claim requires an independent-provider comparison or an exhaustive audit over
smaller ranges.

## Selected Architecture

Use a private SQLite staging database for resumable evidence and a separate
canonical JSON progress file for operational visibility.

SQLite is preferred over a monolithic JSON checkpoint because repeated JSON
replacement would rewrite all accumulated logs and receipts after every durable
unit. It is preferred over an append-only journal because SQLite already
provides transactional commits, torn-write recovery, uniqueness constraints,
indexed resume queries, and integrity checking without a custom hash-chain and
tail-repair protocol.

The progress JSON is intentionally separate. It is small and easy to inspect,
but it is not the source of truth for durable work. If it lags or survives a
crash with `status="running"`, the database and run lock determine the actual
state.

## Paths And Locks

Given an output such as:

```text
research/data/derived/uni_base_lp_ledger.csv
```

the default operational files are:

```text
research/data/derived/uni_base_lp_ledger.csv.checkpoint.sqlite3
research/data/derived/uni_base_lp_ledger.csv.progress.json
research/data/derived/uni_base_lp_ledger.csv.run.lock
research/data/derived/uni_base_lp_ledger.csv.publish.lock
```

The first three are new. The publication lock already exists.

The exporter acquires the nonblocking output-scoped run lock before reading or
creating checkpoint state and retains it through final publication and the
terminal progress update. A second cooperating exporter targeting the same
output fails before it can change checkpoint, progress, or final files. The
existing publication lock remains nested around final pair replacement so the
publisher retains its standalone safety contract.

All output-derived lock, checkpoint, WAL, progress, ledger, and sidecar paths
are checked for symlinks before use. Locks are opened with no-follow semantics;
the exporter fails rather than following or unlinking an aliased path.

Base and BSC use different output-derived namespaces and therefore advance
independently. The status reader may inspect both concurrently but never writes
their databases or lock files.

## Run Identity And Compatibility

Every database contains exactly one run identity. Its canonical fingerprint
binds:

- checkpoint schema version;
- exporter semantic version and SHA-256 digests of every executing checkpoint,
  export, decoder, replay, and coverage-contract source module;
- Python and Web3 runtime versions used by the exporter;
- verification mode;
- pool name, chain name, and numeric chain ID;
- pool ID, PoolManager, PositionManager, the accepted wrapper EntryPoint, token
  addresses, token decimals, fee rate, and price orientation;
- inclusive requested start and end blocks;
- configured block chunk size, the explicit target-action topic, and the
  explicit PositionManager Transfer topic;
- sanitized RPC provider origin containing only scheme, hostname, and optional
  port; endpoint paths, user information, queries, fragments, and credentials
  are forbidden;
- normalized absolute frozen replay-artifact path, SHA-256 digest, byte length,
  row count, exact header digest, parser version, parser-contract digest,
  price-semantics source digest, first/last block, first/last timestamp, chain,
  and pool ID;
- normalized absolute output path; and
- initial start/end block hashes and timestamps.

Full RPC URLs, API keys, HTTP headers, command-line strings, exception messages,
and response diagnostics are forbidden from the database and fingerprint
payload. The sanitized provider origin is required so the provider-conditioned
attestation remains identifiable without retaining credentials.

On a new run, the exporter validates arguments and chain identity, captures the
endpoint snapshot, computes and validates the frozen replay identity, creates
the exact schema transactionally, and records the fingerprint before discovery
begins. The later `replay_input_bind` phase rereads and revalidates the file,
then persists evidence that must equal this preflight identity. Bytes changing
between preflight and binding, or between attempts, make the run incompatible.

On resume, it opens the database without migration, rejects a symlinked file,
runs SQLite integrity and exact-schema checks, recomputes all local identity
fields, re-reads the chain ID and endpoint snapshot, and requires exact
fingerprint equality. Unknown tables, columns, indexes, triggers, schema
versions, gaps, overlaps, or inconsistent phase cursors fail closed. There is no
automatic migration, rewind, or partial salvage.

The 2026-07-22 action-first amendment bumps both checkpoint and progress schema
versions. Any pre-amendment checkpoint or progress file fails closed and
requires explicit `--fresh`; no run had produced a canonical pair under the old
checkpoint schema, so compatibility shims are forbidden.

The same command resumes a compatible checkpoint by default. `--fresh` is the
only supported way to abandon staging state. It is processed while holding the
run lock, reinitializes the checkpoint namespace, and leaves any previously
valid canonical ledger and coverage pair untouched until a new pair is ready.
It refuses symlinked operational paths rather than unlinking their targets.
`--fresh` never repairs or legitimizes a partial final pair.

## SQLite Durability Contract

The implementation uses the standard-library `sqlite3` module with:

- `journal_mode=WAL`;
- `synchronous=FULL`;
- `foreign_keys=ON`;
- `trusted_schema=OFF`;
- explicit transactions for every durable unit; and
- an integrity check on resume before staged data is trusted.

Checkpoint creation uses restrictive local permissions. The checkpoint
namespace comprises the database plus its SQLite-managed `-wal` and `-shm`
siblings. Resume and status open that namespace using SQLite rather than parsing
individual files. Explicit `--fresh` closes all connections, holds the run lock,
refuses symlinks, removes all three known paths, creates the new database, and
`fsync`s the parent directory.

After committing terminal state, the exporter attempts
`wal_checkpoint(TRUNCATE)` with a bounded busy timeout. Status reads use one
short snapshot transaction and close promptly. A busy reader may leave a valid
WAL without invalidating the published pair; the WAL remains part of the
checkpoint namespace and the next exclusive exporter attempt retries the
checkpoint. An I/O, corruption, or non-busy checkpoint failure is surfaced as a
checkpoint-maintenance failure with `output_published` preserved. The schema is
private and versioned; no other analysis may read it as research evidence.

Logical tables cover these responsibilities:

| Table group | Durable responsibility |
| --- | --- |
| Run metadata and phases | Identity, fingerprint, endpoint snapshot, attempts, phase state, and exact durable cursors |
| Action-discovery chunks and witnesses | The complete fixed block partition, every target-pool `ModifyLiquidity` log including full topics/data, and exact per-chunk completion |
| Action candidates and bundles | Canonically normalized target-action transaction and receipt payloads, location facts, and payload digests |
| Block headers | Deduplicated canonical block hash and timestamp reads used during decode |
| Position resolutions | Historical token-position lookups, including explicit no-position results, bound to their query block |
| Decoded action state and rows | Chronological action cursor, current token state including the salt-bearing `PoolPositionKey`, decoded actions, historical resolution results, and exact witness reconciliation |
| Frozen token set | Canonical distinct action token IDs, cardinality, and digest committed only after complete action decoding |
| Transfer-scan chunks | Complete fixed block partitions with total unfiltered transfer-log count/digest and retained relevant count for every chunk |
| Relevant transfer witnesses and bundles | Full log identity for frozen-token transfers plus canonically normalized bundles only for relevant transfer-only transactions |
| Ownership decode | A separate relevant-transfer cursor and chronologically ordered ownership events restricted to the frozen token set |
| Replay input | Exact frozen replay path, bytes/header/parser-contract/source digests, byte length, row count, parser version, chain/pool, range, and endpoint timestamps validated against run identity |
| Action price bindings | Deterministic event-time state attached to each decoded action |

Exact SQL belongs in the implementation plan, but the schema must use primary
keys and uniqueness constraints that make duplicate candidates, witnesses,
actions, token IDs, ownership events, transfer-scan attestations, replay inputs,
and price bindings impossible to accept silently.

SQLite `INTEGER` is permitted only for values proven to fit its signed 64-bit
range, such as block number, transaction index, log index, event order,
timestamps, phase counters, and bounded ticks. EVM integers that may exceed that
range—including token IDs, `sqrt_price_x96`, liquidity, liquidity deltas, and
raw token amounts—use canonical decimal `TEXT` or fixed-width canonical hex
`TEXT` according to one field-specific rule. Python `Decimal` values use
canonical decimal `TEXT`. Hashes and addresses use normalized lowercase hex
`TEXT`. Reads parse and reserialize each value to require exact canonical
round-trip equality; no EVM or accounting value passes through SQLite `REAL`.

## Durable Phase Machine

The only forward phase order is:

```text
preflight
  -> action_discovery
  -> action_fetch
  -> action_decode
  -> token_set_freeze
  -> full_transfer_scan
  -> relevant_transfer_fetch
  -> relevant_transfer_decode
  -> replay_input_bind
  -> price_replay
  -> build
  -> publish
  -> succeeded
```

A failed or interrupted attempt may resume the current phase after compatibility
validation. It may not skip an incomplete phase. Phase completion is stored in
the same transaction as the last unit that satisfies that phase.

### 1. Preflight

Validate the `.csv` output contract, acquire the run lock, resolve pool
configuration, and require a full-RPC start block equal to that pool's configured
inception block. Require the requested range to equal the frozen replay
artifact's first/last block exactly; a ledger may not precede or extend beyond
its price evidence. Then create the Web3 client, require the expected chain ID,
and capture the exact requested start/end headers. Create or validate the
checkpoint before any resumable scan. Candidate-list and fixture modes remain
explicitly unverified and keep their separate requested-range behavior.

### 2. Action Discovery

Partition the inclusive range using the configured chunk size and fetch only
target-pool `ModifyLiquidity` logs. Require block hash/number, transaction
hash/index, log index, PoolManager address, complete topics, and data. Normalize
and sort every witness before atomically committing the witness group and chunk
completion. Empty chunks are positive evidence. Resume skips only committed
action chunks.

The action transaction set is the canonical distinct hash set reconstructed
from these witnesses. It is not inferred from an existing ledger or scalar
last-block marker.

### 3. Action Fetch

Fetch and stage one normalized transaction/receipt bundle for every action
transaction. Require requested, transaction, and receipt hashes to agree;
transaction and receipt block number/index/hash to agree; the location to fall
within the frozen range; and every action witness to appear in the receipt at
the same address, block hash, log index, topics, and data. Fetch order does not
define decode order.

### 4. Action Decode

Decode action bundles in canonical
`(block_number, transaction_index, transaction_hash)` order. Block headers and
historical position resolutions are cached transactionally. Each bounded commit
contains decoded actions, token-position state changes, historical resolutions,
the action-bundle marker, and exact chronological cursor.

Every target-pool witness must map to exactly one represented action identity.
Direct PositionManager calls, multicalls, and supported wrapped calls share that
same invariant. A top-level input that cannot be decoded is not evidence that
the action is irrelevant. Wrapped Base burn transactions must be decoded or the
run fails closed. The supported wrapped path is the configured EntryPoint
`handleOps` call, one representable user operation, the pinned account
`execute(bytes32,bytes)` batch mode, and exactly one configured PositionManager
call within the decoded batch. Its effective sender is the user-operation
sender, never the outer bundler. Unknown targets, selectors, modes, nested
generic recursion, malformed encodings, or zero/multiple matching
PositionManager calls fail closed.

Every raw target-pool modify log is decoded into its full pool, sender, ticks,
signed liquidity delta, salt, and log identity. An action matches exactly one
witness by this semantic identity; ordering is only a deterministic tie-break
after identity equality. Mints pair all mint operations with all zero-address
PositionManager Transfers in operation/log order before structural matching.
Because verified runs begin at the configured pool-inception block, resolving a
target-pool position before its first persisted mint is evidence that the
inception action history is incomplete. The decoder fails closed instead of
synthesizing a pre-range position key. Historical resolution caching remains
useful for explicit not-found classification and resume consistency.
Increase/decrease actions recover the token's prior salt-bearing position key
and require exact pool/ticks/salt/delta agreement. Selecting the first mint,
positionally dequeuing a nonmatching witness, clamping invalid liquidity, or
leaving any action/witness unmatched is forbidden.

Periphery recipients are resolved before receipt attribution:
`MSG_SENDER` (`address(1)`) maps to the effective sender and `ADDRESS_THIS`
(`address(2)`) maps to the configured PositionManager. `SETTLE_PAIR` and
`TAKE_PAIR` currency order is not semantically significant. Withdrawal receipt
attribution accepts at most one ERC-20 `Transfer` per pool token from the
configured PoolManager to the resolved recipient. Multiple target `TAKE_PAIR`
actions, an overlapping same-recipient `TAKE_PAIR`, multiple matching receipt
transfers, or a target withdrawal left without a supported `TAKE_PAIR` are
ambiguous and fail closed rather than producing inferred or zero proceeds.

### 5. Token-Set Freeze

After action decoding is complete, derive the sorted distinct token IDs from
the persisted decoded actions. Commit the full canonical set, count, and digest
in one evidence-bearing transition. Recompute the set from decoded rows during
resume validation. An incomplete action cursor, unreconciled witness, duplicate
or noncanonical token ID, or digest mismatch blocks the transition.

The frozen set is the only authority for subsequent ownership relevance. The
precoverage ledgers may be compared as regression fixtures but may not seed or
restrict this set.

### 6. Full Transfer Scan

Only after the token set freezes, scan all PositionManager ERC-721 `Transfer`
logs over the complete inclusive range. For each chunk, normalize every log and
compute its canonical unfiltered count and digest before filtering. Commit the
unfiltered count/digest, relevant count, relevant witnesses, and chunk
completion atomically. Retain a witness when indexed `topic3` is in the frozen
token set, including zero-address mint and burn transfers. Unrelated witnesses
are covered by the unfiltered attestation but do not become candidates.

The chunk digest preimage is canonical JSON for the complete ordered list of
normalized witness objects with fields `source`, `block_number`, `block_hash`,
`transaction_hash`, `transaction_index`, `log_index`, `address`, `topics`, and
`data`. Objects sort by `(block_number, transaction_index, log_index,
transaction_hash)`; duplicate locations or identities are rejected. JSON uses
sorted object keys, ASCII escaping, and separators `(",", ":")`. The aggregate
digest hashes the same canonical JSON encoding of ordered chunk-attestation
objects with fields `index`, `start_block`, `end_block`, `unfiltered_count`,
and `unfiltered_sha256`.

Filtering applies across the full range, not from a token's first observed
action. Provider errors, oversized responses, or ambiguity trigger deterministic
sequential subdivision/retry under the bounded policy above. Successful child
logs are normalized and merged into their configured parent before its count
and digest are computed, so retry shape cannot change durable evidence. A chunk
is never marked complete after a potentially truncated response. The
2,000-block cap constrains each transient RPC request; it does not constrain the
5,000-block configured parent recorded in the checkpoint and sidecar.

### 7. Relevant Transfer Fetch

The final eligible transaction set is exactly the union of action transaction
hashes and retained relevant-transfer transaction hashes. Reuse already staged
action bundles. Fetch only missing relevant transfer-only bundles and validate
each retained witness against its receipt with the same strict location and log
identity checks used for actions.

### 8. Relevant Transfer Decode

Decode ownership events from eligible bundle receipts in canonical transaction
order, restrict them to the frozen token set, and preserve exact log ordering.
Action and ownership decoding have separate durable markers so a transaction in
both populations is processed once per responsibility without duplicating its
raw bundle. Ownership changes before, within, and after action transactions
remain evidence through the frozen endpoint. Every retained Transfer witness
must reconcile one-to-one with exactly one persisted ownership event using full
log identity, token ID, previous/new owner, and canonical order. Missing,
duplicate, or extra frozen-token ownership events fail closed.

### 9. Replay Input Bind

Do not rerun the completed pool-history exports. Validate the configured frozen
`uni_base_pool_history_replay.csv` or `uni_bsc_pool_history_replay.csv` path,
exact bytes digest, byte length, row count, first/last block, canonical order,
pool/chain identity, and canonical sqrt-price semantics against the run
identity. The exact frozen header is validated; each row requires block time,
chain, pool ID, event type, transaction hash, log index, block number,
`sqrt_price_x96`, tick, token symbols, and event source. Rows must be unique and
strictly canonical by block/log plus deterministic row order. Only `initialize`
and `swap` rows update carried price state, and their positive
`sqrt_price_x96`/tick values are mandatory. The cNGN mid is derived from
`sqrt_price_x96` with the pinned token orientation and decimals; stored
`cngn_usd_price` is diagnostic metadata and never replay state. Commit the
validated metadata as the replay input. A changed or malformed file makes the
checkpoint incompatible. The parser module and price-semantics module are
included in the run-identity source digests.

The parser version names an immutable replay profile. That profile binds the
exact header, event-source policy, price-event classification, and a
price-semantics dependency-closure digest covering `pool_price_semantics.py`,
`clmm_math.py`, and `engine/math/v3.py`. Generic sidecar and manifest loading
validates the persisted `parser_contract_sha256` and price-semantics digest
against the registered immutable profile so sealed research remains inspectable
after later code changes. The parser-contract digest includes the exact exporter
and analysis parser code paths plus their shared Web3 normalization and replay
event contracts, including the event-time state replay algorithm and its price/
carried-event policy. The digest is persisted in the checkpoint replay-input
record before it is copied to the final coverage sidecar. Active export and
analysis recompute the profile from the current checkout and fail closed on
drift; a semantic change therefore requires a new registered parser profile
rather than silently reinterpreting an old artifact.

The frozen artifact facts are:

| Pool | SHA-256 | Bytes | Data rows | First block | Last block |
| --- | --- | ---: | ---: | ---: | ---: |
| `uni-base` | `41c3d5b945abdffde32087590c21115914404f496069519c572e393b667f99b9` | 639921 | 1596 | 42926879 | 47514853 |
| `uni-bsc` | `bf99f9a17ea2ff0048da7c7eec4fa0c8fa3b9e7e5586e6200919b66edaccd1e2` | 1330201 | 3119 | 84655203 | 105135905 |

This is an explicit reuse claim, not a claim that price logs were freshly
rescanned by the LP-ledger exporter. The final coverage sidecar binds the replay
artifact digest and range.

Downstream provenance independently captures the whole replay artifact: bytes
digest, total data-row count, first/last block, and artifact first/last
timestamp. These facts are distinct from swap-only count and activity interval.
Verified publication requires exact equality between that whole-artifact
provenance and the sidecar replay identity.

### 10. Price Replay

Read the validated frozen replay artifact and reconstruct event-time state in
canonical `(block_number, log_index, event_order)` order. Initialize and swap
rows update carried state; each decoded action receives the state available at
its exact event location. Persist one unique binding per action. Missing,
duplicate, crossed-order, nonpositive or malformed sqrt-derived state, or
unseedable state fails closed. The stored `cngn_usd_price` classification may
remain legacy `swap_amount_ratio`; it is diagnostic and never controls replay.

The local replay may be recomputed after interruption because it performs no
RPC reads, but a completed binding phase is reused only after all bindings and
the replay-input digest validate.

### 11. Build And Publish

Load staged decoded actions, ownership events, and price bindings using explicit
canonical ordering. Build ledger rows and render CSV bytes with the existing
serializer. Build a versioned coverage sidecar that separately binds:

- complete action-witness count, digest, and action transaction hashes;
- frozen token-ID count, digest, and canonical IDs;
- complete unfiltered transfer-log count and aggregate chunk digest;
- retained relevant-transfer witness count and digest;
- final eligible transaction count, digest, and hashes;
- exact frozen replay-input digest, byte length, row count, and range; and
- ledger bytes, range, chain ID, and endpoint snapshots.

Only verified full-RPC sidecars use schema v2. Fixture and explicit-candidate
sidecars remain separately typed unverified evidence. A v1 sidecar can never be
loaded or projected as `rpc_verified`.

The final eligible hash list never implies that unrelated transfer transactions
were fetched. The raw transfer-scan attestation and retained relevant subset are
distinct fields. Sidecar replay identity, including digest, range, endpoint
timestamps, parser version, parser-contract digest, and price-semantics source
digest, must equal the replay evidence recomputed independently by the
downstream analysis. A ledger bound to one replay artifact or parser contract
cannot be analyzed against another.

Immediately before publication, recapture start/end headers and require exact
equality with the stored initial snapshot. Commit `publish_started` plus the
expected ledger and sidecar SHA-256 values to the checkpoint before changing
either final path.

The existing staged validation, fail-closed replacement order, rollback, and
publication lock remain. Its durability helpers additionally `fsync` the output
directory after each final rename, rollback rename, or rollback unlink. A run is
not published merely because both paths exist; the final pair must pass the
normal strict coverage loader after directory synchronization.

Before ordinary publication or terminal reconciliation, compare each existing
final file with the checkpoint's freshly rebuilt expected bytes:

- if neither expected file is visible and both finals are absent or form a
  separately validated prior pair, perform ordinary pair publication;
- if both exact expected files are visible, validate them and reconcile success;
- if `publish_started` is durable and exactly one final equals its expected
  bytes, atomically install the other expected file over an absent or stale
  counterpart, `fsync` the directory, and validate the completed pair; and
- otherwise stop without overwriting ambiguous files.

This handles a reboot between the two final renames, including a new ledger
paired temporarily with the old sidecar, without treating arbitrary file
existence as authority. Retained backups remain recovery evidence until the new
pair validates. If checkpoint reconstruction or endpoint revalidation fails,
automatic final reconciliation is forbidden and manual recovery remains
fail-closed.

Only after the reconciled final pair validates may the database record terminal
publication and progress set `output_published=true`. If the process stops after
pair publication but before that terminal commit, the next invocation rebuilds
the expected bytes, revalidates endpoints and the pair, and safely reconciles
the terminal state without repeating completed RPC work.

## Reorg And Provider-Consistency Policy

The requested end block is explicit and frozen. Start and end block hashes and
timestamps are captured before resumable work, revalidated on every attempt,
and checked again after all RPC reads. A changed endpoint makes the checkpoint
incompatible.

Discovery witnesses retain their supplied block identities. Candidate
transactions and receipts must agree with each other and with their staged
witnesses. The exporter does not merge evidence from different forks, silently
discard a suffix, or rewrite the stored endpoint snapshot. A reorg or provider
identity disagreement stops the run and leaves final outputs unchanged. The
operator may investigate or deliberately begin a fresh run after the chain is
stable.

## Progress Contract

The adjacent progress file uses canonical UTF-8 JSON with a trailing newline and
an exact versioned schema. It contains only stable labels, integers, booleans,
UTC timestamps, and nullable rate estimates. Its top-level fields are:

- `schema_version`;
- `run_id`, `attempt_number`, and nullable `checkpoint_generation` (`null` for
  non-checkpointed candidate-list and fixture modes);
- `status`: `running`, `succeeded`, `failed`, or `interrupted`;
- `pool`, `mode`, requested start/end blocks, and output filename;
- `phase`;
- a fixed mapping of every phase to status, unit, completed, and total;
- last durable block, chunk, transaction, or batch identity when applicable;
- action-candidate transaction, decoded-action, frozen-token,
  full-transfer-log, relevant-transfer witness, relevant-transfer transaction,
  ownership-event, bound-action, and ledger-row counters;
- current phase rate and ETA when defined;
- `started_at`, `resumed_at`, `updated_at`, and `finished_at`;
- `output_published`; and
- a nullable error code from a closed non-secret allowlist.

Raw exception text, RPC URLs, request bodies, response bodies, HTTP headers,
environment variables, API keys, and full command arguments are forbidden.
Persisted, status, and terminal failures use an allowlist-only renderer composed
from pool, phase, and stable error code only; they never interpolate exception
type or `str(exception)`. The general RPC redactor is broadened as defense in depth for
Alchemy-style path credentials, query parameters, URL user information,
authorization and API-key headers, bearer tokens, nested exceptions, and known
configured secret values, but exporter safety does not depend on successfully
scrubbing arbitrary raw exception prose.

Progress writes use a temporary file in the destination directory, file flush
and `fsync`, atomic replacement, and parent-directory `fsync`. They occur
immediately at run start, resume, phase changes, publication, and terminal
states. During work they occur no more frequently than every five seconds and
at least every thirty seconds or twenty-five newly durable units. The SQLite
checkpoint remains authoritative if the JSON update lags. A progress-write
failure stops the current attempt after preserving the already committed
checkpoint unit; it is never silently ignored. The next attempt reconstructs
progress from SQLite before resuming work.

Every durable SQLite transaction increments a monotonic
`checkpoint_generation` in the same commit as its phase state and counters. A
progress snapshot names the exact generation it mirrors. Generation values are
operational consistency markers, not research provenance.

After each durable JSON update, stderr receives one concise line such as:

```text
[lp-ledger][uni-bsc] phase=full_transfer_scan progress=812/4097 chunks rate=0.61/s eta=01:29:45 durable_end=88710202
```

Stdout retains its current terminal success line for script compatibility.

## Status Reader

Add a read-only status script that accepts one or more ledger output paths and
reports, for each:

- pool and requested range;
- active, interrupted, failed, or succeeded state;
- current phase and durable progress;
- action-transaction, frozen-token, relevant-transfer, ownership-event, and row
  counts;
- update age, rate, and ETA;
- whether the checkpoint is locally compatible and resumable pending RPC
  endpoint revalidation; and
- whether a canonical pair was published.

The status reader opens existing progress and checkpoint artifacts read-only. It
never creates missing lock or database files. For a full-RPC run, it derives
phase, durable counters, cursor, publication state, run ID, attempt, and
generation from one short SQLite snapshot transaction. It uses progress JSON
only for cached rate, ETA, and heartbeat fields, and only when run ID, attempt,
and generation exactly match that database snapshot.

Candidate-list and fixture runs have no checkpoint database, so their status is
explicitly `non_resumable` and comes from progress JSON plus the run-lock state.
Missing or malformed progress for those modes yields unknown status rather than
an inferred durable cursor.

Missing, malformed, wrong-run, or stale-generation progress JSON is reported as
`progress_unavailable`; it never makes a valid checkpoint non-resumable or
overrides durable database facts. If the run lock is held, the run is active. If
the database phase says running but no process holds the run lock, the reader
reports `interrupted` regardless of heartbeat age. This avoids falsely calling
a long RPC request dead merely because the progress timestamp is old.

The default human output is a compact table suitable for checking Base and BSC
together. A JSON option emits the same normalized facts for automation. Any
corrupt or locally incompatible checkpoint is reported as blocked, not
summarized from best-effort partial parsing. A progress-only problem degrades
rate and ETA reporting but not checkpoint status. The reader never calls RPC,
so it cannot label a checkpoint fully compatible with the current chain; the
exporter performs that final endpoint check before resuming.

## CLI Contract

The existing exporter gains `--fresh` to abandon a full-RPC checkpoint
explicitly. Checkpoint, progress, and run-lock paths are derived from `--out`
and are not independently configurable in the initial implementation. This
keeps one unambiguous lock and state namespace per canonical output.

Full-RPC mode creates or automatically resumes its checkpoint without a
`--resume` flag. A compatible terminal checkpoint validates or reconstructs the
published pair rather than repeating RPC work.

Candidate-list and fixture modes emit progress and use the run lock, but they do
not use the full-RPC checkpoint schema. Supplying `--fresh` with those modes is
rejected so an operational flag cannot imply verified coverage.

## Failure And Recovery Matrix

| Failure boundary | Durable result | Next invocation |
| --- | --- | --- |
| Before a unit transaction | Prior committed state only | Repeats the incomplete unit |
| After a unit transaction, before progress JSON | Database includes the unit | Reconciles progress from SQLite and skips the unit |
| Ordinary RPC or decode exception | Checkpoint retained; progress records a stable error code | Revalidates identity and resumes the current phase |
| SIGINT or SIGTERM | Marks interrupted when cleanup can run | Resumes automatically |
| Host reboot or SIGKILL | Lock releases; progress may still say running | Status reports interrupted; exporter resumes from SQLite |
| Checkpoint corruption or schema drift | Final pair untouched | Fails and requires investigation or explicit `--fresh` |
| Endpoint or chain mismatch | Final pair untouched | Fails as incompatible; no automatic rewind |
| Publication failure with successful rollback | Prior valid pair or no new pair | Rebuilds and retries publication |
| Partial or mixed final pair after uncatchable interruption | Expected hash persisted; reader still fails closed | Exporter rebuilds and completes only an exact checkpoint-matching pair; otherwise operator recovery is required |
| Pair published before terminal checkpoint | Valid pair plus nonterminal staging state | Rebuilds, validates exact bytes and endpoints, and reconciles success |

Normal failures never delete staging evidence automatically. Progress error
codes remain non-secret; detailed stderr messages remain redacted.

## Code Organization

Keep the export script as orchestration rather than embedding SQLite mechanics
into its RPC and decoding functions:

- `research/backtester/lp_ledger_checkpoint.py` owns typed run identity,
  checkpoint schema, phase transitions, exact serialization, integrity checks,
  resume queries, and progress snapshots.
- `research/scripts/export_v4_lp_ledger.py` owns phase orchestration and adapts
  action discovery/decode, transfer attestation/filtering, replay-artifact
  binding, build, and publication behavior to staged inputs.
- `research/scripts/report_lp_ledger_export_status.py` is a thin read-only status
  CLI.
- `research/tests/test_lp_ledger_checkpoint.py` pins storage, resume, corruption,
  progress, and status contracts.
- `research/tests/test_v4_lp_ledger.py` pins exporter integration and byte-equal
  crash/resume behavior.
- `dashboard/docs/lp/pool-history-operations.md` documents default paths,
  commands, status interpretation, recovery, and the distinction between
  checkpoints and verified coverage.

Do not introduce a general-purpose progress abstraction until another concrete
research workflow proves the same contract is reusable.

## Test Strategy

Implementation is test-first. Required cases include:

1. New checkpoint creation with exact identity and restrictive schema.
2. Full-RPC rejection of a non-inception start block, automatic compatible
   resume, and explicit fresh-start behavior.
3. Rejection of schema/version/source/config/range/output/chain/endpoint drift.
4. SQLite corruption, unknown schema objects, gaps, overlaps, duplicate
   witnesses, and invalid cursors.
5. Action-discovery crash injection before and after atomic chunk commits,
   including quiet chunks and complete topics/data identity.
6. Action-bundle crash injection, hash/location/block mismatch, witness
   reconciliation, and resume skipping completed bundles.
7. Chronological action continuation, token-state persistence, historical
   resolution caching, exact action-witness reconciliation, multi-mint mapping,
   and the wrapped Base burn pattern.
8. Token-set freeze exactness, canonical order/digest, incomplete-action
   rejection, and resume-time recomputation.
9. Full transfer-scan crash injection, quiet chunks, unfiltered count/digest,
   irrelevant-transfer exclusion from candidates, relevant-token retention,
   and pre-action/post-action ownership coverage.
10. Relevant transfer-only bundle fetch/decode, action-bundle reuse, exact log
    ordering, and frozen-token enforcement.
11. Replay-artifact digest/range/price-model validation, same-block action/swap
    ordering, and exactly one price binding per action without price-log RPC
    reads.
12. Clean uninterrupted and crash/resumed exports producing byte-identical CSV
   and coverage sidecars under identical RPC fixtures.
13. Run-lock contention before any checkpoint or progress mutation.
14. WAL recovery with live `-wal`/`-shm` siblings, bounded terminal-checkpoint
    contention, and lock-protected fresh cleanup of the complete namespace.
15. Atomic progress replacement, controlled-clock rate/ETA behavior,
    generation-matched SQLite reconciliation, and missing, malformed,
    wrong-run, or stale-generation progress files.
16. Stale-running status becoming interrupted when the lock is free, while a
    held lock remains active even during an old heartbeat.
17. Injected path, query, URL-userinfo, bearer, authorization-header, API-key
    header, environment-style, nested-exception, and configured-value secrets
    never appearing in database rows, raw database/WAL bytes, progress JSON,
    status output, or stderr.
18. Crash injection after each final rename, directory synchronization, partial
    and mixed-pair exact reconciliation, ambiguous-file rejection, and a final
    pair published before the terminal checkpoint reconciling without network
    rescans.
19. Existing publication rollback, coverage validation, fixture, and
    candidate-list tests continuing to pass.

Focused tests use deterministic fake RPC responses and controlled crash points;
they do not depend on live Alchemy access. After the complete local suite and
pre-commit review pass, run a bounded real-RPC smoke range for each chain before
resuming the full frozen exports.

## Operational Sequence

After implementation and verification:

1. Pin and report exact SHA-256 digests for the two frozen replay inputs.
2. Run bounded Base and BSC smoke exports into temporary ignored outputs.
3. Confirm their progress, interruption, automatic resume, final coverage, and
   credential-redaction behavior.
4. Start the full Base and BSC exports with their frozen ranges and canonical
   output paths.
5. Use the status reader to report both runs without relying on terminal
   scrollback.
6. After both canonical pairs validate, reconcile every target action against
   the prior precoverage rows and explain any added or changed row before
   downstream analysis.
7. Run LP attribution QA and the frozen
   cross-pool analysis.
8. Continue into the portfolio-of-positions evaluation only from the validated
   frozen statistical artifacts.

The final public evidence remains the validated ledger/coverage pair and later
research manifest. SQLite and progress files remain ignored operational state.
