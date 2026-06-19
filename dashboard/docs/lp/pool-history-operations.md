---
title: Pool History Operations
order: 5
---

# Pool History Data Operations

The backtester datasets are updated by `scripts/update_v4_pool_history.py`.
The updater runs Base and BSC sequentially, resumes after the last durable
checkpoint, retries transient RPC failures, and validates each CSV before
moving to the next pool.

## Manual update

Update both pools to each chain's latest block:

```bash
python scripts/update_v4_pool_history.py
```

For a reproducible historical cutoff, pass explicit chain end blocks:

```bash
python scripts/update_v4_pool_history.py \
  --base-end-block 47130126 \
  --bsc-end-block 103315324
```

Checkpoints are stored in `data/checkpoints/`. The updater lock in the same
directory prevents manual and scheduled runs from overlapping. Do not delete a
checkpoint unless the corresponding CSV is also being rebuilt deliberately.

## Daily LaunchAgent

Install the plist with an explicit local time. This example uses 03:15:

```bash
python scripts/install_pool_history_launchd.py --hour 3 --minute 15
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.cngn.pool-history-update.plist" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.cngn.pool-history-update.plist"
```

The LaunchAgent uses the repository as its working directory, so the existing
`.env` supplies RPC configuration. Output is written to:

- `logs/pool-history-update.stdout.log`
- `logs/pool-history-update.stderr.log`

Run it immediately without changing the schedule:

```bash
launchctl kickstart -k "gui/$(id -u)/com.cngn.pool-history-update"
```

Unload it with:

```bash
launchctl bootout "gui/$(id -u)/com.cngn.pool-history-update"
```

If a day is missed, the next run catches up from the checkpoint rather than
limiting itself to a calendar-day slice.
