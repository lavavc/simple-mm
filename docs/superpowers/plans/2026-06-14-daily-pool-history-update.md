# Daily Pool History Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add checkpointed, retrying, validated daily updates for both pool-history CSVs and install them as a macOS LaunchAgent.

**Architecture:** Extend the decoder/exporter only with durable chunk checkpoints. Put retry, locking, pool sequencing, target-block selection, and validation in a separate orchestration module so the exporter stays focused on decoding events.

**Tech Stack:** Python 3.12, Web3.py, pytest, macOS launchd.

---

### Task 1: Durable exporter checkpoints

**Files:**
- Modify: `backtester/v4_export.py`
- Test: `tests/test_v4_export.py`

- [ ] Write failing tests proving a checkpoint is parsed, a corrupt checkpoint fails, and resume starts after `max(csv_max_block, checkpoint_block)`.
- [ ] Run `python -m pytest -q tests/test_v4_export.py` and verify the new tests fail because checkpoint helpers do not exist.
- [ ] Add `--checkpoint-file`, atomic JSON checkpoint writes after `flush` + `fsync`, and checkpoint-aware resume start selection.
- [ ] Run `python -m pytest -q tests/test_v4_export.py` and verify all tests pass.

### Task 2: Retrying update orchestrator

**Files:**
- Create: `scripts/update_v4_pool_history.py`
- Create: `tests/test_pool_history_update.py`

- [ ] Write failing tests for bounded exponential retry, sequential pool execution, lock rejection, and validation failures for disorder/duplicates/wrong pool.
- [ ] Run `python -m pytest -q tests/test_pool_history_update.py` and verify failure because the module does not exist.
- [ ] Implement typed pool jobs, subprocess execution of the exporter with `--resume` and `--checkpoint-file`, bounded backoff, an exclusive lock, and post-run CSV validation.
- [ ] Run `python -m pytest -q tests/test_pool_history_update.py` and verify all tests pass.

### Task 3: Daily scheduling

**Files:**
- Create: `scripts/install_pool_history_launchd.py`
- Create: `tests/test_pool_history_launchd.py`
- Create: `dashboard/docs/lp/pool-history-operations.md`

- [ ] Write a failing test that renders a LaunchAgent with an absolute repository path, chosen local hour/minute, and log paths.
- [ ] Run `python -m pytest -q tests/test_pool_history_launchd.py` and verify failure because the installer does not exist.
- [ ] Implement plist rendering/installation and document install, uninstall, logs, manual execution, and failure behavior.
- [ ] Run `python -m pytest -q tests/test_pool_history_launchd.py` and verify all tests pass.

### Task 4: Verification and continuation

**Files:**
- Modify: `data/uni_base_pool_history.csv`
- Modify: `data/uni_bsc_pool_history.csv`
- Modify: `backtester/results/extended_20260609/*`

- [ ] Run the updater through the fixed June 9 end blocks and verify checkpoints equal `47,130,126` and `103,315,324`.
- [ ] Run the complete targeted test suite.
- [ ] Run `scripts/run_extended_rerun.sh` and verify all four walk-forwards, PBO reports, and H12 outputs exist.
- [ ] Install and load the LaunchAgent at the approved local time, then run one manual kickstart and verify a successful log entry.
