from __future__ import annotations

import fcntl
import json
import multiprocessing
import threading
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path

import pytest

from research.backtester.portfolio_checkpoint import (
    CheckpointIdentityError,
    CheckpointStateError,
    PortfolioCheckpointStore,
)
from research.backtester.portfolio_publication import ARTIFACT_SCHEMA_VERSION
from research.scripts.evaluate_parameter_portfolio import PROTOCOL_VERSION


def _hold_checkpoint_lock(
    root: str,
    ready: ProcessEvent,
    release: ProcessEvent,
) -> None:
    lock_path = Path(root) / ".lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        ready.set()
        if not release.wait(5.0):
            raise TimeoutError("checkpoint lock test was not released")


def _identity(pool: str = "uni-base") -> dict[str, object]:
    return {
        "schema_version": "weighted-portfolio-checkpoint/v1",
        "protocol_version": PROTOCOL_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "pool": pool,
        "catalog_sha256": "a" * 64,
        "input_sha256": {"history": "b" * 64, "features": "c" * 64},
        "window_spec": {"train_swaps": 200, "val_swaps": 50},
        "reference_capital_usd": 1200.0,
    }


def _payload(index: int, cash: float = 1000.0) -> dict[str, object]:
    return {
        "window_index": index,
        "training_metrics": {"candidate": {"net_return": 0.01}},
        "carried_closing_cash_usd": {"equal_config": cash},
    }


def test_checkpoint_resume_requires_a_contiguous_prefix(tmp_path: Path) -> None:
    store = PortfolioCheckpointStore(tmp_path / "run", _identity(), total_windows=3)
    store.initialize()
    store.append_window(0, _payload(0))
    store.append_window(1, _payload(1, 1010.0))

    resumed = PortfolioCheckpointStore(tmp_path / "run", _identity(), total_windows=3)
    resumed.initialize()

    assert resumed.next_window_index() == 2
    assert resumed.load_windows() == (_payload(0), _payload(1, 1010.0))
    assert json.loads((tmp_path / "run" / "progress.json").read_text()) == {
        "completed_windows": 2,
        "next_window_index": 2,
        "phase": "primary",
        "pool": "uni-base",
        "total_windows": 3,
    }


def test_checkpoint_rejects_identity_drift(tmp_path: Path) -> None:
    root = tmp_path / "run"
    PortfolioCheckpointStore(root, _identity(), total_windows=1).initialize()

    changed = _identity()
    changed["reference_capital_usd"] = 450.0
    with pytest.raises(CheckpointIdentityError, match="identity"):
        PortfolioCheckpointStore(root, changed, total_windows=1).initialize()


def test_v3_checkpoint_refuses_a_v2_resume_root(tmp_path: Path) -> None:
    root = tmp_path / "run"
    legacy = _identity()
    legacy["protocol_version"] = "2026-07-23"
    legacy["artifact_schema_version"] = "weighted-portfolio-artifacts/v2"
    PortfolioCheckpointStore(root, legacy, total_windows=1).initialize()

    with pytest.raises(CheckpointIdentityError, match="identity"):
        PortfolioCheckpointStore(root, _identity(), total_windows=1).initialize()


def test_checkpoint_rejects_out_of_order_and_non_finite_cash(tmp_path: Path) -> None:
    store = PortfolioCheckpointStore(tmp_path / "run", _identity(), total_windows=2)
    store.initialize()

    with pytest.raises(CheckpointStateError, match="next contiguous"):
        store.append_window(1, _payload(1))
    with pytest.raises(CheckpointStateError, match="finite non-negative"):
        store.append_window(0, _payload(0, float("nan")))


def test_checkpoint_accepts_zero_carried_cash_but_rejects_negative(
    tmp_path: Path,
) -> None:
    zero = PortfolioCheckpointStore(tmp_path / "zero", _identity(), total_windows=1)
    zero.initialize()
    zero.append_window(0, _payload(0, 0.0))

    assert zero.load_windows() == (_payload(0, 0.0),)

    negative = PortfolioCheckpointStore(
        tmp_path / "negative",
        _identity(),
        total_windows=1,
    )
    negative.initialize()
    with pytest.raises(CheckpointStateError, match="finite non-negative"):
        negative.append_window(0, _payload(0, -1.0))


def test_checkpoint_detects_a_missing_middle_file(tmp_path: Path) -> None:
    root = tmp_path / "run"
    store = PortfolioCheckpointStore(root, _identity(), total_windows=3)
    store.initialize()
    store.append_window(0, _payload(0))
    store.append_window(1, _payload(1))
    store.append_window(2, _payload(2))
    (root / "window-000001.json").unlink()

    with pytest.raises(CheckpointStateError, match="contiguous"):
        store.load_windows()


def test_resumed_and_uninterrupted_checkpoint_bytes_match(tmp_path: Path) -> None:
    first = PortfolioCheckpointStore(tmp_path / "first", _identity(), total_windows=2)
    first.initialize()
    first.append_window(0, _payload(0))
    first.append_window(1, _payload(1))

    second = PortfolioCheckpointStore(tmp_path / "second", _identity(), total_windows=2)
    second.initialize()
    second.append_window(0, _payload(0))
    resumed = PortfolioCheckpointStore(tmp_path / "second", _identity(), total_windows=2)
    resumed.initialize()
    resumed.append_window(1, _payload(1))

    names = ("identity.json", "progress.json", "window-000000.json", "window-000001.json")
    for name in names:
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()


def test_checkpoint_phase_is_identity_bound_and_visible_in_progress(
    tmp_path: Path,
) -> None:
    identity = _identity()
    identity["phase"] = "best_sleeve_removal"
    store = PortfolioCheckpointStore(
        tmp_path / "removal",
        identity,
        total_windows=2,
        phase="best_sleeve_removal",
    )
    store.initialize()

    progress = json.loads((tmp_path / "removal" / "progress.json").read_text())
    assert progress["phase"] == "best_sleeve_removal"

    with pytest.raises(CheckpointIdentityError, match="phase"):
        PortfolioCheckpointStore(
            tmp_path / "removal",
            identity,
            total_windows=2,
            phase="primary",
        ).initialize()


def test_checkpoint_store_excludes_a_concurrent_lock_holder(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_checkpoint_lock,
        args=(str(root), ready, release),
    )
    holder.start()
    assert ready.wait(5.0)

    errors: list[BaseException] = []

    def initialize() -> None:
        try:
            PortfolioCheckpointStore(root, _identity(), total_windows=1).initialize()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    waiter = threading.Thread(target=initialize)
    waiter.start()
    waiter.join(0.1)
    assert waiter.is_alive()

    release.set()
    waiter.join(5.0)
    holder.join(5.0)

    assert not waiter.is_alive()
    assert holder.exitcode == 0
    assert errors == []
    assert (root / "identity.json").is_file()


def test_checkpoint_rejects_noncanonical_window_bytes(tmp_path: Path) -> None:
    root = tmp_path / "run"
    store = PortfolioCheckpointStore(root, _identity(), total_windows=1)
    store.initialize()
    store.append_window(0, _payload(0))
    parsed = json.loads((root / "window-000000.json").read_text())
    (root / "window-000000.json").write_text(json.dumps(parsed, indent=2) + "\n")

    with pytest.raises(CheckpointStateError, match="canonical"):
        store.load_windows()
