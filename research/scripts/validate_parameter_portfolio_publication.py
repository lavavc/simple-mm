"""Validate frozen weighted-portfolio publications without modifying evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn, cast

from research.backtester.pbo import compute_pbo
from research.backtester.portfolio_allocation import (
    FAMILY_CAP,
    MIN_FEE_COST_RATIO,
    SCORE_CLIP,
    SHRINKAGE_ALPHA,
    SHRINKAGE_ETA,
    SLEEVE_CAP,
    TrainingMetrics,
    equal_config_weights,
    equal_family_weights,
    is_eligible,
    select_rank_one,
    shrinkage_weights,
)
from research.backtester.portfolio_catalog import (
    ALLOCATION_FAMILY_ORDER,
    PortfolioCatalog,
    build_portfolio_catalog,
)
from research.backtester.portfolio_evaluation import (
    ALLOCATION_RULE_IDS,
    COMPARATOR_IDS,
)
from research.backtester.portfolio_publication import (
    ARTIFACT_SCHEMA_VERSION,
    CSV_FIELDS,
    ECONOMIC_FIELDS,
    REQUIRED_ARTIFACTS,
)
from research.backtester.portfolio_simulator import AGGREGATE_LIQUIDITY_SHARE_CAP
from research.backtester.run import WindowSpec
from research.scripts.evaluate_frozen_family_lp import POOL_EXPERIMENTS
from research.scripts.evaluate_parameter_portfolio import _catalog_sha256

FROZEN_SOURCE_COMMIT = "b331b432bf612ed21413d54a0fd6c0eb76b7c38f"
PROTOCOL_VERSION = "2026-07-24"
FROZEN_SOURCE_CLOSURE = (
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


@dataclass(frozen=True)
class PoolDimensions:
    windows: int
    canonical_units: int = 4_895
    declarations: int = 4_935


FULL_DIMENSIONS = {
    "uni-base": PoolDimensions(windows=26),
    "uni-bsc": PoolDimensions(windows=37),
}

POOL_DIRECTORIES = {
    "uni-base": "uni_base",
    "uni-bsc": "uni_bsc",
}
INVALID_PATH_STATUSES = {
    "invalid_liquidity_cap",
    "invalid_no_validation_swap",
    "invalid_execution_accounting",
    "invalid_terminal_liquidation",
    "invalid_opening_capital",
}
CANDIDATE_INVALID_STATUSES = {
    "invalid_no_validation_swap",
    "invalid_execution_accounting",
    "invalid_terminal_liquidation",
}
TRAINING_STATUSES = {
    "valid",
    "no_position",
    *CANDIDATE_INVALID_STATUSES,
}
PATH_STATUSES = {"valid", "blocked_prior_invalid", *INVALID_PATH_STATUSES}


@dataclass(frozen=True)
class WeightSummary:
    deployed_weight: float
    cash_weight: float
    herfindahl: float
    largest_weight: float
    effective_sleeve_count: float
    removed_weight: float | None
    weights: Mapping[str, float]


@dataclass
class CsvEvidence:
    catalog_ids: tuple[str, ...]
    window_bounds: dict[int, tuple[str, str]]
    candidate_returns: dict[str, list[float]]
    candidate_failures: list[dict[str, object]]
    reset_returns: dict[str, list[float]]
    reset_failures: list[dict[str, object]]
    carried_rows: dict[tuple[str, int], dict[str, str]]
    comparator_rows: dict[tuple[str, int], dict[str, str]]
    weight_summaries: dict[tuple[int, str], WeightSummary]
    removal_all_valid: bool


@dataclass(frozen=True)
class ValidationReport:
    pool: str
    windows: int
    artifacts: int
    manifest_sha256: str
    evidence_status: str


_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ValidationFailure(ValueError):  # noqa: N818
    """A privacy-safe evidence failure suitable for terminal output."""

    def __init__(
        self,
        code: str,
        *,
        pool: str | None = None,
        artifact: str | None = None,
    ) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("validation code is not safe")
        if pool is not None and pool not in FULL_DIMENSIONS:
            raise ValueError("validation pool is not safe")
        if artifact is not None and artifact not in REQUIRED_ARTIFACTS:
            raise ValueError("validation artifact is not safe")
        parts = [f"FAIL code={code}"]
        if pool is not None:
            parts.append(f"pool={pool}")
        if artifact is not None:
            parts.append(f"artifact={artifact}")
        super().__init__(" ".join(parts))
        self.code = code
        self.pool = pool
        self.artifact = artifact


def canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValidationFailure("JSON_DUPLICATE_KEY")
        payload[key] = value
    return payload


def _reject_nonfinite_json(_: str) -> NoReturn:
    raise ValidationFailure("JSON_NONFINITE")


def load_json_strict(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(
                handle,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_json,
            )
    except ValidationFailure:
        raise
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise ValidationFailure("JSON_PARSE") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationFailure("ARTIFACT_READ") from exc
    return digest.hexdigest()


def iter_csv_rows(
    path: Path,
    expected_header: tuple[str, ...],
) -> Iterator[dict[str, str]]:
    if not path.is_file() or path.is_symlink():
        raise ValidationFailure("CSV_SCHEMA")
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None or tuple(header) != expected_header:
                raise ValidationFailure("CSV_SCHEMA")
            for cells in reader:
                if len(cells) != len(expected_header):
                    raise ValidationFailure("CSV_SCHEMA")
                yield dict(zip(expected_header, cells, strict=True))
    except ValidationFailure:
        raise
    except (csv.Error, OSError, UnicodeError) as exc:
        raise ValidationFailure("CSV_SCHEMA") from exc


def validate_artifact_set(directory: Path, *, pool: str) -> None:
    if not directory.is_dir() or directory.is_symlink():
        raise ValidationFailure("ARTIFACT_SET", pool=pool)
    try:
        children = tuple(directory.iterdir())
    except OSError as exc:
        raise ValidationFailure("ARTIFACT_SET", pool=pool) from exc
    if {path.name for path in children} != set(REQUIRED_ARTIFACTS):
        raise ValidationFailure("ARTIFACT_SET", pool=pool)
    if any(not path.is_file() or path.is_symlink() for path in children):
        raise ValidationFailure("ARTIFACT_SET", pool=pool)


def _as_mapping(
    value: object,
    code: str,
    *,
    pool: str | None = None,
    artifact: str | None = None,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationFailure(code, pool=pool, artifact=artifact)
    if not all(isinstance(key, str) for key in value):
        raise ValidationFailure(code, pool=pool, artifact=artifact)
    return value


def _as_int(value: object, code: str, *, pool: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailure(code, pool=pool)
    return value


def validate_artifact_metadata(
    directory: Path,
    manifest: Mapping[str, object],
    *,
    pool: str,
) -> None:
    artifacts = _as_mapping(manifest.get("artifacts"), "MANIFEST_ARTIFACTS", pool=pool)
    expected_names = set(REQUIRED_ARTIFACTS) - {"run_manifest.json"}
    if set(artifacts) != expected_names:
        raise ValidationFailure("MANIFEST_ARTIFACTS", pool=pool)
    for name in (*CSV_FIELDS, "pbo_allocation_rules.json", "summary.md"):
        metadata = _as_mapping(
            artifacts.get(name),
            "MANIFEST_ARTIFACTS",
            pool=pool,
            artifact=name,
        )
        if set(metadata) != {"sha256", "size_bytes", "row_count"}:
            raise ValidationFailure(
                "MANIFEST_ARTIFACTS",
                pool=pool,
                artifact=name,
            )
        expected_sha = metadata.get("sha256")
        expected_size = metadata.get("size_bytes")
        expected_rows = metadata.get("row_count")
        if (
            not isinstance(expected_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise ValidationFailure(
                "MANIFEST_ARTIFACTS",
                pool=pool,
                artifact=name,
            )
        path = directory / name
        try:
            observed_size = path.stat().st_size
        except OSError as exc:
            raise ValidationFailure(
                "ARTIFACT_READ",
                pool=pool,
                artifact=name,
            ) from exc
        if observed_size != expected_size or sha256_file(path) != expected_sha:
            raise ValidationFailure("ARTIFACT_HASH", pool=pool, artifact=name)
        if name in CSV_FIELDS:
            if (
                isinstance(expected_rows, bool)
                or not isinstance(expected_rows, int)
                or expected_rows < 0
            ):
                raise ValidationFailure(
                    "MANIFEST_ARTIFACTS",
                    pool=pool,
                    artifact=name,
                )
            observed_rows = sum(1 for _ in iter_csv_rows(path, CSV_FIELDS[name]))
            if observed_rows != expected_rows:
                raise ValidationFailure("ARTIFACT_ROWS", pool=pool, artifact=name)
        elif expected_rows is not None:
            raise ValidationFailure(
                "MANIFEST_ARTIFACTS",
                pool=pool,
                artifact=name,
            )


def validate_manifest_shape(manifest: Mapping[str, object], *, pool: str) -> None:
    dimensions = FULL_DIMENSIONS[pool]
    expected_keys = {
        "schema_version",
        "protocol_version",
        "run_kind",
        "publication_status",
        "pool",
        "completed_windows",
        "canonical_economic_units",
        "retained_declarations",
        "primary_run_identity",
        "primary_phase",
        "candidate_reset_matrix",
        "removal_phase",
        "best_sleeve_removal_economic_id",
        "pbo_status",
        "artifacts",
    }
    if set(manifest) != expected_keys:
        raise ValidationFailure("MANIFEST_SCHEMA", pool=pool)
    if (
        manifest.get("schema_version") != ARTIFACT_SCHEMA_VERSION
        or manifest.get("protocol_version") != PROTOCOL_VERSION
        or manifest.get("pool") != pool
        or manifest.get("completed_windows") != dimensions.windows
        or manifest.get("canonical_economic_units") != dimensions.canonical_units
        or manifest.get("retained_declarations") != dimensions.declarations
    ):
        raise ValidationFailure("MANIFEST_IDENTITY", pool=pool)

    identity = _as_mapping(
        manifest.get("primary_run_identity"),
        "MANIFEST_IDENTITY",
        pool=pool,
    )
    try:
        validate_identity_hash(identity)
    except ValidationFailure as exc:
        raise ValidationFailure("MANIFEST_IDENTITY", pool=pool) from exc
    if (
        identity.get("protocol_version") != PROTOCOL_VERSION
        or identity.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION
        or identity.get("phase") != "primary"
        or identity.get("pool") != pool
        or identity.get("run_kind") != "full"
        or identity.get("total_windows") != dimensions.windows
    ):
        raise ValidationFailure("MANIFEST_IDENTITY", pool=pool)

    primary_phase = _as_mapping(
        manifest.get("primary_phase"),
        "MANIFEST_GATE",
        pool=pool,
    )
    if set(primary_phase) != {
        "status",
        "completed_windows",
        "expected_windows",
    } or primary_phase != {
        "status": "completed",
        "completed_windows": dimensions.windows,
        "expected_windows": dimensions.windows,
    }:
        raise ValidationFailure("MANIFEST_GATE", pool=pool)

    candidate = _as_mapping(
        manifest.get("candidate_reset_matrix"),
        "MANIFEST_GATE",
        pool=pool,
    )
    removal = _as_mapping(
        manifest.get("removal_phase"),
        "MANIFEST_GATE",
        pool=pool,
    )
    pbo = _as_mapping(manifest.get("pbo_status"), "MANIFEST_GATE", pool=pool)
    if (
        set(candidate)
        != {
            "status",
            "expected_rows",
            "observed_rows",
            "valid_rows",
            "invalid_rows",
            "invalid_status_counts",
        }
        or set(removal)
        != {
            "status",
            "completed_windows",
            "expected_windows",
            "removed_economic_id",
        }
        or set(pbo) != {"candidate_sleeves", "allocation_rules", "carried_paths"}
    ):
        raise ValidationFailure("MANIFEST_SCHEMA", pool=pool)
    expected_candidate_rows = dimensions.windows * dimensions.canonical_units
    common_candidate = {
        "expected_rows": expected_candidate_rows,
        "observed_rows": expected_candidate_rows,
    }
    if any(candidate.get(key) != value for key, value in common_candidate.items()):
        raise ValidationFailure("MANIFEST_GATE", pool=pool)
    if pbo.get("carried_paths") != ("not_applicable_path_dependent_carried_bankroll"):
        raise ValidationFailure("MANIFEST_GATE", pool=pool)

    candidate_status = candidate.get("status")
    if candidate_status == "complete_valid":
        if (
            manifest.get("run_kind") != "completed_amended_protocol_run"
            or manifest.get("publication_status") != "completed"
            or candidate.get("valid_rows") != expected_candidate_rows
            or candidate.get("invalid_rows") != 0
            or candidate.get("invalid_status_counts") != {}
            or removal.get("status") != "completed"
            or removal.get("completed_windows") != dimensions.windows
            or removal.get("expected_windows") != dimensions.windows
            or not isinstance(removal.get("removed_economic_id"), str)
            or not removal.get("removed_economic_id")
            or manifest.get("best_sleeve_removal_economic_id") != removal.get("removed_economic_id")
            or pbo.get("candidate_sleeves") != "computed"
            or pbo.get("allocation_rules") not in {"computed", "invalid_incomplete_matrix"}
        ):
            raise ValidationFailure("MANIFEST_GATE", pool=pool)
    elif candidate_status == "invalid_incomplete_matrix":
        invalid_rows = _as_int(candidate.get("invalid_rows"), "MANIFEST_GATE", pool=pool)
        valid_rows = _as_int(candidate.get("valid_rows"), "MANIFEST_GATE", pool=pool)
        invalid_counts = _as_mapping(
            candidate.get("invalid_status_counts"),
            "MANIFEST_GATE",
            pool=pool,
        )
        if (
            manifest.get("run_kind") != "completed_primary_invalid_candidate_matrix"
            or manifest.get("publication_status") != "completed_with_invalid_candidate_reset_matrix"
            or invalid_rows <= 0
            or valid_rows + invalid_rows != expected_candidate_rows
            or sum(_as_int(value, "MANIFEST_GATE", pool=pool) for value in invalid_counts.values())
            != invalid_rows
            or removal
            != {
                "status": "not_run_incomplete_candidate_reset_matrix",
                "completed_windows": 0,
                "expected_windows": dimensions.windows,
                "removed_economic_id": None,
            }
            or manifest.get("best_sleeve_removal_economic_id") is not None
            or pbo.get("candidate_sleeves") != "invalid_incomplete_matrix"
            or pbo.get("allocation_rules") not in {"computed", "invalid_incomplete_matrix"}
        ):
            raise ValidationFailure("MANIFEST_GATE", pool=pool)
    else:
        raise ValidationFailure("MANIFEST_GATE", pool=pool)


def _git_blob(repo_root: Path, relative: str) -> bytes:
    try:
        return subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "show",
                f"{FROZEN_SOURCE_COMMIT}:{relative}",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValidationFailure("SOURCE_COMMIT") from exc


def validate_source_closure(
    repo_root: Path,
    source_hashes: Mapping[str, object],
    *,
    pool: str,
) -> None:
    if set(source_hashes) != set(FROZEN_SOURCE_CLOSURE):
        raise ValidationFailure("SOURCE_CLOSURE", pool=pool)
    for relative in FROZEN_SOURCE_CLOSURE:
        expected = source_hashes.get(relative)
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ValidationFailure("SOURCE_CLOSURE", pool=pool)
        commit_hash = hashlib.sha256(_git_blob(repo_root, relative)).hexdigest()
        try:
            worktree_hash = sha256_file(repo_root / relative)
        except ValidationFailure as exc:
            raise ValidationFailure("SOURCE_CLOSURE", pool=pool) from exc
        if expected != commit_hash or worktree_hash != commit_hash:
            raise ValidationFailure("SOURCE_CLOSURE", pool=pool)


def validate_run_identity(
    identity: Mapping[str, object],
    *,
    repo_root: Path,
    pool: str,
) -> None:
    dimensions = FULL_DIMENSIONS[pool]
    expected_keys = {
        "protocol_version",
        "artifact_schema_version",
        "phase",
        "pool",
        "run_kind",
        "inputs",
        "source_closure",
        "pool_config",
        "catalog_sha256",
        "canonical_economic_units",
        "retained_declarations",
        "window_spec",
        "total_windows",
        "reference_capital_usd",
        "allocation_constants",
        "comparator_ids",
        "runtime",
        "identity_sha256",
    }
    if set(identity) != expected_keys:
        raise ValidationFailure("MANIFEST_IDENTITY", pool=pool)
    validate_identity_hash(identity)
    if (
        identity.get("protocol_version") != PROTOCOL_VERSION
        or identity.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION
        or identity.get("phase") != "primary"
        or identity.get("pool") != pool
        or identity.get("run_kind") != "full"
        or identity.get("total_windows") != dimensions.windows
        or identity.get("canonical_economic_units") != dimensions.canonical_units
        or identity.get("retained_declarations") != dimensions.declarations
    ):
        raise ValidationFailure("MANIFEST_IDENTITY", pool=pool)

    source_closure = _as_mapping(
        identity.get("source_closure"),
        "SOURCE_CLOSURE",
        pool=pool,
    )
    validate_source_closure(repo_root, source_closure, pool=pool)

    experiment = POOL_EXPERIMENTS[pool]
    inputs = _as_mapping(identity.get("inputs"), "INPUT_IDENTITY", pool=pool)
    expected_inputs = {
        "history_csv": sha256_file(repo_root / experiment.history_csv),
        "feature_csv": sha256_file(repo_root / experiment.feature_csv),
        "qts_feature_csv": sha256_file(repo_root / experiment.qts_feature_csv),
    }
    if inputs != expected_inputs:
        raise ValidationFailure("INPUT_IDENTITY", pool=pool)

    catalog = build_portfolio_catalog(pool, experiment.initial_capital_usd)
    if (
        identity.get("pool_config") != asdict(experiment.pool_config)
        or identity.get("catalog_sha256") != _catalog_sha256(catalog)
        or len(catalog.allocation_units) != dimensions.canonical_units
        or len(catalog.declarations) != dimensions.declarations
        or identity.get("reference_capital_usd") != experiment.initial_capital_usd
    ):
        raise ValidationFailure("CATALOG_IDENTITY", pool=pool)

    expected_spec = asdict(
        WindowSpec(
            mode="swap_count",
            train_swaps=experiment.train_swaps,
            val_swaps=experiment.val_swaps,
            stride_swaps=experiment.val_swaps,
            min_train_swaps=experiment.train_swaps,
            min_train_liquidity_events=0,
            min_val_swaps=experiment.val_swaps,
        )
    )
    expected_constants = {
        "sleeve_cap": SLEEVE_CAP,
        "family_cap": FAMILY_CAP,
        "minimum_fee_cost_ratio": MIN_FEE_COST_RATIO,
        "shrinkage_eta": SHRINKAGE_ETA,
        "shrinkage_alpha": SHRINKAGE_ALPHA,
        "score_clip": list(SCORE_CLIP),
    }
    if (
        identity.get("window_spec") != expected_spec
        or identity.get("allocation_constants") != expected_constants
        or identity.get("comparator_ids") != list(COMPARATOR_IDS)
    ):
        raise ValidationFailure("METHOD_IDENTITY", pool=pool)

    runtime = _as_mapping(
        identity.get("runtime"),
        "RUNTIME_IDENTITY",
        pool=pool,
    )
    if set(runtime) != {"python_implementation", "python_version"} or any(
        not isinstance(value, str) or not value for value in runtime.values()
    ):
        raise ValidationFailure("RUNTIME_IDENTITY", pool=pool)


def validate_identity_hash(identity: Mapping[str, object]) -> None:
    expected = identity.get("identity_sha256")
    if not isinstance(expected, str):
        raise ValidationFailure("IDENTITY_HASH")
    unhashed = dict(identity)
    del unhashed["identity_sha256"]
    observed = hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    if observed != expected:
        raise ValidationFailure("IDENTITY_HASH")


def _finite_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValidationFailure("ECONOMIC_VALUE") from exc
    if not math.isfinite(parsed):
        raise ValidationFailure("ECONOMIC_VALUE")
    return parsed


def validate_economic_row(row: Mapping[str, str], *, status: str) -> None:
    cells = {field: row.get(field, "") for field in ECONOMIC_FIELDS}
    if status != "valid":
        if any(cells.values()):
            raise ValidationFailure("INVALID_ECONOMICS")
        return
    if any(value == "" for value in cells.values()):
        raise ValidationFailure("ECONOMIC_VALUE")

    count_fields = (
        "entry_action_batch_count",
        "scaled_entry_action_batch_count",
        "terminal_position_settlement_count",
        "terminal_loose_cngn_settlement_count",
        "terminal_zero_settlement_count",
        "terminal_inventory_swap_count",
        "value_sample_count",
    )
    counts: dict[str, int] = {}
    for field in count_fields:
        value = cells[field]
        if re.fullmatch(r"0|[1-9][0-9]*", value) is None:
            raise ValidationFailure("ECONOMIC_VALUE")
        counts[field] = int(value)
    if counts["value_sample_count"] < 2:
        raise ValidationFailure("ECONOMIC_VALUE")
    numeric = {
        field: _finite_float(value) for field, value in cells.items() if field not in count_fields
    }

    opening = numeric["opening_capital_usd"]
    closing = numeric["closing_cash_usd"]
    if opening <= 0.0 or closing < 0.0:
        raise ValidationFailure("ECONOMIC_VALUE")
    expected_return = closing / opening - 1.0
    if not math.isclose(
        numeric["window_net_return"],
        expected_return,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValidationFailure("ECONOMIC_RECONCILIATION")
    drawdown = numeric["max_drawdown"]
    if drawdown < -1.0 or drawdown > 0.0:
        raise ValidationFailure("ECONOMIC_VALUE")
    nonnegative = (
        "terminal_liquidation_cost_usd",
        "total_fees_usd",
        "total_fixed_cost_usd",
        "total_variable_cost_usd",
        "external_marked_notional_usd",
        "external_input_value_usd",
        "external_output_value_usd",
        "internal_cross_notional_usd",
        "terminal_fixed_cost_usd",
        "terminal_variable_cost_usd",
        "terminal_external_marked_notional_usd",
    )
    if any(numeric[field] < 0.0 for field in nonnegative):
        raise ValidationFailure("ECONOMIC_VALUE")
    minimum_scale = numeric["minimum_entry_execution_scale"]
    entry_count = counts["entry_action_batch_count"]
    scaled_count = counts["scaled_entry_action_batch_count"]
    inventory_count = counts["terminal_inventory_swap_count"]
    if (
        scaled_count > entry_count
        or not 0.0 <= minimum_scale <= 1.0
        or not (minimum_scale * 256).is_integer()
        or inventory_count > 1
        or (entry_count == 0 and minimum_scale != 1.0)
        or (scaled_count == 0 and minimum_scale != 1.0)
        or (scaled_count > 0 and minimum_scale >= 1.0)
    ):
        raise ValidationFailure("ECONOMIC_VALUE")
    external_input = numeric["external_input_value_usd"]
    external_output = numeric["external_output_value_usd"]
    variable_cost = numeric["total_variable_cost_usd"]
    tolerance = 1e-9 * max(1.0, external_input, external_output, variable_cost)
    if abs(external_input - external_output - variable_cost) > tolerance:
        raise ValidationFailure("ECONOMIC_RECONCILIATION")
    terminal_cost = numeric["terminal_liquidation_cost_usd"]
    total_execution_cost = numeric["total_fixed_cost_usd"] + variable_cost
    terminal_fixed = numeric["terminal_fixed_cost_usd"]
    terminal_variable = numeric["terminal_variable_cost_usd"]
    terminal_notional = numeric["terminal_external_marked_notional_usd"]
    terminal_tolerance = 1e-9 * max(
        1.0,
        terminal_cost,
        terminal_fixed,
        terminal_variable,
        terminal_notional,
    )
    if (
        terminal_cost - total_execution_cost > tolerance
        or abs(terminal_fixed + terminal_variable - terminal_cost) > terminal_tolerance
        or terminal_fixed - numeric["total_fixed_cost_usd"] > terminal_tolerance
        or terminal_variable - variable_cost > terminal_tolerance
        or terminal_notional - numeric["external_marked_notional_usd"] > terminal_tolerance
        or (inventory_count == 0 and (terminal_notional != 0.0 or terminal_variable != 0.0))
        or (inventory_count == 1 and terminal_notional <= 0.0)
    ):
        raise ValidationFailure("ECONOMIC_RECONCILIATION")


def validate_summary(
    directory: Path,
    manifest: Mapping[str, object],
    *,
    pool: str,
) -> None:
    pbo_status = _as_mapping(
        manifest.get("pbo_status"),
        "SUMMARY",
        pool=pool,
        artifact="summary.md",
    )
    expected = "\n".join(
        (
            f"# Corrected weighted portfolio: {pool}",
            "",
            f"- run kind: `{manifest.get('run_kind')}`",
            f"- completed windows: {manifest.get('completed_windows')}",
            f"- canonical economic units: {manifest.get('canonical_economic_units')}",
            f"- retained declarations: {manifest.get('retained_declarations')}",
            f"- candidate reset PBO status: `{pbo_status.get('candidate_sleeves')}`",
            f"- allocation reset PBO status: `{pbo_status.get('allocation_rules')}`",
            "- carried paths: sequential boundary-settled USD capital",
            "- conclusions: diagnostic only; no live-promotion authorization",
            "",
        )
    ).encode()
    try:
        observed = (directory / "summary.md").read_bytes()
    except OSError as exc:
        raise ValidationFailure(
            "SUMMARY",
            pool=pool,
            artifact="summary.md",
        ) from exc
    if observed != expected:
        raise ValidationFailure(
            "SUMMARY",
            pool=pool,
            artifact="summary.md",
        )


def _parse_nonnegative_int(
    value: str,
    *,
    code: str,
    pool: str,
    artifact: str,
) -> int:
    if re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise ValidationFailure(code, pool=pool, artifact=artifact)
    return int(value)


def _parse_finite_float(
    value: str,
    *,
    code: str,
    pool: str,
    artifact: str,
) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValidationFailure(code, pool=pool, artifact=artifact) from exc
    if not math.isfinite(parsed):
        raise ValidationFailure(code, pool=pool, artifact=artifact)
    return parsed


def _close(left: float, right: float, *, scale: float = 1.0) -> bool:
    return math.isclose(
        left,
        right,
        rel_tol=1e-9,
        abs_tol=1e-10 * max(1.0, scale),
    )


def _validate_failure_diagnostic(
    row: Mapping[str, str],
    *,
    status: str,
    absent_statuses: set[str],
    code: str,
    pool: str,
    artifact: str,
    prefix: str = "",
) -> tuple[str, str] | None:
    failure_type = row[f"{prefix}failure_type"]
    failure_reason = row[f"{prefix}failure_reason"]
    if status in absent_statuses:
        if failure_type or failure_reason:
            raise ValidationFailure(code, pool=pool, artifact=artifact)
        return None
    if not failure_type or not failure_reason or len(failure_reason) > 512:
        raise ValidationFailure(code, pool=pool, artifact=artifact)
    return failure_type, failure_reason


def _register_window(
    row: Mapping[str, str],
    *,
    pool: str,
    artifact: str,
    bounds: dict[int, tuple[str, str]],
) -> int:
    if row["schema_version"] != ARTIFACT_SCHEMA_VERSION or row["pool"] != pool:
        raise ValidationFailure("ROW_IDENTITY", pool=pool, artifact=artifact)
    index = _parse_nonnegative_int(
        row["window_index"],
        code="ROW_IDENTITY",
        pool=pool,
        artifact=artifact,
    )
    if index >= FULL_DIMENSIONS[pool].windows:
        raise ValidationFailure("ROW_IDENTITY", pool=pool, artifact=artifact)
    start = row["window_start"]
    end = row["window_end"]
    if not start or not end or start >= end:
        raise ValidationFailure("ROW_IDENTITY", pool=pool, artifact=artifact)
    observed = (start, end)
    prior = bounds.setdefault(index, observed)
    if prior != observed:
        raise ValidationFailure("ROW_IDENTITY", pool=pool, artifact=artifact)
    return index


def _economic_values(
    row: Mapping[str, str],
    *,
    status: str,
    pool: str,
    artifact: str,
) -> dict[str, float] | None:
    try:
        validate_economic_row(row, status=status)
    except ValidationFailure as exc:
        raise ValidationFailure(exc.code, pool=pool, artifact=artifact) from exc
    if status != "valid":
        return None
    return {field: float(row[field]) for field in ECONOMIC_FIELDS if field != "value_sample_count"}


def _validate_catalog(
    directory: Path,
    *,
    pool: str,
) -> tuple[PortfolioCatalog, dict[str, str], tuple[str, ...]]:
    artifact = "configuration_catalog.csv"
    catalog = build_portfolio_catalog(
        pool,
        POOL_EXPERIMENTS[pool].initial_capital_usd,
    )
    units = {unit.sleeve_id: unit for unit in catalog.allocation_units}
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    for declaration in catalog.declarations:
        try:
            row = next(rows)
        except StopIteration as exc:
            raise ValidationFailure(
                "CATALOG_ROWS",
                pool=pool,
                artifact=artifact,
            ) from exc
        unit = units[declaration.economic_id]
        expected = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "pool": pool,
            "economic_id": declaration.economic_id,
            "declaration_kind": declaration.kind,
            "allocation_family": unit.family,
            "declared_family": declaration.family,
            "config_name": declaration.config_name,
            "source_constructor": declaration.source_constructor,
            "behavioral_fingerprint": declaration.behavioral_fingerprint,
        }
        if row != expected:
            raise ValidationFailure(
                "CATALOG_ROWS",
                pool=pool,
                artifact=artifact,
            )
    try:
        next(rows)
    except StopIteration:
        pass
    else:
        raise ValidationFailure("CATALOG_ROWS", pool=pool, artifact=artifact)
    return (
        catalog,
        {key: unit.family for key, unit in units.items()},
        tuple(units),
    )


def _validate_training_rows(
    directory: Path,
    *,
    pool: str,
    catalog_ids: tuple[str, ...],
    families: Mapping[str, str],
    bounds: dict[int, tuple[str, str]],
) -> tuple[
    dict[int, set[str]],
    dict[int, dict[str, TrainingMetrics]],
    dict[tuple[int, str], str],
]:
    artifact = "training_eligibility.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    eligible: dict[int, set[str]] = {index: set() for index in range(FULL_DIMENSIONS[pool].windows)}
    metrics: dict[int, dict[str, TrainingMetrics]] = {
        index: {} for index in range(FULL_DIMENSIONS[pool].windows)
    }
    routed_names: dict[tuple[int, str], str] = {}
    for index in range(FULL_DIMENSIONS[pool].windows):
        for economic_id in catalog_ids:
            try:
                row = next(rows)
            except StopIteration as exc:
                raise ValidationFailure(
                    "TRAINING_ROWS",
                    pool=pool,
                    artifact=artifact,
                ) from exc
            if (
                _register_window(
                    row,
                    pool=pool,
                    artifact=artifact,
                    bounds=bounds,
                )
                != index
                or row["economic_id"] != economic_id
                or row["family"] != families[economic_id]
                or not row["routed_config_name"]
                or row["status"] not in TRAINING_STATUSES
                or row["eligible"] not in {"true", "false"}
            ):
                raise ValidationFailure(
                    "TRAINING_ROWS",
                    pool=pool,
                    artifact=artifact,
                )
            status = row["status"]
            _validate_failure_diagnostic(
                row,
                status=status,
                absent_statuses={"valid", "no_position"},
                code="TRAINING_STATUS",
                pool=pool,
                artifact=artifact,
            )
            routed_names[(index, economic_id)] = row["routed_config_name"]
            metric_fields = (
                "net_return",
                "episode_count",
                "fee_to_transaction_cost_ratio",
                "max_drawdown",
            )
            if status != "valid":
                if row["eligible"] != "false" or any(row[field] for field in metric_fields):
                    raise ValidationFailure(
                        "TRAINING_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
                if status == "no_position" and (
                    families[economic_id] != "directional"
                    or row["routed_config_name"] != "no_position"
                ):
                    raise ValidationFailure(
                        "TRAINING_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
                continue
            if any(not row[field] for field in metric_fields):
                raise ValidationFailure(
                    "TRAINING_STATUS",
                    pool=pool,
                    artifact=artifact,
                )
            net_return = _parse_finite_float(
                row["net_return"],
                code="TRAINING_STATUS",
                pool=pool,
                artifact=artifact,
            )
            episodes = _parse_nonnegative_int(
                row["episode_count"],
                code="TRAINING_STATUS",
                pool=pool,
                artifact=artifact,
            )
            ratio = _parse_finite_float(
                row["fee_to_transaction_cost_ratio"],
                code="TRAINING_STATUS",
                pool=pool,
                artifact=artifact,
            )
            drawdown = _parse_finite_float(
                row["max_drawdown"],
                code="TRAINING_STATUS",
                pool=pool,
                artifact=artifact,
            )
            if episodes < 0 or ratio < 0.0 or not -1.0 <= drawdown <= 0.0:
                raise ValidationFailure(
                    "TRAINING_STATUS",
                    pool=pool,
                    artifact=artifact,
                )
            training = TrainingMetrics(
                net_return=net_return,
                episode_count=episodes,
                fee_to_transaction_cost_ratio=ratio,
                max_drawdown=drawdown,
            )
            metrics[index][economic_id] = training
            expected_eligible = is_eligible(training)
            if (row["eligible"] == "true") != expected_eligible:
                raise ValidationFailure(
                    "TRAINING_ELIGIBILITY",
                    pool=pool,
                    artifact=artifact,
                )
            if expected_eligible:
                eligible[index].add(economic_id)
    try:
        next(rows)
    except StopIteration:
        return eligible, metrics, routed_names
    raise ValidationFailure("TRAINING_ROWS", pool=pool, artifact=artifact)


def _validate_weight_rows(
    directory: Path,
    *,
    pool: str,
    catalog: PortfolioCatalog,
    catalog_ids: tuple[str, ...],
    families: Mapping[str, str],
    eligible: Mapping[int, set[str]],
    training_metrics: Mapping[int, Mapping[str, TrainingMetrics]],
    removed_economic_id: str | None,
    bounds: dict[int, tuple[str, str]],
) -> dict[tuple[int, str], WeightSummary]:
    artifact = "window_weights.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    summaries: dict[tuple[int, str], WeightSummary] = {}
    constructors = {
        "equal_config": equal_config_weights,
        "equal_family": equal_family_weights,
        "shrinkage": shrinkage_weights,
    }
    for index in range(FULL_DIMENSIONS[pool].windows):
        for rule in ALLOCATION_RULE_IDS:
            expected_allocation = constructors[rule](
                catalog.allocation_units,
                training_metrics[index],
            )
            weight_sum = 0.0
            squares = 0.0
            largest = 0.0
            family_weights: dict[str, float] = {family: 0.0 for family in ALLOCATION_FAMILY_ORDER}
            observed_deployed: float | None = None
            observed_cash: float | None = None
            removed_weight: float | None = None
            observed_weights: dict[str, float] = {}
            for economic_id in catalog_ids:
                try:
                    row = next(rows)
                except StopIteration as exc:
                    raise ValidationFailure(
                        "WEIGHT_ROWS",
                        pool=pool,
                        artifact=artifact,
                    ) from exc
                if (
                    _register_window(
                        row,
                        pool=pool,
                        artifact=artifact,
                        bounds=bounds,
                    )
                    != index
                    or row["allocation_rule"] != rule
                    or row["economic_id"] != economic_id
                    or row["family"] != families[economic_id]
                ):
                    raise ValidationFailure(
                        "WEIGHT_ROWS",
                        pool=pool,
                        artifact=artifact,
                    )
                weight = _parse_finite_float(
                    row["weight"],
                    code="WEIGHT_VALUE",
                    pool=pool,
                    artifact=artifact,
                )
                deployed = _parse_finite_float(
                    row["deployed_weight"],
                    code="WEIGHT_VALUE",
                    pool=pool,
                    artifact=artifact,
                )
                cash = _parse_finite_float(
                    row["cash_weight"],
                    code="WEIGHT_VALUE",
                    pool=pool,
                    artifact=artifact,
                )
                if (
                    weight < 0.0
                    or weight > SLEEVE_CAP + 1e-12
                    or (weight > 0.0 and economic_id not in eligible[index])
                    or not math.isclose(
                        weight,
                        expected_allocation.weights.get(economic_id, 0.0),
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    )
                ):
                    raise ValidationFailure(
                        "WEIGHT_CONSTRAINT",
                        pool=pool,
                        artifact=artifact,
                    )
                if observed_deployed is None:
                    observed_deployed = deployed
                    observed_cash = cash
                elif not _close(deployed, observed_deployed) or not _close(
                    cash,
                    observed_cash if observed_cash is not None else math.nan,
                ):
                    raise ValidationFailure(
                        "WEIGHT_CONSTRAINT",
                        pool=pool,
                        artifact=artifact,
                    )
                weight_sum += weight
                observed_weights[economic_id] = weight
                squares += weight * weight
                largest = max(largest, weight)
                family_weights[families[economic_id]] += weight
                if economic_id == removed_economic_id:
                    removed_weight = weight
            assert observed_deployed is not None and observed_cash is not None
            if (
                observed_deployed < 0.0
                or observed_cash < 0.0
                or not _close(weight_sum, observed_deployed)
                or not _close(observed_deployed + observed_cash, 1.0)
                or not math.isclose(
                    observed_deployed,
                    sum(expected_allocation.weights.values()),
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                or not math.isclose(
                    observed_cash,
                    expected_allocation.cash_weight,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                or any(weight > FAMILY_CAP + 1e-12 for weight in family_weights.values())
            ):
                raise ValidationFailure(
                    "WEIGHT_CONSTRAINT",
                    pool=pool,
                    artifact=artifact,
                )
            summaries[(index, rule)] = WeightSummary(
                deployed_weight=observed_deployed,
                cash_weight=observed_cash,
                herfindahl=squares,
                largest_weight=largest,
                effective_sleeve_count=1.0 / squares if squares > 0.0 else 0.0,
                removed_weight=removed_weight,
                weights=observed_weights,
            )
    try:
        next(rows)
    except StopIteration:
        return summaries
    raise ValidationFailure("WEIGHT_ROWS", pool=pool, artifact=artifact)


def _validate_candidate_rows(
    directory: Path,
    *,
    pool: str,
    catalog_ids: tuple[str, ...],
    families: Mapping[str, str],
    routed_names: Mapping[tuple[int, str], str],
    bounds: dict[int, tuple[str, str]],
) -> tuple[dict[str, list[float]], list[dict[str, object]]]:
    artifact = "sleeve_validation_matrix.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    returns: dict[str, list[float]] = {economic_id: [] for economic_id in catalog_ids}
    failures: list[dict[str, object]] = []
    for index in range(FULL_DIMENSIONS[pool].windows):
        for economic_id in catalog_ids:
            try:
                row = next(rows)
            except StopIteration as exc:
                raise ValidationFailure(
                    "CANDIDATE_ROWS",
                    pool=pool,
                    artifact=artifact,
                ) from exc
            status = row["status"]
            route_status = row["route_status"]
            if (
                _register_window(
                    row,
                    pool=pool,
                    artifact=artifact,
                    bounds=bounds,
                )
                != index
                or row["economic_id"] != economic_id
                or row["family"] != families[economic_id]
                or not row["routed_config_name"]
                or row["routed_config_name"] != routed_names[(index, economic_id)]
                or route_status not in {"routed", "no_position"}
                or status not in {"valid", *CANDIDATE_INVALID_STATUSES}
                or (route_status == "no_position" and status != "valid")
                or (route_status == "routed" and row["routed_config_name"] == "no_position")
            ):
                raise ValidationFailure(
                    "CANDIDATE_ROWS",
                    pool=pool,
                    artifact=artifact,
                )
            economics = _economic_values(
                row,
                status=status,
                pool=pool,
                artifact=artifact,
            )
            failure = _validate_failure_diagnostic(
                row,
                status=status,
                absent_statuses={"valid"},
                code="CANDIDATE_STATUS",
                pool=pool,
                artifact=artifact,
            )
            if status == "valid":
                episode_count = _parse_nonnegative_int(
                    row["episode_count"],
                    code="CANDIDATE_STATUS",
                    pool=pool,
                    artifact=artifact,
                )
                assert economics is not None
                returns[economic_id].append(economics["window_net_return"])
                if route_status == "no_position":
                    zero_fields = (
                        "window_net_return",
                        "max_drawdown",
                        "terminal_liquidation_cost_usd",
                        "total_fees_usd",
                        "total_fixed_cost_usd",
                        "total_variable_cost_usd",
                        "external_marked_notional_usd",
                        "external_input_value_usd",
                        "external_output_value_usd",
                        "internal_cross_notional_usd",
                        "entry_action_batch_count",
                        "scaled_entry_action_batch_count",
                        "terminal_position_settlement_count",
                        "terminal_loose_cngn_settlement_count",
                        "terminal_zero_settlement_count",
                        "terminal_inventory_swap_count",
                        "terminal_fixed_cost_usd",
                        "terminal_variable_cost_usd",
                        "terminal_external_marked_notional_usd",
                    )
                    if (
                        families[economic_id] != "directional"
                        or row["routed_config_name"] != "no_position"
                        or episode_count != 0
                        or not _close(
                            economics["opening_capital_usd"],
                            economics["closing_cash_usd"],
                        )
                        or any(economics[field] != 0.0 for field in zero_fields)
                        or economics["minimum_entry_execution_scale"] != 1.0
                    ):
                        raise ValidationFailure(
                            "CANDIDATE_STATUS",
                            pool=pool,
                            artifact=artifact,
                        )
                elif row["routed_config_name"] == "no_position":
                    raise ValidationFailure(
                        "CANDIDATE_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
            else:
                if row["episode_count"]:
                    raise ValidationFailure(
                        "CANDIDATE_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
                if failure is None:
                    raise ValidationFailure(
                        "CANDIDATE_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
                failures.append(
                    {
                        "economic_id": economic_id,
                        "window_index": index,
                        "status": status,
                        "failure": {
                            "exception_type": failure[0],
                            "reason": failure[1],
                        },
                    }
                )
    try:
        next(rows)
    except StopIteration:
        return returns, failures
    raise ValidationFailure("CANDIDATE_ROWS", pool=pool, artifact=artifact)


def _validate_reset_rows(
    directory: Path,
    *,
    pool: str,
    weights: Mapping[tuple[int, str], WeightSummary],
    bounds: dict[int, tuple[str, str]],
) -> tuple[dict[str, list[float]], list[dict[str, object]]]:
    artifact = "reset_portfolio_validation_matrix.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    returns: dict[str, list[float]] = {rule: [] for rule in ALLOCATION_RULE_IDS}
    failures: list[dict[str, object]] = []
    for index in range(FULL_DIMENSIONS[pool].windows):
        for rule in ALLOCATION_RULE_IDS:
            try:
                row = next(rows)
            except StopIteration as exc:
                raise ValidationFailure(
                    "RESET_ROWS",
                    pool=pool,
                    artifact=artifact,
                ) from exc
            status = row["status"]
            if (
                _register_window(
                    row,
                    pool=pool,
                    artifact=artifact,
                    bounds=bounds,
                )
                != index
                or row["allocation_rule"] != rule
                or status
                not in {
                    "valid",
                    "invalid_liquidity_cap",
                    "invalid_no_validation_swap",
                    "invalid_execution_accounting",
                    "invalid_terminal_liquidation",
                }
            ):
                raise ValidationFailure(
                    "RESET_ROWS",
                    pool=pool,
                    artifact=artifact,
                )
            summary = weights[(index, rule)]
            deployed = _parse_finite_float(
                row["deployed_weight"],
                code="RESET_STATUS",
                pool=pool,
                artifact=artifact,
            )
            cash = _parse_finite_float(
                row["cash_weight"],
                code="RESET_STATUS",
                pool=pool,
                artifact=artifact,
            )
            if not _close(deployed, summary.deployed_weight) or not _close(
                cash,
                summary.cash_weight,
            ):
                raise ValidationFailure(
                    "RESET_STATUS",
                    pool=pool,
                    artifact=artifact,
                )
            economics = _economic_values(
                row,
                status=status,
                pool=pool,
                artifact=artifact,
            )
            failure = _validate_failure_diagnostic(
                row,
                status=status,
                absent_statuses={"valid"},
                code="RESET_STATUS",
                pool=pool,
                artifact=artifact,
            )
            if status == "valid":
                if row["observed_share"] or row["cap"]:
                    raise ValidationFailure(
                        "RESET_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
                assert economics is not None
                returns[rule].append(economics["window_net_return"])
            else:
                assert failure is not None
                failures.append(
                    {
                        "allocation_rule": rule,
                        "window_index": index,
                        "status": status,
                        "failure": {
                            "exception_type": failure[0],
                            "reason": failure[1],
                        },
                    }
                )
                if status == "invalid_liquidity_cap":
                    share = _parse_finite_float(
                        row["observed_share"],
                        code="RESET_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
                    cap = _parse_finite_float(
                        row["cap"],
                        code="RESET_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
                    if share <= cap or not _close(cap, AGGREGATE_LIQUIDITY_SHARE_CAP):
                        raise ValidationFailure(
                            "RESET_STATUS",
                            pool=pool,
                            artifact=artifact,
                        )
                elif row["observed_share"] or row["cap"]:
                    raise ValidationFailure(
                        "RESET_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
    try:
        next(rows)
    except StopIteration:
        return returns, failures
    raise ValidationFailure("RESET_ROWS", pool=pool, artifact=artifact)


def _validate_path_status(
    row: Mapping[str, str],
    *,
    method_id: str,
    index: int,
    next_capital: dict[str, float],
    blocked: dict[str, tuple[str, int, str, str]],
    pool: str,
    artifact: str,
) -> dict[str, float] | None:
    status = row["status"]
    if status not in PATH_STATUSES:
        raise ValidationFailure("PATH_STATUS", pool=pool, artifact=artifact)
    economics = _economic_values(
        row,
        status=status,
        pool=pool,
        artifact=artifact,
    )
    failure = _validate_failure_diagnostic(
        row,
        status=status,
        absent_statuses={"valid"},
        code="PATH_STATUS",
        pool=pool,
        artifact=artifact,
    )
    prior_block = blocked.get(method_id)
    if prior_block is None:
        if status == "blocked_prior_invalid":
            raise ValidationFailure("PATH_STATUS", pool=pool, artifact=artifact)
        if status == "valid":
            if row["blocking_status"] or row["blocking_window_index"]:
                raise ValidationFailure("PATH_STATUS", pool=pool, artifact=artifact)
            assert economics is not None
            if not _close(
                economics["opening_capital_usd"],
                next_capital[method_id],
                scale=next_capital[method_id],
            ):
                raise ValidationFailure(
                    "PATH_CONTINUITY",
                    pool=pool,
                    artifact=artifact,
                )
            next_capital[method_id] = economics["closing_cash_usd"]
            return economics
        if status not in INVALID_PATH_STATUSES:
            raise ValidationFailure("PATH_STATUS", pool=pool, artifact=artifact)
        if row["blocking_status"] != status or row["blocking_window_index"] != str(index):
            raise ValidationFailure("PATH_STATUS", pool=pool, artifact=artifact)
        assert failure is not None
        blocked[method_id] = (status, index, failure[0], failure[1])
        return None

    if (
        status != "blocked_prior_invalid"
        or row["blocking_status"] != prior_block[0]
        or row["blocking_window_index"] != str(prior_block[1])
        or failure != prior_block[2:]
    ):
        raise ValidationFailure("PATH_STATUS", pool=pool, artifact=artifact)
    return None


def _validate_carried_rows(
    directory: Path,
    *,
    pool: str,
    bounds: dict[int, tuple[str, str]],
) -> dict[tuple[str, int], dict[str, str]]:
    artifact = "carried_portfolio_path.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    initial_capital = POOL_EXPERIMENTS[pool].initial_capital_usd
    next_capital = {rule: initial_capital for rule in ALLOCATION_RULE_IDS}
    blocked: dict[str, tuple[str, int, str, str]] = {}
    observed: dict[tuple[str, int], dict[str, str]] = {}
    for index in range(FULL_DIMENSIONS[pool].windows):
        for rule in ALLOCATION_RULE_IDS:
            try:
                row = next(rows)
            except StopIteration as exc:
                raise ValidationFailure(
                    "CARRIED_ROWS",
                    pool=pool,
                    artifact=artifact,
                ) from exc
            if (
                _register_window(
                    row,
                    pool=pool,
                    artifact=artifact,
                    bounds=bounds,
                )
                != index
                or row["method_id"] != rule
                or row["method_kind"] != "allocation_rule"
            ):
                raise ValidationFailure(
                    "CARRIED_ROWS",
                    pool=pool,
                    artifact=artifact,
                )
            _validate_path_status(
                row,
                method_id=rule,
                index=index,
                next_capital=next_capital,
                blocked=blocked,
                pool=pool,
                artifact=artifact,
            )
            observed[(rule, index)] = row
    try:
        next(rows)
    except StopIteration:
        return observed
    raise ValidationFailure("CARRIED_ROWS", pool=pool, artifact=artifact)


def _validate_comparator_rows(
    directory: Path,
    *,
    pool: str,
    catalog: PortfolioCatalog,
    catalog_ids: tuple[str, ...],
    training_metrics: Mapping[int, Mapping[str, TrainingMetrics]],
    bounds: dict[int, tuple[str, str]],
) -> dict[tuple[str, int], dict[str, str]]:
    artifact = "comparators.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    initial_capital = POOL_EXPERIMENTS[pool].initial_capital_usd
    next_capital = {comparator: initial_capital for comparator in COMPARATOR_IDS}
    blocked: dict[str, tuple[str, int, str, str]] = {}
    observed: dict[tuple[str, int], dict[str, str]] = {}
    catalog_set = set(catalog_ids)
    static_units = tuple(
        unit
        for unit in catalog.allocation_units
        if unit.family == "static" and getattr(unit, "config_name", None) == "static_spot_w0025"
    )
    if len(static_units) != 1:
        raise ValidationFailure("COMPARATOR_PLAN", pool=pool, artifact=artifact)
    rank_one_families = {
        "ewma_rank_one": "ewma",
        "paper_exclusive_rank_one": "paper_exclusive",
        "frozen_rank_one": "frozen",
        "directional_rank_one": "directional",
    }
    for index in range(FULL_DIMENSIONS[pool].windows):
        for comparator in COMPARATOR_IDS:
            try:
                row = next(rows)
            except StopIteration as exc:
                raise ValidationFailure(
                    "COMPARATOR_ROWS",
                    pool=pool,
                    artifact=artifact,
                ) from exc
            selection = row["selection_status"]
            selected = row["selected_economic_id"]
            if comparator in {"cash", "hold_cngn_mark", "hold_cngn_pool_routed"}:
                expected_selection = "not_applicable"
                expected_selected = ""
            elif comparator == "static_spot_w0025":
                expected_selection = "predeclared"
                expected_selected = static_units[0].sleeve_id
            else:
                family = rank_one_families[comparator]
                selected_unit = select_rank_one(
                    tuple(unit for unit in catalog.allocation_units if unit.family == family),
                    training_metrics[index],
                )
                expected_selection = "selected" if selected_unit is not None else "no_eligible"
                expected_selected = selected_unit.sleeve_id if selected_unit is not None else ""
            if (
                _register_window(
                    row,
                    pool=pool,
                    artifact=artifact,
                    bounds=bounds,
                )
                != index
                or row["comparator_id"] != comparator
                or selection not in {"not_applicable", "predeclared", "selected", "no_eligible"}
                or (selected and selected not in catalog_set)
                or selection != expected_selection
                or selected != expected_selected
                or (selection in {"not_applicable", "no_eligible"} and selected)
                or (selection in {"predeclared", "selected"} and not selected)
            ):
                raise ValidationFailure(
                    "COMPARATOR_ROWS",
                    pool=pool,
                    artifact=artifact,
                )
            _validate_path_status(
                row,
                method_id=comparator,
                index=index,
                next_capital=next_capital,
                blocked=blocked,
                pool=pool,
                artifact=artifact,
            )
            observed[(comparator, index)] = row
    try:
        next(rows)
    except StopIteration:
        return observed
    raise ValidationFailure("COMPARATOR_ROWS", pool=pool, artifact=artifact)


ATTRIBUTION_FIELDS = (
    "opening_value_usd",
    "closing_value_usd",
    "pnl_usd",
    "portfolio_return_contribution",
    "total_fees_usd",
    "total_fixed_cost_usd",
    "total_variable_cost_usd",
    "external_marked_notional_usd",
    "external_input_value_usd",
    "external_output_value_usd",
    "internal_cross_notional_usd",
    "terminal_funding_transfer_usd",
)


def _attribution_values(
    row: Mapping[str, str],
    *,
    pool: str,
    artifact: str,
) -> dict[str, float]:
    values = {
        field: _parse_finite_float(
            row[field],
            code="ATTRIBUTION_VALUE",
            pool=pool,
            artifact=artifact,
        )
        for field in ATTRIBUTION_FIELDS
    }
    if not _close(
        values["pnl_usd"],
        values["closing_value_usd"] - values["opening_value_usd"],
        scale=max(
            abs(values["opening_value_usd"]),
            abs(values["closing_value_usd"]),
        ),
    ):
        raise ValidationFailure(
            "ATTRIBUTION_RECONCILIATION",
            pool=pool,
            artifact=artifact,
        )
    nonnegative = (
        "opening_value_usd",
        "closing_value_usd",
        "total_fees_usd",
        "total_fixed_cost_usd",
        "total_variable_cost_usd",
        "external_marked_notional_usd",
        "external_input_value_usd",
        "external_output_value_usd",
        "internal_cross_notional_usd",
    )
    if any(values[field] < 0.0 for field in nonnegative):
        raise ValidationFailure(
            "ATTRIBUTION_VALUE",
            pool=pool,
            artifact=artifact,
        )
    tolerance = 1e-9 * max(
        1.0,
        values["external_input_value_usd"],
        values["external_output_value_usd"],
        values["total_variable_cost_usd"],
    )
    if (
        abs(
            values["external_input_value_usd"]
            - values["external_output_value_usd"]
            - values["total_variable_cost_usd"]
        )
        > tolerance
    ):
        raise ValidationFailure(
            "ATTRIBUTION_RECONCILIATION",
            pool=pool,
            artifact=artifact,
        )
    return values


def _sum_fields(
    target: dict[str, float],
    values: Mapping[str, float],
) -> None:
    for field in ATTRIBUTION_FIELDS:
        target[field] += values[field]


def _require_attribution_close(
    observed: Mapping[str, float],
    expected: Mapping[str, float],
    *,
    pool: str,
    artifact: str,
) -> None:
    for field in ATTRIBUTION_FIELDS:
        scale = max(1.0, abs(observed[field]), abs(expected[field]))
        if not math.isclose(
            observed[field],
            expected[field],
            rel_tol=1e-8,
            abs_tol=1e-8 * scale,
        ):
            raise ValidationFailure(
                "ATTRIBUTION_RECONCILIATION",
                pool=pool,
                artifact=artifact,
            )


def _validate_attribution_rows(
    directory: Path,
    *,
    pool: str,
    catalog_ids: tuple[str, ...],
    families: Mapping[str, str],
    carried: Mapping[tuple[str, int], Mapping[str, str]],
    weights: Mapping[tuple[int, str], WeightSummary],
    bounds: dict[int, tuple[str, str]],
) -> None:
    artifact = "joint_attribution.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    family_names = tuple(
        family for family in ALLOCATION_FAMILY_ORDER if family in set(families.values())
    )
    for index in range(FULL_DIMENSIONS[pool].windows):
        for rule in ALLOCATION_RULE_IDS:
            carried_row = carried[(rule, index)]
            if carried_row["status"] != "valid":
                continue
            portfolio_opening = float(carried_row["opening_capital_usd"])
            allocation = weights[(index, rule)]
            total = {field: 0.0 for field in ATTRIBUTION_FIELDS}
            family_totals: dict[str, dict[str, float]] = {
                family: {field: 0.0 for field in ATTRIBUTION_FIELDS} for family in family_names
            }
            for economic_id in catalog_ids:
                try:
                    row = next(rows)
                except StopIteration as exc:
                    raise ValidationFailure(
                        "ATTRIBUTION_ROWS",
                        pool=pool,
                        artifact=artifact,
                    ) from exc
                family = families[economic_id]
                if (
                    _register_window(
                        row,
                        pool=pool,
                        artifact=artifact,
                        bounds=bounds,
                    )
                    != index
                    or row["allocation_rule"] != rule
                    or row["row_type"] != "sleeve"
                    or row["economic_id"] != economic_id
                    or row["family"] != family
                ):
                    raise ValidationFailure(
                        "ATTRIBUTION_ROWS",
                        pool=pool,
                        artifact=artifact,
                    )
                values = _attribution_values(row, pool=pool, artifact=artifact)
                expected_opening = portfolio_opening * allocation.weights[economic_id]
                if not math.isclose(
                    values["opening_value_usd"],
                    expected_opening,
                    rel_tol=1e-8,
                    abs_tol=1e-8 * max(1.0, abs(expected_opening)),
                ):
                    raise ValidationFailure(
                        "ATTRIBUTION_ALLOCATION",
                        pool=pool,
                        artifact=artifact,
                    )
                if not _close(
                    values["portfolio_return_contribution"],
                    values["pnl_usd"] / portfolio_opening,
                ):
                    raise ValidationFailure(
                        "ATTRIBUTION_RECONCILIATION",
                        pool=pool,
                        artifact=artifact,
                    )
                _sum_fields(total, values)
                _sum_fields(family_totals[family], values)
            for family in family_names:
                try:
                    row = next(rows)
                except StopIteration as exc:
                    raise ValidationFailure(
                        "ATTRIBUTION_ROWS",
                        pool=pool,
                        artifact=artifact,
                    ) from exc
                if (
                    _register_window(
                        row,
                        pool=pool,
                        artifact=artifact,
                        bounds=bounds,
                    )
                    != index
                    or row["allocation_rule"] != rule
                    or row["row_type"] != "family"
                    or row["economic_id"] != f"family:{family}"
                    or row["family"] != family
                ):
                    raise ValidationFailure(
                        "ATTRIBUTION_ROWS",
                        pool=pool,
                        artifact=artifact,
                    )
                values = _attribution_values(row, pool=pool, artifact=artifact)
                _require_attribution_close(
                    values,
                    family_totals[family],
                    pool=pool,
                    artifact=artifact,
                )
            try:
                cash_row = next(rows)
            except StopIteration as exc:
                raise ValidationFailure(
                    "ATTRIBUTION_ROWS",
                    pool=pool,
                    artifact=artifact,
                ) from exc
            if (
                _register_window(
                    cash_row,
                    pool=pool,
                    artifact=artifact,
                    bounds=bounds,
                )
                != index
                or cash_row["allocation_rule"] != rule
                or cash_row["row_type"] != "cash"
                or cash_row["economic_id"] != "cash"
                or cash_row["family"] != "cash"
            ):
                raise ValidationFailure(
                    "ATTRIBUTION_ROWS",
                    pool=pool,
                    artifact=artifact,
                )
            cash_values = _attribution_values(
                cash_row,
                pool=pool,
                artifact=artifact,
            )
            expected_cash = portfolio_opening * allocation.cash_weight
            cash_zero_fields = tuple(
                field
                for field in ATTRIBUTION_FIELDS
                if field
                not in {
                    "opening_value_usd",
                    "closing_value_usd",
                    "pnl_usd",
                    "portfolio_return_contribution",
                    "terminal_funding_transfer_usd",
                }
            )
            if (
                not math.isclose(
                    cash_values["opening_value_usd"],
                    expected_cash,
                    rel_tol=1e-8,
                    abs_tol=1e-8 * max(1.0, abs(expected_cash)),
                )
                or not math.isclose(
                    cash_values["closing_value_usd"],
                    expected_cash + cash_values["terminal_funding_transfer_usd"],
                    rel_tol=1e-8,
                    abs_tol=1e-8 * max(1.0, abs(expected_cash)),
                )
                or not _close(
                    cash_values["portfolio_return_contribution"],
                    cash_values["pnl_usd"] / portfolio_opening,
                )
                or any(cash_values[field] != 0.0 for field in cash_zero_fields)
            ):
                raise ValidationFailure(
                    "ATTRIBUTION_ALLOCATION",
                    pool=pool,
                    artifact=artifact,
                )
            _sum_fields(total, cash_values)
            carried_expected = {
                "opening_value_usd": float(carried_row["opening_capital_usd"]),
                "closing_value_usd": float(carried_row["closing_cash_usd"]),
                "pnl_usd": float(carried_row["closing_cash_usd"])
                - float(carried_row["opening_capital_usd"]),
                "portfolio_return_contribution": float(carried_row["window_net_return"]),
                "total_fees_usd": float(carried_row["total_fees_usd"]),
                "total_fixed_cost_usd": float(carried_row["total_fixed_cost_usd"]),
                "total_variable_cost_usd": float(carried_row["total_variable_cost_usd"]),
                "external_marked_notional_usd": float(carried_row["external_marked_notional_usd"]),
                "external_input_value_usd": float(carried_row["external_input_value_usd"]),
                "external_output_value_usd": float(carried_row["external_output_value_usd"]),
                "internal_cross_notional_usd": float(carried_row["internal_cross_notional_usd"]),
                "terminal_funding_transfer_usd": 0.0,
            }
            _require_attribution_close(
                total,
                carried_expected,
                pool=pool,
                artifact=artifact,
            )
    try:
        next(rows)
    except StopIteration:
        return
    raise ValidationFailure("ATTRIBUTION_ROWS", pool=pool, artifact=artifact)


def _validate_removal_status(
    row: Mapping[str, str],
    *,
    rule: str,
    index: int,
    next_capital: dict[str, float],
    blocked: dict[str, tuple[str, int, str, str]],
    pool: str,
    artifact: str,
) -> None:
    status = row["removal_status"]
    economic_fields = (
        "removal_opening_capital_usd",
        "removal_closing_cash_usd",
        "removal_window_net_return",
        "removal_max_drawdown",
    )
    failure = _validate_failure_diagnostic(
        row,
        status=status,
        absent_statuses={"valid"},
        code="REMOVAL_STATUS",
        pool=pool,
        artifact=artifact,
        prefix="removal_",
    )
    prior = blocked.get(rule)
    if prior is None and status == "valid":
        if row["removal_blocking_status"] or row["removal_blocking_window_index"]:
            raise ValidationFailure("REMOVAL_STATUS", pool=pool, artifact=artifact)
        values = [
            _parse_finite_float(
                row[field],
                code="REMOVAL_STATUS",
                pool=pool,
                artifact=artifact,
            )
            for field in economic_fields
        ]
        opening, closing, window_return, drawdown = values
        if (
            opening <= 0.0
            or closing < 0.0
            or not -1.0 <= drawdown <= 0.0
            or not _close(opening, next_capital[rule], scale=next_capital[rule])
            or not _close(window_return, closing / opening - 1.0)
        ):
            raise ValidationFailure(
                "REMOVAL_CONTINUITY",
                pool=pool,
                artifact=artifact,
            )
        next_capital[rule] = closing
        return
    if any(row[field] for field in economic_fields):
        raise ValidationFailure("REMOVAL_STATUS", pool=pool, artifact=artifact)
    if prior is None:
        if status not in INVALID_PATH_STATUSES:
            raise ValidationFailure("REMOVAL_STATUS", pool=pool, artifact=artifact)
        if row["removal_blocking_status"] != status or row["removal_blocking_window_index"] != str(
            index
        ):
            raise ValidationFailure("REMOVAL_STATUS", pool=pool, artifact=artifact)
        assert failure is not None
        blocked[rule] = (status, index, failure[0], failure[1])
        return
    if (
        status != "blocked_prior_invalid"
        or row["removal_blocking_status"] != prior[0]
        or row["removal_blocking_window_index"] != str(prior[1])
        or failure != prior[2:]
    ):
        raise ValidationFailure("REMOVAL_STATUS", pool=pool, artifact=artifact)


def _validate_concentration_rows(
    directory: Path,
    manifest: Mapping[str, object],
    *,
    pool: str,
    weights: Mapping[tuple[int, str], WeightSummary],
    carried: Mapping[tuple[str, int], Mapping[str, str]],
    bounds: dict[int, tuple[str, str]],
) -> bool:
    artifact = "concentration_and_contribution.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    candidate = _as_mapping(
        manifest["candidate_reset_matrix"],
        "MANIFEST_GATE",
        pool=pool,
    )
    complete = candidate.get("status") == "complete_valid"
    removal_all_valid = complete
    removed = manifest["best_sleeve_removal_economic_id"]
    next_capital = {
        rule: POOL_EXPERIMENTS[pool].initial_capital_usd for rule in ALLOCATION_RULE_IDS
    }
    blocked: dict[str, tuple[str, int, str, str]] = {}
    for index in range(FULL_DIMENSIONS[pool].windows):
        for rule in ALLOCATION_RULE_IDS:
            try:
                row = next(rows)
            except StopIteration as exc:
                raise ValidationFailure(
                    "CONCENTRATION_ROWS",
                    pool=pool,
                    artifact=artifact,
                ) from exc
            summary = weights[(index, rule)]
            if (
                _register_window(
                    row,
                    pool=pool,
                    artifact=artifact,
                    bounds=bounds,
                )
                != index
                or row["allocation_rule"] != rule
                or row["status"] != carried[(rule, index)]["status"]
                or row["failure_type"] != carried[(rule, index)]["failure_type"]
                or row["failure_reason"] != carried[(rule, index)]["failure_reason"]
            ):
                raise ValidationFailure(
                    "CONCENTRATION_ROWS",
                    pool=pool,
                    artifact=artifact,
                )
            comparisons = {
                "deployed_weight": summary.deployed_weight,
                "cash_weight": summary.cash_weight,
                "herfindahl": summary.herfindahl,
                "largest_weight": summary.largest_weight,
                "effective_sleeve_count": summary.effective_sleeve_count,
            }
            if any(
                not _close(
                    _parse_finite_float(
                        row[field],
                        code="CONCENTRATION_VALUE",
                        pool=pool,
                        artifact=artifact,
                    ),
                    expected,
                )
                for field, expected in comparisons.items()
            ):
                raise ValidationFailure(
                    "CONCENTRATION_VALUE",
                    pool=pool,
                    artifact=artifact,
                )
            if complete:
                if (
                    not isinstance(removed, str)
                    or not removed
                    or row["best_sleeve_removed_economic_id"] != removed
                    or summary.removed_weight is None
                    or not _close(
                        _parse_finite_float(
                            row["best_sleeve_removed_weight"],
                            code="REMOVAL_STATUS",
                            pool=pool,
                            artifact=artifact,
                        ),
                        summary.removed_weight,
                    )
                ):
                    raise ValidationFailure(
                        "REMOVAL_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
                _validate_removal_status(
                    row,
                    rule=rule,
                    index=index,
                    next_capital=next_capital,
                    blocked=blocked,
                    pool=pool,
                    artifact=artifact,
                )
                removal_all_valid = removal_all_valid and row["removal_status"] == "valid"
            else:
                expected_blank = (
                    "best_sleeve_removed_economic_id",
                    "best_sleeve_removed_weight",
                    "removal_blocking_status",
                    "removal_blocking_window_index",
                    "removal_failure_type",
                    "removal_failure_reason",
                    "removal_opening_capital_usd",
                    "removal_closing_cash_usd",
                    "removal_window_net_return",
                    "removal_max_drawdown",
                )
                if row["removal_status"] != "not_run_incomplete_candidate_reset_matrix" or any(
                    row[field] for field in expected_blank
                ):
                    raise ValidationFailure(
                        "REMOVAL_STATUS",
                        pool=pool,
                        artifact=artifact,
                    )
    try:
        next(rows)
    except StopIteration:
        return removal_all_valid
    raise ValidationFailure("CONCENTRATION_ROWS", pool=pool, artifact=artifact)


def _load_canonical_json(
    path: Path,
    *,
    code: str,
    pool: str,
    artifact: str,
) -> Mapping[str, object]:
    payload = _as_mapping(
        load_json_strict(path),
        code,
        pool=pool,
        artifact=artifact,
    )
    try:
        observed = path.read_bytes()
    except OSError as exc:
        raise ValidationFailure(code, pool=pool, artifact=artifact) from exc
    if observed != canonical_json_bytes(payload):
        raise ValidationFailure(code, pool=pool, artifact=artifact)
    return payload


def _computed_pbo(matrix: Sequence[Sequence[float]]) -> dict[str, object]:
    window_count = len(matrix[0])
    partitions = min(8, window_count)
    if partitions % 2:
        partitions -= 1
    result = compute_pbo([list(row) for row in matrix], partitions=partitions)
    return {"status": "computed", **asdict(result)}


def _validate_pbo_payload(
    directory: Path,
    manifest: Mapping[str, object],
    evidence: CsvEvidence,
    *,
    pool: str,
) -> Mapping[str, object]:
    artifact = "pbo_allocation_rules.json"
    payload = _load_canonical_json(
        directory / artifact,
        code="PBO_SCHEMA",
        pool=pool,
        artifact=artifact,
    )
    if set(payload) != {
        "schema_version",
        "pool",
        "candidate_sleeves",
        "allocation_rules",
        "carried_paths",
    } or (payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION or payload.get("pool") != pool):
        raise ValidationFailure("PBO_SCHEMA", pool=pool, artifact=artifact)

    dimensions = FULL_DIMENSIONS[pool]
    if evidence.candidate_failures:
        counts = Counter(str(failure["status"]) for failure in evidence.candidate_failures)
        candidate_expected: dict[str, object] = {
            "status": "invalid_incomplete_matrix",
            "expected_rows": dimensions.windows * dimensions.canonical_units,
            "observed_rows": dimensions.windows * dimensions.canonical_units,
            "valid_rows": (
                dimensions.windows * dimensions.canonical_units - len(evidence.candidate_failures)
            ),
            "invalid_rows": len(evidence.candidate_failures),
            "invalid_status_counts": dict(counts),
            "invalid_observations": evidence.candidate_failures,
        }
        selected_economic_id: str | None = None
    else:
        matrix = [evidence.candidate_returns[economic_id] for economic_id in evidence.catalog_ids]
        if any(len(row) != dimensions.windows for row in matrix):
            raise ValidationFailure("PBO_MATRIX", pool=pool, artifact=artifact)
        candidate_expected = _computed_pbo(matrix)
        means = {
            economic_id: sum(values) / len(values)
            for economic_id, values in evidence.candidate_returns.items()
        }
        selected_economic_id = min(
            means,
            key=lambda economic_id: (-means[economic_id], economic_id),
        )

    if evidence.reset_failures:
        order = {rule: index for index, rule in enumerate(ALLOCATION_RULE_IDS)}
        invalid = sorted(
            evidence.reset_failures,
            key=lambda row: (
                order[str(row["allocation_rule"])],
                cast(int, row["window_index"]),
            ),
        )
        allocation_expected: dict[str, object] = {
            "status": "invalid_incomplete_matrix",
            "invalid_observations": invalid,
        }
    else:
        matrix = [evidence.reset_returns[rule] for rule in ALLOCATION_RULE_IDS]
        if any(len(row) != dimensions.windows for row in matrix):
            raise ValidationFailure("PBO_MATRIX", pool=pool, artifact=artifact)
        allocation_expected = _computed_pbo(matrix)
    expected = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "pool": pool,
        "candidate_sleeves": candidate_expected,
        "allocation_rules": allocation_expected,
        "carried_paths": {"status": "not_applicable_path_dependent_carried_bankroll"},
    }
    if payload != expected:
        raise ValidationFailure("PBO_RECOMPUTE", pool=pool, artifact=artifact)

    manifest_candidate = _as_mapping(
        manifest["candidate_reset_matrix"],
        "MANIFEST_GATE",
        pool=pool,
    )
    manifest_pbo = _as_mapping(
        manifest["pbo_status"],
        "MANIFEST_GATE",
        pool=pool,
    )
    candidate_status = str(candidate_expected["status"])
    allocation_status = str(allocation_expected["status"])
    if (
        manifest_candidate.get("status")
        != ("invalid_incomplete_matrix" if evidence.candidate_failures else "complete_valid")
        or manifest_candidate.get("valid_rows")
        != dimensions.windows * dimensions.canonical_units - len(evidence.candidate_failures)
        or manifest_candidate.get("invalid_rows") != len(evidence.candidate_failures)
        or manifest_candidate.get("invalid_status_counts")
        != dict(Counter(str(failure["status"]) for failure in evidence.candidate_failures))
        or manifest_pbo.get("candidate_sleeves") != candidate_status
        or manifest_pbo.get("allocation_rules") != allocation_status
        or manifest.get("best_sleeve_removal_economic_id") != selected_economic_id
    ):
        raise ValidationFailure("MANIFEST_GATE", pool=pool)
    return payload


def _method_rows(
    evidence: CsvEvidence,
    method_id: str,
    windows: int,
) -> list[Mapping[str, str]]:
    source = evidence.carried_rows if method_id in ALLOCATION_RULE_IDS else evidence.comparator_rows
    return [source[(method_id, index)] for index in range(windows)]


def _require_numeric_match(
    row: Mapping[str, str],
    expected: Mapping[str, float],
    *,
    pool: str,
    artifact: str,
) -> None:
    for field, value in expected.items():
        observed = _parse_finite_float(
            row[field],
            code="AGGREGATE_VALUE",
            pool=pool,
            artifact=artifact,
        )
        if not _close(observed, value, scale=max(abs(observed), abs(value))):
            raise ValidationFailure(
                "AGGREGATE_RECONCILIATION",
                pool=pool,
                artifact=artifact,
            )


def _validate_method_stability(
    directory: Path,
    evidence: CsvEvidence,
    *,
    pool: str,
) -> None:
    artifact = "method_stability.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    windows = FULL_DIMENSIONS[pool].windows
    metric_fields = (
        "cumulative_return",
        "mean_window_return",
        "median_window_return",
        "positive_window_rate",
        "worst_window_return",
        "continuous_max_drawdown",
        "total_fees_usd",
        "total_fixed_cost_usd",
        "total_variable_cost_usd",
    )
    for method_id in (*ALLOCATION_RULE_IDS, *COMPARATOR_IDS):
        try:
            row = next(rows)
        except StopIteration as exc:
            raise ValidationFailure(
                "STABILITY_ROWS",
                pool=pool,
                artifact=artifact,
            ) from exc
        outcomes = _method_rows(evidence, method_id, windows)
        valid = [outcome for outcome in outcomes if outcome["status"] == "valid"]
        invalid = [outcome for outcome in outcomes if outcome["status"] != "valid"]
        expected_kind = "allocation_rule" if method_id in ALLOCATION_RULE_IDS else "comparator"
        if (
            row["schema_version"] != ARTIFACT_SCHEMA_VERSION
            or row["pool"] != pool
            or row["method_id"] != method_id
            or row["method_kind"] != expected_kind
            or row["completed_windows"] != str(windows)
            or row["valid_windows"] != str(len(valid))
            or row["invalid_windows"] != str(len(invalid))
        ):
            raise ValidationFailure(
                "STABILITY_ROWS",
                pool=pool,
                artifact=artifact,
            )
        if invalid:
            first = invalid[0]
            first_status = first["blocking_status"] or first["status"]
            if (
                row["first_invalid_status"] != first_status
                or row["first_invalid_window_index"] != first["blocking_window_index"]
                or row["first_failure_type"] != first["failure_type"]
                or row["first_failure_reason"] != first["failure_reason"]
                or any(row[field] for field in metric_fields)
            ):
                raise ValidationFailure(
                    "STABILITY_STATUS",
                    pool=pool,
                    artifact=artifact,
                )
            continue
        if (
            row["first_invalid_status"]
            or row["first_invalid_window_index"]
            or row["first_failure_type"]
            or row["first_failure_reason"]
        ):
            raise ValidationFailure(
                "STABILITY_STATUS",
                pool=pool,
                artifact=artifact,
            )
        returns = [float(outcome["window_net_return"]) for outcome in valid]
        expected = {
            "cumulative_return": float(valid[-1]["closing_cash_usd"])
            / float(valid[0]["opening_capital_usd"])
            - 1.0,
            "mean_window_return": statistics.fmean(returns),
            "median_window_return": statistics.median(returns),
            "positive_window_rate": sum(value > 0.0 for value in returns) / len(returns),
            "worst_window_return": min(returns),
            "total_fees_usd": sum(float(outcome["total_fees_usd"]) for outcome in valid),
            "total_fixed_cost_usd": sum(
                float(outcome["total_fixed_cost_usd"]) for outcome in valid
            ),
            "total_variable_cost_usd": sum(
                float(outcome["total_variable_cost_usd"]) for outcome in valid
            ),
        }
        _require_numeric_match(
            row,
            expected,
            pool=pool,
            artifact=artifact,
        )
        continuous_drawdown = _parse_finite_float(
            row["continuous_max_drawdown"],
            code="AGGREGATE_VALUE",
            pool=pool,
            artifact=artifact,
        )
        if not -1.0 <= continuous_drawdown <= 0.0:
            raise ValidationFailure(
                "AGGREGATE_VALUE",
                pool=pool,
                artifact=artifact,
            )
    try:
        next(rows)
    except StopIteration:
        return
    raise ValidationFailure("STABILITY_ROWS", pool=pool, artifact=artifact)


def _validate_comparator_conclusions(
    directory: Path,
    evidence: CsvEvidence,
    pbo: Mapping[str, object],
    *,
    pool: str,
) -> None:
    artifact = "comparator_conclusions.csv"
    rows = iter_csv_rows(directory / artifact, CSV_FIELDS[artifact])
    windows = FULL_DIMENSIONS[pool].windows
    candidate_status = str(
        _as_mapping(
            pbo["candidate_sleeves"],
            "PBO_SCHEMA",
            pool=pool,
            artifact="pbo_allocation_rules.json",
        )["status"]
    )
    allocation_status = str(
        _as_mapping(
            pbo["allocation_rules"],
            "PBO_SCHEMA",
            pool=pool,
            artifact="pbo_allocation_rules.json",
        )["status"]
    )
    paired_fields = (
        "mean_paired_excess_return",
        "median_paired_excess_return",
        "worst_paired_excess_return",
        "positive_paired_excess_rate",
    )
    for rule in ALLOCATION_RULE_IDS:
        for comparator in COMPARATOR_IDS:
            try:
                row = next(rows)
            except StopIteration as exc:
                raise ValidationFailure(
                    "CONCLUSION_ROWS",
                    pool=pool,
                    artifact=artifact,
                ) from exc
            pairs: list[float] = []
            invalid_rule = 0
            invalid_comparator = 0
            for index in range(windows):
                rule_row = evidence.carried_rows[(rule, index)]
                comparator_row = evidence.comparator_rows[(comparator, index)]
                rule_valid = rule_row["status"] == "valid"
                comparator_valid = comparator_row["status"] == "valid"
                invalid_rule += not rule_valid
                invalid_comparator += not comparator_valid
                if rule_valid and comparator_valid:
                    pairs.append(
                        float(rule_row["window_net_return"])
                        - float(comparator_row["window_net_return"])
                    )
            if invalid_rule or invalid_comparator:
                conclusion = "not_evaluable_invalid_path"
            elif (
                candidate_status != "computed" or allocation_status != "computed" or len(pairs) < 2
            ):
                conclusion = "insufficient_complete_matrix"
            else:
                conclusion = "diagnostic_only"
            if (
                row["schema_version"] != ARTIFACT_SCHEMA_VERSION
                or row["pool"] != pool
                or row["allocation_rule"] != rule
                or row["comparator_id"] != comparator
                or row["paired_valid_windows"] != str(len(pairs))
                or row["invalid_rule_windows"] != str(invalid_rule)
                or row["invalid_comparator_windows"] != str(invalid_comparator)
                or row["candidate_pbo_status"] != candidate_status
                or row["allocation_pbo_status"] != allocation_status
                or row["conclusion_status"] != conclusion
            ):
                raise ValidationFailure(
                    "CONCLUSION_ROWS",
                    pool=pool,
                    artifact=artifact,
                )
            if conclusion == "diagnostic_only":
                _require_numeric_match(
                    row,
                    {
                        "mean_paired_excess_return": statistics.fmean(pairs),
                        "median_paired_excess_return": statistics.median(pairs),
                        "worst_paired_excess_return": min(pairs),
                        "positive_paired_excess_rate": sum(value > 0.0 for value in pairs)
                        / len(pairs),
                    },
                    pool=pool,
                    artifact=artifact,
                )
            elif any(row[field] for field in paired_fields):
                raise ValidationFailure(
                    "CONCLUSION_ROWS",
                    pool=pool,
                    artifact=artifact,
                )
    try:
        next(rows)
    except StopIteration:
        return
    raise ValidationFailure("CONCLUSION_ROWS", pool=pool, artifact=artifact)


def _validate_csv_evidence(
    directory: Path,
    manifest: Mapping[str, object],
    *,
    pool: str,
) -> tuple[CsvEvidence, Mapping[str, object]]:
    catalog, families, catalog_ids = _validate_catalog(directory, pool=pool)
    bounds: dict[int, tuple[str, str]] = {}
    eligible, training_metrics, routed_names = _validate_training_rows(
        directory,
        pool=pool,
        catalog_ids=catalog_ids,
        families=families,
        bounds=bounds,
    )
    removed = manifest.get("best_sleeve_removal_economic_id")
    weights = _validate_weight_rows(
        directory,
        pool=pool,
        catalog=catalog,
        catalog_ids=catalog_ids,
        families=families,
        eligible=eligible,
        training_metrics=training_metrics,
        removed_economic_id=removed if isinstance(removed, str) else None,
        bounds=bounds,
    )
    candidate_returns, candidate_failures = _validate_candidate_rows(
        directory,
        pool=pool,
        catalog_ids=catalog_ids,
        families=families,
        routed_names=routed_names,
        bounds=bounds,
    )
    reset_returns, reset_failures = _validate_reset_rows(
        directory,
        pool=pool,
        weights=weights,
        bounds=bounds,
    )
    carried = _validate_carried_rows(directory, pool=pool, bounds=bounds)
    comparators = _validate_comparator_rows(
        directory,
        pool=pool,
        catalog=catalog,
        catalog_ids=catalog_ids,
        training_metrics=training_metrics,
        bounds=bounds,
    )
    _validate_attribution_rows(
        directory,
        pool=pool,
        catalog_ids=catalog_ids,
        families=families,
        carried=carried,
        weights=weights,
        bounds=bounds,
    )
    removal_all_valid = _validate_concentration_rows(
        directory,
        manifest,
        pool=pool,
        weights=weights,
        carried=carried,
        bounds=bounds,
    )
    evidence = CsvEvidence(
        catalog_ids=catalog_ids,
        window_bounds=bounds,
        candidate_returns=candidate_returns,
        candidate_failures=candidate_failures,
        reset_returns=reset_returns,
        reset_failures=reset_failures,
        carried_rows=carried,
        comparator_rows=comparators,
        weight_summaries=weights,
        removal_all_valid=removal_all_valid,
    )
    if set(bounds) != set(range(FULL_DIMENSIONS[pool].windows)):
        raise ValidationFailure("WINDOW_COVERAGE", pool=pool)
    pbo = _validate_pbo_payload(directory, manifest, evidence, pool=pool)
    _validate_method_stability(directory, evidence, pool=pool)
    _validate_comparator_conclusions(
        directory,
        evidence,
        pbo,
        pool=pool,
    )
    return evidence, pbo


def validate_pool_publication(
    publication_root: Path,
    repo_root: Path,
    *,
    pool: str,
) -> ValidationReport:
    directory = publication_root / POOL_DIRECTORIES[pool]
    validate_artifact_set(directory, pool=pool)
    manifest = _load_canonical_json(
        directory / "run_manifest.json",
        code="MANIFEST_CANONICAL",
        pool=pool,
        artifact="run_manifest.json",
    )
    validate_manifest_shape(manifest, pool=pool)
    identity = _as_mapping(
        manifest["primary_run_identity"],
        "MANIFEST_IDENTITY",
        pool=pool,
    )
    validate_run_identity(identity, repo_root=repo_root, pool=pool)
    validate_artifact_metadata(directory, manifest, pool=pool)
    evidence, pbo = _validate_csv_evidence(directory, manifest, pool=pool)
    validate_summary(directory, manifest, pool=pool)
    candidate = _as_mapping(
        manifest["candidate_reset_matrix"],
        "MANIFEST_GATE",
        pool=pool,
    )
    candidate_pbo = _as_mapping(
        pbo["candidate_sleeves"],
        "PBO_SCHEMA",
        pool=pool,
        artifact="pbo_allocation_rules.json",
    )
    allocation_pbo = _as_mapping(
        pbo["allocation_rules"],
        "PBO_SCHEMA",
        pool=pool,
        artifact="pbo_allocation_rules.json",
    )
    claim_ready = (
        candidate.get("status") == "complete_valid"
        and candidate_pbo.get("status") == "computed"
        and allocation_pbo.get("status") == "computed"
        and evidence.removal_all_valid
        and all(row["status"] == "valid" for row in evidence.carried_rows.values())
        and all(row["status"] == "valid" for row in evidence.comparator_rows.values())
    )
    evidence_status = "diagnostic_only" if claim_ready else "not_publishable"
    return ValidationReport(
        pool=pool,
        windows=FULL_DIMENSIONS[pool].windows,
        artifacts=len(REQUIRED_ARTIFACTS),
        manifest_sha256=sha256_file(directory / "run_manifest.json"),
        evidence_status=evidence_status,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publication-root",
        type=Path,
        required=True,
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--pool",
        choices=("uni-base", "uni-bsc", "all"),
        default="all",
    )
    parser.add_argument(
        "--source-commit",
        default=FROZEN_SOURCE_COMMIT,
    )
    parser.add_argument(
        "--integrity-only",
        action="store_true",
        help="validate diagnostic artifacts without authorizing article claims",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.source_commit != FROZEN_SOURCE_COMMIT:
        parser.error("source commit is frozen by the validator")
    pools = tuple(FULL_DIMENSIONS) if args.pool == "all" else (args.pool,)
    try:
        reports = [
            validate_pool_publication(
                args.publication_root,
                args.repo_root,
                pool=pool,
            )
            for pool in pools
        ]
    except ValidationFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("FAIL code=VALIDATOR_INTERNAL", file=sys.stderr)
        return 4
    blocked = next(
        (report for report in reports if report.evidence_status != "diagnostic_only"),
        None,
    )
    if blocked is not None and not args.integrity_only:
        print(f"FAIL code=CLAIM_GATE pool={blocked.pool}", file=sys.stderr)
        return 2
    for report in reports:
        print(
            "PASS "
            f"pool={report.pool} "
            f"windows={report.windows} "
            f"artifacts={report.artifacts} "
            f"manifest_sha256={report.manifest_sha256} "
            f"evidence_status={report.evidence_status}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
