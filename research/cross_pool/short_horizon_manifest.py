"""Independent schema-v1 manifest for short-horizon response evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping, TypeAlias, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from research.cross_pool.contracts import CrossPoolContractError, Direction
from research.cross_pool.short_horizon import SHORT_HORIZONS_MS
from research.cross_pool.short_horizon_artifacts import (
    SHORT_HORIZON_ARTIFACT_NAMES,
    ShortHorizonArtifact,
)
from research.cross_pool.short_horizon_inference import (
    MIN_CONDITIONAL_UPDATE_DAYS,
    MIN_JOINT_VALID_RESAMPLES,
    SHORT_HORIZON_BOOTSTRAP_CONFIDENCE_LEVEL,
    SHORT_HORIZON_BOOTSTRAP_RESAMPLES,
    SHORT_HORIZON_BOOTSTRAP_SEED,
    BandStatus,
    BandUnavailableReason,
    Cohort,
    PointwiseInterval,
    ShortHorizonInference,
    ShortHorizonSummary,
    SimultaneousBandFamily,
    SimultaneousBandPoint,
    SimultaneousMetric,
)

SCHEMA_VERSION = "1.0.0"
PARENT_MANIFEST_SHA256 = "adf4fd71fd33604cee70fcba7aa90d2b7763715d3cba68a868d26347c599abfe"
BASE_FEATURES_SHA256 = "47bae897266c00d92823fcae8d004debf9e50d22639ab52c326b121c9b7da23a"
BSC_FEATURES_SHA256 = "aba2dbef0548d4bca4882c26f220da14f963c1727bff855cd7532784c2cfaa5c"
GIRUM_NOTE_SHA256 = "e401f2ddb91305d209d2183745d61e89e437ae3db054b0d4c11178c7db1f3261"
SHORT_HORIZON_SOURCE_DEPENDENCIES = (
    "engine/web3_utils.py",
    "research/cross_pool/bootstrap.py",
    "research/cross_pool/contracts.py",
    "research/cross_pool/event_study.py",
    "research/cross_pool/io.py",
    "research/cross_pool/short_horizon.py",
    "research/cross_pool/short_horizon_artifacts.py",
    "research/cross_pool/short_horizon_inference.py",
    "research/cross_pool/short_horizon_manifest.py",
    "research/cross_pool/short_horizon_manifest.schema.json",
    "research/cross_pool/short_horizon_parent.py",
    "research/cross_pool/short_horizon_publication.py",
    "research/cross_pool/short_horizon_reporting.py",
    "research/scripts/review_cross_pool_short_horizon.py",
    "research/scripts/run_cross_pool_short_horizon.py",
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
ArtifactStatus = Literal["generated_unreviewed", "reviewed", "qa_blocked"]
QaReasonCode = Literal[
    "missing_base_features",
    "missing_bsc_features",
    "missing_girum_note",
    "base_features_hash_mismatch",
    "bsc_features_hash_mismatch",
    "girum_note_hash_mismatch",
    "measurement_failure",
    "inference_failure",
    "parent_anchor_mismatch",
    "reporting_failure",
]

_SCHEMA_PATH = Path(__file__).with_name("short_horizon_manifest.schema.json")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_UTC_REVIEW_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DIRECTION_ORDER: tuple[Direction, ...] = ("bsc_to_base", "base_to_bsc")
_COHORT_ORDER = ("primary", "exclude_same_timestamp")
_PROFILE_ORDER = tuple(
    (direction, cohort) for direction in _DIRECTION_ORDER for cohort in _COHORT_ORDER
)
_ARTIFACT_NAMES = SHORT_HORIZON_ARTIFACT_NAMES
_QA_REASON_CODES: tuple[QaReasonCode, ...] = (
    "missing_base_features",
    "missing_bsc_features",
    "missing_girum_note",
    "base_features_hash_mismatch",
    "bsc_features_hash_mismatch",
    "girum_note_hash_mismatch",
    "measurement_failure",
    "inference_failure",
    "parent_anchor_mismatch",
    "reporting_failure",
)
_PARENT_PAYLOAD: dict[str, JsonValue] = {
    "manifest_sha256": PARENT_MANIFEST_SHA256,
    "schema_version": "2.0.0",
    "artifact_status": "reviewed",
    "article_branch": "leadership_unresolved",
    "economic_class": "no_net_return_improvement",
    "allowed_claim": "aggregate_leadership_unresolved",
    "base_features_sha256": BASE_FEATURES_SHA256,
    "bsc_features_sha256": BSC_FEATURES_SHA256,
}
_CONTRACT_PAYLOAD: dict[str, JsonValue] = {
    "research_role": "post_hoc_exploratory",
    "horizons_ms": list(SHORT_HORIZONS_MS),
    "shock_threshold_bps": 5.0,
    "lookback_ms": 900_000,
    "cluster_ms": 900_000,
    "bootstrap": {
        "unit": "shock_utc_day",
        "resamples": SHORT_HORIZON_BOOTSTRAP_RESAMPLES,
        "seed": SHORT_HORIZON_BOOTSTRAP_SEED,
        "bit_generator": "PCG64",
        "confidence_level": SHORT_HORIZON_BOOTSTRAP_CONFIDENCE_LEVEL,
        "pointwise_rank_convention": "central_nearest_rank",
        "simultaneous_method": "within_direction_max_z",
        "minimum_joint_valid_resamples": MIN_JOINT_VALID_RESAMPLES,
        "minimum_conditional_update_days": MIN_CONDITIONAL_UPDATE_DAYS,
    },
    "price_field": "raw_sqrt_mid",
    "price_orientation": "stablecoin_per_cngn",
    "response_estimator": "simple_return_bps",
    "fee_gap": "direction_fixed_fee_adjusted_bid_ask_log_ratio",
    "timestamp_tie_policy": "exclude_complete_shock_family_sensitivity",
    "common_support": "both_streams_through_900000_ms",
    "stablecoin_parity_assumption": "usdc_equals_usdt",
    "economic_interpretation_floor_bps": 10.0,
}
_PUBLICATION_PAYLOAD: dict[str, JsonValue] = {
    "parent_decision_unchanged": True,
    "permitted_claims": [
        "short_horizon_response_profile",
        "update_incidence_latency_staleness_profile",
        "fee_only_cross_venue_gap_profile",
        "girum_method_compatibility",
    ],
    "forbidden_claims": [
        "causal_price_discovery",
        "unique_directional_leader",
        "deployable_alpha",
        "toxic_flow_attribution",
        "external_lp_profitability",
        "net_executable_profit",
    ],
}
_ANCHOR_COUNTS = {"bsc_to_base": (123, 59), "base_to_bsc": (197, 70)}
_ANCHOR_MEANS = {
    "bsc_to_base": 0.8782298255072702,
    "base_to_bsc": 0.03955408492685122,
}


@dataclass(frozen=True)
class InputIdentity:
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _require_sha256(self.sha256, "input identity")
        _require_positive_int(self.byte_count, "input byte count")


@dataclass(frozen=True)
class SourceDependencyIdentity:
    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        _require_sha256(self.sha256, "source dependency")


@dataclass(frozen=True)
class SourceCapture:
    code_commit: str
    dependencies: tuple[SourceDependencyIdentity, ...]

    def __post_init__(self) -> None:
        _require_commit(self.code_commit)
        paths = tuple(item.relative_path for item in self.dependencies)
        if paths != SHORT_HORIZON_SOURCE_DEPENDENCIES:
            raise CrossPoolContractError(
                "source provenance requires the exact source dependency closure"
            )


@dataclass(frozen=True)
class ShortHorizonProvenance:
    parent_manifest: InputIdentity
    base_features: InputIdentity | None
    bsc_features: InputIdentity | None
    girum_note: InputIdentity | None
    code_commit: str
    source_dependencies: tuple[SourceDependencyIdentity, ...]
    runtime: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_commit(self.code_commit)
        paths = tuple(item.relative_path for item in self.source_dependencies)
        if paths != SHORT_HORIZON_SOURCE_DEPENDENCIES:
            raise CrossPoolContractError("provenance requires the exact source dependency closure")
        keys = tuple(key for key, _ in self.runtime)
        if not keys or keys != tuple(sorted(set(keys))):
            raise CrossPoolContractError("runtime identity keys must be canonical")
        if any(
            not key or key.strip() != key or not value or value.strip() != value
            for key, value in self.runtime
        ):
            raise CrossPoolContractError("runtime identity values must be nonempty")


@dataclass(frozen=True)
class DirectionQaCounts:
    direction: Direction
    detected_shock_count: int
    eligible_shock_count: int
    exclusion_count: int
    same_timestamp_shock_count: int

    def __post_init__(self) -> None:
        if self.direction not in _DIRECTION_ORDER:
            raise CrossPoolContractError("unsupported QA direction")
        for value, label in (
            (self.detected_shock_count, "detected shock count"),
            (self.eligible_shock_count, "eligible shock count"),
            (self.exclusion_count, "exclusion count"),
            (self.same_timestamp_shock_count, "same-timestamp shock count"),
        ):
            _require_nonnegative_int(value, label)
        if self.detected_shock_count != (self.eligible_shock_count + self.exclusion_count):
            raise CrossPoolContractError("QA shock counts must reconcile")
        if self.same_timestamp_shock_count > self.eligible_shock_count:
            raise CrossPoolContractError("same-timestamp shocks cannot exceed eligible shocks")


@dataclass(frozen=True)
class GeneratedManifestInput:
    provenance: ShortHorizonProvenance
    qa_counts: tuple[DirectionQaCounts, ...]
    inferences: tuple[ShortHorizonInference, ...]
    artifacts: tuple[ShortHorizonArtifact, ...]
    parent_anchor_reconciled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, ShortHorizonProvenance):
            raise CrossPoolContractError("generated provenance is invalid")
        if not isinstance(self.parent_anchor_reconciled, bool):
            raise CrossPoolContractError("parent anchor reconciliation flag must be boolean")


@dataclass(frozen=True)
class QaBlockedManifestInput:
    provenance: ShortHorizonProvenance
    reason_codes: tuple[QaReasonCode, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, ShortHorizonProvenance):
            raise CrossPoolContractError("QA-blocked provenance is invalid")
        if any(reason not in _QA_REASON_CODES for reason in self.reason_codes):
            raise CrossPoolContractError("unsupported QA reason code")
        if not self.reason_codes or self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise CrossPoolContractError("QA-blocked reason codes must be nonempty and canonical")


@dataclass(frozen=True)
class ShortHorizonManifestEvidence:
    qa_counts: tuple[DirectionQaCounts, ...]
    inferences: tuple[ShortHorizonInference, ...]
    parent_anchor_reconciled: bool


def short_horizon_manifest_evidence(
    payload: Mapping[str, JsonValue],
) -> ShortHorizonManifestEvidence:
    validate_short_horizon_manifest(payload)
    if payload["artifact_status"] == "qa_blocked":
        raise CrossPoolContractError("QA-blocked manifest has no rendered evidence")
    qa = cast(dict[str, JsonValue], payload["qa"])
    results = cast(dict[str, JsonValue], payload["results"])
    raw_counts = cast(list[JsonValue], qa["counts"])
    raw_cohorts = cast(list[JsonValue], results["cohorts"])
    counts = tuple(
        DirectionQaCounts(
            direction=cast(Direction, cast(dict[str, JsonValue], value)["direction"]),
            detected_shock_count=cast(
                int,
                cast(dict[str, JsonValue], value)["detected_shock_count"],
            ),
            eligible_shock_count=cast(
                int,
                cast(dict[str, JsonValue], value)["eligible_shock_count"],
            ),
            exclusion_count=cast(
                int,
                cast(dict[str, JsonValue], value)["exclusion_count"],
            ),
            same_timestamp_shock_count=cast(
                int,
                cast(dict[str, JsonValue], value)["same_timestamp_shock_count"],
            ),
        )
        for value in raw_counts
    )
    inferences = tuple(
        _inference_from_payload(cast(dict[str, JsonValue], value)) for value in raw_cohorts
    )
    return ShortHorizonManifestEvidence(
        qa_counts=counts,
        inferences=inferences,
        parent_anchor_reconciled=cast(bool, qa["parent_anchor_reconciled"]),
    )


def build_generated_short_horizon_manifest(
    inputs: GeneratedManifestInput,
) -> dict[str, JsonValue]:
    if not isinstance(inputs, GeneratedManifestInput):
        raise CrossPoolContractError("generated manifest input must use GeneratedManifestInput")
    _validate_frozen_provenance(inputs.provenance, allow_unfrozen_inputs=False)
    if not inputs.parent_anchor_reconciled:
        raise CrossPoolContractError("parent anchor must reconcile before publication")
    _validate_generated_support(inputs)
    artifact_names = tuple(artifact.relative_name for artifact in inputs.artifacts)
    if artifact_names != _ARTIFACT_NAMES:
        raise CrossPoolContractError("generated artifacts must use the exact file set")
    if len(set(artifact_names)) != len(artifact_names):
        raise CrossPoolContractError("generated artifact names must be unique")

    manifest: dict[str, JsonValue] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_status": "generated_unreviewed",
        "parent": deepcopy(_PARENT_PAYLOAD),
        "provenance": _provenance_payload(inputs.provenance),
        "contract": deepcopy(_CONTRACT_PAYLOAD),
        "qa": {
            "status": "pass",
            "reasons": [],
            "counts": [_qa_count_payload(count) for count in inputs.qa_counts],
            "parent_anchor_reconciled": True,
        },
        "results": {
            "status": "complete",
            "cohorts": [_inference_payload(value) for value in inputs.inferences],
        },
        "publication": deepcopy(_PUBLICATION_PAYLOAD),
        "artifacts": {artifact.relative_name: artifact.sha256 for artifact in inputs.artifacts},
        "review": {
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at_utc": None,
        },
    }
    validate_short_horizon_manifest(manifest)
    return manifest


def build_qa_blocked_short_horizon_manifest(
    inputs: QaBlockedManifestInput,
) -> dict[str, JsonValue]:
    if not isinstance(inputs, QaBlockedManifestInput):
        raise CrossPoolContractError("QA-blocked manifest input must use QaBlockedManifestInput")
    _validate_frozen_provenance(inputs.provenance, allow_unfrozen_inputs=True)
    _validate_blocked_reason_bindings(inputs.provenance, inputs.reason_codes)
    manifest: dict[str, JsonValue] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_status": "qa_blocked",
        "parent": deepcopy(_PARENT_PAYLOAD),
        "provenance": _provenance_payload(inputs.provenance),
        "contract": deepcopy(_CONTRACT_PAYLOAD),
        "qa": {
            "status": "blocked",
            "reasons": list(inputs.reason_codes),
            "counts": None,
            "parent_anchor_reconciled": None,
        },
        "results": {"status": "unavailable", "cohorts": None},
        "publication": deepcopy(_PUBLICATION_PAYLOAD),
        "artifacts": {},
        "review": {
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at_utc": None,
        },
    }
    validate_short_horizon_manifest(manifest)
    return manifest


def reviewed_short_horizon_manifest(
    payload: Mapping[str, JsonValue],
    *,
    reviewed_by: str,
    reviewed_at_utc: str,
) -> dict[str, JsonValue]:
    validate_short_horizon_manifest(payload)
    if payload["artifact_status"] == "reviewed":
        raise CrossPoolContractError("short-horizon evidence is already reviewed")
    qa = cast(dict[str, JsonValue], payload["qa"])
    if payload["artifact_status"] != "generated_unreviewed" or qa["status"] != "pass":
        raise CrossPoolContractError("review requires QA-pass generated short-horizon evidence")
    _validate_reviewer(reviewed_by)
    _validate_review_timestamp(reviewed_at_utc)
    reviewed = deepcopy(dict(payload))
    reviewed["artifact_status"] = "reviewed"
    reviewed["review"] = {
        "status": "reviewed",
        "reviewed_by": reviewed_by,
        "reviewed_at_utc": reviewed_at_utc,
    }
    validate_short_horizon_manifest(reviewed)
    return reviewed


def canonical_short_horizon_manifest_bytes(
    payload: Mapping[str, JsonValue],
) -> bytes:
    _require_json_value(payload, path="$", allow_mapping=True)
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - recursive guard owns this.
        raise CrossPoolContractError(
            "short-horizon manifest must contain canonical JSON values"
        ) from exc
    return (text + "\n").encode("utf-8")


def validate_short_horizon_manifest(payload: Mapping[str, JsonValue]) -> None:
    if not isinstance(payload, dict):
        raise CrossPoolContractError("short-horizon manifest schema requires a plain JSON object")
    canonical_short_horizon_manifest_bytes(payload)
    errors = sorted(
        _manifest_validator().iter_errors(payload),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    if errors:
        error = errors[0]
        location = "$" + "".join(f"[{part!r}]" for part in error.absolute_path)
        raise CrossPoolContractError(
            f"short-horizon manifest schema validation failed at {location}"
        ) from error
    _validate_manifest_semantics(payload)


def load_and_validate_short_horizon_manifest(path: Path) -> dict[str, JsonValue]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CrossPoolContractError("short-horizon manifest file could not be read") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError(
            "short-horizon manifest must use canonical UTF-8 JSON"
        ) from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except CrossPoolContractError:
        raise
    except json.JSONDecodeError as exc:
        raise CrossPoolContractError("short-horizon manifest must use canonical JSON") from exc
    if not isinstance(parsed, dict):
        raise CrossPoolContractError("short-horizon manifest schema requires a JSON object")
    payload = cast(dict[str, JsonValue], parsed)
    if raw != canonical_short_horizon_manifest_bytes(payload):
        raise CrossPoolContractError("short-horizon manifest bytes are not canonical")
    validate_short_horizon_manifest(payload)
    return payload


def capture_short_horizon_source(
    repo_root: Path,
) -> SourceCapture:
    paths = SHORT_HORIZON_SOURCE_DEPENDENCIES
    commit = _git_read(repo_root, ("rev-parse", "--verify", "HEAD")).strip()
    _require_commit(commit)
    tracked = tuple(
        line
        for line in _git_read(
            repo_root,
            ("ls-files", "--error-unmatch", "--", *paths),
        ).splitlines()
        if line
    )
    if set(tracked) != set(paths):
        raise CrossPoolContractError("source dependency closure contains an untracked path")
    _require_clean_dependency_paths(repo_root, paths)

    dependencies: list[SourceDependencyIdentity] = []
    for relative_path in paths:
        path = repo_root / relative_path
        try:
            before = path.lstat()
            raw = path.read_bytes()
            after = path.lstat()
        except OSError as exc:
            raise CrossPoolContractError("source dependency closure could not be read") from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise CrossPoolContractError("source dependency must be one stable regular file")
        if raw != _git_bytes(repo_root, ("show", f"{commit}:{relative_path}")):
            raise CrossPoolContractError("source dependency closure is dirty")
        dependencies.append(
            SourceDependencyIdentity(
                relative_path=relative_path,
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    _require_clean_dependency_paths(repo_root, paths)
    if _git_read(repo_root, ("rev-parse", "--verify", "HEAD")).strip() != commit:
        raise CrossPoolContractError("source commit changed during capture")
    return SourceCapture(code_commit=commit, dependencies=tuple(dependencies))


def _validate_generated_support(inputs: GeneratedManifestInput) -> None:
    if tuple(count.direction for count in inputs.qa_counts) != _DIRECTION_ORDER:
        raise CrossPoolContractError("QA directions must use canonical order")
    profiles = tuple((value.direction, value.cohort) for value in inputs.inferences)
    if profiles != _PROFILE_ORDER:
        raise CrossPoolContractError("inference profiles must use canonical order")
    counts_by_direction = {count.direction: count for count in inputs.qa_counts}
    inferences = {(value.direction, value.cohort): value for value in inputs.inferences}
    for direction in _DIRECTION_ORDER:
        expected_events, expected_days = _ANCHOR_COUNTS[direction]
        qa = counts_by_direction[direction]
        if (
            qa.detected_shock_count != expected_events
            or qa.eligible_shock_count != expected_events
            or qa.exclusion_count != 0
        ):
            raise CrossPoolContractError("generated QA counts must match the frozen parent anchor")
        primary = inferences[(direction, "primary")]
        sensitivity = inferences[(direction, "exclude_same_timestamp")]
        if (
            primary.eligible_event_count != expected_events
            or primary.shock_day_count != expected_days
            or sensitivity.eligible_event_count != expected_events - qa.same_timestamp_shock_count
            or sensitivity.shock_day_count > primary.shock_day_count
        ):
            raise CrossPoolContractError(
                "inference support must reconcile with QA and parent anchors"
            )
        anchor = primary.summaries[-1].unconditional_mean_response_bps.point
        if anchor != _ANCHOR_MEANS[direction]:
            raise CrossPoolContractError(
                "15-minute response mean must match the frozen parent anchor"
            )


def _validate_frozen_provenance(
    provenance: ShortHorizonProvenance,
    *,
    allow_unfrozen_inputs: bool,
) -> None:
    if provenance.parent_manifest.sha256 != PARENT_MANIFEST_SHA256:
        raise CrossPoolContractError("parent manifest identity does not match the freeze")
    expected_inputs = (
        ("base features", provenance.base_features, BASE_FEATURES_SHA256),
        ("BSC features", provenance.bsc_features, BSC_FEATURES_SHA256),
        ("Girum note", provenance.girum_note, GIRUM_NOTE_SHA256),
    )
    for label, identity, expected_digest in expected_inputs:
        if allow_unfrozen_inputs:
            continue
        if identity is None:
            raise CrossPoolContractError(f"{label} identity is required")
        if identity.sha256 != expected_digest:
            raise CrossPoolContractError(f"{label} identity does not match the freeze")


def _validate_blocked_reason_bindings(
    provenance: ShortHorizonProvenance,
    reasons: tuple[QaReasonCode, ...],
) -> None:
    bindings = (
        (
            provenance.base_features,
            BASE_FEATURES_SHA256,
            "missing_base_features",
            "base_features_hash_mismatch",
        ),
        (
            provenance.bsc_features,
            BSC_FEATURES_SHA256,
            "missing_bsc_features",
            "bsc_features_hash_mismatch",
        ),
        (
            provenance.girum_note,
            GIRUM_NOTE_SHA256,
            "missing_girum_note",
            "girum_note_hash_mismatch",
        ),
    )
    for identity, expected_digest, missing_reason, mismatch_reason in bindings:
        expected_reason: str | None
        if identity is None:
            expected_reason = missing_reason
        elif identity.sha256 != expected_digest:
            expected_reason = mismatch_reason
        else:
            expected_reason = None
        for reason in (missing_reason, mismatch_reason):
            if (reason in reasons) != (reason == expected_reason):
                raise CrossPoolContractError(
                    "QA-blocked input reasons must match observed identities"
                )
    if not any(
        reason
        in {
            "measurement_failure",
            "inference_failure",
            "parent_anchor_mismatch",
            "reporting_failure",
        }
        for reason in reasons
    ) and all(
        identity is not None and identity.sha256 == expected_digest
        for identity, expected_digest, _, _ in bindings
    ):
        raise CrossPoolContractError("QA-blocked reasons do not identify an extension failure")


def _provenance_payload(provenance: ShortHorizonProvenance) -> dict[str, JsonValue]:
    return {
        "inputs": {
            "parent_manifest": _input_payload(provenance.parent_manifest),
            "base_features": _optional_input_payload(provenance.base_features),
            "bsc_features": _optional_input_payload(provenance.bsc_features),
            "girum_note": _optional_input_payload(provenance.girum_note),
        },
        "code_commit": provenance.code_commit,
        "source_dependencies": {
            item.relative_path: item.sha256 for item in provenance.source_dependencies
        },
        "runtime": {key: value for key, value in provenance.runtime},
    }


def _input_payload(identity: InputIdentity) -> dict[str, JsonValue]:
    return {"sha256": identity.sha256, "byte_count": identity.byte_count}


def _optional_input_payload(
    identity: InputIdentity | None,
) -> dict[str, JsonValue] | None:
    return None if identity is None else _input_payload(identity)


def _qa_count_payload(count: DirectionQaCounts) -> dict[str, JsonValue]:
    return {
        "direction": count.direction,
        "detected_shock_count": count.detected_shock_count,
        "eligible_shock_count": count.eligible_shock_count,
        "exclusion_count": count.exclusion_count,
        "same_timestamp_shock_count": count.same_timestamp_shock_count,
    }


def _inference_payload(inference: ShortHorizonInference) -> dict[str, JsonValue]:
    return {
        "direction": inference.direction,
        "cohort": inference.cohort,
        "eligible_event_count": inference.eligible_event_count,
        "shock_day_count": inference.shock_day_count,
        "resamples": inference.resamples,
        "seed": inference.seed,
        "confidence_level": _json_number(inference.confidence_level),
        "summaries": [_summary_payload(summary) for summary in inference.summaries],
        "simultaneous_bands": [_band_payload(family) for family in inference.simultaneous_bands],
    }


def _summary_payload(summary: ShortHorizonSummary) -> dict[str, JsonValue]:
    return {
        "horizon_ms": summary.horizon_ms,
        "eligible_event_count": summary.eligible_event_count,
        "shock_day_count": summary.shock_day_count,
        "unconditional_mean_response_bps": _interval_payload(
            summary.unconditional_mean_response_bps
        ),
        "unconditional_median_response_bps": _interval_payload(
            summary.unconditional_median_response_bps
        ),
        "direction_agreement_share": _json_number(summary.direction_agreement_share),
        "zero_response_share": _json_number(summary.zero_response_share),
        "update_count": summary.update_count,
        "right_censored_count": summary.right_censored_count,
        "update_incidence": _json_number(summary.update_incidence),
        "observed_update_day_count": summary.observed_update_day_count,
        "conditional_first_update_mean_response_bps": _optional_json_number(
            summary.conditional_first_update_mean_response_bps
        ),
        "conditional_first_update_median_response_bps": _optional_json_number(
            summary.conditional_first_update_median_response_bps
        ),
        "conditional_first_update_median_delay_ms": _optional_json_number(
            summary.conditional_first_update_median_delay_ms
        ),
        "conditional_first_update_p95_delay_ms": _optional_json_number(
            summary.conditional_first_update_p95_delay_ms
        ),
        "target_start_median_age_ms": _json_number(summary.target_start_median_age_ms),
        "target_start_p95_age_ms": _json_number(summary.target_start_p95_age_ms),
        "target_end_median_age_ms": _json_number(summary.target_end_median_age_ms),
        "target_end_p95_age_ms": _json_number(summary.target_end_p95_age_ms),
        "mean_fee_gap_start_bps": _json_number(summary.mean_fee_gap_start_bps),
        "mean_fee_gap_end_bps": _json_number(summary.mean_fee_gap_end_bps),
        "positive_fee_gap_start_share": _json_number(summary.positive_fee_gap_start_share),
        "positive_fee_gap_end_share": _json_number(summary.positive_fee_gap_end_share),
        "mean_fee_gap_closure_bps": _json_number(summary.mean_fee_gap_closure_bps),
    }


def _interval_payload(interval: PointwiseInterval) -> dict[str, JsonValue]:
    return {
        "point": _json_number(interval.point),
        "lower": _json_number(interval.lower),
        "upper": _json_number(interval.upper),
    }


def _band_payload(family: SimultaneousBandFamily) -> dict[str, JsonValue]:
    return {
        "metric": family.metric,
        "status": family.status,
        "reason": family.reason,
        "valid_joint_resamples": family.valid_joint_resamples,
        "critical_value": _optional_json_number(family.critical_value),
        "points": [_band_point_payload(point) for point in family.points],
    }


def _band_point_payload(point: SimultaneousBandPoint) -> dict[str, JsonValue]:
    return {
        "horizon_ms": point.horizon_ms,
        "point": _json_number(point.point),
        "lower": _json_number(point.lower),
        "upper": _json_number(point.upper),
        "bootstrap_standard_deviation": _json_number(point.bootstrap_standard_deviation),
    }


def _validate_manifest_semantics(payload: Mapping[str, JsonValue]) -> None:
    if payload["parent"] != _PARENT_PAYLOAD:
        raise CrossPoolContractError("short-horizon parent binding is not frozen")
    if payload["contract"] != _CONTRACT_PAYLOAD:
        raise CrossPoolContractError("short-horizon research contract is not frozen")
    if payload["publication"] != _PUBLICATION_PAYLOAD:
        raise CrossPoolContractError("short-horizon publication boundary is not frozen")
    status = cast(ArtifactStatus, payload["artifact_status"])
    provenance = cast(dict[str, JsonValue], payload["provenance"])
    _validate_provenance_payload(
        provenance,
        allow_unfrozen_inputs=status == "qa_blocked",
    )
    qa = cast(dict[str, JsonValue], payload["qa"])
    results = cast(dict[str, JsonValue], payload["results"])
    artifacts = cast(dict[str, JsonValue], payload["artifacts"])
    review = cast(dict[str, JsonValue], payload["review"])
    if status == "qa_blocked":
        reasons = cast(list[JsonValue], qa["reasons"])
        if (
            qa
            != {
                "status": "blocked",
                "reasons": reasons,
                "counts": None,
                "parent_anchor_reconciled": None,
            }
            or not reasons
            or tuple(cast(str, value) for value in reasons)
            != tuple(sorted(set(cast(str, value) for value in reasons)))
            or any(value not in _QA_REASON_CODES for value in reasons)
            or results != {"status": "unavailable", "cohorts": None}
            or artifacts
            or review
            != {
                "status": "pending",
                "reviewed_by": None,
                "reviewed_at_utc": None,
            }
        ):
            raise CrossPoolContractError("QA-blocked manifest semantics are invalid")
        _validate_blocked_reason_payload(
            cast(dict[str, JsonValue], provenance["inputs"]),
            tuple(cast(str, value) for value in reasons),
        )
        return
    if qa["status"] != "pass" or qa["reasons"] != []:
        raise CrossPoolContractError("publishable manifest requires QA pass")
    if qa["parent_anchor_reconciled"] is not True:
        raise CrossPoolContractError("publishable manifest requires parent reconciliation")
    _validate_qa_counts(cast(list[JsonValue], qa["counts"]))
    _validate_results_payload(results, cast(list[JsonValue], qa["counts"]))
    if set(artifacts) != set(_ARTIFACT_NAMES):
        raise CrossPoolContractError("publishable manifest artifact set is invalid")
    if status == "generated_unreviewed":
        if review != {
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at_utc": None,
        }:
            raise CrossPoolContractError("generated manifest review state is invalid")
    elif status == "reviewed":
        if review["status"] != "reviewed":
            raise CrossPoolContractError("reviewed manifest review state is invalid")
        _validate_reviewer(cast(str, review["reviewed_by"]))
        _validate_review_timestamp(cast(str, review["reviewed_at_utc"]))
    else:
        raise CrossPoolContractError("unsupported short-horizon artifact status")


def _validate_provenance_payload(
    payload: Mapping[str, JsonValue],
    *,
    allow_unfrozen_inputs: bool,
) -> None:
    inputs = cast(dict[str, JsonValue], payload["inputs"])
    parent = cast(dict[str, JsonValue], inputs["parent_manifest"])
    if parent["sha256"] != PARENT_MANIFEST_SHA256:
        raise CrossPoolContractError("parent manifest provenance is invalid")
    for name, digest in (
        ("base_features", BASE_FEATURES_SHA256),
        ("bsc_features", BSC_FEATURES_SHA256),
        ("girum_note", GIRUM_NOTE_SHA256),
    ):
        identity = inputs[name]
        if not allow_unfrozen_inputs and identity is None:
            raise CrossPoolContractError(f"{name} provenance is required")
        if (
            not allow_unfrozen_inputs
            and identity is not None
            and cast(dict[str, JsonValue], identity)["sha256"] != digest
        ):
            raise CrossPoolContractError(f"{name} provenance is not frozen")
    dependencies = cast(dict[str, JsonValue], payload["source_dependencies"])
    paths = tuple(dependencies)
    if paths != SHORT_HORIZON_SOURCE_DEPENDENCIES:
        raise CrossPoolContractError(
            "source provenance requires the exact source dependency closure"
        )
    runtime = cast(dict[str, JsonValue], payload["runtime"])
    if tuple(runtime) != tuple(sorted(runtime)):
        raise CrossPoolContractError("runtime provenance is not canonical")


def _validate_blocked_reason_payload(
    inputs: Mapping[str, JsonValue],
    reasons: tuple[str, ...],
) -> None:
    bindings = (
        (
            inputs["base_features"],
            BASE_FEATURES_SHA256,
            "missing_base_features",
            "base_features_hash_mismatch",
        ),
        (
            inputs["bsc_features"],
            BSC_FEATURES_SHA256,
            "missing_bsc_features",
            "bsc_features_hash_mismatch",
        ),
        (
            inputs["girum_note"],
            GIRUM_NOTE_SHA256,
            "missing_girum_note",
            "girum_note_hash_mismatch",
        ),
    )
    for raw_identity, expected_digest, missing_reason, mismatch_reason in bindings:
        if raw_identity is None:
            expected_reason = missing_reason
        elif cast(dict[str, JsonValue], raw_identity)["sha256"] != expected_digest:
            expected_reason = mismatch_reason
        else:
            expected_reason = None
        for reason in (missing_reason, mismatch_reason):
            if (reason in reasons) != (reason == expected_reason):
                raise CrossPoolContractError("QA-blocked input reasons must match provenance")


def _validate_qa_counts(raw_counts: list[JsonValue]) -> None:
    counts = tuple(cast(dict[str, JsonValue], value) for value in raw_counts)
    if tuple(value["direction"] for value in counts) != _DIRECTION_ORDER:
        raise CrossPoolContractError("QA count directions are not canonical")
    for count in counts:
        direction = cast(Direction, count["direction"])
        expected_events, _ = _ANCHOR_COUNTS[direction]
        same_timestamp_count = count["same_timestamp_shock_count"]
        if (
            count["detected_shock_count"] != expected_events
            or count["eligible_shock_count"] != expected_events
            or count["exclusion_count"] != 0
            or not isinstance(same_timestamp_count, int)
            or isinstance(same_timestamp_count, bool)
            or same_timestamp_count < 0
        ):
            raise CrossPoolContractError("QA counts do not match parent support")


def _validate_results_payload(
    payload: Mapping[str, JsonValue],
    raw_counts: list[JsonValue],
) -> None:
    if payload["status"] != "complete" or not isinstance(payload["cohorts"], list):
        raise CrossPoolContractError("publishable results must be complete")
    cohorts = tuple(cast(dict[str, JsonValue], value) for value in payload["cohorts"])
    if tuple((value.get("direction"), value.get("cohort")) for value in cohorts) != (
        _PROFILE_ORDER
    ):
        raise CrossPoolContractError("result cohorts are not canonical")
    counts = {
        cast(Direction, count["direction"]): count
        for count in (cast(dict[str, JsonValue], value) for value in raw_counts)
    }
    expected_cohort_keys = {
        "direction",
        "cohort",
        "eligible_event_count",
        "shock_day_count",
        "resamples",
        "seed",
        "confidence_level",
        "summaries",
        "simultaneous_bands",
    }
    typed_inferences: list[ShortHorizonInference] = []
    for cohort in cohorts:
        if set(cohort) != expected_cohort_keys:
            raise CrossPoolContractError("result cohort fields are invalid")
        direction = cast(Direction, cohort["direction"])
        cohort_name = cast(str, cohort["cohort"])
        inference = _inference_from_payload(cohort)
        typed_inferences.append(inference)
        expected_events, expected_days = _ANCHOR_COUNTS[direction]
        same_timestamp = cast(int, counts[direction]["same_timestamp_shock_count"])
        expected_cohort_events = (
            expected_events if cohort_name == "primary" else expected_events - same_timestamp
        )
        if (
            cohort["eligible_event_count"] != expected_cohort_events
            or (cohort_name == "primary" and cohort["shock_day_count"] != expected_days)
            or cohort["resamples"] != SHORT_HORIZON_BOOTSTRAP_RESAMPLES
            or cohort["seed"] != SHORT_HORIZON_BOOTSTRAP_SEED
            or cohort["confidence_level"] != SHORT_HORIZON_BOOTSTRAP_CONFIDENCE_LEVEL
        ):
            raise CrossPoolContractError("result cohort support or bootstrap is invalid")
        if cohort_name == "primary":
            if (
                inference.summaries[-1].unconditional_mean_response_bps.point
                != _ANCHOR_MEANS[direction]
            ):
                raise CrossPoolContractError("15-minute parent mean is not reconciled")
    typed_by_key = {
        (inference.direction, inference.cohort): inference for inference in typed_inferences
    }
    for direction in _DIRECTION_ORDER:
        primary = typed_by_key[(direction, "primary")]
        sensitivity = typed_by_key[(direction, "exclude_same_timestamp")]
        if sensitivity.shock_day_count > primary.shock_day_count:
            raise CrossPoolContractError("same-timestamp sensitivity cannot add shock days")


def _inference_from_payload(payload: Mapping[str, JsonValue]) -> ShortHorizonInference:
    summaries_raw = payload["summaries"]
    bands_raw = payload["simultaneous_bands"]
    if not isinstance(summaries_raw, list) or not isinstance(bands_raw, list):
        raise CrossPoolContractError("inference summaries and bands must be arrays")
    direction = cast(Direction, payload["direction"])
    return ShortHorizonInference(
        direction=direction,
        cohort=cast(Cohort, payload["cohort"]),
        eligible_event_count=_require_json_positive_int(
            payload["eligible_event_count"], "eligible event count"
        ),
        shock_day_count=_require_json_positive_int(payload["shock_day_count"], "shock day count"),
        resamples=_require_json_positive_int(payload["resamples"], "resamples"),
        seed=_require_json_nonnegative_int(payload["seed"], "seed"),
        confidence_level=_require_json_finite_number(
            payload["confidence_level"], "confidence level"
        ),
        summaries=tuple(
            _summary_from_payload(direction, cast(dict[str, JsonValue], value))
            for value in summaries_raw
        ),
        simultaneous_bands=tuple(
            _band_from_payload(cast(dict[str, JsonValue], value)) for value in bands_raw
        ),
    )


def _summary_from_payload(
    direction: Direction,
    payload: Mapping[str, JsonValue],
) -> ShortHorizonSummary:
    expected_keys = {
        "horizon_ms",
        "eligible_event_count",
        "shock_day_count",
        "unconditional_mean_response_bps",
        "unconditional_median_response_bps",
        "direction_agreement_share",
        "zero_response_share",
        "update_count",
        "right_censored_count",
        "update_incidence",
        "observed_update_day_count",
        "conditional_first_update_mean_response_bps",
        "conditional_first_update_median_response_bps",
        "conditional_first_update_median_delay_ms",
        "conditional_first_update_p95_delay_ms",
        "target_start_median_age_ms",
        "target_start_p95_age_ms",
        "target_end_median_age_ms",
        "target_end_p95_age_ms",
        "mean_fee_gap_start_bps",
        "mean_fee_gap_end_bps",
        "positive_fee_gap_start_share",
        "positive_fee_gap_end_share",
        "mean_fee_gap_closure_bps",
    }
    if set(payload) != expected_keys:
        raise CrossPoolContractError("summary fields are invalid")
    return ShortHorizonSummary(
        direction=direction,
        horizon_ms=_require_json_positive_int(payload["horizon_ms"], "horizon"),
        eligible_event_count=_require_json_positive_int(
            payload["eligible_event_count"], "eligible event count"
        ),
        shock_day_count=_require_json_positive_int(payload["shock_day_count"], "shock day count"),
        unconditional_mean_response_bps=_interval_from_payload(
            cast(dict[str, JsonValue], payload["unconditional_mean_response_bps"])
        ),
        unconditional_median_response_bps=_interval_from_payload(
            cast(dict[str, JsonValue], payload["unconditional_median_response_bps"])
        ),
        direction_agreement_share=_require_json_finite_number(
            payload["direction_agreement_share"], "direction agreement share"
        ),
        zero_response_share=_require_json_finite_number(
            payload["zero_response_share"], "zero response share"
        ),
        update_count=_require_json_nonnegative_int(payload["update_count"], "update count"),
        right_censored_count=_require_json_nonnegative_int(
            payload["right_censored_count"], "right-censored count"
        ),
        update_incidence=_require_json_finite_number(
            payload["update_incidence"], "update incidence"
        ),
        observed_update_day_count=_require_json_nonnegative_int(
            payload["observed_update_day_count"], "observed update day count"
        ),
        conditional_first_update_mean_response_bps=_optional_payload_number(
            payload["conditional_first_update_mean_response_bps"],
            "conditional first-update mean response",
        ),
        conditional_first_update_median_response_bps=_optional_payload_number(
            payload["conditional_first_update_median_response_bps"],
            "conditional first-update median response",
        ),
        conditional_first_update_median_delay_ms=_optional_payload_number(
            payload["conditional_first_update_median_delay_ms"],
            "conditional first-update median delay",
        ),
        conditional_first_update_p95_delay_ms=_optional_payload_number(
            payload["conditional_first_update_p95_delay_ms"],
            "conditional first-update p95 delay",
        ),
        target_start_median_age_ms=_require_json_finite_number(
            payload["target_start_median_age_ms"], "target start median age"
        ),
        target_start_p95_age_ms=_require_json_finite_number(
            payload["target_start_p95_age_ms"], "target start p95 age"
        ),
        target_end_median_age_ms=_require_json_finite_number(
            payload["target_end_median_age_ms"], "target end median age"
        ),
        target_end_p95_age_ms=_require_json_finite_number(
            payload["target_end_p95_age_ms"], "target end p95 age"
        ),
        mean_fee_gap_start_bps=_require_json_finite_number(
            payload["mean_fee_gap_start_bps"], "mean fee gap start"
        ),
        mean_fee_gap_end_bps=_require_json_finite_number(
            payload["mean_fee_gap_end_bps"], "mean fee gap end"
        ),
        positive_fee_gap_start_share=_require_json_finite_number(
            payload["positive_fee_gap_start_share"],
            "positive fee gap start share",
        ),
        positive_fee_gap_end_share=_require_json_finite_number(
            payload["positive_fee_gap_end_share"], "positive fee gap end share"
        ),
        mean_fee_gap_closure_bps=_require_json_finite_number(
            payload["mean_fee_gap_closure_bps"], "mean fee gap closure"
        ),
    )


def _interval_from_payload(payload: Mapping[str, JsonValue]) -> PointwiseInterval:
    if set(payload) != {"point", "lower", "upper"}:
        raise CrossPoolContractError("pointwise interval fields are invalid")
    return PointwiseInterval(
        point=_require_json_finite_number(payload["point"], "interval point"),
        lower=_require_json_finite_number(payload["lower"], "interval lower"),
        upper=_require_json_finite_number(payload["upper"], "interval upper"),
    )


def _band_from_payload(payload: Mapping[str, JsonValue]) -> SimultaneousBandFamily:
    if set(payload) != {
        "metric",
        "status",
        "reason",
        "valid_joint_resamples",
        "critical_value",
        "points",
    } or not isinstance(payload["points"], list):
        raise CrossPoolContractError("simultaneous-band fields are invalid")
    reason = payload["reason"]
    if reason is not None and not isinstance(reason, str):
        raise CrossPoolContractError("simultaneous-band reason is invalid")
    return SimultaneousBandFamily(
        metric=cast(SimultaneousMetric, payload["metric"]),
        status=cast(BandStatus, payload["status"]),
        reason=cast(BandUnavailableReason | None, reason),
        valid_joint_resamples=_require_json_nonnegative_int(
            payload["valid_joint_resamples"], "valid joint resamples"
        ),
        critical_value=_optional_payload_number(payload["critical_value"], "critical value"),
        points=tuple(
            _band_point_from_payload(cast(dict[str, JsonValue], value))
            for value in payload["points"]
        ),
    )


def _band_point_from_payload(
    payload: Mapping[str, JsonValue],
) -> SimultaneousBandPoint:
    if set(payload) != {
        "horizon_ms",
        "point",
        "lower",
        "upper",
        "bootstrap_standard_deviation",
    }:
        raise CrossPoolContractError("simultaneous-band point fields are invalid")
    return SimultaneousBandPoint(
        horizon_ms=_require_json_positive_int(payload["horizon_ms"], "horizon"),
        point=_require_json_finite_number(payload["point"], "band point"),
        lower=_require_json_finite_number(payload["lower"], "band lower"),
        upper=_require_json_finite_number(payload["upper"], "band upper"),
        bootstrap_standard_deviation=_require_json_finite_number(
            payload["bootstrap_standard_deviation"],
            "band bootstrap standard deviation",
        ),
    )


@lru_cache(maxsize=1)
def _manifest_validator() -> Draft202012Validator:
    try:
        schema = json.loads(
            _SCHEMA_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise CrossPoolContractError(
            "short-horizon manifest schema is unavailable or invalid"
        ) from exc
    if not isinstance(schema, dict):
        raise CrossPoolContractError("short-horizon manifest schema must be an object")
    return Draft202012Validator(schema)


def _git_read(repo_root: Path, args: tuple[str, ...]) -> str:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=repo_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CrossPoolContractError("source Git provenance could not be read") from exc
    return completed.stdout


def _git_bytes(repo_root: Path, args: tuple[str, ...]) -> bytes:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=repo_root,
            env=environment,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CrossPoolContractError("source Git provenance could not be read") from exc
    return completed.stdout


def _require_clean_dependency_paths(
    repo_root: Path,
    paths: tuple[str, ...],
) -> None:
    status_output = _git_read(
        repo_root,
        ("status", "--porcelain=v1", "--untracked-files=all", "--", *paths),
    )
    if status_output:
        raise CrossPoolContractError("source dependency closure is dirty")


def _validate_relative_path(value: str) -> None:
    candidate = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or candidate.is_absolute()
        or candidate.as_posix() != value
        or any(part in ("", ".", "..") for part in candidate.parts)
    ):
        raise CrossPoolContractError("source dependency path must be safe and relative")


def _json_number(value: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CrossPoolContractError("manifest numeric value must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise CrossPoolContractError("manifest numeric value must be finite")
    return 0 if result == 0.0 else value


def _optional_json_number(value: float | None) -> int | float | None:
    return None if value is None else _json_number(value)


def _require_json_finite_number(value: JsonValue, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CrossPoolContractError(f"{label} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise CrossPoolContractError(f"{label} must be finite")
    return result


def _optional_payload_number(value: JsonValue, label: str) -> float | None:
    return None if value is None else _require_json_finite_number(value, label)


def _require_json_positive_int(value: JsonValue, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CrossPoolContractError(f"{label} must be a positive integer")
    return value


def _require_json_nonnegative_int(value: JsonValue, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrossPoolContractError(f"{label} must be a nonnegative integer")
    return value


def _require_sha256(value: str, label: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise CrossPoolContractError(f"{label} SHA-256 must be lowercase hexadecimal")


def _require_commit(value: str) -> None:
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise CrossPoolContractError("source commit must be lowercase hexadecimal")


def _require_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CrossPoolContractError(f"{label} must be a positive integer")


def _require_nonnegative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrossPoolContractError(f"{label} must be a nonnegative integer")


def _validate_reviewer(value: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CrossPoolContractError("reviewer identity must be explicit and nonempty")


def _validate_review_timestamp(value: str) -> None:
    if not isinstance(value, str) or _UTC_REVIEW_PATTERN.fullmatch(value) is None:
        raise CrossPoolContractError("review timestamp must be an explicit UTC Z value")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise CrossPoolContractError("review timestamp must be an explicit UTC Z value") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise CrossPoolContractError("review timestamp must be an explicit UTC Z value")


def _require_json_value(
    value: object,
    *,
    path: str,
    allow_mapping: bool = False,
) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CrossPoolContractError(
                f"short-horizon manifest JSON number at {path} must be finite"
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        if not allow_mapping and not isinstance(value, dict):
            raise CrossPoolContractError(
                f"short-horizon manifest JSON object at {path} must be a plain dictionary"
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise CrossPoolContractError(
                    f"short-horizon manifest JSON object key at {path} must be a string"
                )
            _require_json_value(item, path=f"{path}.{key}")
        return
    raise CrossPoolContractError(
        f"short-horizon manifest value at {path} is not a supported JSON value"
    )


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CrossPoolContractError(f"short-horizon manifest contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise CrossPoolContractError(
        f"short-horizon manifest contains non-finite JSON constant {value}"
    )
