# V4 LP Ledger Checkpoint And Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the full-RPC Base and BSC V4 LP-ledger exports automatically
resumable and durably observable without weakening their verified coverage or
atomic publication contracts.

**Architecture:** A private `LPLedgerCheckpoint` SQLite boundary owns exact run
identity, source-specific staged evidence, progress generations, and read-only
status snapshots. The exporter first discovers, fetches, and reconciles every
target-pool action; freezes the resulting token-ID set; attests the complete
PositionManager Transfer scan while fetching only relevant ownership bundles;
then binds the existing frozen replay artifact and publishes a richer verified
coverage pair. Operational progress is a small generation-matched JSON mirror;
it is never research evidence.

**Tech Stack:** Python 3.12, standard-library `sqlite3`, `fcntl`, canonical
JSON/CSV, Web3.py, frozen dataclasses, pytest, Ruff, mypy, and existing
research-only RPC helpers.

## Global Constraints

- Only the primary Sol Ultra agent may edit, stage, or commit. Terra Extra High
  agents are read-only reviewers.
- Every production behavior begins with a focused failing test whose expected
  failure is observed before implementation.
- Full-RPC mode requires `start_block == config.default_start_block`; Base is
  `42_926_879` and BSC is `84_655_203`.
- Candidate-list and fixture modes remain unverified and non-checkpointed.
- Checkpoint, progress, and lock paths derive only from `--out`; no independent
  path overrides are added.
- SQLite uses WAL, `synchronous=FULL`, foreign keys, trusted-schema off,
  no-follow path handling, restrictive permissions, and exact schema checks.
- SQLite `INTEGER` stores only bounded signed-64-bit values. EVM integers and
  Decimal accounting values use validated canonical text; SQLite `REAL` is
  forbidden.
- The checkpoint binds exact pool/config/range/output/endpoint/runtime/source
  identity and never stores an RPC URL, credential, header, raw exception, or
  command line.
- Target-action discovery and full-transfer attestation use independent fixed
  range partitions; quiet ranges are durable evidence in each stream.
- Every target-pool action is fetched and reconciled before the canonical token
  set freezes. Existing precoverage rows never define completeness.
- The complete PositionManager Transfer scan records an unfiltered count and
  digest per chunk, but only frozen-token transfers become fetch candidates.
- Action and relevant-transfer decode remain canonical by
  `(block_number, transaction_index, transaction_hash)` and event replay remains
  canonical by `(block_number, log_index, event_order)`.
- Existing Base/BSC replay CSVs are reused only after exact byte-digest, range,
  order, pool, and `sqrt_mid` validation; the ledger exporter does not rescan
  Initialize/Swap logs.
- Base replay is exactly SHA-256
  `41c3d5b945abdffde32087590c21115914404f496069519c572e393b667f99b9`,
  639921 bytes, 1596 data rows, blocks 42926879 through 47514853. BSC replay is
  exactly SHA-256
  `bf99f9a17ea2ff0048da7c7eec4fa0c8fa3b9e7e5586e6200919b66edaccd1e2`,
  1330201 bytes, 3119 data rows, blocks 84655203 through 105135905.
- Start/end headers are revalidated on every resume and before publication.
  Conflicting block identities fail closed.
- No partial checkpoint is an `rpc_verified` ledger. Only the validated final
  CSV/coverage pair is canonical evidence.
- Preserve existing final-pair replacement order and rollback behavior, add
  parent-directory fsync, and reconcile only checkpoint-exact expected bytes.
- Do not remove or weaken `.gitignore`; leave `.firecrawl/`,
  `quidax_cngn_usdt.json`, and `uv.lock` untouched.
- Update `dashboard/docs/lp/pool-history-operations.md` with behavior changes in
  the same implementation series.

---

## File Structure

- Create `research/backtester/lp_ledger_checkpoint.py`: typed checkpoint paths,
  identity, SQLite schema/store, run locks, staged evidence, progress snapshots,
  and read-only status facts.
- Create `research/scripts/report_lp_ledger_export_status.py`: thin human/JSON
  status CLI over read-only checkpoint snapshots.
- Create `research/tests/test_lp_ledger_checkpoint.py`: storage, locking,
  progress, status, and crash-boundary contracts.
- Modify `research/scripts/export_v4_lp_ledger.py`: phase adapters, checkpointed
  full-RPC orchestration, safe terminal errors, and publication reconciliation.
- Modify `research/tests/test_v4_lp_ledger.py`: staged phase integration,
  byte-equal resume, publication-crash, and CLI tests.
- Modify `engine/web3_utils.py`: defense-in-depth credential scrubber expansion.
- Modify `tests/test_web3_boundaries.py`: path/query/userinfo/header/bearer/nested
  and configured-secret redaction tests.
- Modify `research/backtester/lp_ledger_attribution.py`: versioned action,
  transfer, token-set, replay-input, and selected-bundle coverage evidence.
- Modify `research/cross_pool/manifest.py` and
  `research/cross_pool/article_manifest.schema.json`: preserve the richer
  coverage proof in final research evidence.
- Modify `research/tests/test_cross_pool_market_structure.py` and
  `research/tests/test_cross_pool_reporting.py`: sidecar-v2 and manifest gates.
- Modify `dashboard/docs/lp/pool-history-operations.md`: paths, status commands,
  automatic resume, fresh start, failure recovery, and evidentiary boundaries.

## Binding 2026-07-22 Amendment

Tasks 1 and 2 were implemented against checkpoint schema v1 before the live
transfer benchmark exposed the all-PositionManager candidate error. Their path,
locking, SQLite durability, canonical serialization, block-identity, and
transactional foundations remain valid. Their old phase list, two-source shared
discovery partition, flat witness shape, candidate union, combined decode
cursor, replay RPC tables, and `BuildInputs.candidate_hashes` contract are
historical implementation evidence only and are superseded by Task 4 below.

Task 3 retains atomic progress, status, lock, and secret-safety behavior but must
be committed only after its phase list and counters reflect schema v2. Tasks 4
through 9 below replace the original Tasks 4 through 8 in full. Checkpoint and
progress schema versions both increment; old operational files fail closed and
require explicit `--fresh`. No canonical ledger pair was produced by schema v1.

---

### Task 1 (Completed Under Schema V1; Corrected By Task 4): Add Checkpoint Identity, Paths, Run Lock, And Frozen SQLite Schema

The exact phase list and SQL below record the already committed v1
implementation. They are not current requirements after the binding amendment;
Task 4 replaces them with schema v2 while preserving the valid path, lock,
durability, and canonical-codec behavior.

**Files:**
- Create: `research/backtester/lp_ledger_checkpoint.py`
- Create: `research/tests/test_lp_ledger_checkpoint.py`

**Interfaces:**
- Consumes: output `Path`, frozen pool/config facts, source-file digests,
  Python/Web3 versions, and exact endpoint headers.
- Produces: `CheckpointPaths`, `BlockHeader`, `EndpointSnapshot`, `RunIdentity`,
  `CheckpointSnapshot`, `RunLock`, and `LPLedgerCheckpoint.create_or_resume`.

- [ ] **Step 1: Write the failing checkpoint-foundation tests**

Add tests with these exact public calls:

```python
def test_checkpoint_paths_are_output_derived(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    assert paths.database == tmp_path / "ledger.csv.checkpoint.sqlite3"
    assert paths.progress == tmp_path / "ledger.csv.progress.json"
    assert paths.run_lock == tmp_path / "ledger.csv.run.lock"
    assert paths.publish_lock == tmp_path / "ledger.csv.publish.lock"


def test_compatible_resume_increments_attempt_and_generation(tmp_path: Path) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    identity = _identity(paths.output)
    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            first = run.snapshot()
        with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=False) as run:
            resumed = run.snapshot()
    assert resumed.attempt_number == first.attempt_number + 1
    assert resumed.generation == first.generation + 1


@pytest.mark.parametrize("mutation", ("range", "endpoint", "source", "output"))
def test_incompatible_resume_fails_without_mutating_state(
    tmp_path: Path, mutation: str
) -> None:
    paths = CheckpointPaths.from_output(tmp_path / "ledger.csv")
    original = _identity(paths.output)
    with acquire_run_lock(paths):
        with LPLedgerCheckpoint.create_or_resume(paths, original, fresh=False) as run:
            before = run.snapshot()
        with pytest.raises(CheckpointContractError, match="identity"):
            LPLedgerCheckpoint.create_or_resume(
                paths, _mutated_identity(original, mutation), fresh=False
            )
        with LPLedgerCheckpoint.open_status(paths) as run:
            assert run.snapshot() == before
```

Also pin: `.csv` validation; canonical identity JSON and SHA-256; injected secret
absence; file mode `0o600`; WAL/foreign-key/full-sync/trusted-schema pragmas;
exact tables/columns/indexes with no triggers/views; integrity/user-version
rejection; lock contention before DB/progress mutation; no-follow rejection for
every existing parent component and operational/final leaf; read-only status
not creating absent files; and
`fresh=True` removing only database/WAL/SHM/progress while preserving an existing
ledger/coverage pair.

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

Run:

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py
```

Expected: collection fails with
`ModuleNotFoundError: No module named 'research.backtester.lp_ledger_checkpoint'`.

- [ ] **Step 3: Implement the typed identity, paths, locks, and exact schema**

Use these public shapes and operations:

```text
Phase = preflight | candidate_discovery | candidate_fetch |
        chronological_decode | price_event_scan | price_replay | build |
        publish | succeeded
RunStatus = running | succeeded | failed | interrupted
ExportStatusState = active | interrupted | failed | succeeded | blocked |
                    non_resumable | unknown

CheckpointPaths:
  output, database, wal, shm, progress, run_lock, publish_lock: Path
  from_output(output: Path) -> CheckpointPaths

BlockHeader:
  block_number: int
  block_hash: str
  timestamp_ms: int

EndpointSnapshot:
  start, end: BlockHeader

RunIdentity:
  schema_version: int
  exporter_version: str
  verification_mode: Literal["rpc_verified"]
  pool, chain: str
  chain_id: int
  pool_id, pool_manager, position_manager, state_view: str
  token0_address, token1_address: str
  token0_decimals, token1_decimals: int
  fee_rate: str
  invert_price: bool
  start_block, end_block, chunk_size: int
  discovery_topics: tuple[str, ...]
  output_path: str
  endpoint: EndpointSnapshot
  python_version, web3_version: str
  source_sha256: tuple[tuple[str, str], ...]
  canonical_bytes() -> bytes
  sha256() -> str
