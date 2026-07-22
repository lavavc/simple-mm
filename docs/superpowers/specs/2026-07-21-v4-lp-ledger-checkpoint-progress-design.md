# V4 LP Ledger Checkpoint And Progress Design

**Date:** 2026-07-21

**Status:** Approved design, pending implementation

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
closed, but all completed RPC work was lost. The replacement must preserve the
existing evidence contract while making expensive internal work resumable and
observable.

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

## Non-Goals

- A partial checkpoint is not an LP ledger, coverage sidecar, frozen result, or
  publication artifact.
- This change does not alter the July 15 statistical specification, candidate
  discovery sources, LP-decoding semantics, event-time price methodology,
  portfolio rules, or article evidence policy.
- This change does not create a generic workflow or checkpoint framework for
  unrelated research scripts.
- Candidate-list and fixture exports do not become verified or resumable merely
  because they emit progress.
- The initial implementation does not add new RPC request fan-out or change the
  provider retry policy. Performance changes require separate evidence.

## Existing Evidence Boundary

The July 15 design requires each verified ledger to bind exact CSV bytes to a
full inclusive scan from pool inception through the requested end block. Full
discovery is the union of target-pool PoolManager `ModifyLiquidity` logs and
PositionManager ERC-721 `Transfer` logs. Transactions and receipts must agree on
identity and location, endpoint headers must match before and after all reads,
and the final CSV and coverage sidecar must publish as one validated pair.

Those rules remain authoritative. Checkpointing operates strictly before the
final pair-publication boundary. A completed checkpoint can reconstruct a
proposed pair, but only successful coverage construction, staged validation,
and final pair publication create canonical evidence.

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
- pool ID, PoolManager, PositionManager, StateView, token addresses, token
  decimals, fee rate, and price orientation;
- inclusive requested start and end blocks;
- configured block chunk size and discovery topics;
- normalized absolute output path; and
- initial start/end block hashes and timestamps.

RPC URLs, API keys, HTTP headers, command-line strings, exception messages, and
response diagnostics are forbidden from the database and fingerprint payload.

On a new run, the exporter validates arguments and chain identity, captures the
endpoint snapshot, creates the exact schema transactionally, and records the
fingerprint before discovery begins.

On resume, it opens the database without migration, rejects a symlinked file,
runs SQLite integrity and exact-schema checks, recomputes all local identity
fields, re-reads the chain ID and endpoint snapshot, and requires exact
fingerprint equality. Unknown tables, columns, indexes, triggers, schema
versions, gaps, overlaps, or inconsistent phase cursors fail closed. There is no
automatic migration, rewind, or partial salvage.

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
| Discovery chunks | The complete fixed block partition and proof that both discovery queries completed for each interval |
| Discovery witnesses | Source stream, block identity, transaction hash/index, log index, address, and topic for each discovered log |
| Candidate bundles | Canonically normalized transaction and receipt payloads, location facts, and payload digests |
| Block headers | Deduplicated canonical block hash and timestamp reads used during decode |
| Position resolutions | Historical token-position lookups, including explicit no-position results, bound to their query block |
| Decoded state and rows | Chronological decoder cursor, current token state, decoded actions, and ownership events committed together |
| Replay chunks and events | Complete fixed block partitions plus normalized Initialize/Swap evidence bound to event-block hashes |
| Replay seed | The explicit no-seed decision or the prior-block StateView snapshot and its canonical block identity |
| Action price bindings | Deterministic event-time state attached to each decoded action |

Exact SQL belongs in the implementation plan, but the schema must use primary
keys and uniqueness constraints that make duplicate candidates, witnesses,
actions, ownership events, and price bindings impossible to accept silently.

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
  -> candidate_discovery
  -> candidate_fetch
  -> chronological_decode
  -> price_event_scan
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
inception block. Then create the Web3 client, require the expected chain ID, and
capture the exact requested start/end headers. Create or validate the checkpoint
before any resumable scan. Candidate-list and fixture modes remain explicitly
unverified and keep their separate requested-range behavior.

### 2. Candidate Discovery

Partition the inclusive range using the existing configured chunk size. For
each incomplete chunk:

1. fetch target-pool `ModifyLiquidity` logs;
2. fetch PositionManager `Transfer` logs;
3. validate required log identity and range fields;
4. normalize and sort witnesses; and
5. insert both witness groups and the completed chunk row in one database
   transaction.

A crash after one RPC response but before the transaction repeats both queries.
No chunk is complete unless both sources completed. An empty chunk is recorded
as positive evidence that both scans covered a quiet interval. The final
candidate set is the canonical union of transaction hashes reconstructed from
all completed witnesses; it is never trusted from a scalar last-block marker.

### 3. Candidate Fetch

For every canonical candidate not already staged, fetch its transaction and
receipt. Normalize Web3 boundary types into strict JSON-compatible values and
require:

- requested, transaction, and receipt hashes to agree;
- transaction and receipt block numbers and transaction indexes to agree;
- transaction and receipt block hashes to be present and agree;
- the location to fall within the requested range; and
- every discovery witness for that candidate to be represented by its receipt,
  including the same block hash and log index.

Persist validated bundles in bounded transactions. Resume skips only bundles
whose normalized payload and digest pass database constraints. Final decode
order never depends on fetch or commit order.

### 4. Chronological Decode

Decode the complete staged candidate set in canonical
`(block_number, transaction_index, transaction_hash)` order. Block headers and
historical position resolutions are cached transactionally so a restart does
not repeat successful network reads.

