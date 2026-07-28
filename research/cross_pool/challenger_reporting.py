"""Deterministic tables, report, and figures for challenger evidence."""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import TypeAlias, cast

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.figure import Figure
from numpy.typing import NDArray

from research.cross_pool.challenger_artifacts import (
    CHALLENGER_ARTIFACT_NAMES,
    ChallengerArtifact,
)
from research.cross_pool.challenger_contracts import (
    FEATURE_VARIANTS,
    FRESHNESS_SUPPORTS,
    MODEL_FAMILIES,
    ChallengerContrast,
    ChallengerFitFailure,
    ChallengerFoldAudit,
    ChallengerInference,
    ChallengerMetric,
    ChallengerPrediction,
    ChallengerStudy,
    FeatureVariant,
    FreshnessSupport,
    ModelFamily,
)
from research.cross_pool.contracts import CrossPoolContractError, Direction, Regime
from research.cross_pool.reporting import canonical_json_bytes

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)

_PREDICTION_FIELDS = (
    "timestamp_ms",
    "target_timestamp_ms",
    "horizon_ms",
    "refit_timestamp_ms",
    "fold_index",
    "direction",
    "family",
    "variant",
    "target_regime",
    "source_regime",
    "target_age_ms",
    "source_age_ms",
    "actual_bps",
    "updated",
    "prediction_bps",
    "update_probability",
    "conditional_prediction_bps",
)
_AUDIT_FIELDS = (
    "family",
    "variant",
    "direction",
    "horizon_ms",
    "fold_index",
    "refit_timestamp_ms",
    "max_training_target_timestamp_ms",
    "training_rows",
    "training_update_rows",
    "condition_number",
    "iterations",
    "robust_scale",
    "downweighted_fraction",
    "logistic_condition_number",
    "logistic_iterations",
    "spline_knots_json",
    "spline_extrapolation_count",
)
_METRIC_FIELDS = (
    "family",
    "variant",
    "direction",
    "horizon_ms",
    "support",
    "status",
    "reason",
    "rows",
    "target_days",
    "update_rows",
    "update_days",
    "update_incidence",
    "mae_bps",
    "mse_bps2",
    "rmse_bps",
    "directional_rows",
    "directional_accuracy",
    "brier_loss",
    "log_loss",
    "calibration_error",
    "conditional_update_mae_bps",
)
_CONTRAST_FIELDS = (
    "family",
    "direction",
    "horizon_ms",
    "support",
    "endpoint",
    "contrast",
    "comparator_variant",
    "candidate_variant",
    "status",
    "reason",
    "rows",
    "target_days",
    "complete_blocks",
    "update_days",
    "point",
    "raw_lower",
    "raw_upper",
    "bootstrap_standard_error",
    "simultaneous_lower",
    "simultaneous_upper",
    "adjusted_p_value",
)
_DIRECTIONS: tuple[Direction, ...] = ("bsc_to_base", "base_to_bsc")
_HORIZON_LABEL = {900_000: "15m", 3_600_000: "1h", 14_400_000: "4h"}
_EXPECTED_OOS_ROWS = {900_000: 8_438, 3_600_000: 2_109, 14_400_000: 527}
_EXPECTED_FOLD_INDICES = tuple(range(13))
_FAMILY_LABEL = {
    "ols": "OLS",
    "arx2": "ARX(2)",
    "gam": "Additive",
    "huber": "Huber",
    "two_part": "Two-part",
}
_CSV_FIELDS = {
    "challenger_predictions.csv": _PREDICTION_FIELDS,
    "challenger_fold_audits.csv": _AUDIT_FIELDS,
    "challenger_metrics.csv": _METRIC_FIELDS,
    "challenger_contrasts.csv": _CONTRAST_FIELDS,
}


def render_challenger_artifacts(
    study: ChallengerStudy,
    inference: ChallengerInference,
) -> tuple[ChallengerArtifact, ...]:
    diagnostics = _diagnostics_payload(study, inference)
    content: dict[str, bytes] = {
        "challenger_predictions.csv": _prediction_csv(study),
        "challenger_fold_audits.csv": _audit_csv(study),
        "challenger_metrics.csv": _metric_csv(inference),
        "challenger_contrasts.csv": _contrast_csv(inference),
        "challenger_diagnostics.json": canonical_json_bytes(diagnostics),
        "challenger_report.md": _report_bytes(study, inference),
        "model_error_comparison.png": _model_error_figure(inference.metrics),
        "source_price_contrasts.png": _source_price_figure(inference.contrasts),
        "freshness_sensitivity.png": _freshness_figure(inference.contrasts),
        "two_part_calibration.png": _calibration_figure(inference),
        "loss_dependence.png": _dependence_figure(diagnostics),
    }
    artifacts = tuple(
        ChallengerArtifact(relative_name=name, content=content[name])
        for name in CHALLENGER_ARTIFACT_NAMES
    )
    validate_rendered_challenger_artifacts(artifacts, study, inference)
    return artifacts