```

`run_id` is generated once when a new checkpoint is created and lives in run
state; it is not part of the deterministic compatibility identity. `RunIdentity`
must exclude URL/error/headers by construction. Source digests
include `lp_ledger_checkpoint.py`, `export_v4_lp_ledger.py`, `v4_export.py`,
`v4_lp_ledger.py`, `v4_event_replay.py`, and `lp_ledger_attribution.py`.
`CheckpointPaths.from_output` computes one lexically normalized absolute path
with `os.path.abspath`; `RunIdentity.output_path` is that exact value, so
relative aliases cannot form different identities. Before any read, create,
replace, or unlink, a shared validator walks every existing component from the
filesystem root with `lstat`, rejects symlinks, and rechecks the derived leaf
immediately before the operation. Lock and database leaf creation additionally
use `os.open` with `O_NOFOLLOW | O_CLOEXEC` and mode `0o600`.

Create the complete schema at version 1 in one transaction. It must contain:

```text
schema_meta, run_identity, run_state, phase_state, publication_state,
chain_blocks, discovery_chunks, discovery_witnesses, candidate_bundles,
position_resolutions, decoded_transactions, decoded_actions,
ownership_events, decoder_token_state, replay_chunks, replay_events,
replay_seed, action_price_bindings
```

The schema contract is `PRAGMA application_id = 1280330819` (`0x4c504c43`,
ASCII `LPLC`) and `PRAGMA user_version = 1`. Every table is `STRICT`; no table
may contain a `REAL`, `BLOB`, or dynamically typed column. Version 1 is exactly:

```sql
CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at_utc TEXT NOT NULL CHECK (length(created_at_utc) > 0)
) STRICT;

CREATE TABLE run_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    fingerprint_sha256 TEXT NOT NULL UNIQUE CHECK (length(fingerprint_sha256) = 64),
    canonical_json TEXT NOT NULL CHECK (length(canonical_json) > 0),
    FOREIGN KEY (singleton) REFERENCES schema_meta(singleton)
) STRICT;

CREATE TABLE run_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    run_id TEXT NOT NULL UNIQUE CHECK (length(run_id) = 36),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','interrupted')),
    phase TEXT NOT NULL CHECK (phase IN (
        'preflight','candidate_discovery','candidate_fetch',
        'chronological_decode','price_event_scan','price_replay',
        'build','publish','succeeded'
    )),
    started_at_utc TEXT NOT NULL CHECK (length(started_at_utc) > 0),
    resumed_at_utc TEXT NOT NULL CHECK (length(resumed_at_utc) > 0),
    updated_at_utc TEXT NOT NULL CHECK (length(updated_at_utc) > 0),
    finished_at_utc TEXT,
    error_code TEXT CHECK (error_code IS NULL OR error_code IN (
        'rpc_error','checkpoint_incompatible','checkpoint_corrupt',
        'decode_error','replay_error','publication_error',
        'checkpoint_maintenance_error','interrupted','unknown_error'
    )),
    output_published INTEGER NOT NULL DEFAULT 0 CHECK (output_published IN (0,1)),
    FOREIGN KEY (singleton) REFERENCES schema_meta(singleton)
) STRICT;

CREATE TABLE phase_state (
    phase TEXT PRIMARY KEY CHECK (phase IN (
        'preflight','candidate_discovery','candidate_fetch',
        'chronological_decode','price_event_scan','price_replay',
        'build','publish','succeeded'
    )),
    status TEXT NOT NULL CHECK (status IN ('pending','running','completed')),
    unit TEXT NOT NULL CHECK (length(unit) > 0),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed >= 0),
    total INTEGER CHECK (total IS NULL OR total >= completed),
    last_durable_json TEXT,
    updated_generation INTEGER NOT NULL CHECK (updated_generation >= 1)
) STRICT;

CREATE TABLE publication_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state TEXT NOT NULL CHECK (state IN ('not_started','started','published')),
    expected_ledger_sha256 TEXT CHECK (
        expected_ledger_sha256 IS NULL OR length(expected_ledger_sha256) = 64
    ),
    expected_sidecar_sha256 TEXT CHECK (
        expected_sidecar_sha256 IS NULL OR length(expected_sidecar_sha256) = 64
    ),
    started_generation INTEGER CHECK (started_generation IS NULL OR started_generation >= 1),
    published_generation INTEGER CHECK (published_generation IS NULL OR published_generation >= 1),
    CHECK (
        (state = 'not_started' AND expected_ledger_sha256 IS NULL
            AND expected_sidecar_sha256 IS NULL AND started_generation IS NULL
            AND published_generation IS NULL)
        OR
        (state = 'started' AND expected_ledger_sha256 IS NOT NULL
            AND expected_sidecar_sha256 IS NOT NULL AND started_generation IS NOT NULL
            AND published_generation IS NULL)
        OR
        (state = 'published' AND expected_ledger_sha256 IS NOT NULL
            AND expected_sidecar_sha256 IS NOT NULL AND started_generation IS NOT NULL
            AND published_generation IS NOT NULL)
    ),
    FOREIGN KEY (singleton) REFERENCES schema_meta(singleton)
) STRICT;

CREATE TABLE chain_blocks (
    block_number INTEGER PRIMARY KEY CHECK (block_number >= 0),
    block_hash TEXT NOT NULL UNIQUE CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    timestamp_ms INTEGER CHECK (timestamp_ms IS NULL OR timestamp_ms >= 0),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    UNIQUE (block_number, block_hash)
) STRICT;

CREATE TABLE discovery_chunks (
    chunk_index INTEGER PRIMARY KEY CHECK (chunk_index >= 0),
    start_block INTEGER NOT NULL UNIQUE CHECK (start_block >= 0),
    end_block INTEGER NOT NULL UNIQUE CHECK (end_block >= start_block),
    status TEXT NOT NULL CHECK (status IN ('pending','completed')),
    modify_witness_count INTEGER NOT NULL DEFAULT 0 CHECK (modify_witness_count >= 0),
    transfer_witness_count INTEGER NOT NULL DEFAULT 0 CHECK (transfer_witness_count >= 0),
    completed_generation INTEGER CHECK (completed_generation IS NULL OR completed_generation >= 1),
    CHECK (
        (status = 'pending' AND modify_witness_count = 0
            AND transfer_witness_count = 0 AND completed_generation IS NULL)
        OR (status = 'completed' AND completed_generation IS NOT NULL)
    )
) STRICT;