Each bounded decode commit includes:

- decoded actions;
- ownership events;
- token-position state changes;
- historical resolution results consumed by the batch; and
- the exact chronological cursor.

If a process stops before commit, that bounded batch is replayed from the last
committed state. Required target-pool action reconciliation remains unchanged:
every discovered target-pool `ModifyLiquidity` witness must map to a represented
decoded action or the export fails.

### 5. Price-Event Scan

Scan Initialize and Swap evidence over the same requested inclusive range in
fixed chunks. A replay chunk commits only after both topic reads succeed and all
events are normalized with their block hashes. Quiet chunks are durable. Every
event block is checked against a canonical header record, and one block number
cannot acquire two hashes anywhere in the checkpoint. Network evidence is
therefore reusable even if local replay later stops.

### 6. Price Replay

Before replay, determine the earliest staged event block. Persist either the
explicit rule-driven no-seed result at pool inception or the StateView
`sqrt_price_x96` and tick snapshot from the immediately preceding block,
including that block's number and canonical hash. The seed read and its header
commit atomically and are never repeated or replaced during a compatible
resume.

Reconstruct event-time pool state from that staged seed, staged
Initialize/Swap events, and decoded actions using the existing canonical
`(block_number, log_index, event_order)` semantics. Persist one unique price
binding for every decoded action. Missing, duplicate, mixed-block-hash,
crossed-order, or unseedable state fails closed.

The implementation may recompute an incomplete local replay from staged events;
it must not repeat completed RPC scans. Any future incremental replay
optimization must prove byte equality with the full deterministic replay.

### 7. Build And Publish

Load staged decoded actions, ownership events, and price bindings using explicit
canonical ordering. Build ledger rows and render CSV bytes with the existing
serializer. Reconstruct the full canonical candidate set from discovery
witnesses and build the normal coverage sidecar.

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
- action, ownership, candidate, replay-event, and ledger-row counters;
- current phase rate and ETA when defined;
- `started_at`, `resumed_at`, `updated_at`, and `finished_at`;
- `output_published`; and
- a nullable error code from a closed non-secret allowlist.

Raw exception text, RPC URLs, request bodies, response bodies, HTTP headers,
environment variables, API keys, and full command arguments are forbidden.
Persisted, status, and terminal failures use an allowlist-only renderer composed
from pool, phase, stable error code, and exception class; they never interpolate
`str(exception)`. The general RPC redactor is broadened as defense in depth for
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
[lp-ledger][uni-bsc] phase=candidate_discovery progress=812/4097 chunks rate=0.61/s eta=01:29:45 durable_end=88710202
```

Stdout retains its current terminal success line for script compatibility.

## Status Reader

Add a read-only status script that accepts one or more ledger output paths and
reports, for each:

- pool and requested range;
- active, interrupted, failed, or succeeded state;
- current phase and durable progress;
- candidate and row counts;
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
  existing discovery, decode, replay, build, and publication behavior to staged
  inputs.
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
5. Discovery crash injection before and after the atomic two-source chunk
   commit, including quiet chunks.
6. Candidate-bundle crash injection, hash/location/block mismatch, witness
   reconciliation, and resume skipping completed bundles.
7. Chronological decoder continuation, token-state persistence, historical
   resolution caching, and required action reconciliation.
8. Replay scan continuation, quiet chunks, mixed-fork rejection, durable seed
   reuse, canonical ordering, and exactly one price binding per action.
9. Clean uninterrupted and crash/resumed exports producing byte-identical CSV
   and coverage sidecars under identical RPC fixtures.
10. Run-lock contention before any checkpoint or progress mutation.
11. WAL recovery with live `-wal`/`-shm` siblings, bounded terminal-checkpoint
    contention, and lock-protected fresh cleanup of the complete namespace.
12. Atomic progress replacement, controlled-clock rate/ETA behavior,
    generation-matched SQLite reconciliation, and missing, malformed,
    wrong-run, or stale-generation progress files.
13. Stale-running status becoming interrupted when the lock is free, while a
    held lock remains active even during an old heartbeat.
14. Injected path, query, URL-userinfo, bearer, authorization-header, API-key
    header, environment-style, nested-exception, and configured-value secrets
    never appearing in database rows, raw database/WAL bytes, progress JSON,
    status output, or stderr.
15. Crash injection after each final rename, directory synchronization, partial
    and mixed-pair exact reconciliation, ambiguous-file rejection, and a final
    pair published before the terminal checkpoint reconciling without network
    rescans.
16. Existing publication rollback, coverage validation, fixture, and
    candidate-list tests continuing to pass.

Focused tests use deterministic fake RPC responses and controlled crash points;
they do not depend on live Alchemy access. After the complete local suite and
pre-commit review pass, run a bounded real-RPC smoke range for each chain before
resuming the full frozen exports.

## Operational Sequence

After implementation and verification:

1. Run bounded Base and BSC smoke exports into temporary ignored outputs.
2. Confirm their progress, interruption, automatic resume, final coverage, and
   credential-redaction behavior.
3. Start the full Base and BSC exports with their frozen ranges and canonical
   output paths.
4. Use the status reader to report both runs without relying on terminal
   scrollback.
5. After both canonical pairs validate, run LP attribution QA and the frozen
   cross-pool analysis.
6. Continue into the portfolio-of-positions evaluation only from the validated
   frozen statistical artifacts.

The final public evidence remains the validated ledger/coverage pair and later
research manifest. SQLite and progress files remain ignored operational state.
