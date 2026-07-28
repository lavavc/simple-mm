"""Strict loader for the reviewed cross-pool parent evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from research.cross_pool.challenger_contracts import (
    ChallengerParent,
    ParentAnchor,
    ParentPanel,
    ParentPrediction,
    ProjectedParentRow,
)
from research.cross_pool.contracts import (
    CrossPoolContractError,
    Direction,
    PanelRow,
    Regime,
)
from research.cross_pool.publication import OutputPublicationError, validate_evidence_directory

PARENT_ARTIFACT_SHA256: dict[str, str] = {
    "article_manifest.json": "adf4fd71fd33604cee70fcba7aa90d2b7763715d3cba68a868d26347c599abfe",
    "panel_15m.csv": "58c2f41ff3d344c092bf1e88a4836e8638ca03bb44a602f4b7b9317b3bf6e550",
    "panel_1h.csv": "4abd93587f4d0fd6613c4f7189425e5cc382f02bb188b85a247c1f79fdd3862b",
    "panel_4h.csv": "aa7f7feb8a6b30d9daed2fdb5c81a8018e54eb4cba041a6482b5b72eda94da65",
    "predictive_predictions.csv": (
        "182b76606395890aceec96f5388945508961d8b11539022874c8b9e890af157a"
    ),
}

_DAY_MS = 86_400_000
_WEEK_MS = 7 * _DAY_MS
_HORIZON_FILES = (
    (900_000, "panel_15m.csv"),
    (3_600_000, "panel_1h.csv"),
    (14_400_000, "panel_4h.csv"),
)
_DIRECTION_ORDER = {"bsc_to_base": 0, "base_to_bsc": 1}
_PANEL_FIELDS = (
    "timestamp_ms",
    "horizon_ms",
    "base_state_timestamp_ms",
    "bsc_state_timestamp_ms",
    "base_forward_state_timestamp_ms",
    "bsc_forward_state_timestamp_ms",
    "base_lag_block_number",
    "bsc_lag_block_number",
    "base_state_block_number",
    "bsc_state_block_number",
    "base_forward_block_number",
    "bsc_forward_block_number",
    "base_age_ms",
    "bsc_age_ms",
    "base_mid",
    "bsc_mid",
    "base_trailing_return_bps",
    "bsc_trailing_return_bps",
    "base_minus_bsc_gap_bps",
    "base_forward_return_bps",
    "bsc_forward_return_bps",
    "base_regime",
    "bsc_regime",
)
_PREDICTION_FIELDS = (
    "timestamp_ms",
    "target_timestamp_ms",
    "horizon_ms",
    "refit_timestamp_ms",
    "fold_index",
    "direction",
    "actual_bps",
    "baseline_prediction_bps",
    "cross_prediction_bps",
)


def load_challenger_parent(evidence_dir: Path) -> ChallengerParent:
    """Load only the exact reviewed parent package registered by the design."""
    try:
        manifest = validate_evidence_directory(evidence_dir)
    except (OSError, OutputPublicationError, CrossPoolContractError) as exc:
        raise CrossPoolContractError("parent evidence directory is invalid") from exc

    raw_by_name: dict[str, bytes] = {}
    for name, expected_digest in PARENT_ARTIFACT_SHA256.items():
        try:
            raw = (evidence_dir / name).read_bytes()
        except OSError as exc:
            raise CrossPoolContractError(f"parent artifact {name} could not be read") from exc
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise CrossPoolContractError(f"parent artifact {name} does not match the frozen hash")
        raw_by_name[name] = raw

    _validate_manifest_projection(manifest)
    panels = tuple(
        ParentPanel(
            horizon_ms=horizon_ms,
            rows=_parse_panel_bytes(
                raw_by_name[name],
                expected_horizon_ms=horizon_ms,
            ),
        )
        for horizon_ms, name in _HORIZON_FILES
    )
    predictions = _parse_prediction_bytes(raw_by_name["predictive_predictions.csv"])
    target_days = _validate_parent_alignment(panels, predictions)
    return ChallengerParent(
        anchor=ParentAnchor(
            artifact_sha256=tuple(PARENT_ARTIFACT_SHA256.items()),
            article_branch="leadership_unresolved",
            review_status="reviewed",
        ),
        panels=panels,
        predictions=predictions,
        target_days_utc=target_days,
    )


def project_parent_row(
    row: PanelRow,
    direction: Direction,
    *,
    predecessor: PanelRow | None,
) -> ProjectedParentRow:
    if predecessor is not None and (
        predecessor.horizon_ms != row.horizon_ms
        or predecessor.timestamp_ms + row.horizon_ms != row.timestamp_ms
    ):
        raise CrossPoolContractError("projected predecessor is not the prior regular row")
    if direction == "bsc_to_base":
        previous_return = (
            None if predecessor is None else predecessor.base_trailing_return_bps
        )
        return ProjectedParentRow(
            timestamp_ms=row.timestamp_ms,
            target_timestamp_ms=row.timestamp_ms + row.horizon_ms,
            horizon_ms=row.horizon_ms,
            direction=direction,
            target_return_bps=row.base_trailing_return_bps,
            previous_target_return_bps=previous_return,
            source_return_bps=row.bsc_trailing_return_bps,
            gap_bps=row.base_minus_bsc_gap_bps,
            target_age_ms=row.base_age_ms,
            source_age_ms=row.bsc_age_ms,
            actual_bps=row.base_forward_return_bps,
            updated=row.base_forward_state_timestamp_ms > row.base_state_timestamp_ms,
            target_regime=row.base_regime,
            source_regime=row.bsc_regime,
        )
    if direction == "base_to_bsc":
        previous_return = (
            None if predecessor is None else predecessor.bsc_trailing_return_bps
        )
        return ProjectedParentRow(
            timestamp_ms=row.timestamp_ms,
            target_timestamp_ms=row.timestamp_ms + row.horizon_ms,
            horizon_ms=row.horizon_ms,
            direction=direction,
            target_return_bps=row.bsc_trailing_return_bps,
            previous_target_return_bps=previous_return,
            source_return_bps=row.base_trailing_return_bps,
            gap_bps=-row.base_minus_bsc_gap_bps,
            target_age_ms=row.bsc_age_ms,
            source_age_ms=row.base_age_ms,
            actual_bps=row.bsc_forward_return_bps,
            updated=row.bsc_forward_state_timestamp_ms > row.bsc_state_timestamp_ms,
            target_regime=row.bsc_regime,
            source_regime=row.base_regime,
        )
    raise CrossPoolContractError(f"unsupported direction {direction!r}")


def _validate_manifest_projection(manifest: Mapping[str, object]) -> None:
    review = manifest.get("review")
    publication = manifest.get("publication")
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("schema_version") != "2.0.0"
        or manifest.get("artifact_status") != "reviewed"
        or not isinstance(review, dict)
        or review.get("status") != "reviewed"
        or not isinstance(publication, dict)
        or publication.get("article_branch") != "leadership_unresolved"
        or publication.get("allowed_claims") != ["aggregate_leadership_unresolved"]
        or not isinstance(artifacts, dict)
    ):
        raise CrossPoolContractError("parent review or decision projection is not frozen")
    for name, expected in PARENT_ARTIFACT_SHA256.items():
        if name == "article_manifest.json":
            continue
        if artifacts.get(name) != expected:
            raise CrossPoolContractError(f"parent manifest does not bind {name}")


def _parse_panel_bytes(raw: bytes, *, expected_horizon_ms: int) -> tuple[PanelRow, ...]:
    rows = _csv_rows(raw, _PANEL_FIELDS, "parent panel")
    parsed: list[PanelRow] = []
    for row_number, row in enumerate(rows, start=2):
        horizon_ms = _canonical_int(row["horizon_ms"], row_number, positive=True)
        if horizon_ms != expected_horizon_ms:
            raise CrossPoolContractError("parent panel horizon is not frozen")
        parsed.append(
            PanelRow(
                timestamp_ms=_canonical_int(row["timestamp_ms"], row_number, positive=True),
                horizon_ms=horizon_ms,
                base_state_timestamp_ms=_canonical_int(
                    row["base_state_timestamp_ms"], row_number, positive=True
                ),
                bsc_state_timestamp_ms=_canonical_int(
                    row["bsc_state_timestamp_ms"], row_number, positive=True
                ),
                base_forward_state_timestamp_ms=_canonical_int(
                    row["base_forward_state_timestamp_ms"], row_number, positive=True
                ),
                bsc_forward_state_timestamp_ms=_canonical_int(
                    row["bsc_forward_state_timestamp_ms"], row_number, positive=True
                ),
                base_lag_block_number=_canonical_int(
                    row["base_lag_block_number"], row_number, positive=True
                ),
                bsc_lag_block_number=_canonical_int(
                    row["bsc_lag_block_number"], row_number, positive=True
                ),
                base_state_block_number=_canonical_int(
                    row["base_state_block_number"], row_number, positive=True
                ),
                bsc_state_block_number=_canonical_int(
                    row["bsc_state_block_number"], row_number, positive=True
                ),
                base_forward_block_number=_canonical_int(
                    row["base_forward_block_number"], row_number, positive=True
                ),
                bsc_forward_block_number=_canonical_int(
                    row["bsc_forward_block_number"], row_number, positive=True
                ),
                base_age_ms=_canonical_int(row["base_age_ms"], row_number, positive=False),
                bsc_age_ms=_canonical_int(row["bsc_age_ms"], row_number, positive=False),
                base_mid=_canonical_float(row["base_mid"], row_number, positive=True),
                bsc_mid=_canonical_float(row["bsc_mid"], row_number, positive=True),
                base_trailing_return_bps=_canonical_float(
                    row["base_trailing_return_bps"], row_number
                ),
                bsc_trailing_return_bps=_canonical_float(
                    row["bsc_trailing_return_bps"], row_number
                ),
                base_minus_bsc_gap_bps=_canonical_float(
                    row["base_minus_bsc_gap_bps"], row_number
                ),
                base_forward_return_bps=_canonical_float(
                    row["base_forward_return_bps"], row_number
                ),
                bsc_forward_return_bps=_canonical_float(
                    row["bsc_forward_return_bps"], row_number
                ),
                base_regime=_canonical_regime(row["base_regime"], row_number),
                bsc_regime=_canonical_regime(row["bsc_regime"], row_number),
            )
        )
    if not parsed:
        raise CrossPoolContractError("parent panel must contain rows")
    timestamps = tuple(row.timestamp_ms for row in parsed)
    if timestamps != tuple(sorted(set(timestamps))) or any(
        current - previous != expected_horizon_ms
        for previous, current in zip(timestamps, timestamps[1:], strict=False)
    ):
        raise CrossPoolContractError("parent panel timestamps are not in canonical order")
    return tuple(parsed)


def _parse_prediction_bytes(raw: bytes) -> tuple[ParentPrediction, ...]:
    rows = _csv_rows(raw, _PREDICTION_FIELDS, "parent predictions")
    parsed: list[ParentPrediction] = []
    keys: list[tuple[int, int, int]] = []
    for row_number, row in enumerate(rows, start=2):
        direction = _canonical_direction(row["direction"], row_number)
        timestamp_ms = _canonical_int(row["timestamp_ms"], row_number, positive=True)
        target_timestamp_ms = _canonical_int(
            row["target_timestamp_ms"], row_number, positive=True
        )
        horizon_ms = _canonical_int(row["horizon_ms"], row_number, positive=True)
        refit_timestamp_ms = _canonical_int(
            row["refit_timestamp_ms"], row_number, positive=True
        )
        fold_index = _canonical_int(row["fold_index"], row_number, positive=False)
        if target_timestamp_ms != timestamp_ms + horizon_ms:
            raise CrossPoolContractError("parent prediction target timestamp is inconsistent")
        if not refit_timestamp_ms <= timestamp_ms < refit_timestamp_ms + _WEEK_MS:
            raise CrossPoolContractError("parent prediction is outside its refit fold")
        parsed.append(
            ParentPrediction(
                timestamp_ms=timestamp_ms,
                target_timestamp_ms=target_timestamp_ms,
                horizon_ms=horizon_ms,
                refit_timestamp_ms=refit_timestamp_ms,
                fold_index=fold_index,
                direction=direction,
                actual_bps=_canonical_float(row["actual_bps"], row_number),
                baseline_prediction_bps=_canonical_float(
                    row["baseline_prediction_bps"], row_number
                ),
                cross_prediction_bps=_canonical_float(
                    row["cross_prediction_bps"], row_number
                ),
            )
        )
        keys.append((_DIRECTION_ORDER[direction], timestamp_ms, target_timestamp_ms))
    if not parsed or tuple(keys) != tuple(sorted(set(keys))):
        raise CrossPoolContractError("parent predictions are not in canonical order")
    return tuple(parsed)


def _validate_parent_alignment(
    panels: tuple[ParentPanel, ...],
    predictions: tuple[ParentPrediction, ...],
) -> tuple[date, ...]:
    panel_by_key = {
        (row.timestamp_ms, row.horizon_ms): row
        for panel in panels
        for row in panel.rows
    }
    if len(panel_by_key) != sum(len(panel.rows) for panel in panels):
        raise CrossPoolContractError("parent panel keys are not unique")
    predecessor_by_key: dict[tuple[int, int], PanelRow | None] = {}
    for panel in panels:
        for index, row in enumerate(panel.rows):
            predecessor_by_key[(row.timestamp_ms, row.horizon_ms)] = (
                None if index == 0 else panel.rows[index - 1]
            )

    group_days: dict[tuple[Direction, int], set[date]] = {}
    folds: dict[tuple[Direction, int, int], int] = {}
    for prediction in predictions:
        key = (prediction.timestamp_ms, prediction.horizon_ms)
        try:
            panel_row = panel_by_key[key]
        except KeyError as exc:
            raise CrossPoolContractError("parent prediction has no matching panel row") from exc
        projected = project_parent_row(
            panel_row,
            prediction.direction,
            predecessor=predecessor_by_key[key],
        )
        if projected.actual_bps != prediction.actual_bps:
            raise CrossPoolContractError("parent prediction target disagrees with its panel row")
        fold_key = (prediction.direction, prediction.horizon_ms, prediction.fold_index)
        existing_refit = folds.setdefault(fold_key, prediction.refit_timestamp_ms)
        if existing_refit != prediction.refit_timestamp_ms:
            raise CrossPoolContractError("parent fold maps to multiple refit timestamps")
        target_day = datetime.fromtimestamp(
            prediction.target_timestamp_ms / 1_000,
            tz=UTC,
        ).date()
        group_days.setdefault((prediction.direction, prediction.horizon_ms), set()).add(
            target_day
        )
    expected_groups = {
        (direction, horizon_ms)
        for direction in cast(tuple[Direction, ...], tuple(_DIRECTION_ORDER))
        for horizon_ms, _ in _HORIZON_FILES
    }
    if set(group_days) != expected_groups:
        raise CrossPoolContractError("parent prediction grid is incomplete")
    calendars = {tuple(sorted(days)) for days in group_days.values()}
    if len(calendars) != 1:
        raise CrossPoolContractError("parent horizons do not share one target-day calendar")
    calendar = next(iter(calendars))
    if len(calendar) != 88:
        raise CrossPoolContractError("parent target-day calendar is not frozen")
    return calendar


def _csv_rows(
    raw: bytes,
    fields: tuple[str, ...],
    label: str,
) -> tuple[dict[str, str], ...]:
    if not raw.endswith(b"\n") or b"\r" in raw:
        raise CrossPoolContractError(f"{label} has noncanonical newlines")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError(f"{label} must use UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != fields:
        raise CrossPoolContractError(f"{label} header is not frozen")
    rows: list[dict[str, str]] = []
    for row_number, raw_row in enumerate(reader, start=2):
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise CrossPoolContractError(f"{label} row {row_number} has unexpected cells")
        rows.append(cast(dict[str, str], raw_row))
    return tuple(rows)


def _canonical_int(value: str, row_number: int, *, positive: bool) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CrossPoolContractError(f"row {row_number} has an invalid integer") from exc
    if str(parsed) != value or parsed < (1 if positive else 0):
        raise CrossPoolContractError(f"row {row_number} has a noncanonical integer")
    return parsed


def _canonical_float(value: str, row_number: int, *, positive: bool = False) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise CrossPoolContractError(f"row {row_number} has an invalid float") from exc
    rendered = "0" if parsed == 0.0 else repr(parsed)
    if not math.isfinite(parsed) or rendered != value or (positive and parsed <= 0.0):
        raise CrossPoolContractError(f"row {row_number} has a noncanonical float")
    return parsed


def _canonical_direction(value: str, row_number: int) -> Direction:
    if value not in _DIRECTION_ORDER:
        raise CrossPoolContractError(f"row {row_number} has an invalid direction")
    return cast(Direction, value)


def _canonical_regime(value: str, row_number: int) -> Regime:
    if value not in ("early", "mixed", "late"):
        raise CrossPoolContractError(f"row {row_number} has an invalid regime")
    return cast(Regime, value)