def validate_rendered_challenger_artifacts(
    artifacts: Sequence[ChallengerArtifact],
    study: ChallengerStudy,
    inference: ChallengerInference,
) -> None:
    materialized = tuple(artifacts)
    names = tuple(artifact.relative_name for artifact in materialized)
    if names != CHALLENGER_ARTIFACT_NAMES or len(set(names)) != len(names):
        raise CrossPoolContractError("challenger artifact set or order is invalid")
    by_name = {artifact.relative_name: artifact.content for artifact in materialized}
    expected_rows = {
        "challenger_predictions.csv": len(study.predictions),
        "challenger_fold_audits.csv": len(study.fold_audits),
        "challenger_metrics.csv": len(inference.metrics),
        "challenger_contrasts.csv": len(inference.contrasts),
    }
    expected_tables = {
        "challenger_predictions.csv": _prediction_csv(study),
        "challenger_fold_audits.csv": _audit_csv(study),
        "challenger_metrics.csv": _metric_csv(inference),
        "challenger_contrasts.csv": _contrast_csv(inference),
    }
    for name, row_count in expected_rows.items():
        raw = by_name[name]
        validate_challenger_artifact_projection(name, raw)
        if len(raw.splitlines()) != row_count + 1:
            raise CrossPoolContractError(f"{name} row count does not match typed evidence")
        if raw != expected_tables[name]:
            raise CrossPoolContractError(f"{name} does not match typed evidence")
    try:
        diagnostics = json.loads(by_name["challenger_diagnostics.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CrossPoolContractError("challenger diagnostics are not valid JSON") from exc
    if (
        not isinstance(diagnostics, dict)
        or diagnostics.get("schema_version") != "1.0.0"
        or diagnostics.get("parent_decision_unchanged") is not True
    ):
        raise CrossPoolContractError("challenger diagnostics projection is invalid")
    report = by_name["challenger_report.md"]
    if not report.endswith(b"\n") or b"leadership_unresolved" not in report:
        raise CrossPoolContractError("challenger report claim boundary is missing")
    for name in CHALLENGER_ARTIFACT_NAMES:
        validate_challenger_artifact_projection(name, by_name[name])
    if validate_challenger_artifact_bundle(by_name) != len(study.failures):
        raise CrossPoolContractError("challenger failure evidence does not match the study")


def validate_challenger_artifact_projection(name: str, raw: bytes) -> None:
    """Reject malformed artifact bytes even when a manifest hashes them."""
    if name in _CSV_FIELDS:
        rows = _parse_projected_csv(name, raw)
        if name == "challenger_predictions.csv":
            _validate_prediction_projection(rows)
        elif name == "challenger_fold_audits.csv":
            _validate_audit_projection(rows)
        elif name == "challenger_metrics.csv":
            _validate_metric_projection(rows)
        else:
            _validate_contrast_projection(rows)
        return
    if name == "challenger_diagnostics.json":
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CrossPoolContractError("challenger diagnostics are not valid JSON") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "1.0.0"
            or payload.get("research_role") != "post_hoc_exploratory"
            or payload.get("parent_decision") != "leadership_unresolved"
            or payload.get("parent_decision_unchanged") is not True
            or raw != canonical_json_bytes(cast(dict[str, JsonValue], payload))
        ):
            raise CrossPoolContractError("challenger diagnostics projection is invalid")
        return
    if name == "challenger_report.md":
        if not raw.endswith(b"\n") or b"leadership_unresolved" not in raw:
            raise CrossPoolContractError("challenger report claim boundary is missing")
        return
    if name.endswith(".png"):
        if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) <= 10_000:
            raise CrossPoolContractError(f"{name} is not a complete PNG")
        return
    raise CrossPoolContractError(f"unsupported challenger artifact {name}")


