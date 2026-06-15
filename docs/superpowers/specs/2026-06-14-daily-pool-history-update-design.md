# Daily Pool History Update Design

## Goal

Keep the Base and BSC Uniswap v4 history CSVs current without truncating them,
duplicating rows, or losing progress when an RPC request fails.

## Architecture

The existing exporter remains the only component that decodes on-chain events.
It gains an optional per-pool checkpoint file recording the last block whose
entire chunk was flushed to disk. On resume, the exporter starts after the
greater of the CSV's highest event block and the checkpoint, so quiet block
ranges are not repeatedly scanned.

A new orchestration script runs pools sequentially, always with `--resume` and
checkpointing. It retries transient failures with bounded exponential backoff,
then validates CSV ordering, uniqueness, pool identity, and checkpoint coverage.
The same entry point serves the current June 9 catch-up and a daily macOS
`launchd` job. A non-blocking lock prevents overlapping updates.

## Data Safety

- Existing CSVs are never opened in write mode by the updater.
- Each chunk is flushed and `fsync`ed before its checkpoint is atomically
  replaced.
- A checkpoint advances only after every row in that chunk is durable.
- Validation failure returns non-zero and leaves the prior durable data intact.
- The updater fails loudly after the configured retry limit.

## Scheduling

The repository contains the wrapper and a LaunchAgent template. Installation
materializes the template under `~/Library/LaunchAgents`, with stdout/stderr in
the repository's `logs/` directory. The local execution time is configurable at
installation so scheduling does not leak into data-export logic.

## Testing

Unit tests pin checkpoint parsing, atomic progress writes, resume precedence,
retry/backoff behavior, lock behavior, and CSV validation. A smoke run over an
already-current short range verifies that the real CLI exits successfully
without duplicating rows.
