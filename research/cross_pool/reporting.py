"""Deterministic, side-effect-free serializers for cross-pool evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from research.cross_pool.contracts import (
    CausalPanel,
    ConfidenceInterval,
    CrossPoolContractError,
    Direction,
    DirectionalPredictiveAudit,
    DtwNullResult,
    DtwWeekResult,
    EventExclusion,
    EventExclusionReason,
    EventResponse,
    EventStudyResult,
    PanelRow,
    PredictionRow,
    PredictiveInference,
    StreamQuality,
)
from research.cross_pool.figures import (
    dtw_lag_png,
    event_response_png,
    predictive_performance_png,
    price_gap_png,
)
from research.cross_pool.manifest import (
    JsonValue,
    canonical_manifest_bytes,
    predictive_audit_payload,
    predictive_inference_payload,
)
from research.cross_pool.market_structure import (
    VenueStructureSummary,
    serialize_market_structure,
)
from research.cross_pool.pipeline import DirectionalAnalysis, StatisticalAnalysis

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
_DIRECTION_ORDER = {"bsc_to_base": 0, "base_to_bsc": 1}
_EVENT_FIELDS = (
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
_DTW_PATH_FIELDS = (
    "week_start_timestamp_ms",
    "direction",
    "band_steps",
    "path_index",
    "source_index",
    "target_index",
    "path_length",
    "total_cost",
    "normalized_cost",
    "median_signed_lag_steps",
)
_DTW_NULL_FIELDS = (
    "week_start_timestamp_ms",
    "direction",
    "band_steps",
    "rotation_days",
    "path_length",
    "total_cost",
    "normalized_cost",
    "observed_normalized_cost",
    "observed_cost_improvement",
    "median_signed_lag_steps",
    "observed_median_signed_lag_steps",
    "observed_signed_lag_difference_steps",
    "observed_cost_rank",
    "observed_absolute_lag_rank",
)


@dataclass(frozen=True)
class RenderedArtifact:
    relative_name: str
    content: bytes

    def __post_init__(self) -> None:
        if not self.relative_name or self.relative_name.startswith(('/', '\\')):
            raise CrossPoolContractError("artifact name must be a relative filename")
        if "/" in self.relative_name or "\\" in self.relative_name:
            raise CrossPoolContractError("artifact name must not contain directories")
        if not self.content:
            raise CrossPoolContractError("rendered artifact content must be nonempty")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def canonical_json_bytes(payload: Mapping[str, JsonValue]) -> bytes:
    """Serialize a finite JSON mapping with the shared manifest convention."""
    return canonical_manifest_bytes(payload)


def panel_csv_bytes(panel: CausalPanel) -> bytes:
    if not isinstance(panel, CausalPanel) or not panel.rows:
        raise CrossPoolContractError("panel reporting requires a nonempty CausalPanel")
    if any(row.horizon_ms != panel.horizon_ms for row in panel.rows):
        raise CrossPoolContractError("panel rows must match the panel horizon")
    timestamps = tuple(row.timestamp_ms for row in panel.rows)
    if timestamps != tuple(sorted(set(timestamps))):
        raise CrossPoolContractError(
            "panel rows must use unique increasing decision timestamps"
        )

    rows = tuple(_panel_csv_row(row) for row in panel.rows)
    return _csv_bytes(_PANEL_FIELDS, rows)


def prediction_csv_bytes(rows: Sequence[PredictionRow]) -> bytes:
    materialized = tuple(rows)
    if not materialized:
        raise CrossPoolContractError("prediction reporting requires at least one row")
    keys = tuple(
        (row.direction, row.timestamp_ms, row.target_timestamp_ms)
        for row in materialized
    )
    if len(set(keys)) != len(keys):
        raise CrossPoolContractError("prediction reporting keys must be unique")
    ordered = tuple(
        sorted(
            materialized,
            key=lambda row: (
                _DIRECTION_ORDER[row.direction],
                row.timestamp_ms,
                row.target_timestamp_ms,
            ),
        )
    )
    csv_rows = tuple(
        (
            row.timestamp_ms,
            row.target_timestamp_ms,
            row.horizon_ms,
            row.refit_timestamp_ms,
            row.fold_index,
            row.direction,
            _float_text(row.actual_bps),
            _float_text(row.baseline_prediction_bps),
            _float_text(row.cross_prediction_bps),
        )
        for row in ordered
    )
    return _csv_bytes(_PREDICTION_FIELDS, csv_rows)


def event_study_csv_bytes(results: Sequence[EventStudyResult]) -> bytes:
    """Render measured and excluded event outcomes through one closed schema."""
    materialized = tuple(results)
    if not materialized or any(
        not isinstance(result, EventStudyResult) for result in materialized
    ):
        raise CrossPoolContractError(
            "event reporting requires at least one EventStudyResult"
        )

    outcomes: list[EventResponse | EventExclusion] = []
    for result in materialized:
        outcomes.extend(result.responses)
        outcomes.extend(result.exclusions)
    if not outcomes:
        raise CrossPoolContractError("event reporting requires at least one outcome")

    keys = tuple(_event_key(row) for row in outcomes)
    if len(set(keys)) != len(keys):
        raise CrossPoolContractError("event reporting keys must be globally unique")
    ordered = sorted(outcomes, key=_event_key)
    rows = tuple(_event_csv_row(row) for row in ordered)
    return _csv_bytes(_EVENT_FIELDS, rows)


def dtw_weekly_paths_csv_bytes(rows: Sequence[DtwWeekResult]) -> bytes:
    """Expand each deterministic weekly DTW path into one row per match."""
    materialized = tuple(rows)
    if not materialized or any(
        not isinstance(row, DtwWeekResult) for row in materialized
    ):
        raise CrossPoolContractError(
            "DTW path reporting requires at least one weekly result"
        )
    keys = tuple(
        (row.week_start_timestamp_ms, row.direction, row.band_steps)
        for row in materialized
    )
    if len(set(keys)) != len(keys):
        raise CrossPoolContractError("DTW weekly reporting keys must be unique")

    ordered = sorted(
        materialized,
        key=lambda row: (
            row.week_start_timestamp_ms,
            _DIRECTION_ORDER[row.direction],
            row.band_steps,
        ),
    )
    expanded = tuple(
        (
            row.week_start_timestamp_ms,
            row.direction,
            row.band_steps,
            path_index,
            source_index,
            target_index,
            row.path_length,
            _float_text(row.total_cost),
            _float_text(row.normalized_cost),
            _float_text(row.median_signed_lag_steps),
        )
        for row in ordered
        for path_index, (source_index, target_index) in enumerate(row.matches)
    )
    return _csv_bytes(_DTW_PATH_FIELDS, expanded)


def dtw_nulls_csv_bytes(rows: Sequence[DtwNullResult]) -> bytes:
    """Render rotation nulls with deterministic strict observed-value ranks."""
    materialized = tuple(rows)
    if not materialized or any(
        not isinstance(row, DtwNullResult) for row in materialized
    ):
        raise CrossPoolContractError(
            "DTW null reporting requires at least one rotation result"
        )

    row_keys = tuple(
        (
            row.week_start_timestamp_ms,
            row.direction,
            row.band_steps,
            row.rotation_days,
        )
        for row in materialized
    )
    if len(set(row_keys)) != len(row_keys):
        raise CrossPoolContractError("DTW null reporting keys must be unique")

    grouped: dict[tuple[int, Direction, int], list[DtwNullResult]] = {}
    for row in materialized:
        key = (row.week_start_timestamp_ms, row.direction, row.band_steps)
        grouped.setdefault(key, []).append(row)

    ranks: dict[tuple[int, Direction, int], tuple[int, int]] = {}
    for key, group in grouped.items():
        rotations = tuple(sorted(row.rotation_days for row in group))
        if rotations != (1, 2, 3, 4, 5, 6):
            raise CrossPoolContractError(
                "DTW null reporting requires rotations one through six"
            )
        observed_costs = {row.observed_normalized_cost for row in group}
        observed_lags = {row.observed_median_signed_lag_steps for row in group}
        if len(observed_costs) != 1 or len(observed_lags) != 1:
            raise CrossPoolContractError(
                "DTW null group must use one observed cost and lag"
            )
        observed_cost = next(iter(observed_costs))
        observed_absolute_lag = abs(next(iter(observed_lags)))
        ranks[key] = (
            1 + sum(row.normalized_cost < observed_cost for row in group),
            1
            + sum(
                abs(row.median_signed_lag_steps) < observed_absolute_lag
                for row in group
            ),
        )

    ordered = sorted(
        materialized,
        key=lambda row: (
            row.week_start_timestamp_ms,
            _DIRECTION_ORDER[row.direction],
            row.band_steps,
            row.rotation_days,
        ),
    )
    csv_rows = tuple(_dtw_null_csv_row(row, ranks) for row in ordered)
    return _csv_bytes(_DTW_NULL_FIELDS, csv_rows)


def causal_audit_counts_payload(analysis: StatisticalAnalysis) -> dict[str, JsonValue]:
    return {
        "base_stream_rows": analysis.base_quality.rows,
        "bsc_stream_rows": analysis.bsc_quality.rows,
        "panel_15m_rows": len(analysis.panel_15m.rows),
        "panel_1h_rows": len(analysis.panel_1h.rows),
        "panel_4h_rows": len(analysis.panel_4h.rows),
    }


def data_quality_payload(analysis: StatisticalAnalysis) -> dict[str, JsonValue]:
    return {
        "streams": [
            _stream_quality_payload(analysis.base_quality),
            _stream_quality_payload(analysis.bsc_quality),
        ],
        "causal_audit_counts": causal_audit_counts_payload(analysis),
    }


def predictive_metrics_payload(analysis: StatisticalAnalysis) -> dict[str, JsonValue]:
    primary_audit = _directional_audit(analysis.primary)
    reverse_audit = _directional_audit(analysis.reverse)
    return {
        "status": "complete",
        "primary": predictive_audit_payload(primary_audit),
        "reverse": predictive_audit_payload(reverse_audit),
        "sensitivity": {
            "panel_15m": {
                "primary": predictive_inference_payload(analysis.primary.inference_15m),
                "reverse": predictive_inference_payload(analysis.reverse.inference_15m),
            },
            "panel_4h": {
                "primary": predictive_inference_payload(analysis.primary.inference_4h),
                "reverse": predictive_inference_payload(analysis.reverse.inference_4h),
            },
        },
    }


def event_study_manifest_payload(analysis: StatisticalAnalysis) -> dict[str, JsonValue]:
    primary = analysis.primary
    reverse = analysis.reverse
    exclusion_counts: dict[str, JsonValue] = {
        "primary_missing_target_start_state": _exclusion_count(
            primary.event_study,
            "missing_target_start_state",
        ),
        "primary_missing_target_end_state": _exclusion_count(
            primary.event_study,
            "missing_target_end_state",
        ),
        "reverse_missing_target_start_state": _exclusion_count(
            reverse.event_study,
            "missing_target_start_state",
        ),
        "reverse_missing_target_end_state": _exclusion_count(
            reverse.event_study,
            "missing_target_end_state",
        ),
    }
    return {
        "status": "complete",
        "primary": _event_direction_payload(primary),
        "reverse": _event_direction_payload(reverse),
        "exclusion_counts": exclusion_counts,
    }


def dtw_null_summary_payload(analysis: StatisticalAnalysis) -> dict[str, JsonValue]:
    primary = analysis.primary.dtw_nulls
    reverse = analysis.reverse.dtw_nulls
    return {
        "primary_rotation_comparison_count": len(primary),
        "primary_observed_cost_better_share": _observed_cost_better_share(primary),
        "reverse_rotation_comparison_count": len(reverse),
        "reverse_observed_cost_better_share": _observed_cost_better_share(reverse),
    }


def market_structure_manifest_payload(
    analysis: StatisticalAnalysis,
) -> dict[str, JsonValue]:
    return {
        "status": "complete",
        "venues": [
            _venue_structure_manifest_payload(row)
            for row in analysis.market_structure.venues
        ],
    }


def render_statistical_artifacts(
    analysis: StatisticalAnalysis,
) -> tuple[RenderedArtifact, ...]:
    predictions = tuple(
        row
        for directional in (analysis.primary, analysis.reverse)
        for walk_forward in (
            directional.walk_forward_15m,
            directional.walk_forward_1h,
            directional.walk_forward_4h,
        )
        for row in walk_forward.predictions
    )
    inferences = tuple(
        inference
        for horizon_ms in (900_000, 3_600_000, 14_400_000)
        for inference in (
            _inference_at_horizon(analysis.primary, horizon_ms),
            _inference_at_horizon(analysis.reverse, horizon_ms),
        )
    )
    event_summaries = (
        analysis.primary.event_summaries + analysis.reverse.event_summaries
    )
    return (
        RenderedArtifact(
            "data_quality.json",
            canonical_json_bytes(data_quality_payload(analysis)),
        ),
        RenderedArtifact(
            "market_structure.json",
            serialize_market_structure(analysis.market_structure.venues).encode("utf-8"),
        ),
        RenderedArtifact("panel_15m.csv", panel_csv_bytes(analysis.panel_15m)),
        RenderedArtifact("panel_1h.csv", panel_csv_bytes(analysis.panel_1h)),
        RenderedArtifact("panel_4h.csv", panel_csv_bytes(analysis.panel_4h)),
        RenderedArtifact(
            "predictive_predictions.csv",
            prediction_csv_bytes(predictions),
        ),
        RenderedArtifact(
            "predictive_metrics.json",
            canonical_json_bytes(predictive_metrics_payload(analysis)),
        ),
        RenderedArtifact(
            "event_study.csv",
            event_study_csv_bytes(
                (analysis.primary.event_study, analysis.reverse.event_study)
            ),
        ),
        RenderedArtifact(
            "dtw_weekly_paths.csv",
            dtw_weekly_paths_csv_bytes(
                analysis.primary.dtw_weeks + analysis.reverse.dtw_weeks
            ),
        ),
        RenderedArtifact(
            "dtw_nulls.csv",
            dtw_nulls_csv_bytes(
                analysis.primary.dtw_nulls + analysis.reverse.dtw_nulls
            ),
        ),
        RenderedArtifact(
            "statistical_report.md",
            statistical_report_bytes(analysis),
        ),
        RenderedArtifact("price_gap.png", price_gap_png(analysis.panel_1h)),
        RenderedArtifact(
            "predictive_performance.png",
            predictive_performance_png(inferences),
        ),
        RenderedArtifact(
            "event_response.png",
            event_response_png(event_summaries),
        ),
        RenderedArtifact(
            "dtw_lag.png",
            dtw_lag_png(
                (analysis.primary.dtw_stability, analysis.reverse.dtw_stability)
            ),
        ),
    )


def statistical_report_bytes(analysis: StatisticalAnalysis) -> bytes:
    primary = _directional_audit(analysis.primary)
    reverse = _directional_audit(analysis.reverse)
    lines = [
        "# Frozen Cross-Pool Statistical Report",
        "",
        "## Out-of-sample predictive results",
        "",
        _predictive_report_line("BSC to Base", primary.inference),
        _predictive_report_line("Base to BSC", reverse.inference),
        "",
        "The 15-minute and four-hour estimates are pre-specified sensitivity checks. "
        "The one-hour paired tests determine each evidence class; the frozen "
        "robustness gates, including DTW band stability, determine whether a "
        "directional publication branch is available.",
        "",
        "## Event-study diagnostics",
        "",
        _event_report_line("BSC to Base", analysis.primary),
        _event_report_line("Base to BSC", analysis.reverse),
        "",
        "## Constrained dynamic-time-warping diagnostics",
        "",
        _dtw_report_line("BSC to Base", analysis.primary),
        _dtw_report_line("Base to BSC", analysis.reverse),
        "",
        "## Data quality",
        "",
        f"Base feature events: {analysis.base_quality.rows}; "
        f"BSC feature events: {analysis.bsc_quality.rows}.",
        f"Causal panels contain {len(analysis.panel_15m.rows)} 15-minute, "
        f"{len(analysis.panel_1h.rows)} one-hour, and "
        f"{len(analysis.panel_4h.rows)} four-hour rows.",
        "",
        "## Market structure (post hoc, non-causal)",
        "",
        "Venue activity, volume, and owner concentration are descriptive context only. "
        "They do not identify price discovery, toxic flow, or external LP profitability.",
    ]
    for venue in analysis.market_structure.venues:
        lines.append(
            f"- {venue.pool}: {venue.swap_count} swaps, "
            f"{venue.meaningful_move_count} moves of at least 10 bps, "
            f"known-owner capital coverage {venue.exact_known_owner_capital_coverage}."
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _stream_quality_payload(quality: StreamQuality) -> dict[str, JsonValue]:
    gaps = quality.update_gap_quantiles_ms
    return {
        "pool": quality.pool,
        "rows": quality.rows,
        "first_timestamp_ms": quality.first_timestamp_ms,
        "last_timestamp_ms": quality.last_timestamp_ms,
        "update_gap_quantiles_ms": (
            None
            if gaps is None
            else {"p50": gaps.p50, "p95": gaps.p95, "p99": gaps.p99}
        ),
        "pre_transition_rows": quality.pre_transition_rows,
        "post_transition_rows": quality.post_transition_rows,
    }


def _directional_audit(
    analysis: DirectionalAnalysis,
) -> DirectionalPredictiveAudit:
    return analysis.audit_1h


def _event_direction_payload(
    analysis: DirectionalAnalysis,
) -> dict[str, JsonValue]:
    outcomes: tuple[EventResponse | EventExclusion, ...] = (
        analysis.event_study.responses + analysis.event_study.exclusions
    )
    return {
        "direction": analysis.direction,
        "shock_count": len({row.shock_timestamp_ms for row in outcomes}),
        "response_count": len(analysis.event_study.responses),
        "exclusion_count": len(analysis.event_study.exclusions),
        "summaries": [
            {
                "horizon_ms": row.horizon_ms,
                "event_count": row.event_count,
                "event_day_count": row.event_day_count,
                "mean_response_bps": _confidence_interval_payload(
                    row.mean_response_bps
                ),
                "median_response_bps": _confidence_interval_payload(
                    row.median_response_bps
                ),
                "direction_agreement": _confidence_interval_payload(
                    row.direction_agreement
                ),
            }
            for row in analysis.event_summaries
        ],
    }


def _confidence_interval_payload(
    interval: ConfidenceInterval,
) -> dict[str, JsonValue]:
    return {
        "point": interval.point,
        "lower": interval.lower,
        "upper": interval.upper,
    }


def _exclusion_count(
    result: EventStudyResult,
    reason: EventExclusionReason,
) -> int:
    return sum(row.reason == reason for row in result.exclusions)


def _observed_cost_better_share(rows: Sequence[DtwNullResult]) -> float:
    if not rows:
        raise CrossPoolContractError("DTW null summary requires rotation comparisons")
    return sum(row.observed_cost_improvement > 0.0 for row in rows) / len(rows)


def _venue_structure_manifest_payload(
    row: VenueStructureSummary,
) -> dict[str, JsonValue]:
    return {
        "pool": row.pool,
        "activity_start_timestamp_ms": row.activity_start_timestamp_ms,
        "activity_end_timestamp_ms": row.activity_end_timestamp_ms,
        "swap_count": row.swap_count,
        "meaningful_move_count": row.meaningful_move_count,
        "fee_rate_fraction": _decimal_text(row.fee_rate),
        "median_update_gap_ms": _decimal_text(row.update_gaps_ms.median),
        "p95_update_gap_ms": _decimal_text(row.update_gaps_ms.p95),
        "median_active_liquidity_units": _decimal_text(
            row.active_liquidity.median
        ),
        "total_volume_usd": _decimal_text(row.total_volume_usd),
        "known_owner_count": row.known_owner_count,
        "exact_known_owner_capital_coverage_fraction": _decimal_text(
            row.exact_known_owner_capital_coverage
        ),
        "exact_opening_capital_top_owner_share_fraction": _decimal_text(
            row.exact_opening_capital_top_owner_share
        ),
        "exact_opening_capital_top_three_share_fraction": _decimal_text(
            row.exact_opening_capital_top_three_share
        ),
        "exact_opening_capital_hhi_fraction": _decimal_text(
            row.exact_opening_capital_hhi
        ),
    }


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise CrossPoolContractError("serialized decimal must be finite")
    return format(value, "f")


def _inference_at_horizon(
    analysis: DirectionalAnalysis,
    horizon_ms: int,
) -> PredictiveInference:
    by_horizon = {
        900_000: analysis.inference_15m,
        3_600_000: analysis.audit_1h.inference,
        14_400_000: analysis.inference_4h,
    }
    try:
        return by_horizon[horizon_ms]
    except KeyError as exc:  # pragma: no cover - callers use the frozen tuple.
        raise CrossPoolContractError("unsupported predictive horizon") from exc


def _predictive_report_line(label: str, inference: PredictiveInference) -> str:
    metrics = inference.metrics
    interval = inference.bootstrap.mae_improvement_bps
    return (
        f"- {label}: {inference.evidence_class}; cross-pool MAE "
        f"{metrics.cross_mae_bps:.6g} bps versus target-only MAE "
        f"{metrics.baseline_mae_bps:.6g} bps; paired MAE improvement "
        f"{interval.point:.6g} bps (95% interval "
        f"[{interval.lower:.6g}, {interval.upper:.6g}])."
    )


def _event_report_line(label: str, analysis: DirectionalAnalysis) -> str:
    outcomes = analysis.event_study.responses + analysis.event_study.exclusions
    summaries = "; ".join(
        f"{row.horizon_ms // 60_000}m mean {row.mean_response_bps.point:.6g} bps "
        f"(95% [{row.mean_response_bps.lower:.6g}, "
        f"{row.mean_response_bps.upper:.6g}]), median "
        f"{row.median_response_bps.point:.6g} bps, agreement "
        f"{row.direction_agreement.point:.3f}"
        for row in analysis.event_summaries
    )
    return (
        f"- {label}: {len({row.shock_timestamp_ms for row in outcomes})} shocks, "
        f"{len(analysis.event_study.responses)} measured horizon responses, and "
        f"{len(analysis.event_study.exclusions)} excluded horizon responses; "
        f"{summaries}."
    )


def _dtw_report_line(label: str, analysis: DirectionalAnalysis) -> str:
    stability = analysis.dtw_stability
    primary_lag_minutes = stability.aggregate_median_lag_by_band[4] * 15.0
    primary_nulls = tuple(row for row in analysis.dtw_nulls if row.band_steps == 4)
    better_count = sum(row.observed_cost_improvement > 0.0 for row in primary_nulls)
    return (
        f"- {label}: primary-band aggregate signed lag "
        f"{primary_lag_minutes:.6g} minutes; same-sign week share "
        f"{stability.primary_band_same_sign_week_share:.3f}; "
        f"the observed path cost beat {better_count}/{len(primary_nulls)} "
        "whole-day rotation-null comparisons; "
        f"band_unstable={str(stability.band_unstable).lower()}."
    )


def _panel_csv_row(row: PanelRow) -> tuple[object, ...]:
    return (
        row.timestamp_ms,
        row.horizon_ms,
        row.base_state_timestamp_ms,
        row.bsc_state_timestamp_ms,
        row.base_forward_state_timestamp_ms,
        row.bsc_forward_state_timestamp_ms,
        row.base_lag_block_number,
        row.bsc_lag_block_number,
        row.base_state_block_number,
        row.bsc_state_block_number,
        row.base_forward_block_number,
        row.bsc_forward_block_number,
        row.base_age_ms,
        row.bsc_age_ms,
        _float_text(row.base_mid),
        _float_text(row.bsc_mid),
        _float_text(row.base_trailing_return_bps),
        _float_text(row.bsc_trailing_return_bps),
        _float_text(row.base_minus_bsc_gap_bps),
        _float_text(row.base_forward_return_bps),
        _float_text(row.bsc_forward_return_bps),
        row.base_regime,
        row.bsc_regime,
    )


def _event_key(row: EventResponse | EventExclusion) -> tuple[int, int, int]:
    return (
        _DIRECTION_ORDER[row.direction],
        row.shock_timestamp_ms,
        row.horizon_ms,
    )


def _event_csv_row(row: EventResponse | EventExclusion) -> tuple[object, ...]:
    if isinstance(row, EventResponse):
        return (
            "response",
            row.direction,
            row.shock_timestamp_ms,
            row.shock_day_utc.isoformat(),
            row.horizon_ms,
            row.source_pool,
            row.target_pool,
            _float_text(row.source_move_bps),
            row.target_start_timestamp_ms,
            row.target_end_timestamp_ms,
            _float_text(row.target_response_bps),
            "true" if row.direction_agrees else "false",
            "",
        )
    return (
        "exclusion",
        row.direction,
        row.shock_timestamp_ms,
        row.shock_day_utc.isoformat(),
        row.horizon_ms,
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        row.reason,
    )


def _dtw_null_csv_row(
    row: DtwNullResult,
    ranks: Mapping[tuple[int, Direction, int], tuple[int, int]],
) -> tuple[object, ...]:
    observed_cost_rank, observed_lag_rank = ranks[
        (row.week_start_timestamp_ms, row.direction, row.band_steps)
    ]
    return (
        row.week_start_timestamp_ms,
        row.direction,
        row.band_steps,
        row.rotation_days,
        row.path_length,
        _float_text(row.total_cost),
        _float_text(row.normalized_cost),
        _float_text(row.observed_normalized_cost),
        _float_text(row.observed_cost_improvement),
        _float_text(row.median_signed_lag_steps),
        _float_text(row.observed_median_signed_lag_steps),
        _float_text(row.observed_signed_lag_difference_steps),
        observed_cost_rank,
        observed_lag_rank,
    )


def _csv_bytes(
    header: tuple[str, ...],
    rows: Sequence[tuple[object, ...]],
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _float_text(value: float) -> str:
    if isinstance(value, bool) or not math.isfinite(value):
        raise CrossPoolContractError("reported floating-point values must be finite")
    if value == 0.0:
        return "0"
    return repr(value)