def validate_challenger_artifact_bundle(artifacts: Mapping[str, bytes]) -> int:
    if set(artifacts) != set(CHALLENGER_ARTIFACT_NAMES):
        raise CrossPoolContractError("challenger artifact bundle is incomplete")
    for name in CHALLENGER_ARTIFACT_NAMES:
        validate_challenger_artifact_projection(name, artifacts[name])

    prediction_rows = _parse_projected_csv(
        "challenger_predictions.csv",
        artifacts["challenger_predictions.csv"],
    )
    grouped_predictions: dict[tuple[str, str, str, int], list[dict[str, str]]] = {}
    for row in prediction_rows:
        key = (row["family"], row["variant"], row["direction"], int(row["horizon_ms"]))
        grouped_predictions.setdefault(key, []).append(row)

    try:
        diagnostics = json.loads(artifacts["challenger_diagnostics.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise CrossPoolContractError("challenger diagnostics are invalid") from exc
    failures = diagnostics.get("model_fit_failures")
    if not isinstance(failures, list):
        raise CrossPoolContractError("challenger failure registry is invalid")
    failure_keys: set[tuple[str, str, str, int]] = set()
    typed_failures: list[ChallengerFitFailure] = []
    for item in failures:
        if not isinstance(item, dict):
            raise CrossPoolContractError("challenger failure row is invalid")
        try:
            key = (
                _model_family(item["family"]),
                _feature_variant(item["variant"]),
                _direction(item["direction"]),
                int(item["horizon_ms"]),
            )
            fold_index = item["fold_index"]
            refit_timestamp_ms = item["refit_timestamp_ms"]
            reason = item["reason"]
        except (KeyError, TypeError, ValueError) as exc:
            raise CrossPoolContractError("challenger failure row is invalid") from exc
        if (
            key[3] not in _HORIZON_LABEL
            or not isinstance(fold_index, int)
            or fold_index < 0
            or not isinstance(refit_timestamp_ms, int)
            or refit_timestamp_ms <= 0
            or not isinstance(reason, str)
            or not reason
            or key in failure_keys
        ):
            raise CrossPoolContractError("challenger failure row is invalid")
        failure_keys.add(key)
        typed_failures.append(
            ChallengerFitFailure(
                family=key[0],
                variant=key[1],
                direction=key[2],
                horizon_ms=key[3],
                fold_index=fold_index,
                refit_timestamp_ms=refit_timestamp_ms,
                reason=reason,
            )
        )

    expected_combinations = {
        (family, variant, direction, horizon)
        for family in MODEL_FAMILIES
        for variant in FEATURE_VARIANTS
        for direction in _DIRECTIONS
        for horizon in _HORIZON_LABEL
    }
    successful_keys = set(grouped_predictions)
    if (
        successful_keys & failure_keys
        or successful_keys | failure_keys != expected_combinations
    ):
        raise CrossPoolContractError("challenger prediction/failure registry is inconsistent")

    for key, rows in grouped_predictions.items():
        horizon = key[3]
        if len(rows) != _EXPECTED_OOS_ROWS[horizon]:
            raise CrossPoolContractError("challenger prediction cell row count is invalid")
        reference = grouped_predictions.get(("ols", "target_only", key[2], horizon))
        if reference is None or _projected_prediction_identity(rows) != (
            _projected_prediction_identity(reference)
        ):
            raise CrossPoolContractError("challenger prediction cells are not matched")

    audit_rows = _parse_projected_csv(
        "challenger_fold_audits.csv",
        artifacts["challenger_fold_audits.csv"],
    )
    grouped_audits: dict[tuple[str, str, str, int], list[int]] = {}
    for row in audit_rows:
        key = (row["family"], row["variant"], row["direction"], int(row["horizon_ms"]))
        grouped_audits.setdefault(key, []).append(int(row["fold_index"]))
    if set(grouped_audits) != successful_keys or any(
        tuple(indices) != _EXPECTED_FOLD_INDICES for indices in grouped_audits.values()
    ):
        raise CrossPoolContractError("challenger fold-audit registry is inconsistent")

    metric_rows = _parse_projected_csv(
        "challenger_metrics.csv",
        artifacts["challenger_metrics.csv"],
    )
    for row in metric_rows:
        key = (row["family"], row["variant"], row["direction"], int(row["horizon_ms"]))
        if (key in failure_keys) != (row["status"] == "model_fit_failed"):
            raise CrossPoolContractError("challenger metric fit status is inconsistent")
    _validate_recomputed_artifacts(
        artifacts,
        prediction_rows=prediction_rows,
        audit_rows=audit_rows,
        failures=tuple(typed_failures),
    )
    return len(failure_keys)


def _projected_prediction_identity(
    rows: Sequence[dict[str, str]],
) -> tuple[tuple[str, ...], ...]:
    fields = (
        "timestamp_ms",
        "target_timestamp_ms",
        "refit_timestamp_ms",
        "fold_index",
        "target_regime",
        "source_regime",
        "target_age_ms",
        "source_age_ms",
        "actual_bps",
        "updated",
    )
    return tuple(tuple(row[field] for field in fields) for row in rows)


def _validate_recomputed_artifacts(
    artifacts: Mapping[str, bytes],
    *,
    prediction_rows: tuple[dict[str, str], ...],
    audit_rows: tuple[dict[str, str], ...],
    failures: tuple[ChallengerFitFailure, ...],
) -> None:
    from research.cross_pool.challenger_inference import infer_challengers

    predictions = tuple(_typed_prediction(row) for row in prediction_rows)
    audits = tuple(_typed_audit(row) for row in audit_rows)
    target_days = tuple(
        sorted(
            {
                datetime.fromtimestamp(row.target_timestamp_ms / 1_000, tz=UTC).date()
                for row in predictions
            }
        )
    )
    study = ChallengerStudy(
        predictions=predictions,
        fold_audits=audits,
        failures=failures,
        target_days_utc=target_days,
    )
    inference = infer_challengers(study)
    diagnostics = _diagnostics_payload(study, inference)
    expected = {
        "challenger_metrics.csv": _metric_csv(inference),
        "challenger_contrasts.csv": _contrast_csv(inference),
        "challenger_diagnostics.json": canonical_json_bytes(diagnostics),
        "challenger_report.md": _report_bytes(study, inference),
        "model_error_comparison.png": _model_error_figure(inference.metrics),
        "source_price_contrasts.png": _source_price_figure(inference.contrasts),
        "freshness_sensitivity.png": _freshness_figure(inference.contrasts),
        "two_part_calibration.png": _calibration_figure(inference),
        "loss_dependence.png": _dependence_figure(diagnostics),
    }
    for name, content in expected.items():
        if artifacts[name] != content:
            raise CrossPoolContractError(
                f"{name} does not reconcile to challenger predictions"
            )


def _typed_prediction(row: Mapping[str, str]) -> ChallengerPrediction:
    return ChallengerPrediction(
        timestamp_ms=int(row["timestamp_ms"]),
        target_timestamp_ms=int(row["target_timestamp_ms"]),
        horizon_ms=int(row["horizon_ms"]),
        refit_timestamp_ms=int(row["refit_timestamp_ms"]),
        fold_index=int(row["fold_index"]),
        direction=_direction(row["direction"]),
        family=_model_family(row["family"]),
        variant=_feature_variant(row["variant"]),
        target_regime=cast(Regime, row["target_regime"]),
        source_regime=cast(Regime, row["source_regime"]),
        target_age_ms=int(row["target_age_ms"]),
        source_age_ms=int(row["source_age_ms"]),
        actual_bps=float(row["actual_bps"]),
        updated=row["updated"] == "true",
        prediction_bps=float(row["prediction_bps"]),
        update_probability=_optional_canonical_float(row["update_probability"]),
        conditional_prediction_bps=_optional_canonical_float(
            row["conditional_prediction_bps"]
        ),
    )


def _typed_audit(row: Mapping[str, str]) -> ChallengerFoldAudit:
    raw_knots = json.loads(row["spline_knots_json"])
    knots = tuple((float(pair[0]), float(pair[1])) for pair in raw_knots)
    logistic_iterations = row["logistic_iterations"]
    return ChallengerFoldAudit(
        family=_model_family(row["family"]),
        variant=_feature_variant(row["variant"]),
        direction=_direction(row["direction"]),
        horizon_ms=int(row["horizon_ms"]),
        fold_index=int(row["fold_index"]),
        refit_timestamp_ms=int(row["refit_timestamp_ms"]),
        max_training_target_timestamp_ms=int(row["max_training_target_timestamp_ms"]),
        training_rows=int(row["training_rows"]),
        training_update_rows=int(row["training_update_rows"]),
        condition_number=float(row["condition_number"]),
        iterations=int(row["iterations"]),
        robust_scale=_optional_canonical_float(row["robust_scale"]),
        downweighted_fraction=_optional_canonical_float(row["downweighted_fraction"]),
        logistic_condition_number=_optional_canonical_float(
            row["logistic_condition_number"]
        ),
        logistic_iterations=(
            None if logistic_iterations == "" else int(logistic_iterations)
        ),
        spline_knots=knots,
        spline_extrapolation_count=int(row["spline_extrapolation_count"]),
    )


def _parse_projected_csv(name: str, raw: bytes) -> tuple[dict[str, str], ...]:
    if not raw.endswith(b"\n") or b"\r" in raw:
        raise CrossPoolContractError(f"{name} is not canonical LF CSV")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError(f"{name} is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = _CSV_FIELDS[name]
    if tuple(reader.fieldnames or ()) != fields:
        raise CrossPoolContractError(f"{name} header is not frozen")
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise CrossPoolContractError(f"{name} contains an irregular row")
        rows.append(cast(dict[str, str], raw_row))
    return tuple(rows)


def _validate_prediction_projection(rows: tuple[dict[str, str], ...]) -> None:
    keys: list[tuple[int, int, int, int, int]] = []
    for row in rows:
        family = _model_family(row["family"])
        variant = _feature_variant(row["variant"])
        direction = _direction(row["direction"])
        timestamp = _canonical_int(row["timestamp_ms"], positive=True)
        target = _canonical_int(row["target_timestamp_ms"], positive=True)
        horizon = _horizon(row["horizon_ms"])
        _canonical_int(row["refit_timestamp_ms"], positive=True)
        _canonical_int(row["fold_index"], positive=False)
        _canonical_int(row["target_age_ms"], positive=False)
        _canonical_int(row["source_age_ms"], positive=False)
        if target != timestamp + horizon:
            raise CrossPoolContractError("challenger prediction target timestamp is invalid")
        if row["target_regime"] not in ("early", "mixed", "late") or row[
            "source_regime"
        ] not in ("early", "mixed", "late"):
            raise CrossPoolContractError("challenger prediction regime is invalid")
        if row["updated"] not in ("true", "false"):
            raise CrossPoolContractError("challenger update label is invalid")
        _canonical_float(row["actual_bps"])
        prediction = _canonical_float(row["prediction_bps"])
        probability = _optional_canonical_float(row["update_probability"])
        conditional = _optional_canonical_float(row["conditional_prediction_bps"])
        if family == "two_part":
            if probability is None or conditional is None or not 0.0 <= probability <= 1.0:
                raise CrossPoolContractError("two-part prediction components are invalid")
            if prediction != probability * conditional:
                raise CrossPoolContractError("two-part combined prediction is inconsistent")
        elif probability is not None or conditional is not None:
            raise CrossPoolContractError("continuous prediction has two-part components")
        keys.append(
            (
                MODEL_FAMILIES.index(family),
                FEATURE_VARIANTS.index(variant),
                _DIRECTIONS.index(direction),
                horizon,
                timestamp,
            )
        )
    if not rows or tuple(keys) != tuple(sorted(set(keys))):
        raise CrossPoolContractError("challenger prediction ordering is invalid")


def _validate_audit_projection(rows: tuple[dict[str, str], ...]) -> None:
    keys: list[tuple[int, int, int, int, int]] = []
    for row in rows:
        family = _model_family(row["family"])
        variant = _feature_variant(row["variant"])
        direction = _direction(row["direction"])
        horizon = _horizon(row["horizon_ms"])
        fold = _canonical_int(row["fold_index"], positive=False)
        for field in (
            "refit_timestamp_ms",
            "max_training_target_timestamp_ms",
            "training_rows",
        ):
            _canonical_int(row[field], positive=True)
        _canonical_int(row["training_update_rows"], positive=False)
        _canonical_int(row["iterations"], positive=True)
        _canonical_int(row["spline_extrapolation_count"], positive=False)
        _canonical_float(row["condition_number"])
        for field in (
            "robust_scale",
            "downweighted_fraction",
            "logistic_condition_number",
        ):
            _optional_canonical_float(row[field])
        if row["logistic_iterations"]:
            _canonical_int(row["logistic_iterations"], positive=True)
        try:
            knots = json.loads(row["spline_knots_json"])
        except json.JSONDecodeError as exc:
            raise CrossPoolContractError("challenger spline knots are invalid") from exc
        if not isinstance(knots, list):
            raise CrossPoolContractError("challenger spline knots are invalid")
        keys.append(
            (
                MODEL_FAMILIES.index(family),
                FEATURE_VARIANTS.index(variant),
                _DIRECTIONS.index(direction),
                horizon,
                fold,
            )
        )
    if not rows or tuple(keys) != tuple(sorted(set(keys))):
        raise CrossPoolContractError("challenger audit ordering is invalid")


def _validate_metric_projection(rows: tuple[dict[str, str], ...]) -> None:
    expected = {
        (family, variant, direction, horizon, support)
        for family in MODEL_FAMILIES
        for variant in FEATURE_VARIANTS
        for direction in _DIRECTIONS
        for horizon in _HORIZON_LABEL
        for support in FRESHNESS_SUPPORTS
    }
    keys: set[tuple[str, str, str, int, str]] = set()
    for row in rows:
        key = (
            _model_family(row["family"]),
            _feature_variant(row["variant"]),
            _direction(row["direction"]),
            _horizon(row["horizon_ms"]),
            _freshness_support(row["support"]),
        )
        if key in keys:
            raise CrossPoolContractError("challenger metric key is duplicated")
        keys.add(key)
        status = row["status"]
        if status not in ("available", "model_fit_failed", "no_rows"):
            raise CrossPoolContractError("challenger metric status is invalid")
        counts = {
            field: _canonical_int(row[field], positive=False)
            for field in (
                "rows",
                "target_days",
                "update_rows",
                "update_days",
                "directional_rows",
            )
        }
        numeric = {
            field: _optional_canonical_float(row[field])
            for field in _METRIC_FIELDS[11:]
            if field != "directional_rows"
        }
        if (status == "available") != (row["reason"] == ""):
            raise CrossPoolContractError("challenger metric reason is inconsistent")
        if status == "available":
            if any(
                numeric[field] is None
                for field in ("update_incidence", "mae_bps", "mse_bps2", "rmse_bps")
            ):
                raise CrossPoolContractError("available challenger metric is incomplete")
            component_fields = ("brier_loss", "log_loss", "calibration_error")
            if key[0] == "two_part":
                if any(numeric[field] is None for field in component_fields):
                    raise CrossPoolContractError("two-part metric components are incomplete")
                if (counts["update_rows"] > 0) != (
                    numeric["conditional_update_mae_bps"] is not None
                ):
                    raise CrossPoolContractError(
                        "two-part conditional metric support is inconsistent"
                    )
            elif any(
                numeric[field] is not None
                for field in (*component_fields, "conditional_update_mae_bps")
            ):
                raise CrossPoolContractError("continuous metric has two-part components")
        elif any(value is not None for value in numeric.values()):
            raise CrossPoolContractError("unavailable challenger metric has numeric results")
    if len(rows) != 270 or keys != expected:
        raise CrossPoolContractError("challenger metric registry is incomplete")


def _validate_contrast_projection(rows: tuple[dict[str, str], ...]) -> None:
    contrasts = (
        ("source_age", "target_only", "source_age"),
        ("source_price", "source_age", "full_source"),
        ("total_source", "target_only", "full_source"),
    )
    expected: set[tuple[str, str, int, str, str, str]] = set()
    for family in MODEL_FAMILIES:
        endpoints = ("mae",) if family != "two_part" else (
            "mae",
            "brier",
            "conditional_update_mae",
        )
        for endpoint in endpoints:
            for contrast, _, _ in contrasts:
                for direction in _DIRECTIONS:
                    for horizon in _HORIZON_LABEL:
                        for support in FRESHNESS_SUPPORTS:
                            expected.add((family, direction, horizon, support, endpoint, contrast))
    keys: set[tuple[str, str, int, str, str, str]] = set()
    variant_map = {name: (comparator, candidate) for name, comparator, candidate in contrasts}
    for row in rows:
        family = _model_family(row["family"])
        direction = _direction(row["direction"])
        horizon = _horizon(row["horizon_ms"])
        row_support = _freshness_support(row["support"])
        endpoint = row["endpoint"]
        if endpoint not in ("mae", "brier", "conditional_update_mae"):
            raise CrossPoolContractError("challenger contrast endpoint is invalid")
        contrast = row["contrast"]
        if contrast not in variant_map:
            raise CrossPoolContractError("challenger contrast kind is invalid")
        if (row["comparator_variant"], row["candidate_variant"]) != variant_map[contrast]:
            raise CrossPoolContractError("challenger contrast variants are invalid")
        key = (family, direction, horizon, row_support, endpoint, contrast)
        if key in keys:
            raise CrossPoolContractError("challenger contrast key is duplicated")
        keys.add(key)
        status = row["status"]
        if status not in ("adjudicable", "not_adjudicable"):
            raise CrossPoolContractError("challenger contrast status is invalid")
        for field in ("rows", "target_days", "complete_blocks", "update_days"):
            _canonical_int(row[field], positive=False)
        numeric = {
            field: _optional_canonical_float(row[field])
            for field in _CONTRAST_FIELDS[14:]
        }
        if status == "adjudicable":
            if row["reason"] or any(
                numeric[field] is None
                for field in ("point", "raw_lower", "raw_upper", "bootstrap_standard_error")
            ):
                raise CrossPoolContractError("adjudicable contrast is incomplete")
            adjusted_fields = (
                numeric["simultaneous_lower"],
                numeric["simultaneous_upper"],
                numeric["adjusted_p_value"],
            )
            if contrast == "source_price":
                if not all(value is not None for value in adjusted_fields):
                    raise CrossPoolContractError("challenger max-t fields are incomplete")
            elif any(value is not None for value in adjusted_fields):
                raise CrossPoolContractError("descriptive contrast has max-t fields")
        elif (
            not row["reason"]
            or any(
                numeric[field] is not None
                for field in (
                    "raw_lower",
                    "raw_upper",
                    "bootstrap_standard_error",
                    "simultaneous_lower",
                    "simultaneous_upper",
                    "adjusted_p_value",
                )
            )
        ):
            raise CrossPoolContractError("unavailable contrast is inconsistent")
    if len(rows) != 378 or keys != expected:
        raise CrossPoolContractError("challenger contrast registry is incomplete")


def _canonical_int(value: str, *, positive: bool) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CrossPoolContractError("challenger table integer is invalid") from exc
    if str(parsed) != value or parsed < (1 if positive else 0):
        raise CrossPoolContractError("challenger table integer is noncanonical")
    return parsed


def _canonical_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise CrossPoolContractError("challenger table float is invalid") from exc
    if not math.isfinite(parsed) or _float_text(parsed) != value:
        raise CrossPoolContractError("challenger table float is noncanonical")
    return parsed


def _optional_canonical_float(value: str) -> float | None:
    return None if value == "" else _canonical_float(value)


def _model_family(value: str) -> ModelFamily:
    if value not in MODEL_FAMILIES:
        raise CrossPoolContractError("challenger model family is invalid")
    return value


def _feature_variant(value: str) -> FeatureVariant:
    if value not in FEATURE_VARIANTS:
        raise CrossPoolContractError("challenger feature variant is invalid")
    return value


def _direction(value: str) -> Direction:
    if value not in _DIRECTIONS:
        raise CrossPoolContractError("challenger direction is invalid")
    return value


def _horizon(value: str) -> int:
    parsed = _canonical_int(value, positive=True)
    if parsed not in _HORIZON_LABEL:
        raise CrossPoolContractError("challenger horizon is invalid")
    return parsed


def _freshness_support(value: str) -> FreshnessSupport:
    if value not in FRESHNESS_SUPPORTS:
        raise CrossPoolContractError("challenger freshness support is invalid")
    return value


def _prediction_csv(study: ChallengerStudy) -> bytes:
    rows = (
        (
            row.timestamp_ms,
            row.target_timestamp_ms,
            row.horizon_ms,
            row.refit_timestamp_ms,
            row.fold_index,
            row.direction,
            row.family,
            row.variant,
            row.target_regime,
            row.source_regime,
            row.target_age_ms,
            row.source_age_ms,
            _float_text(row.actual_bps),
            "true" if row.updated else "false",
            _float_text(row.prediction_bps),
            _optional_float(row.update_probability),
            _optional_float(row.conditional_prediction_bps),
        )
        for row in study.predictions
    )
    return _csv_bytes(_PREDICTION_FIELDS, rows)


def _audit_csv(study: ChallengerStudy) -> bytes:
    rows = (
        (
            row.family,
            row.variant,
            row.direction,
            row.horizon_ms,
            row.fold_index,
            row.refit_timestamp_ms,
            row.max_training_target_timestamp_ms,
            row.training_rows,
            row.training_update_rows,
            _float_text(row.condition_number),
            row.iterations,
            _optional_float(row.robust_scale),
            _optional_float(row.downweighted_fraction),
            _optional_float(row.logistic_condition_number),
            "" if row.logistic_iterations is None else row.logistic_iterations,
            json.dumps(row.spline_knots, separators=(",", ":")),
            row.spline_extrapolation_count,
        )
        for row in study.fold_audits
    )
    return _csv_bytes(_AUDIT_FIELDS, rows)


def _metric_csv(inference: ChallengerInference) -> bytes:
    rows = (
        (
            row.family,
            row.variant,
            row.direction,
            row.horizon_ms,
            row.support,
            row.status,
            row.reason or "",
            row.rows,
            row.target_days,
            row.update_rows,
            row.update_days,
            _optional_float(row.update_incidence),
            _optional_float(row.mae_bps),
            _optional_float(row.mse_bps2),
            _optional_float(row.rmse_bps),
            row.directional_rows,
            _optional_float(row.directional_accuracy),
            _optional_float(row.brier_loss),
            _optional_float(row.log_loss),
            _optional_float(row.calibration_error),
            _optional_float(row.conditional_update_mae_bps),
        )
        for row in inference.metrics
    )
    return _csv_bytes(_METRIC_FIELDS, rows)


def _contrast_csv(inference: ChallengerInference) -> bytes:
    rows = (
        (
            row.family,
            row.direction,
            row.horizon_ms,
            row.support,
            row.endpoint,
            row.contrast,
            row.comparator_variant,
            row.candidate_variant,
            row.status,
            row.reason or "",
            row.rows,
            row.target_days,
            row.complete_blocks,
            row.update_days,
            _optional_float(row.point),
            _optional_float(row.raw_lower),
            _optional_float(row.raw_upper),
            _optional_float(row.bootstrap_standard_error),
            _optional_float(row.simultaneous_lower),
            _optional_float(row.simultaneous_upper),
            _optional_float(row.adjusted_p_value),
        )
        for row in inference.contrasts
    )
    return _csv_bytes(_CONTRAST_FIELDS, rows)


def _diagnostics_payload(
    study: ChallengerStudy,
    inference: ChallengerInference,
) -> dict[str, JsonValue]:
    failure_rows = [
        {
            "family": row.family,
            "variant": row.variant,
            "direction": row.direction,
            "horizon_ms": row.horizon_ms,
            "fold_index": row.fold_index,
            "refit_timestamp_ms": row.refit_timestamp_ms,
            "reason": row.reason,
        }
        for row in study.failures
    ]
    reliability = [
        {
            "variant": row.variant,
            "direction": row.direction,
            "horizon_ms": row.horizon_ms,
            "support": row.support,
            "group_index": row.group_index,
            "rows": row.rows,
            "mean_probability": row.mean_probability,
            "observed_incidence": row.observed_incidence,
        }
        for row in inference.reliability
    ]
    dependence = _loss_dependence(study)
    return {
        "schema_version": "1.0.0",
        "research_role": "post_hoc_exploratory",
        "parent_decision_unchanged": True,
        "parent_decision": "leadership_unresolved",
        "bootstrap": {
            "scheme": "seven_day_circular_moving_block",
            "resamples": inference.bootstrap_draws,
            "seed": inference.bootstrap_seed,
            "block_days": inference.block_days,
            "simultaneous_critical_value": inference.simultaneous_critical_value,
        },
        "model_fit_failures": cast(list[JsonValue], failure_rows),
        "reliability": cast(list[JsonValue], reliability),
        "source_price_loss_dependence": cast(list[JsonValue], dependence),
    }


def _loss_dependence(study: ChallengerStudy) -> list[dict[str, JsonValue]]:
    grouped: dict[
        tuple[ModelFamily, FeatureVariant, Direction, int],
        tuple[ChallengerPrediction, ...],
    ] = {}
    for family in MODEL_FAMILIES:
        for variant in ("source_age", "full_source"):
            for direction in _DIRECTIONS:
                for horizon_ms in _HORIZON_LABEL:
                    rows = tuple(
                        row
                        for row in study.predictions
                        if row.family == family
                        and row.variant == variant
                        and row.direction == direction
                        and row.horizon_ms == horizon_ms
                    )
                    if rows:
                        grouped[(family, variant, direction, horizon_ms)] = rows
    result: list[dict[str, JsonValue]] = []
    for family in MODEL_FAMILIES:
        for direction in _DIRECTIONS:
            for horizon_ms in _HORIZON_LABEL:
                source_age = grouped.get((family, "source_age", direction, horizon_ms))
                full_source = grouped.get((family, "full_source", direction, horizon_ms))
                if source_age is None or full_source is None:
                    continue
                _validate_row_alignment(source_age, full_source)
                daily: dict[date, list[float]] = {}
                fold: dict[int, list[float]] = {}
                regime: dict[tuple[str, str], list[float]] = {}
                for baseline, full in zip(source_age, full_source, strict=True):
                    difference = abs(baseline.actual_bps - baseline.prediction_bps) - abs(
                        full.actual_bps - full.prediction_bps
                    )
                    daily.setdefault(_target_day(baseline), []).append(difference)
                    fold.setdefault(baseline.fold_index, []).append(difference)
                    regime.setdefault(
                        (baseline.target_regime, baseline.source_regime),
                        [],
                    ).append(difference)
                ordered_days = study.target_days_utc
                day_values = np.full(len(ordered_days), np.nan, dtype=np.float64)
                for index, day in enumerate(ordered_days):
                    values = daily.get(day)
                    if values:
                        day_values[index] = math.fsum(values) / len(values)
                result.append(
                    {
                        "family": family,
                        "direction": direction,
                        "horizon_ms": horizon_ms,
                        "support": "all",
                        "endpoint": "mae",
                        "acf_lags_1_to_8": [
                            _acf_pairwise(day_values, lag) for lag in range(1, 9)
                        ],
                        "fold_improvement_bps": [
                            {
                                "fold_index": fold_index,
                                "point": math.fsum(values) / len(values),
                            }
                            for fold_index, values in sorted(fold.items())
                        ],
                        "joint_regime_improvement_bps": [
                            {
                                "target_regime": key[0],
                                "source_regime": key[1],
                                "rows": len(values),
                                "point": math.fsum(values) / len(values),
                            }
                            for key, values in sorted(regime.items())
                        ],
                    }
                )
    return result


def _report_bytes(study: ChallengerStudy, inference: ChallengerInference) -> bytes:
    lines = [
        "# Cross-pool predictive challengers",
        "",
        "**Research role:** post-hoc exploratory robustness study  ",
        "**Parent decision:** `leadership_unresolved`, unchanged",
        "",
        "## What was tested",
        "",
        "The sealed OLS result remains the reference. ARX(2) adds a second target-return lag. "
        "The restricted additive model allows fixed piecewise-linear age and price-gap effects. "
        "Huber regression downweights large residuals. The two-part model separately estimates "
        "whether the target updates and its signed return conditional on an update.",
        "",
        "Update incidence is the share of rows with a newer observed target-pool state by the "
        "forecast horizon. It is not defined as the share of nonzero returns.",
        "",
        "## Estimator assumptions",
        "",
        "- **OLS and ARX(2):** a stable linear conditional mean within each expanding fold. "
        "The block bootstrap, not coefficient standard errors, handles dependent losses.",
        "- **Restricted additive:** effects may bend at two fixed training-fold knots, but remain "
        "additive and contain no interactions or fitted smoothing search.",
        "- **Huber:** the conditional location is linear, while extreme residuals receive less "
        "weight. Zero MAD scale or non-convergence makes the cell not adjudicable.",
        "- **Two-part:** one model describes update incidence and another describes the signed "
        "return conditional on update. Their product is a conditional-mean forecast.",
        "",
        "## One-hour source-price contrast",
        "",
        "Positive values mean that source return and price-gap features lowered MAE after source "
        "age was already included. Intervals below are simultaneous where available.",
        "",
        "| Model | Direction | Improvement (bps) | Simultaneous 95% interval | "
        "Adjusted p | Status |",
        "|---|---|---:|---:|---:|---|",
    ]
    headline = [
        row
        for row in inference.contrasts
        if row.horizon_ms == 3_600_000
        and row.support == "all"
        and row.endpoint == "mae"
        and row.contrast == "source_price"
    ]
    for row in headline:
        interval = (
            "—"
            if row.simultaneous_lower is None or row.simultaneous_upper is None
            else f"[{row.simultaneous_lower:.6f}, {row.simultaneous_upper:.6f}]"
        )
        lines.append(
            "| "
            f"{_FAMILY_LABEL[row.family]} | {row.direction} | {_markdown_float(row.point)} | "
            f"{interval} | {_markdown_float(row.adjusted_p_value)} | "
            f"{row.status.replace('_', ' ')} |"
        )
    lines.extend(
        (
            "",
            "## Fit and support",
            "",
            f"The walk-forward run produced {len(study.predictions):,} long-form predictions, "
            f"{len(study.fold_audits):,} completed fold fits, and {len(study.failures)} "
            "registered model-cell failures. Failures remain visible rather than being replaced "
            "by another estimator.",
            "",
            "Freshness checks score the same fitted forecasts when both observed pool states are "
            "no older than four hours or one hour. They do not refit models on the selected rows.",
            "",
            "## Figure guide",
            "",
            "### One-hour model error",
            "",
            "![One-hour OOS MAE by model and feature set](model_error_comparison.png)",
            "",
            "Each bar is an absolute OOS forecast error. Lower is better; gray uses only target "
            "features and teal adds the full source block.",
            "",
            "### Incremental source-price value",
            "",
            "![One-hour source-price MAE contrasts](source_price_contrasts.png)",
            "",
            "Points to the right of zero favor source return and price-gap features after source "
            "age is already present. Horizontal lines are simultaneous 95% intervals where the "
            "cell is adjudicable.",
            "",
            "### Freshness sensitivity",
            "",
            "![BSC-to-Base freshness sensitivity](freshness_sensitivity.png)",
            "",
            "The same BSC-to-Base forecasts are rescored on progressively fresher state pairs; "
            "the models are not refitted.",
            "",
            "### Update calibration",
            "",
            "![Two-part update calibration](two_part_calibration.png)",
            "",
            "Predicted update probabilities are grouped against observed update incidence. "
            "The dashed diagonal marks perfect calibration.",
            "",
            "### Loss dependence",
            "",
            "![Day-level source-price loss dependence](loss_dependence.png)",
            "",
            "The lines show calendar-day autocorrelation in paired source-price loss "
            "improvements. Persistent values motivate block rather than row-wise resampling.",
            "",
            "## Uncertainty",
            "",
            "Loss uncertainty uses one 10,000-draw seven-day circular block bootstrap shared "
            "across the full 88-day OOS calendar. The max-t family adjusts the registered "
            "source-price comparisons together. A cell with inadequate days, complete blocks, "
            "update-days, variance, or a failed fit is not adjudicable.",
            "",
            "## Claim boundary",
            "",
            "These results cannot establish causal price discovery, executable alpha, trading "
            "profitability, or LP profitability. A positive adjusted result would remain a "
            "diagnostic within this sealed post-hoc OOS sample. It would require fresh-data "
            "replication "
            "before changing the reviewed conclusion.",
            "",
        )
    )
    return "\n".join(lines).encode("utf-8")


def _model_error_figure(metrics: tuple[ChallengerMetric, ...]) -> bytes:
    figure, axes = plt.subplots(1, 2, figsize=(12, 5), sharey=True)
    for axis, direction in zip(axes, _DIRECTIONS, strict=True):
        rows = [
            row
            for row in metrics
            if row.direction == direction
            and row.horizon_ms == 3_600_000
            and row.support == "all"
            and row.status == "available"
            and row.variant in ("target_only", "full_source")
        ]
        labels = [f"{_FAMILY_LABEL[row.family]}\n{row.variant}" for row in rows]
        values = [cast(float, row.mae_bps) for row in rows]
        colors = ["#9da3a8" if row.variant == "target_only" else "#176b78" for row in rows]
        axis.bar(np.arange(len(rows)), values, color=colors)
        axis.set_xticks(np.arange(len(rows)), labels, rotation=35, ha="right")
        axis.set_title(direction.replace("_to_", " → "))
        axis.grid(axis="y", alpha=0.25)
    axes[0].set_ylabel("Out-of-sample MAE (bps)")
    figure.suptitle("One-hour predictive error by model and feature set")
    figure.tight_layout()
    return _png_bytes(figure)


def _source_price_figure(contrasts: tuple[ChallengerContrast, ...]) -> bytes:
    rows = [
        row
        for row in contrasts
        if row.horizon_ms == 3_600_000
        and row.support == "all"
        and row.endpoint == "mae"
        and row.contrast == "source_price"
        and row.point is not None
    ]
    figure, axis = plt.subplots(figsize=(10, 6))
    positions = np.arange(len(rows))
    values = np.asarray([cast(float, row.point) for row in rows])
    colors = ["#176b78" if row.status == "adjudicable" else "#9da3a8" for row in rows]
    axis.scatter(values, positions, color=colors, zorder=3)
    for position, row in zip(positions, rows, strict=True):
        if row.simultaneous_lower is not None and row.simultaneous_upper is not None:
            axis.plot(
                [row.simultaneous_lower, row.simultaneous_upper],
                [position, position],
                color="#176b78",
                linewidth=2,
            )
    axis.axvline(0.0, color="black", linewidth=1)
    axis.set_yticks(
        positions,
        [f"{_FAMILY_LABEL[row.family]} · {row.direction}" for row in rows],
    )
    axis.set_xlabel("MAE improvement from source price block (bps)")
    axis.set_title("One-hour incremental source-price value")
    axis.grid(axis="x", alpha=0.25)
    figure.tight_layout()
    return _png_bytes(figure)


def _freshness_figure(contrasts: tuple[ChallengerContrast, ...]) -> bytes:
    figure, axis = plt.subplots(figsize=(10, 6))
    for family in MODEL_FAMILIES:
        rows = [
            row
            for row in contrasts
            if row.family == family
            and row.direction == "bsc_to_base"
            and row.horizon_ms == 3_600_000
            and row.endpoint == "mae"
            and row.contrast == "source_price"
            and row.point is not None
        ]
        if not rows:
            continue
        rows.sort(key=lambda row: FRESHNESS_SUPPORTS.index(row.support))
        axis.plot(
            [FRESHNESS_SUPPORTS.index(row.support) for row in rows],
            [cast(float, row.point) for row in rows],
            marker="o",
            label=_FAMILY_LABEL[family],
        )
    axis.axhline(0.0, color="black", linewidth=1)
    axis.set_xticks(np.arange(3), ("All", "Both ≤4h", "Both ≤1h"))
    axis.set_ylabel("Source-price MAE improvement (bps)")
    axis.set_title("BSC → Base one-hour freshness sensitivity")
    axis.grid(axis="y", alpha=0.25)
    axis.legend(frameon=False)
    figure.tight_layout()
    return _png_bytes(figure)


def _calibration_figure(inference: ChallengerInference) -> bytes:
    figure, axis = plt.subplots(figsize=(8, 7))
    for direction in _DIRECTIONS:
        for horizon_ms in _HORIZON_LABEL:
            rows = [
                row
                for row in inference.reliability
                if row.variant == "full_source"
                and row.direction == direction
                and row.horizon_ms == horizon_ms
                and row.support == "all"
            ]
            if not rows:
                continue
            axis.plot(
                [row.mean_probability for row in rows],
                [row.observed_incidence for row in rows],
                marker="o",
                label=f"{direction} · {_HORIZON_LABEL[horizon_ms]}",
            )
    axis.plot((0, 1), (0, 1), linestyle="--", color="black", linewidth=1)
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.set_xlabel("Mean predicted update probability")
    axis.set_ylabel("Observed update incidence")
    axis.set_title("Two-part update calibration")
    axis.grid(alpha=0.25)
    axis.legend(frameon=False, fontsize=8)
    figure.tight_layout()
    return _png_bytes(figure)


def _dependence_figure(diagnostics: Mapping[str, JsonValue]) -> bytes:
    raw = diagnostics["source_price_loss_dependence"]
    assert isinstance(raw, list)
    figure, axis = plt.subplots(figsize=(10, 6))
    for item in raw:
        assert isinstance(item, dict)
        if item["horizon_ms"] != 3_600_000 or item["family"] not in (
            "ols",
            "arx2",
            "gam",
        ):
            continue
        values = item["acf_lags_1_to_8"]
        assert isinstance(values, list)
        plotted_values: list[float] = []
        for value in values:
            if value is None:
                plotted_values.append(float("nan"))
            elif isinstance(value, (int, float)):
                plotted_values.append(float(value))
            else:
                raise CrossPoolContractError(
                    "dependence diagnostic contains a non-numeric ACF value"
                )
        axis.plot(
            np.arange(1, 9),
            plotted_values,
            marker="o",
            label=f"{_FAMILY_LABEL[cast(ModelFamily, item['family'])]} · {item['direction']}",
        )
    axis.axhline(0.0, color="black", linewidth=1)
    axis.set_xlabel("Calendar-day lag")
    axis.set_ylabel("Loss-difference autocorrelation")
    axis.set_title("One-hour residual loss dependence")
    axis.grid(alpha=0.25)
    axis.legend(frameon=False, fontsize=8)
    figure.tight_layout()
    return _png_bytes(figure)


def _acf_pairwise(values: NDArray[np.float64], lag: int) -> float | None:
    current = values[lag:]
    previous = values[:-lag]
    valid = np.isfinite(current) & np.isfinite(previous)
    if np.count_nonzero(valid) < 3:
        return None
    left = current[valid]
    right = previous[valid]
    if np.std(left) == 0.0 or np.std(right) == 0.0:
        return None
    value = float(np.corrcoef(left, right)[0, 1])
    return value if math.isfinite(value) else None


def _validate_row_alignment(
    baseline: tuple[ChallengerPrediction, ...],
    full: tuple[ChallengerPrediction, ...],
) -> None:
    if tuple((row.timestamp_ms, row.actual_bps) for row in baseline) != tuple(
        (row.timestamp_ms, row.actual_bps) for row in full
    ):
        raise CrossPoolContractError("diagnostic challenger rows are not aligned")


def _target_day(row: ChallengerPrediction) -> date:
    return datetime.fromtimestamp(row.target_timestamp_ms / 1_000, tz=UTC).date()


def _csv_bytes(fields: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fields)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _float_text(value: float) -> str:
    if not math.isfinite(value):
        raise CrossPoolContractError("challenger artifact float must be finite")
    return "0" if value == 0.0 else repr(value)


def _optional_float(value: float | None) -> str:
    return "" if value is None else _float_text(value)


def _markdown_float(value: float | None) -> str:
    return "—" if value is None else f"{value:.6f}"


def _png_bytes(figure: Figure) -> bytes:
    buffer = io.BytesIO()
    figure.savefig(
        buffer,
        format="png",
        dpi=160,
        bbox_inches="tight",
        metadata={"Software": "automated-infra"},
    )
    plt.close(figure)
    return buffer.getvalue()


__all__ = [
    "CHALLENGER_ARTIFACT_NAMES",
    "render_challenger_artifacts",
    "validate_challenger_artifact_bundle",
    "validate_challenger_artifact_projection",
    "validate_rendered_challenger_artifacts",
]
