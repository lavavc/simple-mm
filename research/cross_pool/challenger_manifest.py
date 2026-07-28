"""Canonical manifest and review states for challenger evidence."""

from __future__ import annotations

import json
import platform
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias, cast

import jsonschema
import matplotlib
import numpy as np

from research.cross_pool.challenger_artifacts import (
    CHALLENGER_ARTIFACT_NAMES,
    ChallengerArtifact,
)
from research.cross_pool.challenger_contracts import (
    ChallengerInference,
    ChallengerParent,
    ChallengerStudy,
)
from research.cross_pool.challenger_estimators import (
    CONVERGENCE_TOLERANCE,
    HUBER_DELTA,
    HUBER_MAD_FACTOR,
    MAXIMUM_CONDITION_NUMBER,
    MAXIMUM_ITERATIONS,
    MAXIMUM_LINE_SEARCH_HALVINGS,
    RIDGE_LOGISTIC_PENALTY,
)
from research.cross_pool.challenger_inference import (
    BOOTSTRAP_BLOCK_DAYS,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    MINIMUM_COMPLETE_BLOCKS,
    MINIMUM_TARGET_DAYS,
    MINIMUM_UPDATE_DAYS,
)
from research.cross_pool.challenger_parent import PARENT_ARTIFACT_SHA256
from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.reporting import canonical_json_bytes

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)

