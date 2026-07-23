"""Causal walk-forward evaluation of the frozen LP parameter portfolio."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import sys
from collections import defaultdict
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.backtester.data import Event, load_v4_events
from research.backtester.portfolio_allocation import (
    FAMILY_CAP,
    MIN_FEE_COST_RATIO,
    SCORE_CLIP,
    SHRINKAGE_ALPHA,
    SHRINKAGE_ETA,
    SLEEVE_CAP,
)
from research.backtester.portfolio_catalog import (
    PortfolioCatalog,
    SleeveDefinition,
    build_portfolio_catalog,
)
from research.backtester.portfolio_checkpoint import PortfolioCheckpointStore
from research.backtester.portfolio_evaluation import (
    COMPARATOR_IDS,
    PrimaryWindowRecord,
    RemovalWindowRecord,
    assess_candidate_reset_matrix,
    evaluate_primary_window,
    evaluate_removal_window,
    primary_window_from_payload,
    primary_window_to_payload,
    removal_window_from_payload,
    removal_window_to_payload,
    restore_primary_path_states,
    restore_removal_path_states,
    select_best_reset_sleeve,
)
from research.backtester.portfolio_publication import (
    ARTIFACT_SCHEMA_VERSION,
    publish_artifacts,
)
from research.backtester.run import (
    WindowSpec,
    _iter_window_slices,
)
from research.scripts.evaluate_flow_gated_lp import build_entry_states
from research.scripts.evaluate_frozen_family_lp import POOL_EXPERIMENTS, PoolExperiment

PROTOCOL_VERSION = "2026-07-23"
SOURCE_CLOSURE = (
    "engine/math/v3.py",
    "engine/venues/dex/uniswap_base.py",
    "engine/venues/dex/uniswap_bsc.py",
    "research/backtester/clmm_math.py",
    "research/backtester/data.py",
    "research/backtester/entry_eligibility.py",
    "research/backtester/metrics.py",
    "research/backtester/params.py",
    "research/backtester/pbo.py",
    "research/backtester/pool_state.py",
    "research/backtester/portfolio_allocation.py",
    "research/backtester/portfolio_catalog.py",
    "research/backtester/portfolio_checkpoint.py",
    "research/backtester/portfolio_comparators.py",
    "research/backtester/portfolio_errors.py",
    "research/backtester/portfolio_evaluation.py",
    "research/backtester/portfolio_path.py",
    "research/backtester/portfolio_publication.py",
    "research/backtester/portfolio_simulator.py",
    "research/backtester/position_runtime.py",
    "research/backtester/run.py",
    "research/backtester/simulator.py",
    "research/backtester/sizing.py",
    "research/backtester/strategy.py",
    "research/scripts/evaluate_directional_paper_lp.py",
    "research/scripts/evaluate_flow_gated_lp.py",
    "research/scripts/evaluate_frozen_family_lp.py",
    "research/scripts/evaluate_parameter_portfolio.py",
)


def _limit_catalog(catalog: PortfolioCatalog, limit: int | None) -> PortfolioCatalog:
    if limit is None:
        return catalog
    if limit <= 0:
        raise ValueError("catalog limit must be positive")
    by_family: defaultdict[str, list[SleeveDefinition]] = defaultdict(list)
    for sleeve in catalog.sleeves:
        by_family[sleeve.family].append(sleeve)
    selected_ids: set[str] = set()
    for family, family_sleeves in by_family.items():
        if family == "static":
            comparator = [
                sleeve
                for sleeve in family_sleeves
                if sleeve.config_name == "static_spot_w0025"
            ]
            if len(comparator) != 1:
                raise ValueError(
                    "smoke catalog requires exactly one static_spot_w0025"
                )
            ordered = comparator + [
                sleeve for sleeve in family_sleeves if sleeve not in comparator
            ]
        else:
            ordered = family_sleeves
        selected_ids.update(sleeve.sleeve_id for sleeve in ordered[:limit])
    sleeves = [
        sleeve for sleeve in catalog.sleeves if sleeve.sleeve_id in selected_ids
    ]
    policies = catalog.directional_policies[:limit]
    return PortfolioCatalog(catalog.pool, tuple(sleeves), tuple(policies))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a CSV header")
        return [dict(row) for row in reader]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _catalog_sha256(catalog: PortfolioCatalog) -> str:
    canonical_units = []
    for unit in catalog.allocation_units:
        canonical_units.append(
            {
                "economic_id": unit.sleeve_id,
                "family": unit.family,
                "config_name": getattr(unit, "config_name", getattr(unit, "profile", "")),
                "behavioral_fingerprint": (
                    unit.parameter_fingerprint
                    if isinstance(unit, SleeveDefinition)
                    else unit.behavioral_fingerprint
                ),
            }
        )
    return _canonical_sha256(
        {
            "pool": catalog.pool,
            "canonical_units": canonical_units,
            "declarations": [asdict(row) for row in catalog.declarations],
        }
    )


def _run_identity(
    experiment: PoolExperiment,
    catalog: PortfolioCatalog,
    spec: WindowSpec,
    total_windows: int,
    *,
    full_run: bool,
) -> dict[str, object]:
    sources = {
        relative: _sha256_file(REPO_ROOT / relative)
        for relative in SOURCE_CLOSURE
    }
    inputs = {
        "history_csv": _sha256_file(experiment.history_csv),
        "feature_csv": _sha256_file(experiment.feature_csv),
        "qts_feature_csv": _sha256_file(experiment.qts_feature_csv),
    }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "phase": "primary",
        "pool": experiment.pool,
        "run_kind": "full" if full_run else "smoke",
        "inputs": inputs,
        "source_closure": sources,
        "pool_config": asdict(experiment.pool_config),
        "catalog_sha256": _catalog_sha256(catalog),
        "canonical_economic_units": len(catalog.allocation_units),
        "retained_declarations": len(catalog.declarations),
        "window_spec": asdict(spec),
        "total_windows": total_windows,
        "reference_capital_usd": experiment.initial_capital_usd,
        "allocation_constants": {
            "sleeve_cap": SLEEVE_CAP,
            "family_cap": FAMILY_CAP,
            "minimum_fee_cost_ratio": MIN_FEE_COST_RATIO,
            "shrinkage_eta": SHRINKAGE_ETA,
            "shrinkage_alpha": SHRINKAGE_ALPHA,
            "score_clip": SCORE_CLIP,
        },
        "comparator_ids": COMPARATOR_IDS,
        "runtime": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
        },
    }


def _require_checkpoint_mode(root: Path, *, resume: bool) -> None:
    if root.exists() and any(root.iterdir()) and not resume:
        raise FileExistsError(
            f"checkpoint directory already contains state; pass --resume: {root}"
        )


def evaluate_pool(
    experiment: PoolExperiment,
    *,
    out_dir: Path,
    checkpoint_dir: Path,
    max_windows: int | None,
    catalog_limit_per_family: int | None,
    full_run: bool,
    resume: bool,
) -> tuple[PrimaryWindowRecord, ...]:
    if out_dir.exists():
        raise FileExistsError(f"publication directory already exists: {out_dir}")
    catalog = _limit_catalog(
        build_portfolio_catalog(experiment.pool, experiment.initial_capital_usd),
        catalog_limit_per_family,
    )
    feature_rows = _read_csv(experiment.feature_csv)
    qts_rows = _read_csv(experiment.qts_feature_csv)
    entry_states = build_entry_states(
        feature_rows,
        qts_rows=qts_rows,
        train_swaps=experiment.train_swaps,
        val_swaps=experiment.val_swaps,
        stride_swaps=experiment.val_swaps,
        flow_threshold=Decimal("0.90"),
        train_return_max=Decimal("0"),
        max_windows=max_windows,
    )
    states = {int(row["window_index"]): row for row in entry_states}
    events: list[Event] = list(
        load_v4_events(
            str(experiment.history_csv),
            pool_id=experiment.pool_config.pool_address,
        )
    )
    spec = WindowSpec(
        mode="swap_count",
        train_swaps=experiment.train_swaps,
        val_swaps=experiment.val_swaps,
        stride_swaps=experiment.val_swaps,
        min_train_swaps=experiment.train_swaps,
        min_train_liquidity_events=0,
        min_val_swaps=experiment.val_swaps,
    )
    slices = tuple(
        item
        for item in _iter_window_slices(events, spec, max_windows=max_windows)
        if item.skipped_reason is None
    )
    if not slices:
        raise ValueError(f"{experiment.pool} produced no complete evaluation windows")
    if [item.window.index for item in slices] != list(range(len(slices))):
        raise ValueError("completed evaluation windows are not a contiguous prefix")
    if set(states) != {item.window.index for item in slices}:
        raise ValueError("entry-state windows do not match evaluation windows")

    identity = _run_identity(
        experiment,
        catalog,
        spec,
        len(slices),
        full_run=full_run,
    )
    primary_root = checkpoint_dir / "primary"
    _require_checkpoint_mode(primary_root, resume=resume)
    primary_store = PortfolioCheckpointStore(
        primary_root,
        identity,
        total_windows=len(slices),
        phase="primary",
    )
    primary_store.initialize()
    records = [
        primary_window_from_payload(payload)
        for payload in primary_store.load_windows()
    ]
    path_states = restore_primary_path_states(
        records,
        experiment.initial_capital_usd,
    )
    for item in slices[len(records) :]:
        def report_unit_progress(
            phase: str,
            completed_units: int,
            total_units: int,
        ) -> None:
            print(
                f"{experiment.pool} primary window "
                f"{item.window.index + 1}/{len(slices)} {phase} "
                f"{completed_units}/{total_units} units evaluated",
                file=sys.stderr,
                flush=True,
            )

        record = evaluate_primary_window(
            catalog=catalog,
            window_slice=item,
            entry_state=states[item.window.index],
            pool_config=experiment.pool_config,
            reference_bankroll_usd=experiment.initial_capital_usd,
            path_states=path_states,
            progress_callback=report_unit_progress,
        )
        primary_store.append_window(
            item.window.index,
            primary_window_to_payload(record),
        )
        records.append(record)
        print(
            f"{experiment.pool} primary {len(records)}/{len(slices)} windows durable",
            file=sys.stderr,
            flush=True,
        )
    if len(records) != len(slices):
        raise ValueError("primary checkpoint did not reach the complete window count")

    removal_root = checkpoint_dir / "best_sleeve_removal"
    expected_economic_ids = tuple(
        unit.sleeve_id for unit in catalog.allocation_units
    )
    candidate_matrix = assess_candidate_reset_matrix(
        records,
        expected_economic_ids,
    )
    removal_records: list[RemovalWindowRecord] = []
    if candidate_matrix.status == "complete_valid":
        removed_economic_id = select_best_reset_sleeve(
            records,
            expected_economic_ids,
        )
        removal_identity = {
            "protocol_version": PROTOCOL_VERSION,
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "phase": "best_sleeve_removal",
            "pool": experiment.pool,
            "primary_identity_sha256": primary_store.identity_sha256,
            "removed_economic_id": removed_economic_id,
            "total_windows": len(slices),
        }
        _require_checkpoint_mode(removal_root, resume=resume)
        removal_store = PortfolioCheckpointStore(
            removal_root,
            removal_identity,
            total_windows=len(slices),
            phase="best_sleeve_removal",
        )
        removal_store.initialize()
        removal_records.extend(
            removal_window_from_payload(payload)
            for payload in removal_store.load_windows()
        )
        removal_states = restore_removal_path_states(
            removal_records,
            experiment.initial_capital_usd,
        )
        for item in slices[len(removal_records) :]:
            removal_record = evaluate_removal_window(
                catalog=catalog,
                primary_record=records[item.window.index],
                window_slice=item,
                entry_state=states[item.window.index],
                pool_config=experiment.pool_config,
                removed_economic_id=removed_economic_id,
                path_states=removal_states,
            )
            removal_store.append_window(
                item.window.index,
                removal_window_to_payload(removal_record),
            )
            removal_records.append(removal_record)
            print(
                f"{experiment.pool} removal "
                f"{len(removal_records)}/{len(slices)} windows durable",
                file=sys.stderr,
                flush=True,
            )
        if len(removal_records) != len(slices):
            raise ValueError(
                "removal checkpoint did not reach the complete window count"
            )
    elif removal_root.exists() and any(removal_root.iterdir()):
        raise ValueError(
            "invalid candidate matrix conflicts with an existing removal checkpoint"
        )
    publication_run_kind = (
        "smoke"
        if not full_run
        else "completed_amended_protocol_run"
        if candidate_matrix.status == "complete_valid"
        else "completed_primary_invalid_candidate_matrix"
    )
    publish_artifacts(
        out_dir,
        catalog=catalog,
        records=records,
        removal_records=removal_records,
        run_identity={
            **primary_store.identity,
            "identity_sha256": primary_store.identity_sha256,
        },
        run_kind=publication_run_kind,
    )
    return tuple(records)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", choices=["uni-base", "uni-bsc", "all"], default="all")
    parser.add_argument(
        "--out-dir", type=Path, default=Path("research/results/parameter_portfolio")
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=Path("research/results/parameter_portfolio_checkpoints"),
    )
    parser.add_argument("--max-windows", type=int)
    parser.add_argument(
        "--catalog-limit-per-family",
        type=int,
        help="Smoke-test only; forbidden when --full-run is set",
    )
    parser.add_argument("--full-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser


def validate_cli_args(args: argparse.Namespace) -> None:
    if args.full_run and (
        args.max_windows is not None or args.catalog_limit_per_family is not None
    ):
        raise ValueError("full run forbids truncation flags")
    if args.max_windows is not None and args.max_windows <= 0:
        raise ValueError("max windows must be positive")
    if args.catalog_limit_per_family is not None and args.catalog_limit_per_family <= 0:
        raise ValueError("catalog limit must be positive")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_cli_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    pools = POOL_EXPERIMENTS if args.pool == "all" else (args.pool,)
    for pool in pools:
        evaluate_pool(
            POOL_EXPERIMENTS[pool],
            out_dir=args.out_dir / pool.replace("-", "_"),
            checkpoint_dir=args.checkpoint_dir / pool.replace("-", "_"),
            max_windows=args.max_windows,
            catalog_limit_per_family=args.catalog_limit_per_family,
            full_run=args.full_run,
            resume=args.resume,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
