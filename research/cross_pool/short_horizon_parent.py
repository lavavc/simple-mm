"""Exact 15-minute reconciliation against the frozen reviewed parent package."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from statistics import median
from typing import Mapping, cast

from research.cross_pool.contracts import CrossPoolContractError, Direction
from research.cross_pool.short_horizon import ShortHorizonStudy
from research.cross_pool.short_horizon_manifest import (
    BASE_FEATURES_SHA256,
    BSC_FEATURES_SHA256,
    PARENT_MANIFEST_SHA256,
)

PARENT_EVENT_STUDY_SHA256 = "b9e36198523b1b4ea070dd368999fa138f58df440a8be7e95a2c9a9c18e22211"
PARENT_15M_PROJECTION_SHA256 = "f795f1b58672cf32d3977bab836fb1537465ce241b8129a49626f67799faca12"

_DIRECTION_ORDER: tuple[Direction, ...] = ("bsc_to_base", "base_to_bsc")
_PARENT_HORIZONS_MS = (900_000, 3_600_000, 14_400_000)
_PARENT_FIELDS = (
    "outcome_type",
    "direction",
    "shock_timestamp_ms",
    "shock_day_utc",
    "horizon_ms",
    "source_pool",
    "target_pool",
    "source_move_bps",
    "target_start_timestamp_ms",
    "target_end_timestamp_ms",
    "target_response_bps",
    "direction_agrees",
    "exclusion_reason",
)
_ANCHOR_SUMMARIES = {
    "bsc_to_base": (123, 59, 0.8782298255072702, 0.0, 0.1951219512195122),
    "base_to_bsc": (197, 70, 0.03955408492685122, 0.0, 0.24873096446700507),
}


@dataclass(frozen=True)
class ParentAnchor:
    manifest_sha256: str
    event_study_sha256: str
    projection_15m_sha256: str
    event_counts: tuple[tuple[Direction, int], ...]


def validate_frozen_parent_anchor(
    parent_manifest_bytes: bytes,
    parent_event_study_bytes: bytes,
    studies: tuple[ShortHorizonStudy, ...],
) -> ParentAnchor:
    if hashlib.sha256(parent_manifest_bytes).hexdigest() != PARENT_MANIFEST_SHA256:
        raise CrossPoolContractError("parent manifest bytes do not match the frozen identity")
    manifest = _load_json_object(parent_manifest_bytes, "parent manifest")
    _validate_parent_manifest_projection(manifest, parent_event_study_bytes)
    parent_rows = _parse_parent_event_rows(parent_event_study_bytes)
    parent_projection = _projection_bytes(
        tuple(row for row in parent_rows if row["horizon_ms"] == "900000")
    )
    if hashlib.sha256(parent_projection).hexdigest() != PARENT_15M_PROJECTION_SHA256:
        raise CrossPoolContractError("parent 15-minute projection is not frozen")
    candidate_projection = _candidate_projection_bytes(studies)
    if candidate_projection != parent_projection:
        raise CrossPoolContractError("short-horizon 15-minute rows do not match the parent")
    _validate_anchor_statistics(studies, manifest)
    return ParentAnchor(
        manifest_sha256=PARENT_MANIFEST_SHA256,
        event_study_sha256=PARENT_EVENT_STUDY_SHA256,
        projection_15m_sha256=PARENT_15M_PROJECTION_SHA256,
        event_counts=tuple(
            (direction, _ANCHOR_SUMMARIES[direction][0]) for direction in _DIRECTION_ORDER
        ),
    )


def _validate_parent_manifest_projection(
    manifest: Mapping[str, object],
    event_study_bytes: bytes,
) -> None:
    try:
        publication = cast(dict[str, object], manifest["publication"])
        provenance = cast(dict[str, object], manifest["provenance"])
        input_sha256 = cast(dict[str, object], provenance["input_sha256"])
        artifacts = cast(dict[str, object], manifest["artifacts"])
        event_study = cast(dict[str, object], manifest["event_study"])
        review = cast(dict[str, object], manifest["review"])
    except (KeyError, TypeError) as exc:
        raise CrossPoolContractError("parent manifest projection is incomplete") from exc
    if (
        manifest.get("schema_version") != "2.0.0"
        or manifest.get("artifact_status") != "reviewed"
        or review.get("status") != "reviewed"
        or publication.get("article_branch") != "leadership_unresolved"
        or publication.get("economic_class") != "no_net_return_improvement"
        or publication.get("allowed_claims") != ["aggregate_leadership_unresolved"]
        or input_sha256.get("base_features") != BASE_FEATURES_SHA256
        or input_sha256.get("bsc_features") != BSC_FEATURES_SHA256
        or event_study.get("status") != "complete"
    ):
        raise CrossPoolContractError("parent decision or input binding is not frozen")
    event_digest = hashlib.sha256(event_study_bytes).hexdigest()
    if (
        event_digest != PARENT_EVENT_STUDY_SHA256
        or artifacts.get("event_study.csv") != event_digest
    ):
        raise CrossPoolContractError("parent event-study artifact does not match its manifest")


def _parse_parent_event_rows(raw: bytes) -> tuple[dict[str, str], ...]:
    if not raw.endswith(b"\n") or b"\r" in raw:
        raise CrossPoolContractError("parent event-study CSV has noncanonical newlines")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError("parent event-study CSV must use UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != _PARENT_FIELDS:
        raise CrossPoolContractError("parent event-study CSV header is not frozen")
    rows: list[dict[str, str]] = []
    keys: list[tuple[int, int, int]] = []
    for row_number, raw_row in enumerate(reader, start=2):
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise CrossPoolContractError(f"parent event row {row_number} has unexpected cells")
        row = cast(dict[str, str], raw_row)
        direction = _parse_direction(row["direction"], row_number)
        direction_index = _DIRECTION_ORDER.index(direction)
        shock_timestamp = _canonical_int(row["shock_timestamp_ms"], row_number)
        horizon = _canonical_int(row["horizon_ms"], row_number)
        if row["outcome_type"] != "response" or row["exclusion_reason"]:
            raise CrossPoolContractError("frozen parent event study must contain only responses")
        if horizon not in _PARENT_HORIZONS_MS:
            raise CrossPoolContractError("parent event-study horizon is not frozen")
        _canonical_int(row["target_start_timestamp_ms"], row_number)
        _canonical_int(row["target_end_timestamp_ms"], row_number)
        _canonical_float(row["source_move_bps"], row_number)
        _canonical_float(row["target_response_bps"], row_number)
        if row["direction_agrees"] not in ("true", "false"):
            raise CrossPoolContractError("parent event-study boolean is not canonical")
        keys.append((direction_index, shock_timestamp, horizon))
        rows.append(row)
    if tuple(keys) != tuple(sorted(set(keys))):
        raise CrossPoolContractError("parent event-study rows are not unique and canonical")
    expected_total = sum(values[0] for values in _ANCHOR_SUMMARIES.values()) * len(
        _PARENT_HORIZONS_MS
    )
    if len(rows) != expected_total:
        raise CrossPoolContractError("parent event-study row count is not frozen")
    return tuple(rows)


def _candidate_projection_bytes(studies: tuple[ShortHorizonStudy, ...]) -> bytes:
    if tuple(study.direction for study in studies) != _DIRECTION_ORDER:
        raise CrossPoolContractError("candidate studies are not in canonical order")
    rows: list[dict[str, str]] = []
    for study in studies:
        if study.exclusions:
            raise CrossPoolContractError(
                "candidate common-support exclusions break the parent anchor"
            )
        for row in study.responses:
            if row.horizon_ms != 900_000:
                continue
            rows.append(
                {
                    "outcome_type": "response",
                    "direction": row.direction,
                    "shock_timestamp_ms": str(row.shock_timestamp_ms),
                    "shock_day_utc": row.shock_day_utc.isoformat(),
                    "horizon_ms": str(row.horizon_ms),
                    "source_pool": row.source_pool,
                    "target_pool": row.target_pool,
                    "source_move_bps": _float_text(row.source_move_bps),
                    "target_start_timestamp_ms": str(row.target_start_timestamp_ms),
                    "target_end_timestamp_ms": str(row.target_end_timestamp_ms),
                    "target_response_bps": _float_text(row.target_response_bps),
                    "direction_agrees": "true" if row.direction_agrees else "false",
                    "exclusion_reason": "",
                }
            )
    return _projection_bytes(tuple(rows))


def _projection_bytes(rows: tuple[Mapping[str, str], ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_PARENT_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _validate_anchor_statistics(
    studies: tuple[ShortHorizonStudy, ...],
    manifest: Mapping[str, object],
) -> None:
    event_study = cast(dict[str, object], manifest["event_study"])
    manifest_by_direction = {
        cast(str, cast(dict[str, object], event_study[key])["direction"]): cast(
            dict[str, object],
            event_study[key],
        )
        for key in ("primary", "reverse")
    }
    for study in studies:
        rows = tuple(row for row in study.responses if row.horizon_ms == 900_000)
        expected = _ANCHOR_SUMMARIES[study.direction]
        values = tuple(row.target_response_bps for row in rows)
        observed = (
            len(rows),
            len({row.shock_day_utc for row in rows}),
            math.fsum(values) / len(values),
            float(median(values)),
            sum(row.direction_agrees for row in rows) / len(rows),
        )
        if observed != expected:
            raise CrossPoolContractError("candidate 15-minute statistics do not match the parent")
        summaries = cast(
            list[dict[str, object]],
            manifest_by_direction[study.direction]["summaries"],
        )
        summary = next(item for item in summaries if item["horizon_ms"] == 900_000)
        manifest_observed = (
            summary["event_count"],
            summary["event_day_count"],
            cast(dict[str, object], summary["mean_response_bps"])["point"],
            cast(dict[str, object], summary["median_response_bps"])["point"],
            cast(dict[str, object], summary["direction_agreement"])["point"],
        )
        if manifest_observed != expected:
            raise CrossPoolContractError("parent manifest summary does not match the frozen anchor")


def _load_json_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CrossPoolContractError(f"{label} is not valid canonical JSON") from exc
    if not isinstance(parsed, dict):
        raise CrossPoolContractError(f"{label} must be a JSON object")
    return cast(dict[str, object], parsed)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CrossPoolContractError("parent manifest contains a duplicate key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise CrossPoolContractError(f"parent manifest contains non-finite constant {value}")


def _parse_direction(value: str, row_number: int) -> Direction:
    if value not in _DIRECTION_ORDER:
        raise CrossPoolContractError(f"parent event row {row_number} has invalid direction")
    return value


def _canonical_int(value: str, row_number: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CrossPoolContractError(f"parent event row {row_number} has invalid integer") from exc
    if str(parsed) != value or parsed <= 0:
        raise CrossPoolContractError(f"parent event row {row_number} has noncanonical integer")
    return parsed


def _canonical_float(value: str, row_number: int) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise CrossPoolContractError(f"parent event row {row_number} has invalid float") from exc
    if not math.isfinite(parsed) or _float_text(parsed) != value:
        raise CrossPoolContractError(f"parent event row {row_number} has noncanonical float")
    return parsed


def _float_text(value: float) -> str:
    if not math.isfinite(value):
        raise CrossPoolContractError("parent-anchor float must be finite")
    return "0" if value == 0.0 else repr(value)