_SCHEMA_PATH = Path(__file__).with_name("challenger_manifest.schema.json")
CHALLENGER_SOURCE_PATHS = (
    "engine/__init__.py",
    "engine/math/__init__.py",
    "engine/math/v3.py",
    "engine/web3_utils.py",
    "research/__init__.py",
    "research/backtester/__init__.py",
    "research/backtester/clmm_math.py",
    "research/backtester/lp_ledger_attribution.py",
    "research/backtester/pool_price_semantics.py",
    "research/backtester/v4_event_replay.py",
    "research/cross_pool/__init__.py",
    "research/cross_pool/article_manifest.schema.json",
    "research/cross_pool/bootstrap.py",
    "research/cross_pool/challenger_artifacts.py",
    "research/cross_pool/challenger_contracts.py",
    "research/cross_pool/challenger_estimators.py",
    "research/cross_pool/challenger_inference.py",
    "research/cross_pool/challenger_manifest.py",
    "research/cross_pool/challenger_manifest.schema.json",
    "research/cross_pool/challenger_parent.py",
    "research/cross_pool/challenger_publication.py",
    "research/cross_pool/challenger_reporting.py",
    "research/cross_pool/challengers.py",
    "research/cross_pool/contracts.py",
    "research/cross_pool/dtw.py",
    "research/cross_pool/event_study.py",
    "research/cross_pool/figures.py",
    "research/cross_pool/manifest.py",
    "research/cross_pool/market_structure.py",
    "research/cross_pool/panel.py",
    "research/cross_pool/pipeline.py",
    "research/cross_pool/predictive.py",
    "research/cross_pool/publication.py",
    "research/cross_pool/qa.py",
    "research/cross_pool/reporting.py",
    "research/scripts/run_cross_pool_challengers.py",
)
_EXPECTED_CONFIGURATION: dict[str, JsonValue] = {
    "directions": ["bsc_to_base", "base_to_bsc"],
    "horizons_ms": [900000, 3600000, 14400000],
    "families": ["ols", "arx2", "gam", "huber", "two_part"],
    "variants": ["target_only", "source_age", "full_source"],
    "freshness_supports": ["all", "both_age_le_4h", "both_age_le_1h"],
    "oos_rows_per_direction": {"900000": 8438, "3600000": 2109, "14400000": 527},
    "fold_indices": list(range(13)),
    "ols": {
        "standardization": "training_fold_zscore",
        "maximum_condition_number": MAXIMUM_CONDITION_NUMBER,
        "target_only_columns": ["target_return", "target_age_ms"],
        "source_age_columns": ["target_return", "target_age_ms", "source_age_ms"],
        "full_source_columns": [
            "target_return",
            "target_age_ms",
            "source_return",
            "signed_gap",
            "source_age_ms",
        ],
    },
    "arx2": {
        "predecessor": "prior_regular_panel_row",
        "predecessor_derived_before_support_filtering": True,
        "missing_training_predecessor": "exclude_row",
        "missing_validation_predecessor": "fail_cell",
        "maximum_condition_number": MAXIMUM_CONDITION_NUMBER,
    },
    "huber": {
        "delta": HUBER_DELTA,
        "mad_factor": HUBER_MAD_FACTOR,
        "zero_scale_rule": "nonpositive_exact_mad",
        "tolerance": CONVERGENCE_TOLERANCE,
        "maximum_iterations": MAXIMUM_ITERATIONS,
        "maximum_condition_number": MAXIMUM_CONDITION_NUMBER,
    },
    "gam": {
        "basis": "piecewise_linear_hinge",
        "age_transform": "log1p_age_ms_over_60000",
        "knot_quantiles": [1.0 / 3.0, 2.0 / 3.0],
        "quantile_method": "linear",
        "validation_extrapolation": "linear_and_counted",
        "interactions": False,
        "penalty": None,
        "maximum_condition_number": MAXIMUM_CONDITION_NUMBER,
    },
    "two_part": {
        "update_definition": "forward_state_timestamp_gt_state_timestamp",
        "logistic_objective": "mean_nll_plus_half_lambda_l2_nonintercept",
        "ridge_penalty_mean_nll": RIDGE_LOGISTIC_PENALTY,
        "line_search": "backtracking_halving_nonincreasing_objective",
        "maximum_line_search_halvings": MAXIMUM_LINE_SEARCH_HALVINGS,
        "tolerance": CONVERGENCE_TOLERANCE,
        "maximum_iterations": MAXIMUM_ITERATIONS,
        "maximum_condition_number": MAXIMUM_CONDITION_NUMBER,
        "conditional_model": "huber_signed_return_on_updates",
        "conditional_training_minimum": "design_columns_plus_one",
        "combined_forecast": "update_probability_times_conditional_mean",
        "log_loss_clipping": "finfo_eps_only",
    },
    "bootstrap": {
        "scheme": "circular_moving_block",
        "generator": "PCG64",
        "calendar_days": 88,
        "block_days": BOOTSTRAP_BLOCK_DAYS,
        "resamples": BOOTSTRAP_RESAMPLES,
        "seed": BOOTSTRAP_SEED,
        "confidence_level": 0.95,
        "raw_interval": "central_nearest_rank",
        "raw_interval_zero_based_indices": [249, 9749],
        "standard_error_ddof": 1,
        "max_t": "single_step_centered_absolute",
        "max_t_critical_zero_based_index": 9499,
        "adjusted_p_value": "plus_one_two_sided",
        "maximum_family_cells": 126,
    },
    "adequacy": {
        "minimum_target_days": MINIMUM_TARGET_DAYS,
        "minimum_complete_blocks": MINIMUM_COMPLETE_BLOCKS,
        "minimum_update_days": MINIMUM_UPDATE_DAYS,
    },
}
_ALLOWED_CLAIMS = ["post_hoc_incremental_forecast_diagnostic"]
_FORBIDDEN_CLAIMS = [
    "causal_price_discovery",
    "permanent_venue_leader",
    "deployable_alpha",
    "trading_profitability",
    "lp_profitability",
    "parent_decision_revision_without_fresh_data",
]