CREATE TABLE discovery_witnesses (
    source TEXT NOT NULL CHECK (source IN ('pool_modify','position_transfer')),
    transaction_hash TEXT NOT NULL CHECK (
        length(transaction_hash) = 66 AND substr(transaction_hash, 1, 2) = '0x'
    ),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    chunk_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    address TEXT NOT NULL CHECK (length(address) = 42 AND substr(address, 1, 2) = '0x'),
    topic0 TEXT NOT NULL CHECK (length(topic0) = 66 AND substr(topic0, 1, 2) = '0x'),
    topic1 TEXT CHECK (topic1 IS NULL OR (length(topic1) = 66 AND substr(topic1, 1, 2) = '0x')),
    PRIMARY KEY (source, transaction_hash, log_index),
    UNIQUE (source, block_number, log_index),
    FOREIGN KEY (chunk_index) REFERENCES discovery_chunks(chunk_index),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE candidate_bundles (
    transaction_hash TEXT PRIMARY KEY CHECK (
        length(transaction_hash) = 66 AND substr(transaction_hash, 1, 2) = '0x'
    ),
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    transaction_json TEXT NOT NULL CHECK (length(transaction_json) > 0),
    receipt_json TEXT NOT NULL CHECK (length(receipt_json) > 0),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    UNIQUE (block_number, transaction_index),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE position_resolutions (
    token_id TEXT NOT NULL,
    query_block INTEGER NOT NULL CHECK (query_block >= 0),
    query_block_hash TEXT NOT NULL CHECK (
        length(query_block_hash) = 66 AND substr(query_block_hash, 1, 2) = '0x'
    ),
    found INTEGER NOT NULL CHECK (found IN (0,1)),
    pool_id TEXT,
    tick_lower INTEGER,
    tick_upper INTEGER,
    liquidity_after TEXT,
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    PRIMARY KEY (token_id, query_block),
    CHECK (
        (found = 0 AND pool_id IS NULL AND tick_lower IS NULL
            AND tick_upper IS NULL AND liquidity_after IS NULL)
        OR
        (found = 1 AND pool_id IS NOT NULL AND tick_lower IS NOT NULL
            AND tick_upper IS NOT NULL AND liquidity_after IS NOT NULL)
    ),
    FOREIGN KEY (query_block, query_block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE decoded_transactions (
    transaction_hash TEXT PRIMARY KEY,
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    action_count INTEGER NOT NULL CHECK (action_count >= 0),
    ownership_count INTEGER NOT NULL CHECK (ownership_count >= 0),
    decoder_state_sha256 TEXT NOT NULL CHECK (length(decoder_state_sha256) = 64),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    FOREIGN KEY (transaction_hash) REFERENCES candidate_bundles(transaction_hash),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash),
    UNIQUE (block_number, transaction_index)
) STRICT;

CREATE TABLE decoded_actions (
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_hash TEXT NOT NULL,
    token_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (length(action_type) > 0),
    action_json TEXT NOT NULL CHECK (length(action_json) > 0),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    PRIMARY KEY (block_number, log_index, event_order),
    FOREIGN KEY (transaction_hash) REFERENCES decoded_transactions(transaction_hash),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE ownership_events (
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_hash TEXT NOT NULL,
    token_id TEXT NOT NULL,
    previous_owner TEXT CHECK (
        previous_owner IS NULL OR (length(previous_owner) = 42 AND substr(previous_owner, 1, 2) = '0x')
    ),
    new_owner TEXT CHECK (
        new_owner IS NULL OR (length(new_owner) = 42 AND substr(new_owner, 1, 2) = '0x')
    ),
    PRIMARY KEY (block_number, log_index, event_order),
    FOREIGN KEY (transaction_hash) REFERENCES decoded_transactions(transaction_hash),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE decoder_token_state (
    token_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL,
    tick_lower INTEGER NOT NULL,
    tick_upper INTEGER NOT NULL,
    liquidity_after TEXT NOT NULL,
    last_block_number INTEGER NOT NULL CHECK (last_block_number >= 0),
    last_log_index INTEGER NOT NULL CHECK (last_log_index >= 0),
    last_event_order INTEGER NOT NULL CHECK (last_event_order >= 0),
    updated_generation INTEGER NOT NULL CHECK (updated_generation >= 1)
) STRICT;

CREATE TABLE replay_chunks (
    chunk_index INTEGER PRIMARY KEY CHECK (chunk_index >= 0),
    start_block INTEGER NOT NULL UNIQUE CHECK (start_block >= 0),
    end_block INTEGER NOT NULL UNIQUE CHECK (end_block >= start_block),
    status TEXT NOT NULL CHECK (status IN ('pending','completed')),
    initialize_event_count INTEGER NOT NULL DEFAULT 0 CHECK (initialize_event_count >= 0),
    swap_event_count INTEGER NOT NULL DEFAULT 0 CHECK (swap_event_count >= 0),
    completed_generation INTEGER CHECK (completed_generation IS NULL OR completed_generation >= 1),
    CHECK (
        (status = 'pending' AND initialize_event_count = 0
            AND swap_event_count = 0 AND completed_generation IS NULL)
        OR (status = 'completed' AND completed_generation IS NOT NULL)
    )
) STRICT;

CREATE TABLE replay_events (
    chunk_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    block_hash TEXT NOT NULL CHECK (
        length(block_hash) = 66 AND substr(block_hash, 1, 2) = '0x'
    ),
    transaction_hash TEXT NOT NULL CHECK (
        length(transaction_hash) = 66 AND substr(transaction_hash, 1, 2) = '0x'
    ),
    transaction_index INTEGER NOT NULL CHECK (transaction_index >= 0),
    event_type TEXT NOT NULL CHECK (event_type IN ('initialize','swap')),
    sqrt_price_x96 TEXT NOT NULL,
    tick INTEGER NOT NULL,
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    PRIMARY KEY (block_number, log_index, event_order),
    FOREIGN KEY (chunk_index) REFERENCES replay_chunks(chunk_index),
    FOREIGN KEY (block_number, block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE replay_seed (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    first_event_block INTEGER NOT NULL CHECK (first_event_block >= 0),
    seed_kind TEXT NOT NULL CHECK (seed_kind IN ('none','state')),
    sqrt_price_x96 TEXT,
    tick INTEGER,
    state_source TEXT,
    state_block_number INTEGER,
    state_block_hash TEXT,
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    CHECK (
        (seed_kind = 'none' AND sqrt_price_x96 IS NULL AND tick IS NULL
            AND state_source IS NULL AND state_block_number IS NULL
            AND state_block_hash IS NULL)
        OR
        (seed_kind = 'state' AND sqrt_price_x96 IS NOT NULL AND tick IS NOT NULL
            AND state_source IS NOT NULL AND state_block_number IS NOT NULL
            AND state_block_hash IS NOT NULL)
    ),
    FOREIGN KEY (state_block_number, state_block_hash)
        REFERENCES chain_blocks(block_number, block_hash)
) STRICT;

CREATE TABLE action_price_bindings (
    block_number INTEGER NOT NULL CHECK (block_number >= 0),
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    event_time_sqrt_price_x96 TEXT NOT NULL,
    event_time_tick INTEGER NOT NULL,
    event_time_state_source TEXT NOT NULL CHECK (event_time_state_source IN (
        'self_event','prior_event','prior_block','same_block_prior_event'
    )),
    committed_generation INTEGER NOT NULL CHECK (committed_generation >= 1),
    PRIMARY KEY (block_number, log_index, event_order),
    FOREIGN KEY (block_number, log_index, event_order)
        REFERENCES decoded_actions(block_number, log_index, event_order)
) STRICT;

CREATE INDEX discovery_chunks_status_order
    ON discovery_chunks(status, chunk_index);
CREATE INDEX discovery_witnesses_candidate_order
    ON discovery_witnesses(transaction_hash, block_number, log_index, source);
CREATE INDEX candidate_bundles_decode_order
    ON candidate_bundles(block_number, transaction_index, transaction_hash);
CREATE INDEX decoded_transactions_location_order
    ON decoded_transactions(block_number, transaction_index, transaction_hash);
CREATE INDEX decoded_actions_transaction_order
    ON decoded_actions(transaction_hash, block_number, log_index, event_order);
CREATE INDEX decoded_actions_token_order
    ON decoded_actions(token_id, block_number, log_index, event_order);
CREATE INDEX ownership_events_token_order
    ON ownership_events(token_id, block_number, log_index, event_order);
CREATE INDEX replay_chunks_status_order
    ON replay_chunks(status, chunk_index);
CREATE INDEX replay_events_canonical_order
    ON replay_events(chunk_index, block_number, log_index, event_order);
CREATE INDEX phase_state_status
    ON phase_state(status, phase);
```

All `TEXT` EVM quantities and token IDs use canonical unsigned decimal text;
signed liquidity deltas remain inside canonical action JSON as signed decimal
text. Decimal fields in action JSON use finite fixed-point text without exponent
or negative zero. Hashes, addresses, topics, and SHA-256 values are lowercase.
`transaction_json`, `receipt_json`, and `action_json` use the same canonical
UTF-8 JSON encoder, and every payload digest is verified on read. Discovery and
candidate commits upsert each observed `(block_number, block_hash)` into
`chain_blocks` with a null timestamp; a later verified header may fill that
timestamp exactly once but may never change the hash. Header loads used by
decode/replay require a non-null timestamp. Thus the composite foreign keys
enforce the one-fork rule even before a header RPC has occurred. The exact
schema manifest includes the normalized `sqlite_master.sql`, `PRAGMA
table_info`, foreign-key lists, and index column order shown above; any extra or
missing table, index, trigger, view, or column is incompatible.

All state mutations use `BEGIN IMMEDIATE` and increment `run_state.generation`
in the same commit. Create the database through `os.open` with `O_NOFOLLOW` and
mode `0o600`; acquire `fcntl.LOCK_EX | LOCK_NB` before opening it. Exact-schema
validation compares `sqlite_master`, `PRAGMA table_info`, indexes, application
ID, and user version. Do not migrate or salvage.

- [ ] **Step 4: Run the focused tests and type/import checks**

Run:

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py
python3 -m py_compile research/backtester/lp_ledger_checkpoint.py
```

Expected: all Task 1 tests pass; compilation succeeds.

- [ ] **Step 5: Commit Task 1**

```bash
git add research/backtester/lp_ledger_checkpoint.py research/tests/test_lp_ledger_checkpoint.py
git commit -m "feat: add LP ledger checkpoint foundation"
```

---

### Task 2 (Completed Under Schema V1; Corrected By Task 4): Add Transactional Staged-Evidence APIs

The flat candidate union and combined action/ownership APIs below record the
already committed v1 implementation. Task 4 replaces those interfaces with
source-specific action, transfer-attestation, and ownership phases before any
exporter adapter uses them.

**Files:**
- Modify: `research/backtester/lp_ledger_checkpoint.py`
- Modify: `research/tests/test_lp_ledger_checkpoint.py`

**Interfaces:**
- Consumes: `DiscoveryWitness`, `CandidateBundle`, decoded dataclasses,
  `LedgerPositionState`, `ReplayEvent`, `PoolStateSnapshot`, and bounded phase
  partitions.
- Produces: stage-specific commit/query methods used by exporter adapters.

- [ ] **Step 1: Write failing storage transaction and ordering tests**

Pin these exact tests and assertions:

- `test_discovery_chunk_commits_both_sources_and_quiet_ranges_atomically`
  commits one `BlockRange` with both witness sets, then asserts no incomplete
  discovery ranges remain and `candidate_hashes()` is the sorted expected union.
- `test_candidate_bundles_decode_in_location_order_not_fetch_order` commits
  transaction indexes 7 then 5, then asserts `undecoded_bundles()` returns
  indexes `[5, 7]`.
- `test_large_evm_values_round_trip_without_sqlite_numeric_coercion` persists
  `str(2**160 - 1)` in a replay seed and asserts the exact text is returned.

Also test: quiet chunk completion; precommit exception leaves no witnesses or
cursor; postcommit exception preserves all; duplicate/gapped/overlapping chunks;
one block number with two hashes; missing witness receipt reconciliation;
canonical JSON/digest mismatch; ownership-only candidates recorded in
`decoded_transactions`; explicit no-position resolution caching; decoder-state
insert/update/delete in the same transaction as actions and cursor; two-topic
replay chunks; seed/no-seed exclusivity; duplicate action/replay/binding keys;
one binding per action; and monotonic generation per committed unit.

- [ ] **Step 2: Run the new tests and confirm missing-method failures**

Run:

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py
```

Expected: failures name the first missing staged-evidence method, beginning with
`commit_discovery_chunk`.

- [ ] **Step 3: Implement the staged evidence types and APIs**

Use these immutable boundary types:

```text
BlockRange:
  index, start_block, end_block: int

DiscoveryWitness:
  source: Literal["pool_modify", "position_transfer"]
  block_number, transaction_index, log_index: int
  block_hash, transaction_hash, address, topic0: str
  topic1: str | None

CandidateBundle:
  transaction_hash, block_hash, transaction_json, receipt_json,
  payload_sha256: str
  block_number, transaction_index: int

PositionResolution:
  token_id, query_block: int
  found: bool
  state: LedgerPositionState | None
  invariant: found is exactly equivalent to state is not None

ReplaySeed:
  first_event_block: int
  state: PoolStateSnapshot | None
  state_block_number: int | None
  state_block_hash: str | None

ReplayEvidence:
  block_number, log_index, event_order, transaction_index: int
  block_hash, transaction_hash: str
  event_type: Literal["initialize", "swap"]
  sqrt_price_x96, tick: int

ActionPriceBinding:
  block_number, log_index, event_order: int
  event_time_sqrt_price_x96: int
  event_time_tick: int
  event_time_state_source: Literal[
    "self_event", "prior_event", "prior_block", "same_block_prior_event"
  ]
  identity: exactly (block_number, log_index, event_order), matching one action

PublicationState:
  state: Literal["not_started", "started", "published"]
  expected_ledger_sha256, expected_sidecar_sha256: str | None
  started_generation, published_generation: int | None

BuildInputs:
  actions: tuple[DecodedLiquidityAction, ...]
  ownership_events: tuple[OwnershipEvent, ...]
  replay_events: tuple[ReplayedEvent, ...]
  action_price_bindings: tuple[ActionPriceBinding, ...]
  candidate_hashes: tuple[str, ...]
```

Implement these store methods without RPC knowledge:

```text
incomplete_discovery_chunks() -> tuple[BlockRange, ...]
commit_discovery_chunk(block_range, modify, transfers) -> CheckpointSnapshot
candidate_hashes() -> tuple[str, ...]
required_action_witnesses(tx_hash) -> tuple[DiscoveryWitness, ...]
unfetched_candidate_hashes() -> tuple[str, ...]
commit_candidate_bundle(bundle) -> CheckpointSnapshot
undecoded_bundles() -> tuple[CandidateBundle, ...]
load_header(block_number) -> BlockHeader | None
load_decoder_state() -> dict[int, LedgerPositionState]
load_decoded_actions() -> tuple[DecodedLiquidityAction, ...]
load_ownership_events() -> tuple[OwnershipEvent, ...]
load_position_resolution(token_id, query_block) -> PositionResolution | None
commit_decoded_transaction(bundle, headers, resolutions, state_upserts, state_deletes, actions, owners) -> CheckpointSnapshot
incomplete_replay_chunks() -> tuple[BlockRange, ...]
commit_replay_chunk(block_range, headers, events: Sequence[ReplayEvidence]) -> CheckpointSnapshot
load_replay_events() -> tuple[ReplayEvent, ...]
load_replay_seed() -> ReplaySeed | None
commit_replay_seed(seed, header) -> CheckpointSnapshot
commit_action_price_bindings(bindings) -> CheckpointSnapshot
load_action_price_bindings() -> tuple[ActionPriceBinding, ...]
load_build_inputs() -> BuildInputs
load_publication_state() -> PublicationState
begin_publication(expected_ledger_sha256, expected_sidecar_sha256) -> CheckpointSnapshot
mark_published() -> CheckpointSnapshot
checkpoint_terminal_wal() -> Literal["truncated", "busy"]
```

All collection parameters above are immutable sequences with concrete element
types from this section or the existing ledger/replay modules. Publication hash
parameters are lowercase 64-character SHA-256 hex strings. `mark_published`
requires durable `publish_started` state and is called only after strict final
pair validation; `checkpoint_terminal_wal` never clears that fact. A burned
position appears in `state_deletes` and is removed in the same transaction as
its decoded marker, actions, ownership events, cached headers/resolutions,
cursor, counters, and generation increment.

Serialize EVM integers with regex-validated canonical decimal text and Decimal
values as finite fixed-point text without exponent or negative zero. Store
normalized hashes/addresses/topics as lowercase hex. Never insert a float.

- [ ] **Step 4: Run focused storage tests**

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py
python3 -m py_compile research/backtester/lp_ledger_checkpoint.py
```

Expected: all checkpoint storage tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add research/backtester/lp_ledger_checkpoint.py research/tests/test_lp_ledger_checkpoint.py
git commit -m "feat: stage resumable LP ledger evidence"
```

---

### Task 3 (Uncommitted V1 Foundation; Finish Only Through Task 4): Add Progress, Read-Only Status, And Secret-Safe Failures

This section records the already written progress/status/redaction foundation.
Do not execute or accept Steps 1–4 independently: their schema-v1 baseline was
green before the amendment, while Task 4 now owns the schema-v2 RED/GREEN
cycle, phase/counter replacement, and the only commit containing this work.

**Files:**
- Modify: `research/backtester/lp_ledger_checkpoint.py`
- Create: `research/scripts/report_lp_ledger_export_status.py`
- Modify: `research/tests/test_lp_ledger_checkpoint.py`
- Modify: `engine/web3_utils.py`
- Modify: `tests/test_web3_boundaries.py`

**Interfaces:**
- Consumes: one transactional `CheckpointSnapshot`, run-lock state, controlled
  UTC/monotonic clocks, and configured sensitive values.
- Produces: canonical progress JSON, normalized status facts, human/JSON status
  output, `ErrorCode`, and allowlist-only failure rendering.

- [x] **Historical Step 1: Write progress, status, and redaction tests**

Add tests proving:

```python
def test_safe_failure_never_interpolates_exception_text() -> None:
    secret = "current-provider-secret"
    line = render_safe_failure(
        pool="uni-base",
        phase="action_fetch",
        error_code="rpc_error",
        exception=RuntimeError(f"Authorization: Bearer {secret}"),
    )
    assert secret not in line
    assert "RuntimeError" not in line
    assert line == "[lp-ledger][uni-base] phase=action_fetch error=rpc_error"
```

`test_status_uses_sqlite_generation_and_rejects_stale_progress` writes a
generation-3 progress record, advances SQLite to generation 4, and asserts the
status has generation 4, `progress_state == "progress_unavailable"`, and no
rate. `test_running_checkpoint_with_free_lock_reports_interrupted` creates a
running checkpoint without holding its lock and asserts status is
`"interrupted"`.

Parameterize `redact_rpc_credentials` for `/v2/<secret>`, URL userinfo, query
keys, `Authorization`, bearer, `X-API-Key`, nested exceptions, and explicit
configured secret values. Test missing/malformed/wrong-run/wrong-attempt/
wrong-generation progress; atomic replace failure preserving the prior file;
phase-local rate/ETA with controlled clocks; action/frozen-token/full-transfer/
relevant-transfer/ownership counters; active lock despite an old update;
read-only status with a live WAL; corrupt/incompatible checkpoint reporting as
`blocked`; progress-only modes reporting `non_resumable` or `unknown`; bounded
write scheduling at five seconds, thirty seconds, or twenty-five newly durable
units; and human/JSON CLI output for Base+BSC.

- [x] **Historical Step 2: Confirm the missing-progress/status failures**

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py tests/test_web3_boundaries.py
```

Expected: failures name `write_progress_atomically`, `read_export_status`, and
the unredacted credential vectors.

- [x] **Historical Step 3: Implement the schema-v1-neutral progress/status foundation**

Add these exact public interfaces:

```text
ErrorCode = Literal[
    "rpc_error",
    "checkpoint_incompatible",
    "checkpoint_corrupt",
    "decode_error",
    "replay_error",
    "publication_error",
    "checkpoint_maintenance_error",
    "interrupted",
    "unknown_error",
]

progress_from_snapshot(
  snapshot: CheckpointSnapshot,
  now: datetime,
  monotonic_seconds: float,
  previous: ProgressSnapshot | None,
) -> ProgressSnapshot
write_progress_atomically(paths: CheckpointPaths, progress: ProgressSnapshot) -> None
read_export_status(paths: CheckpointPaths) -> ExportStatus
render_safe_failure(
  pool: str,
  phase: str,
  error_code: ErrorCode,
  exception: BaseException,
) -> str
```

`read_export_status` opens SQLite with `mode=ro`, uses one short snapshot
transaction, and takes rate/ETA only from matching
`(run_id, attempt_number, generation)`. It probes only an existing run-lock path
and never creates one. The CLI accepts positional output paths plus `--json`.

Extend `redact_rpc_credentials(raw, *, sensitive_values=())` only as defense in
depth. The exporter-safe renderer must use fixed fields and must not stringify
the exception or emit arbitrary exception class names.

- [x] **Historical Step 4: Verify the foundation against schema v1**

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py tests/test_web3_boundaries.py
python3 -m py_compile research/scripts/report_lp_ledger_export_status.py engine/web3_utils.py
```

Historical result: all focused schema-v1 tests passed and compilation
succeeded. Task 4 deliberately makes the new v2 tests fail before replacing the
obsolete phases and counters.

- [ ] **Step 5: Carry the Task 3 foundation into Task 4 without committing it**

Task 3 is not an independently acceptable commit after the binding amendment.
Keep its atomic progress, read-only status, lock, and secret-safety work in the
working tree, but run Task 4's schema-v2 RED/GREEN cycle before committing the
combined progress/checkpoint change.

---

### Task 4: Rebase The Checkpoint To Action-First Schema V2 And Finish Progress

**Files:**
- Modify: `research/backtester/lp_ledger_checkpoint.py`
- Modify: `research/tests/test_lp_ledger_checkpoint.py`
- Modify: `research/scripts/report_lp_ledger_export_status.py`
- Modify: `engine/main.py`
- Modify: `engine/web3_utils.py`
- Modify: `tests/test_web3_boundaries.py`

**Interfaces:**
- Consumes: the valid Task 1/2 SQLite code, lock, canonical-codec, and Task 3
  progress/redaction foundations. Existing schema-v1 operational artifacts are
  invalid inputs and fail closed.
- Produces: checkpoint schema v2, progress schema v2, source-specific action and
  transfer APIs, frozen-token evidence, replay-input evidence, and truthful
  action-first progress/status facts.

- [ ] **Step 1: Write the failing schema-v2 and progress-v2 tests**

Pin this phase tuple exactly:

```python
(
    "preflight",
    "action_discovery",
    "action_fetch",
    "action_decode",
    "token_set_freeze",
    "full_transfer_scan",
    "relevant_transfer_fetch",
    "relevant_transfer_decode",
    "replay_input_bind",
    "price_replay",
    "build",
    "publish",
    "succeeded",
)
```

Add focused tests that require:

```text
DiscoveryWitness(..., topics: tuple[str, ...], data: str)
PositionKeyMapping(
  token_id, pool_id, tick_lower, tick_upper, salt,
  mint_block_number, mint_log_index, mint_event_order,
)
ReplayInputEvidence(
  path, sha256, byte_length, row_count, header_sha256, parser_version,
  price_semantics_sha256, chain, pool_id, first_block, last_block,
  first_timestamp_ms, last_timestamp_ms, price_event_count,
  price_events_sha256,
)

incomplete_action_chunks() -> tuple[BlockRange, ...]
commit_action_chunk(block_range, witnesses) -> CheckpointSnapshot
action_candidate_hashes() -> tuple[str, ...]
unfetched_action_hashes() -> tuple[str, ...]
commit_action_bundle(bundle) -> CheckpointSnapshot
undecoded_action_bundles() -> tuple[CandidateBundle, ...]
commit_decoded_action_transaction(..., position_keys) -> CheckpointSnapshot
freeze_action_token_set() -> CheckpointSnapshot
frozen_token_ids() -> tuple[int, ...]

incomplete_transfer_chunks() -> tuple[BlockRange, ...]
commit_transfer_chunk(
  block_range,
  unfiltered_count: int,
  unfiltered_sha256: str,
  relevant_witnesses: Sequence[DiscoveryWitness],
) -> CheckpointSnapshot
relevant_transfer_transaction_hashes() -> tuple[str, ...]
unfetched_relevant_transfer_hashes() -> tuple[str, ...]
commit_relevant_transfer_bundle(bundle) -> CheckpointSnapshot
undecoded_relevant_transfer_bundles() -> tuple[CandidateBundle, ...]
commit_decoded_relevant_transfer_transaction(...) -> CheckpointSnapshot

commit_replay_input(evidence: ReplayInputEvidence) -> CheckpointSnapshot
commit_action_price_bindings(bindings) -> CheckpointSnapshot
```

Require complete topic tuples and data to round-trip canonically. Require token
set freeze to sort/deduplicate decoded-action IDs, persist count/digest, reject
incomplete action decode, and detect digest tampering. Require transfer chunks
to retain only frozen-token witnesses while preserving the caller-supplied
unfiltered count/digest. Reject a count below the retained-witness count and
reject duplicate or unfrozen relevant witnesses. Adapter tests in Task 7—not
the checkpoint store—prove the digest is computed from the complete normalized
log response before filtering.

Directly exercise zero/overlap storage semantics: an empty frozen set still
allows full-transfer scan completion followed by durable `0/0`
relevant-transfer fetch/decode phases; an action/relevant-transfer overlap
reuses exactly one raw bundle row but creates separate transfer-fetch and
transfer-decode relation markers; and the relevant-fetch total counts every
distinct retained-transfer hash, including reused action bundles.

Pin named build inputs: action transaction hashes, frozen token IDs,
unfiltered-transfer attestation, relevant transfer hashes/witnesses, eligible
bundle hashes, replay input, actions, ownership events, and price bindings. A
flat `candidate_hashes` field is forbidden.

Update progress/status tests to require
`action_candidate_transaction_count`, `action_count`, `frozen_token_count`,
`full_transfer_log_count`, `relevant_transfer_witness_count`,
`relevant_transfer_transaction_count`, `ownership_event_count`,
`bound_action_count`, and `ledger_row_count`. Their exact sources are:

| Field | Exact meaning |
| --- | --- |
| `action_candidate_transaction_count` | Distinct transaction hashes among action witnesses |
| `action_count` | Persisted decoded liquidity actions |
| `frozen_token_count` | Persisted frozen token IDs |
| `full_transfer_log_count` | Sum of all unfiltered PositionManager Transfer logs across committed chunks |
| `relevant_transfer_witness_count` | Retained Transfer logs whose indexed token ID belongs to the frozen set |
| `relevant_transfer_transaction_count` | Distinct hashes among retained witnesses, including hashes that overlap action transactions |
| `ownership_event_count` | Persisted frozen-token ownership events |
| `bound_action_count` | Persisted action price bindings |
| `ledger_row_count` | Built canonical ledger rows |

The `relevant_transfer_fetch` total is every distinct relevant-transfer hash,
not merely cache misses. An overlapping action bundle still receives a separate
durable relevant-transfer-fetch relation marker after its existing raw bundle
passes the retained-witness validation. Require schema-v1 database/progress
artifacts to fail closed.

Preflight rejects either endpoint unless
`requested_start == replay_input.first_block` and
`requested_end == replay_input.last_block`. The checkpoint schema owns every
`ReplayInputEvidence` field in Task 4; Task 8 validates and fills that record
without another schema change.

Pin the schema-v2 durable mapping to accept and validate
`token_set_sha256`, `unfiltered_transfer_sha256`, and
`replay_input_sha256`. Canonical progress parsing rejects obsolete
`candidate_count` and `replay_event_count`. CLI JSON and human-table tests must
assert the exact v2 keys and headings; neither output may contain `CANDIDATES`.

- [ ] **Step 2: Run the focused tests and observe the obsolete-union failures**

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py tests/test_web3_boundaries.py
```

Expected: the new tests fail on the old phase tuple, schema version, flat
`topic0/topic1` witness, shared discovery partition, union candidate methods,
replay RPC tables, and ambiguous counters. Existing redaction tests remain
green.

- [ ] **Step 3: Implement the minimal schema-v2/store/progress replacement**

Bump checkpoint and progress schema versions. Replace the shared discovery
partition with action and transfer partitions. Keep one deduplicated raw bundle
cache but use separate action-fetch, action-decode, relevant-transfer-fetch,
and relevant-transfer-decode relations/cursors. Remove replay RPC chunk/seed
tables and add one exact replay-input record. Extend witness receipt matching to
require the entire topics tuple and log data.

Replace `RunIdentity.state_view` and the vague combined `discovery_topics` with
the accepted wrapper EntryPoint, explicit action topic, explicit Transfer topic,
and the complete frozen replay identity. Add an immutable
`token_position_keys` table now so Task 6 can persist mint-derived
`PositionKeyMapping` rows without a later schema change; it is never a
salt-to-token inference fallback.

Every evidence-bearing final commit advances only to the immediate next phase
in the same transaction. Recompute token-set, witness, candidate, and replay
facts during resume validation. Preserve existing no-follow paths, WAL,
directory safety, canonical JSON, progress generation matching, read-only
status, and credential redaction unchanged except for the new phases/counters.
Preflight computes the replay artifact identity bound into `RunIdentity`;
`replay_input_bind` rereads the file and commits evidence only if it is exactly
equal. Test file-byte drift both before binding and on resume.

- [ ] **Step 4: Run focused verification and compilation**

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py tests/test_web3_boundaries.py
python3 -m ruff check research/backtester/lp_ledger_checkpoint.py research/scripts/report_lp_ledger_export_status.py research/tests/test_lp_ledger_checkpoint.py engine/main.py engine/web3_utils.py tests/test_web3_boundaries.py
python3 -m mypy research/backtester/lp_ledger_checkpoint.py
python3 -m py_compile research/backtester/lp_ledger_checkpoint.py research/scripts/report_lp_ledger_export_status.py engine/main.py engine/web3_utils.py
git diff --check
```

Expected: focused tests and checks pass; only the known third-party WebSockets
deprecation warning may remain.

- [ ] **Step 5: Commit Task 4**

```bash
git add engine/main.py engine/web3_utils.py research/backtester/lp_ledger_checkpoint.py research/scripts/report_lp_ledger_export_status.py research/tests/test_lp_ledger_checkpoint.py tests/test_web3_boundaries.py
git commit -m "feat: checkpoint action-first LP ledger progress"
```

---

### Task 5: Stage Complete Target-Action Discovery And Bundles

**Files:**
- Modify: `research/scripts/export_v4_lp_ledger.py`
- Modify: `research/tests/test_v4_lp_ledger.py`

**Interfaces:**
- Consumes: schema-v2 action chunk/bundle APIs, fixed frozen ranges, PoolManager
  `ModifyLiquidity` logs, and existing RPC normalization validators.
- Produces: `_stage_action_discovery(...)`,
  `_fetch_and_validate_candidate_bundle(...)`, and
  `_stage_action_bundles(...)`.

- [ ] **Step 1: Write failing target-action discovery/fetch tests**

Test quiet action chunks, complete topics/data normalization, crash before and
after chunk commit, resume skipping only committed chunks, missing log identity,
wrong pool/topic/address, canonical action hash set, transaction/receipt/hash/
location disagreement, exact receipt witness matching, fetch-order independence,
and committed-bundle reuse.

Use these adapter calls:

```python
_stage_action_discovery(run, fake_w3, config, start_block, end_block)
_stage_action_bundles(run, fake_w3, start_block, end_block)
```

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "action_discovery or action_bundle"
```

Expected: failures name the missing action-specific adapters.

- [ ] **Step 3: Implement only action discovery and action bundle staging**

Query target-pool `ModifyLiquidity` logs once per incomplete action chunk.
Normalize full witness identity, commit quiet chunks, and fetch only canonical
action transaction hashes. Split the existing point-read logic into one bundle
validator reusable by Task 7. Do not query PositionManager Transfers in this
task and do not retain the old all-transfer regression helper on the verified
path.

- [ ] **Step 4: Run GREEN and regressions**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "action or requested_hash or transaction_location"
python3 -m py_compile research/scripts/export_v4_lp_ledger.py
git diff --check
```

- [ ] **Step 5: Commit Task 5**

```bash
git add research/scripts/export_v4_lp_ledger.py research/tests/test_v4_lp_ledger.py
git commit -m "feat: stage complete LP action evidence"
```

---

### Task 6: Decode Every Target Action And Freeze Relevant Token IDs

**Files:**
- Modify: `research/backtester/v4_lp_ledger.py`
- Modify: `research/scripts/export_v4_lp_ledger.py`
- Modify: `research/tests/test_v4_lp_ledger.py`

**Interfaces:**
- Consumes: canonical action bundles/witnesses, historical position cache,
  existing direct/multicall decoder, and the observed ERC-4337 wrapper shape.
- Produces: `position_manager_calls_for_transaction(...)`,
  `_stage_action_decode(...)`, and `_freeze_action_token_set(...)`.

- [ ] **Step 1: Write failing direct, wrapped, multi-action, and freeze tests**

Pin an anonymized local fixture for the observed wrapped path:

```text
EntryPoint handleOps((address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[],address)
  -> account execute(bytes32,bytes), batch mode 0x01
  -> (address,uint256,bytes)[]
  -> configured PositionManager multicall(bytes[])
```

Assert the synthetic PositionManager call uses the user-operation sender—not
the bundler—as `from`. Assert all 43 known Base wrapper-pattern burn witnesses
are representable in an integration fixture and that witness salts map to the
decoded token IDs. Add fail-closed tests for malformed `handleOps`, unsupported
execution mode, a wrong outer EntryPoint, zero or multiple matching
PositionManager calls, noncanonical ABI payloads, action/log count or identity
mismatch, and a target action with no decoder representation.

Add an exact multi-mint test proving each mint action maps to a distinct
zero-address PositionManager Transfer by log order; `_find_minted_token_id()`
may not select the first mint for multiple actions. Test chronological resume,
historical position found/not-found caching, state restoration, and atomic
token-set freeze. Require durable position state to include the salt-bearing
`PoolPositionKey`. Test same-log-order rows with wrong pool, ticks, salt, or
signed delta; excessive decrements must fail rather than clamp liquidity.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "wrapped_action or multi_mint or action_decode or token_set"
```

Expected: wrapped calls produce no actions, multi-mint mapping is ambiguous,
and token freeze is unavailable.

- [ ] **Step 3: Implement the smallest explicit wrapper and reconciliation path**

Decode only the pinned EntryPoint/account batch shape plus existing direct and
PositionManager `multicall` inputs. Return explicit call contexts containing
PositionManager input, the user-operation effective sender, wrapper source,
user-operation index, batch index, and outer transaction hash. Require the
outer target to be the configured EntryPoint, exactly one representable user
operation, exact batch mode `0x01`, and exactly one configured PositionManager
call. Permit unrelated batch calls, but reject unknown selector aliases,
unsupported mode bits, nested generic recursion, malformed/noncanonical ABI,
or any zero/multiple-target ambiguity.

Decode every raw target-pool log into a full structural witness containing log
index, pool ID, PoolManager sender, ticks, signed liquidity delta, and salt.
Match every decoded action to exactly one witness by semantic identity; use
ordering only as a deterministic tie-break after identity equality. Pair all
mint operations with all zero-address PositionManager Transfers in global
operation/log order, then match their pool/ticks/delta and persist the resulting
salt-bearing position key. For increases/decreases, recover the token's prior
key and require exact pool/ticks/salt/delta agreement. Any unmatched or extra
action/witness fails; there is no positional dequeue or salt-based token-ID
fallback.

Stage action decoding in transaction order with cached headers and historical
position resolutions. Commit decoded actions/state/cursor atomically. Once all
action bundles reconcile exactly, call the checkpoint's evidence-bearing token
freeze transition. The precoverage ledger is used only in Task 9 comparison.

- [ ] **Step 4: Run GREEN and decoder regressions**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "decode or action or mint or ownership"
python3 -m py_compile research/backtester/v4_lp_ledger.py research/scripts/export_v4_lp_ledger.py
git diff --check
```

- [ ] **Step 5: Commit Task 6**

```bash
git add research/backtester/v4_lp_ledger.py research/scripts/export_v4_lp_ledger.py research/tests/test_v4_lp_ledger.py
git commit -m "feat: reconcile wrapped LP actions"
```

---

### Task 7: Attest All Transfers And Fetch Only Relevant Ownership Bundles

**Files:**
- Modify: `research/scripts/export_v4_lp_ledger.py`
- Modify: `research/tests/test_v4_lp_ledger.py`

**Interfaces:**
- Consumes: frozen token IDs, complete PositionManager Transfer log stream,
  schema-v2 transfer chunks, shared bundle validator, and ownership decoder.
- Produces: `_stage_full_transfer_scan(...)`,
  `_stage_relevant_transfer_bundles(...)`, and
  `_stage_relevant_transfer_decode(...)`.

- [ ] **Step 1: Write failing transfer-attestation/filter tests**

For each fake chunk include relevant, irrelevant, mint, burn, transfer-only,
same-action-transaction, and quiet cases. Assert canonical unfiltered
count/digest covers every returned log; only `topic3` IDs in the frozen set are
persisted as relevant witnesses; filtering spans the entire inception-to-end
range; irrelevant hashes never enter bundle reads; action bundles are reused;
and every retained witness matches exactly one receipt log. Require the
unfiltered digest to be recomputed from the full normalized response rather
than trusted from an injected scalar, and reject a count below relevant count,
duplicate retained witnesses, or any retained witness absent from that full
response.

Add deterministic provider subdivision tests: an oversized/error response does
not commit a chunk, subdivided children cover the exact parent interval without
gaps/overlap, and no response is accepted as silently truncated. Test ownership
ordering within the same transaction, ownership changes before first action and
after final action, zero-address endpoints, unfrozen-token rejection, and
crash/resume for scan, fetch, and decode independently. An empty frozen-token
set still completes the full scan and both zero-work transfer phases durably.
An action/transfer overlap reuses one raw bundle but advances separate durable
relevant-fetch and ownership-decode markers. Every retained witness must map
one-to-one to one ownership event with identical full log identity, token ID,
from/to addresses, and canonical order; missing, duplicate, or extra relevant
ownership events fail.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "transfer_scan or relevant_transfer or ownership_decode"
```

- [ ] **Step 3: Implement full attestation and relevant-only point reads**

For each configured parent chunk, normalize all Transfer logs before computing
its canonical digest. After three retryable transport, rate-limit, or
range-size failures, subdivide sequentially and deterministically until all
children succeed or a single-block leaf fails; child intervals are transient,
and their merged canonical log set is committed only under the fixed parent.
Non-retryable, malformed, explicitly truncated, or exhausted responses fail
without a parent commit. Filter only after validation using indexed `topic3`.
Commit the unfiltered count/digest and relevant witnesses together. Derive the
relevant transaction set from retained witnesses, subtract already staged
action bundles only for RPC point reads, and still mark every relevant hash in
the separate fetch relation. Decode only frozen-token ownership events and
reconcile them exactly to retained witnesses. Preserve one raw bundle when a
transaction belongs to both populations and separate action/ownership decode
markers.

- [ ] **Step 4: Run GREEN and prove no unrelated point reads**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "transfer or ownership or candidate"
python3 -m py_compile research/scripts/export_v4_lp_ledger.py
git diff --check
```

- [ ] **Step 5: Commit Task 7**

```bash
git add research/scripts/export_v4_lp_ledger.py research/tests/test_v4_lp_ledger.py
git commit -m "feat: filter LP ownership bundle reads"
```

---

### Task 8: Bind Frozen Replay Inputs And Publish Coverage Sidecar V2

**Files:**
- Modify: `research/scripts/export_v4_lp_ledger.py`
- Modify: `research/backtester/lp_ledger_attribution.py`
- Modify: `research/cross_pool/manifest.py`
- Modify: `research/cross_pool/article_manifest.schema.json`
- Modify: `research/tests/test_v4_lp_ledger.py`
- Modify: `research/tests/test_cross_pool_market_structure.py`
- Modify: `research/tests/test_cross_pool_reporting.py`

**Interfaces:**
- Consumes: decoded actions, ownership events, frozen replay CSV, action/
  transfer/token attestations, eligible bundle set, and endpoint snapshot.
- Produces: `_bind_frozen_replay_input(...)`, `_stage_price_replay(...)`,
  `RpcLedgerCoverage` schema v2, and `_build_rpc_ledger_pair_from_checkpoint(...)`.

- [ ] **Step 1: Write failing replay-drift, ordering, sidecar-v2, and manifest tests**

Require exact replay path/digest/byte length/row count/range/timestamps,
pool/chain, frozen CSV header, parser version, and canonical order. Validate
required row identities and accept only `initialize`/`swap` rows as price-state
updates; derive price from positive `sqrt_price_x96`, token orientation, and
decimals via `pool_price_semantics.py`. Never use stored `cngn_usd_price` as
state. Test an action before and after a same-block swap, malformed rows,
duplicate order keys, changed artifact bytes before bind and on resume, missing
price state, and exact one-binding-per-action cardinality. Assert zero price-log
RPC calls.

Pin coverage mutations independently for action witness count/digest/hashes,
frozen token IDs/digest, unfiltered transfer count/digest, relevant witness
count/digest/hashes, eligible bundle hashes/digest, reconciliation facts,
replay input digest/range/counts, endpoint facts, and ledger bytes. Every
mutation must fail the strict loader and manifest gate.

All set digests are SHA-256 over UTF-8 canonical JSON with sorted keys and no
insignificant whitespace. Action/relevant witnesses use full normalized log
objects sorted by `(block_number, transaction_index, log_index,
transaction_hash)`; token IDs are numerically sorted canonical decimal strings;
hash sets are sorted normalized lowercase hashes. A full-transfer chunk digest
covers its complete normalized witness array before filtering. The aggregate
full-transfer digest covers the ordered configured-parent records
`(index,start,end,count,sha256)` and separately binds PositionManager address,
Transfer topic, and inclusive run range; transient subdivision leaves never
enter the preimage. Eligible bundles use sorted hashes plus their validated raw
bundle digests.

Action reconciliation evidence contains count, digest, and exact-success status
over sorted action-witness-to-decoded-action identities. Ownership
reconciliation analogously covers every retained Transfer witness. Replay
evidence includes exact endpoint timestamps, parser version, price-semantics
source digest, and row/order facts. The distilled manifest evidence must copy
these fields and require its independently captured Base/BSC replay digest,
range, and timestamps to equal the corresponding ledger coverage values.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py research/tests/test_cross_pool_market_structure.py research/tests/test_cross_pool_reporting.py -k "replay_input or staged_replay or coverage or manifest"
```

- [ ] **Step 3: Implement local replay binding and sidecar-v2 propagation**

Load and validate the existing replay CSV without RPC. Bind exact artifact
metadata in the checkpoint, derive event-time states from its canonical
Initialize/Swap stream, and commit one action binding each. Build ledger bytes
from explicit canonical actions/ownership/bindings.

Bump the sidecar schema and replace the ambiguous scan-source field with named
action, token, full-transfer, relevant-transfer, eligible-bundle, replay-input,
and reconciliation evidence. Update strict parsing/serialization, distilled
verified evidence, manifest JSON/schema, and test fixtures. Do not accept v1 as
verified through a compatibility shim. The manifest gate compares each ledger's
replay-input digest, range, and timestamps to the exact replay artifact consumed
by the cross-pool analysis and fails on any mismatch.

- [ ] **Step 4: Run GREEN and integrated research regressions**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py research/tests/test_cross_pool_market_structure.py research/tests/test_cross_pool_reporting.py
python3 -m ruff check research/backtester/lp_ledger_attribution.py research/cross_pool/manifest.py research/scripts/export_v4_lp_ledger.py
python3 -m py_compile research/backtester/lp_ledger_attribution.py research/cross_pool/manifest.py research/scripts/export_v4_lp_ledger.py
git diff --check
```

- [ ] **Step 5: Commit Task 8**

```bash
git add research/backtester/lp_ledger_attribution.py research/cross_pool/manifest.py research/cross_pool/article_manifest.schema.json research/scripts/export_v4_lp_ledger.py research/tests/test_v4_lp_ledger.py research/tests/test_cross_pool_market_structure.py research/tests/test_cross_pool_reporting.py
git commit -m "feat: attest relevant LP ledger coverage"
```

---

### Task 9: Wire Publication, Prove Resume Equality, Document, And Run Frozen Exports

**Files:**
- Modify: `research/scripts/export_v4_lp_ledger.py`
- Modify: `research/tests/test_v4_lp_ledger.py`
- Modify: `research/tests/test_lp_ledger_checkpoint.py`
- Modify: `dashboard/docs/lp/pool-history-operations.md`

**Interfaces:**
- Consumes: all schema-v2 phases, expected final bytes/hashes, endpoint
  revalidation, publication reconciliation, frozen Base/BSC ranges, and ignored
  operational paths.
- Produces: automatic full-RPC resume, canonical Base/BSC ledger/coverage pairs,
  precoverage reconciliation report, and the gate into the final backtest.

- [ ] **Step 1: Write failing orchestration and crash/resume acceptance tests**

Pin the public order:

```text
preflight -> action discovery/fetch/decode -> token freeze -> full transfer scan
-> relevant transfer fetch/decode -> replay input bind -> price replay -> build
-> endpoint revalidation -> publish -> succeeded
```

Inject process death after every committed phase and after each final rename.
For every boundary require resumed CSV and sidecar bytes to equal a clean run,
no committed action/transfer chunk or bundle to repeat, no price-log RPC call,
and strict sidecar-v2 loading. Scan database/WAL/SHM/progress/stdout/stderr bytes
for injected credentials.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py research/tests/test_v4_lp_ledger.py -k "full_rpc or crash_resume or byte_equal or raw_secret or publication"
```

- [ ] **Step 3: Implement public orchestration and exact publication recovery**

Wire the schema-v2 phases under the output run lock. Preserve start/end endpoint
checks, expected-hash-before-rename, directory fsync, exact mixed-pair
reconciliation, ambiguous-file refusal, terminal WAL handling, safe errors, and
progress updates. Candidate-list/fixture modes remain unverified and do not
create schema-v2 checkpoints.

- [ ] **Step 4: Update the operations runbook**

Document schema-v2 phases/counters, explicit `--fresh` requirement for schema
v1, frozen replay digest binding, complete-transfer versus relevant-bundle
evidence, automatic resume/status commands, checkpoint namespace, mixed-pair
recovery, and the rule that operational files are not research evidence.

- [ ] **Step 5: Run final local verification and Sol/Terra review**

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py research/tests/test_v4_lp_ledger.py research/tests/test_v4_event_replay.py research/tests/test_cross_pool_market_structure.py research/tests/test_cross_pool_reporting.py tests/test_web3_boundaries.py
python3 -m ruff check research/backtester/lp_ledger_checkpoint.py research/backtester/lp_ledger_attribution.py research/backtester/v4_lp_ledger.py research/cross_pool/manifest.py research/scripts/export_v4_lp_ledger.py research/scripts/report_lp_ledger_export_status.py
python3 -m mypy research/backtester/lp_ledger_checkpoint.py
python3 -m py_compile research/backtester/lp_ledger_checkpoint.py research/backtester/lp_ledger_attribution.py research/backtester/v4_lp_ledger.py research/cross_pool/manifest.py research/scripts/export_v4_lp_ledger.py research/scripts/report_lp_ledger_export_status.py
git diff --check
```

Resolve every Critical or Important review finding and rerun its covering tests.

- [ ] **Step 6: Run bounded real-RPC Base/BSC smoke-resume checks**

Use ignored temporary outputs from each pool's inception through one small
chunk. Confirm at least one durable unit, interrupt, resume, and require strict
sidecar-v2 validation without printing or persisting RPC credentials.

- [ ] **Step 7: Run the frozen exports and reconcile precoverage rows**

Run both frozen ranges to canonical outputs. Require every target action to be
classified, every prior row key to match exactly or produce an explicit
fail-closed difference report, and all newly reconstructed rows—especially the
wrapped Base burns—to pass attribution, ownership, range, and endpoint QA.

- [ ] **Step 8: Commit Task 9 before starting downstream results**

```bash
git add research/scripts/export_v4_lp_ledger.py research/tests/test_v4_lp_ledger.py research/tests/test_lp_ledger_checkpoint.py dashboard/docs/lp/pool-history-operations.md
git commit -m "test: prove action-first LP ledger recovery"
```

Only validated canonical pairs may enter the portfolio-of-positions backtest,
manifest generation, figures, or article results.

---

## Appendix: Superseded 2026-07-22 Tasks

The original Tasks 4 through 8 below are retained only as an audit trail. They
must not be executed: they union every PositionManager Transfer transaction
into the bundle candidate set and independently rescan price logs.

### Superseded Original Task 4 (Do Not Execute): Stage Candidate Discovery And Transaction Bundles

**Files:**
- Modify: `research/scripts/export_v4_lp_ledger.py`
- Modify: `research/tests/test_v4_lp_ledger.py`

**Interfaces:**
- Consumes: `LPLedgerCheckpoint`, fixed discovery ranges, Web3 log/transaction/
  receipt reads, and the current normalization validators.
- Produces: `_stage_candidate_discovery(...)`,
  `_fetch_and_validate_candidate_bundle(...)`, and
  `_stage_candidate_bundles(...)`.

- [ ] **Step 1: Write failing staged discovery/fetch tests**

Add exact integration tests for: both discovery sources in one commit; quiet
chunk completion; crash after the first RPC response repeating both queries;
resume skipping only committed chunks; missing block hash/transaction index/log
index rejection; canonical candidate union; transaction/receipt block-hash
agreement; every witness present at the same receipt log index and block hash;
precommit retry; and committed bundle skip.

Use this adapter contract in tests:

```python
_stage_candidate_discovery(run, fake_w3, config, start_block, end_block)
_stage_candidate_bundles(run, fake_w3, start_block, end_block)
```

- [ ] **Step 2: Run the staged integration tests and observe missing adapters**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "staged_discovery or staged_candidate"
```

Expected: tests fail because `_stage_candidate_discovery` and
`_stage_candidate_bundles` do not exist.

- [ ] **Step 3: Implement discovery witness normalization and bundle staging**

Keep `_discover_rpc_lp_candidates` as the noncheckpointed regression helper.
The staged discovery adapter queries the same two sources for every incomplete
range, requires all log identity fields, builds `DiscoveryWitness` objects, and
commits only after both responses validate.

Split transaction fetching from `_decode_rpc_lp_inputs`:

```text
_fetch_and_validate_candidate_bundle(
  w3: Web3,
  requested_hash: str,
  witnesses: Sequence[DiscoveryWitness],
  start_block: int,
  end_block: int,
) -> CandidateBundle

_stage_candidate_bundles(
  run: LPLedgerCheckpoint,
  w3: Web3,
  start_block: int,
  end_block: int,
) -> None
```

Normalize mappings, sequences, bytes/HexBytes, booleans, `None`, strings, and
integers into canonical JSON; reject floats and unknown objects. Every integer
becomes canonical decimal text. Validate against staged witnesses before commit.

- [ ] **Step 4: Run focused and existing discovery/decode-boundary tests**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "candidate or discovery or requested_hash or transaction_location"
python3 -m py_compile research/scripts/export_v4_lp_ledger.py
```

Expected: staged and prior low-level tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add research/scripts/export_v4_lp_ledger.py research/tests/test_v4_lp_ledger.py
git commit -m "feat: checkpoint LP ledger candidate reads"
```

---

### Superseded Original Task 5 (Do Not Execute): Stage Chronological Decode And Historical Caches

**Files:**
- Modify: `research/scripts/export_v4_lp_ledger.py`
- Modify: `research/tests/test_v4_lp_ledger.py`

**Interfaces:**
- Consumes: staged bundles ordered by location, current pure ownership/action
  decoders, staged block headers, position resolutions, and decoder token state.
- Produces: `_stage_chronological_decode(run, w3, config)`.

- [ ] **Step 1: Write failing chronological resume tests**

Reuse the existing mint → ownership transfer → increase fixture and assert:

```python
_stage_chronological_decode(run, fake_w3, config)
assert [action.token_id for action in run.load_decoded_actions()] == [77, 77]
assert run.load_decoder_state()[77].liquidity_after == 1110
```

Add tests for location order despite hash-order fetch; restart restoring state;
header cache reuse; position cache reuse; explicit no-position persistence;
ownership-only decoded transaction marker; conflicting bundle/header hash;
one represented action per target-pool modify witness; and crash before commit
replaying only the current transaction.

- [ ] **Step 2: Run tests and observe the missing decode adapter**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "staged_decode"
```

Expected: failures because `_stage_chronological_decode` is missing.

- [ ] **Step 3: Implement one-candidate transactional decode**

Add this adapter:

```text
_stage_chronological_decode(
  run: LPLedgerCheckpoint,
  w3: Web3,
  config: ExportPoolConfig,
) -> None
```

For each undecoded bundle: restore token state; obtain or stage a block header;
resolve positions from `(token_id, query_block)` cache before RPC; call
`decode_ownership_events_from_receipt` and `decode_liquidity_actions_for_tx`
unchanged; reconcile required action log indexes; diff token state; and commit
headers, resolutions, actions, owners, state changes, decoded marker, cursor,
counters, and generation atomically. Preserve `_decode_rpc_lp_inputs` as the
noncheckpointed candidate-list wrapper using the same split helpers.

- [ ] **Step 4: Run decode and ledger regressions**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "decode or chronological or ownership"
python3 -m py_compile research/scripts/export_v4_lp_ledger.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add research/scripts/export_v4_lp_ledger.py research/tests/test_v4_lp_ledger.py
git commit -m "feat: checkpoint LP ledger decoding"
```

---

### Superseded Original Task 6 (Do Not Execute): Stage Price Evidence, Replay Seed, Bindings, And Final Build

**Files:**
- Modify: `research/scripts/export_v4_lp_ledger.py`
- Modify: `research/tests/test_v4_lp_ledger.py`

**Interfaces:**
- Consumes: staged actions, fixed replay ranges, canonical block headers,
  StateView seed, and current `attach_event_time_state`/`build_lp_ledger_rows`.
- Produces: `_stage_price_event_scan(...)`, `_stage_price_replay(...)`, and
  `_build_rpc_ledger_pair_from_checkpoint(...)`.

- [ ] **Step 1: Write failing replay and deterministic-build tests**

Test: Initialize+Swap queries commit together; quiet replay chunks; crash after
first topic repeats both; resume skips committed chunks; event/header block-hash
conflict; duplicate replay identity; persisted prior-block seed reuse with no
second StateView call; explicit no-seed at inception; action before/after a
same-block swap; missing/extra price binding rejection; witness-union coverage;
and clean staged bytes matching the existing deterministic fixture bytes.

- [ ] **Step 2: Run tests and observe missing replay/build adapters**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "staged_price or staged_replay or staged_build"
```

Expected: failures name the three missing private adapters.

- [ ] **Step 3: Implement the staged price scan, seed, replay, and build**

Use:

```text
PreparedLedgerPair:
  row_count: int
  ledger_bytes, coverage_bytes: bytes
  ledger_sha256, coverage_sha256: str

_stage_price_event_scan(
  run: LPLedgerCheckpoint,
  w3: Web3,
  config: ExportPoolConfig,
  start_block: int,
  end_block: int,
) -> None
_stage_price_replay(
  run: LPLedgerCheckpoint,
  w3: Web3,
  config: ExportPoolConfig,
) -> None
_build_rpc_ledger_pair_from_checkpoint(
  run: LPLedgerCheckpoint,
  pool: str,
  chain_id: int,
  endpoint: EndpointSnapshot,
) -> PreparedLedgerPair
```

The scan covers the full requested range even if there are no decoded actions.
Every event block gets one canonical header. Seed and header commit together;
resume uses the stored seed verbatim. Replay uses the existing pure event-order
helper and requires exact action/binding cardinality. Build loads explicit
canonical orders, calls existing ledger/CSV/coverage builders, and derives the
coverage candidate set only from discovery witnesses.

- [ ] **Step 4: Run replay/build regressions**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "price or replay or staged_build or coverage"
python3 -m pytest -q research/tests/test_v4_event_replay.py
python3 -m py_compile research/scripts/export_v4_lp_ledger.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add research/scripts/export_v4_lp_ledger.py research/tests/test_v4_lp_ledger.py
git commit -m "feat: checkpoint LP ledger price replay"
```

---

### Superseded Original Task 7 (Do Not Execute): Add Directory-Durable Publication Reconciliation And Public CLI Wiring

**Files:**
- Modify: `research/scripts/export_v4_lp_ledger.py`
- Modify: `research/tests/test_v4_lp_ledger.py`

**Interfaces:**
- Consumes: `PreparedLedgerPair`, persisted expected hashes, run/progress state,
  final endpoint revalidation, and existing coverage loader/publisher.
- Produces: automatic full-RPC checkpoint/resume, `--fresh`, hash-gated pair
  reconciliation, directory fsync, and terminal progress.

- [ ] **Step 1: Write failing publication and full-CLI lifecycle tests**

Add tests for: non-inception full-RPC rejection before Web3 creation; run-lock
contention before checkpoint/progress mutation; `--fresh` rejected for fixture
and candidate modes; resume endpoint mismatch before staged work; expected
hashes and `publish_started` committed before rename; parent-directory fsync
after every publish/rollback rename/unlink; crash after ledger rename producing
automatic exact sidecar completion; mixed new ledger/old sidecar reconciliation;
both expected files recognized; ambiguous files untouched; final pair strict
validation before `output_published`; crash after publication but before terminal
checkpoint reconciling without RPC rescans; and candidate/fixture behavior
remaining unverified/noncheckpointed while still using the run lock and
progress file. Also test terminal `wal_checkpoint(TRUNCATE)`: a bounded busy
result preserves success for retry on the next attempt, while a non-busy
maintenance failure preserves `output_published=true` and surfaces the closed
`checkpoint_maintenance_error` code.

Update existing full-RPC fixtures that use block `100` by replacing the test
pool config with `default_start_block=100`; do not weaken production inception
validation.

- [ ] **Step 2: Run tests and observe missing CLI/reconciliation behavior**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py -k "publication or resume or fresh or full_rpc"
```

Expected: focused tests fail on missing `--fresh`, directory fsync, or exact
reconciliation.

- [ ] **Step 3: Implement the public full-RPC phase orchestration**

The full-RPC branch first validates arguments and the pool-inception range
without constructing Web3. It then constructs Web3, validates the chain,
captures the initial endpoint snapshot, computes the deterministic
`RunIdentity`, derives `CheckpointPaths`, and only then enters this lock-scoped
orchestration:

```python
with acquire_run_lock(paths):
    with LPLedgerCheckpoint.create_or_resume(paths, identity, fresh=fresh) as run:
        _stage_candidate_discovery(run, w3, config, start_block, end_block)
        _stage_candidate_bundles(run, w3, start_block, end_block)
        _stage_chronological_decode(run, w3, config)
        _stage_price_event_scan(run, w3, config, start_block, end_block)
        _stage_price_replay(run, w3, config)
        prepared = _build_rpc_ledger_pair_from_checkpoint(run, pool, chain_id, endpoint)
        final_endpoint = _capture_rpc_coverage_endpoint(w3, start_block, end_block)
        _require_matching_endpoint(endpoint, final_endpoint)
        run.begin_publication(prepared.ledger_sha256, prepared.coverage_sha256)
        _publish_or_reconcile_ledger_pair(pool, output_path, prepared)
        run.mark_published()
        run.checkpoint_terminal_wal()
```

`begin_publication` accepts only the two already computed SHA-256 strings and
persists them with `publish_started`; the final-file reconciler separately
receives the expected bytes. The run lock must be held before checkpoint,
progress, or final-file inspection or mutation.

Write progress after every phase transition and bounded durable intervals. Map
failures to closed error codes and print only `render_safe_failure(...)`.
SIGINT/SIGTERM mark interrupted when cleanup runs; SIGKILL/reboot is inferred by
the free lock on the next status read. `--fresh` is full-RPC-only.

Fixture and candidate-list branches use the same output-derived run lock and
progress writer, with `checkpoint_generation=null` and explicit
`non_resumable` status, but never create or inspect the SQLite checkpoint.
Terminal WAL truncation uses a bounded busy timeout. A busy result is retained
for the next exclusive attempt without invalidating the published pair; every
other checkpoint failure is reported as maintenance failure without clearing
the durable publication fact.

Add `_fsync_directory`, make replace/unlink helpers directory durable, and add a
publication-lock-held reconciliation function with the exact state table from
the design. Replace `_publication_lock`'s `Path.open` with no-follow lock
creation and route every final/staged/backup read, replace, and unlink through
the shared component-and-leaf validator. Revalidate immediately before each
operation; a symlink is an error even where unlinking it would otherwise be
safe. Do not overwrite an ambiguous final.

- [ ] **Step 4: Run the complete focused LP/checkpoint/web3 suite**

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py research/tests/test_v4_lp_ledger.py tests/test_web3_boundaries.py
python3 -m py_compile research/backtester/lp_ledger_checkpoint.py research/scripts/export_v4_lp_ledger.py research/scripts/report_lp_ledger_export_status.py engine/web3_utils.py
```

Expected: all tests pass; only the known third-party WebSockets deprecation
warning may remain.

- [ ] **Step 5: Commit Task 7**

```bash
git add research/scripts/export_v4_lp_ledger.py research/tests/test_v4_lp_ledger.py
git commit -m "feat: resume verified LP ledger exports"
```

---

### Superseded Original Task 8 (Do Not Execute): Prove Crash/Resume Byte Equality, Document Operations, And Smoke Both Chains

**Files:**
- Modify: `research/tests/test_v4_lp_ledger.py`
- Modify: `research/tests/test_lp_ledger_checkpoint.py`
- Modify: `dashboard/docs/lp/pool-history-operations.md`

**Interfaces:**
- Consumes: complete checkpointed exporter, fake-RPC crash harness, status CLI,
  frozen Base/BSC inception blocks, and ignored temporary outputs.
- Produces: byte-equality proof, raw secret scans, operational runbook, bounded
  real-RPC smoke evidence, and readiness to restart the frozen run.

- [ ] **Step 1: Write the failing subprocess crash/resume acceptance tests**

Use a deterministic fake-RPC subprocess harness with `os._exit` after the
committed discovery, bundle, decode, replay-scan, publication-ledger-rename, and
publication-sidecar-rename boundaries. For every boundary:

```python
assert resumed_output.read_bytes() == clean_output.read_bytes()
assert resumed_coverage.read_bytes() == clean_coverage.read_bytes()
assert load_ledger_coverage("uni-base", resumed_output).verification_mode == "rpc_verified"
```

Scan the raw database, WAL, SHM, progress, stdout, and stderr bytes for every
injected credential form. Assert status reports active under a held lock,
interrupted after `os._exit`, and succeeded only after the final pair validates.

- [ ] **Step 2: Run acceptance tests and confirm the first unhandled boundary**

```bash
python3 -m pytest -q research/tests/test_v4_lp_ledger.py research/tests/test_lp_ledger_checkpoint.py -k "crash_resume or byte_equal or raw_secret"
```

Expected: at least the first newly added crash-boundary test fails until its
recovery path is complete.

- [ ] **Step 3: Make only the minimal recovery corrections required by the acceptance tests**

Keep corrections inside the existing checkpoint/exporter interfaces. Do not add
a second checkpoint format, best-effort salvage, partial publication status, or
provider-specific bypass.

- [ ] **Step 4: Update the LP operations runbook**

Document exact canonical commands, derived `.checkpoint.sqlite3`, `-wal`,
`-shm`, `.progress.json`, and `.run.lock` files, automatic resume, `--fresh`,
status commands for both outputs, generation/stale-progress semantics,
hash-gated mixed-pair recovery, and the statement that checkpoint/progress files
are operational state rather than verified evidence.

- [ ] **Step 5: Run final local verification**

```bash
python3 -m pytest -q research/tests/test_lp_ledger_checkpoint.py research/tests/test_v4_lp_ledger.py research/tests/test_v4_event_replay.py research/tests/test_cross_pool_market_structure.py tests/test_web3_boundaries.py
python3 -m ruff check research/backtester/lp_ledger_checkpoint.py research/scripts/export_v4_lp_ledger.py research/scripts/report_lp_ledger_export_status.py research/tests/test_lp_ledger_checkpoint.py research/tests/test_v4_lp_ledger.py engine/web3_utils.py tests/test_web3_boundaries.py
python3 -m mypy research/backtester/lp_ledger_checkpoint.py
python3 -m py_compile research/backtester/lp_ledger_checkpoint.py research/scripts/export_v4_lp_ledger.py research/scripts/report_lp_ledger_export_status.py engine/web3_utils.py
git diff --check
```

Expected: all project tests/checks pass; no new warning or error is introduced.

- [ ] **Step 6: Run bounded real-RPC Base and BSC smoke/resume checks**

Use ignored temporary outputs beginning at each frozen inception block and one
small configured chunk. Interrupt only after the status command confirms at
least one durable unit, rerun the same command, and require it to skip that unit
and publish a strictly loadable pair. Do not display, persist, or pass the RPC
URL/key on the command line.

- [ ] **Step 7: Commit Task 8**

```bash
git add research/tests/test_v4_lp_ledger.py research/tests/test_lp_ledger_checkpoint.py dashboard/docs/lp/pool-history-operations.md
git commit -m "test: prove LP ledger crash recovery"
```

- [ ] **Step 8: Perform final Sol and Terra reviews before restarting the frozen run**

Review the complete diff and verification evidence for correctness, frozen
methodology, fail-closed behavior, comments, documentation, modularity, and
secret safety. Resolve every Critical or Important Terra finding, rerun the
covering tests, and only then start the canonical Base and BSC exports.