def build_generated_challenger_manifest(
    *,
    parent: ChallengerParent,
    study: ChallengerStudy,
    inference: ChallengerInference,
    artifacts: tuple[ChallengerArtifact, ...],
    code_commit: str,
    source_diff_sha256: str,
) -> dict[str, JsonValue]:
    if parent.anchor.artifact_sha256 != tuple(PARENT_ARTIFACT_SHA256.items()):
        raise CrossPoolContractError("challenger parent anchor is not frozen")
    if len(inference.metrics) != 270 or len(inference.contrasts) != 378:
        raise CrossPoolContractError("challenger cell registry is incomplete")
    source_price_cells = sum(row.contrast == "source_price" for row in inference.contrasts)
    if source_price_cells != 126:
        raise CrossPoolContractError("challenger source-price registry is incomplete")
    artifact_map = _artifact_map(artifacts)
    manifest: dict[str, JsonValue] = {
        "schema_version": "1.0.0",
        "artifact_status": "generated_unreviewed",
        "research_role": "post_hoc_exploratory",
        "parent_decision": "leadership_unresolved",
        "parent_decision_unchanged": True,
        "parent_artifacts": dict(PARENT_ARTIFACT_SHA256),
        "source_identity": {
            "code_commit": code_commit,
            "source_diff_sha256": source_diff_sha256,
            "source_paths": list(CHALLENGER_SOURCE_PATHS),
        },
        "runtime": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "matplotlib": matplotlib.__version__,
        },
        "configuration": deepcopy(_EXPECTED_CONFIGURATION),
        "registry": {
            "metric_cells": len(inference.metrics),
            "contrast_cells": len(inference.contrasts),
            "source_price_cells": source_price_cells,
            "model_fit_failures": len(study.failures),
        },
        "artifacts": artifact_map,
        "qa": {"status": "pass", "reasons": []},
        "review": {
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at_utc": None,
        },
        "claims": {
            "allowed": list(_ALLOWED_CLAIMS),
            "forbidden": list(_FORBIDDEN_CLAIMS),
        },
    }
    validate_challenger_manifest(manifest)
    return manifest


def build_blocked_challenger_manifest(
    *,
    reason: str,
    code_commit: str,
    source_diff_sha256: str,
) -> dict[str, JsonValue]:
    if not reason:
        raise CrossPoolContractError("blocked challenger reason must be nonempty")
    manifest: dict[str, JsonValue] = {
        "schema_version": "1.0.0",
        "artifact_status": "qa_blocked",
        "research_role": "post_hoc_exploratory",
        "parent_decision": "leadership_unresolved",
        "parent_decision_unchanged": True,
        "parent_artifacts": dict(PARENT_ARTIFACT_SHA256),
        "source_identity": {
            "code_commit": code_commit,
            "source_diff_sha256": source_diff_sha256,
            "source_paths": list(CHALLENGER_SOURCE_PATHS),
        },
        "runtime": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "matplotlib": matplotlib.__version__,
        },
        "configuration": deepcopy(_EXPECTED_CONFIGURATION),
        "registry": {
            "metric_cells": 270,
            "contrast_cells": 378,
            "source_price_cells": 126,
            "model_fit_failures": 0,
        },
        "artifacts": {},
        "qa": {"status": "blocked", "reasons": [reason]},
        "review": {
            "status": "blocked",
            "reviewed_by": None,
            "reviewed_at_utc": None,
        },
        "claims": {
            "allowed": list(_ALLOWED_CLAIMS),
            "forbidden": list(_FORBIDDEN_CLAIMS),
        },
    }
    validate_challenger_manifest(manifest)
    return manifest


def reviewed_challenger_manifest(
    payload: dict[str, JsonValue],
    *,
    reviewed_by: str,
    reviewed_at_utc: str,
) -> dict[str, JsonValue]:
    validate_challenger_manifest(payload)
    if payload["artifact_status"] != "generated_unreviewed":
        raise CrossPoolContractError("review requires generated challenger evidence")
    if not reviewed_by or reviewed_by.strip() != reviewed_by:
        raise CrossPoolContractError("challenger reviewer identity must be explicit")
    _validate_review_timestamp(reviewed_at_utc)
    reviewed = deepcopy(payload)
    reviewed["artifact_status"] = "reviewed"
    reviewed["review"] = {
        "status": "reviewed",
        "reviewed_by": reviewed_by,
        "reviewed_at_utc": reviewed_at_utc,
    }
    validate_challenger_manifest(reviewed)
    return reviewed


def canonical_challenger_manifest_bytes(payload: dict[str, JsonValue]) -> bytes:
    return canonical_json_bytes(payload)


def validate_challenger_manifest(payload: dict[str, JsonValue]) -> None:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)
    except (OSError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
        raise CrossPoolContractError("challenger manifest schema validation failed") from exc
    if payload.get("parent_artifacts") != PARENT_ARTIFACT_SHA256:
        raise CrossPoolContractError("challenger manifest parent hashes are not frozen")
    if payload.get("configuration") != _EXPECTED_CONFIGURATION:
        raise CrossPoolContractError("challenger manifest configuration is not frozen")
    source_identity = payload.get("source_identity")
    if not isinstance(source_identity, dict) or source_identity.get("source_paths") != list(
        CHALLENGER_SOURCE_PATHS
    ):
        raise CrossPoolContractError("challenger source closure is not frozen")
    claims = payload.get("claims")
    if claims != {"allowed": _ALLOWED_CLAIMS, "forbidden": _FORBIDDEN_CLAIMS}:
        raise CrossPoolContractError("challenger manifest claim boundary is not frozen")
    status = payload["artifact_status"]
    qa = cast(dict[str, JsonValue], payload["qa"])
    review = cast(dict[str, JsonValue], payload["review"])
    artifacts = cast(dict[str, JsonValue], payload["artifacts"])
    if status == "generated_unreviewed":
        if (
            qa != {"status": "pass", "reasons": []}
            or review
            != {"status": "pending", "reviewed_by": None, "reviewed_at_utc": None}
            or set(artifacts) != set(CHALLENGER_ARTIFACT_NAMES)
        ):
            raise CrossPoolContractError("generated challenger state is invalid")
    elif status == "reviewed":
        if qa != {"status": "pass", "reasons": []} or review.get("status") != "reviewed":
            raise CrossPoolContractError("reviewed challenger state is invalid")
        _validate_review_timestamp(cast(str, review["reviewed_at_utc"]))
        if not isinstance(review.get("reviewed_by"), str) or not review["reviewed_by"]:
            raise CrossPoolContractError("reviewed challenger identity is invalid")
        if set(artifacts) != set(CHALLENGER_ARTIFACT_NAMES):
            raise CrossPoolContractError("reviewed challenger artifacts are incomplete")
    elif status == "qa_blocked":
        if (
            qa.get("status") != "blocked"
            or not qa.get("reasons")
            or review
            != {"status": "blocked", "reviewed_by": None, "reviewed_at_utc": None}
            or artifacts
        ):
            raise CrossPoolContractError("blocked challenger state is invalid")
    canonical_challenger_manifest_bytes(payload)


def load_challenger_manifest(path: Path) -> dict[str, JsonValue]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CrossPoolContractError("challenger manifest could not be read") from exc
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CrossPoolContractError("challenger manifest is not canonical JSON") from exc
    if not isinstance(payload, dict):
        raise CrossPoolContractError("challenger manifest must be an object")
    result = cast(dict[str, JsonValue], payload)
    validate_challenger_manifest(result)
    if raw != canonical_challenger_manifest_bytes(result):
        raise CrossPoolContractError("challenger manifest bytes are not canonical")
    return result


def _artifact_map(artifacts: tuple[ChallengerArtifact, ...]) -> dict[str, JsonValue]:
    if tuple(artifact.relative_name for artifact in artifacts) != CHALLENGER_ARTIFACT_NAMES:
        raise CrossPoolContractError("challenger artifact order is not frozen")
    return {artifact.relative_name: artifact.sha256 for artifact in artifacts}


def _validate_review_timestamp(value: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CrossPoolContractError("review timestamp must be an explicit UTC Z value")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CrossPoolContractError("review timestamp is invalid") from exc
    if parsed.tzinfo != UTC:
        raise CrossPoolContractError("review timestamp must be UTC")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CrossPoolContractError("challenger manifest contains a duplicate key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise CrossPoolContractError(f"challenger manifest contains non-finite value {value}")
