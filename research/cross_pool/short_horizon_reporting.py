"""Deterministic artifacts and semantic validation for short-horizon evidence."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal, Mapping, Sequence, cast

from matplotlib import rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from research.cross_pool.contracts import CrossPoolContractError, Direction, PoolName
from research.cross_pool.short_horizon import (
    SHORT_HORIZONS_MS,
    FirstUpdateStatus,
    ShortHorizonExclusion,
    ShortHorizonResponse,
    ShortHorizonStudy,
)
from research.cross_pool.short_horizon_artifacts import (
    SHORT_HORIZON_ARTIFACT_NAMES,
    ShortHorizonArtifact,
)
from research.cross_pool.short_horizon_inference import (
    SHORT_HORIZON_BOOTSTRAP_CONFIDENCE_LEVEL,
    SHORT_HORIZON_BOOTSTRAP_RESAMPLES,
    SHORT_HORIZON_BOOTSTRAP_SEED,
    ShortHorizonInference,
    SimultaneousBandFamily,
    infer_short_horizon,
)
from research.cross_pool.short_horizon_manifest import (
    DirectionQaCounts,
    JsonValue,
    canonical_short_horizon_manifest_bytes,
)

_DIRECTION_ORDER: tuple[Direction, ...] = ("bsc_to_base", "base_to_bsc")
_COHORT_ORDER = ("primary", "exclude_same_timestamp")
_EVENT_FIELDS = (
    "outcome_type",
    "direction",
    "shock_timestamp_ms",
    "shock_day_utc",
    "horizon_ms",
    "source_pool",
    "target_pool",
    "source_move_bps",
    "source_move_sign",
    "source_shock_raw_mid",
    "source_shock_bid",
    "source_shock_ask",
    "source_end_timestamp_ms",
    "source_end_age_ms",
    "source_end_raw_mid",
    "source_end_bid",
    "source_end_ask",
    "target_start_timestamp_ms",
    "target_start_age_ms",
    "target_start_raw_mid",
    "target_start_bid",
    "target_start_ask",
    "target_end_timestamp_ms",
    "target_end_age_ms",
    "target_end_raw_mid",
    "target_end_bid",
    "target_end_ask",
    "target_response_bps",
    "zero_response",
    "direction_agrees",
    "target_same_timestamp",
    "first_update_status",
    "first_update_timestamp_ms",
    "first_update_delay_ms",
    "first_update_raw_mid",
    "first_update_response_bps",
    "first_update_direction_agrees",
    "fee_gap_start_bps",
    "fee_gap_end_bps",
    "fee_gap_start_positive_bps",
    "fee_gap_end_positive_bps",
    "fee_gap_closure_bps",
    "exclusion_reason",
)
_SUMMARY_FIELDS = (
    "direction",
    "cohort",
    "horizon_ms",
    "eligible_event_count",
    "shock_day_count",
    "unconditional_mean_response_bps",
    "unconditional_mean_lower_bps",
    "unconditional_mean_upper_bps",
    "unconditional_median_response_bps",
    "unconditional_median_lower_bps",
    "unconditional_median_upper_bps",
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
    "unconditional_mean_band_status",
    "unconditional_mean_band_reason",
    "unconditional_mean_band_valid_joint_resamples",
    "unconditional_mean_band_critical_value",
    "unconditional_mean_band_lower_bps",
    "unconditional_mean_band_upper_bps",
    "unconditional_mean_band_bootstrap_standard_deviation",
    "update_incidence_band_status",
    "update_incidence_band_reason",
    "update_incidence_band_valid_joint_resamples",
    "update_incidence_band_critical_value",
    "update_incidence_band_lower",
    "update_incidence_band_upper",
    "update_incidence_band_bootstrap_standard_deviation",
    "conditional_first_update_mean_band_status",
    "conditional_first_update_mean_band_reason",
    "conditional_first_update_mean_band_valid_joint_resamples",
    "conditional_first_update_mean_band_critical_value",
    "conditional_first_update_mean_band_lower_bps",
    "conditional_first_update_mean_band_upper_bps",
    "conditional_first_update_mean_band_bootstrap_standard_deviation",
)
_FIGURE_RC = {
    "font.family": "DejaVu Sans",
    "font.size": 8.0,
    "axes.titlesize": 9.0,
    "axes.labelsize": 8.0,
    "legend.fontsize": 7.0,
    "savefig.dpi": 120.0,
    "figure.dpi": 120.0,
}


@dataclass(frozen=True)
class ShortHorizonEvidence:
    studies: tuple[ShortHorizonStudy, ...]
    inferences: tuple[ShortHorizonInference, ...]
    parent_anchor_reconciled: bool

    def __post_init__(self) -> None:
        if tuple(study.direction for study in self.studies) != _DIRECTION_ORDER:
            raise CrossPoolContractError("short-horizon studies must use canonical order")
        profiles = tuple((value.direction, value.cohort) for value in self.inferences)
        expected_profiles = tuple(
            (direction, cohort) for direction in _DIRECTION_ORDER for cohort in _COHORT_ORDER
        )
        if profiles != expected_profiles:
            raise CrossPoolContractError("short-horizon inference must use canonical order")
        if not isinstance(self.parent_anchor_reconciled, bool):
            raise CrossPoolContractError("parent-anchor state must be boolean")
        by_direction = {study.direction: study for study in self.studies}
        for inference in self.inferences:
            study = by_direction[inference.direction]
            expected_count = (
                study.eligible_shock_count
                if inference.cohort == "primary"
                else len(
                    {row.shock_timestamp_ms for row in study.responses_excluding_same_timestamp}
                )
            )
            if inference.eligible_event_count != expected_count:
                raise CrossPoolContractError("inference support does not match its study")

    @property
    def qa_counts(self) -> tuple[DirectionQaCounts, ...]:
        return tuple(
            DirectionQaCounts(
                direction=study.direction,
                detected_shock_count=study.detected_shock_count,
                eligible_shock_count=study.eligible_shock_count,
                exclusion_count=len(study.exclusions),
                same_timestamp_shock_count=len(
                    {row.shock_timestamp_ms for row in study.responses if row.target_same_timestamp}
                ),
            )
            for study in self.studies
        )


def render_short_horizon_artifacts(
    evidence: ShortHorizonEvidence,
) -> tuple[ShortHorizonArtifact, ...]:
    if not isinstance(evidence, ShortHorizonEvidence):
        raise CrossPoolContractError("reporting requires typed short-horizon evidence")
    return (
        ShortHorizonArtifact(
            "short_horizon_events.csv",
            short_horizon_events_csv_bytes(evidence.studies),
        ),
        ShortHorizonArtifact(
            "short_horizon_summaries.csv",
            short_horizon_summaries_csv_bytes(evidence.inferences),
        ),
        ShortHorizonArtifact(
            "short_horizon_qa.json",
            canonical_short_horizon_manifest_bytes(_qa_payload(evidence)),
        ),
        ShortHorizonArtifact(
            "short_horizon_report.md",
            short_horizon_report_bytes(evidence),
        ),
        ShortHorizonArtifact(
            "short_horizon_response.png",
            _response_figure(evidence.inferences),
        ),
        ShortHorizonArtifact(
            "short_horizon_updates.png",
            _updates_figure(evidence.inferences),
        ),
        ShortHorizonArtifact(
            "short_horizon_fee_gap.png",
            _fee_gap_figure(evidence.inferences),
        ),
    )


def validate_rendered_short_horizon_artifacts(
    artifacts: Sequence[ShortHorizonArtifact],
    evidence: ShortHorizonEvidence,
) -> None:
    materialized = tuple(artifacts)
    if tuple(item.relative_name for item in materialized) != SHORT_HORIZON_ARTIFACT_NAMES:
        raise CrossPoolContractError("short-horizon artifacts must use the exact file set")
    reconstructed_studies = parse_short_horizon_events_csv(materialized[0].content)
    if reconstructed_studies != evidence.studies:
        raise CrossPoolContractError("event artifact does not match typed evidence")
    recomputed = tuple(
        infer_short_horizon(study, exclude_same_timestamp=exclude)
        for study in reconstructed_studies
        for exclude in (False, True)
    )
    if recomputed != evidence.inferences:
        raise CrossPoolContractError("manifest inference does not derive from event rows")
    regenerated = render_short_horizon_artifacts(
        ShortHorizonEvidence(
            studies=reconstructed_studies,
            inferences=recomputed,
            parent_anchor_reconciled=evidence.parent_anchor_reconciled,
        )
    )
    if materialized != regenerated:
        raise CrossPoolContractError("deterministic artifact bytes do not match typed evidence")


def short_horizon_events_csv_bytes(
    studies: Sequence[ShortHorizonStudy],
) -> bytes:
    materialized = tuple(studies)
    if tuple(study.direction for study in materialized) != _DIRECTION_ORDER:
        raise CrossPoolContractError("event reporting requires canonical study order")
    rows: list[dict[str, str]] = []
    for study in materialized:
        outcomes: list[tuple[int, int, ShortHorizonResponse | ShortHorizonExclusion]] = [
            (row.shock_timestamp_ms, 0, row) for row in study.responses
        ]
        outcomes.extend((row.shock_timestamp_ms, 1, row) for row in study.exclusions)
        for _, _, outcome in sorted(
            outcomes,
            key=lambda item: (
                item[0],
                item[1],
                item[2].horizon_ms if isinstance(item[2], ShortHorizonResponse) else -1,
            ),
        ):
            rows.append(
                _response_csv_row(outcome)
                if isinstance(outcome, ShortHorizonResponse)
                else _exclusion_csv_row(outcome)
            )
    return _csv_bytes(_EVENT_FIELDS, rows)


def parse_short_horizon_events_csv(raw: bytes) -> tuple[ShortHorizonStudy, ...]:
    if not raw.endswith(b"\n") or b"\r" in raw:
        raise CrossPoolContractError("short-horizon event CSV must use canonical newlines")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError("short-horizon event CSV must use UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != _EVENT_FIELDS:
        raise CrossPoolContractError("short-horizon event CSV header is invalid")
    responses: dict[Direction, list[ShortHorizonResponse]] = {
        "bsc_to_base": [],
        "base_to_bsc": [],
    }
    exclusions: dict[Direction, list[ShortHorizonExclusion]] = {
        "bsc_to_base": [],
        "base_to_bsc": [],
    }
    for row_number, raw_row in enumerate(reader, start=2):
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise CrossPoolContractError(f"event CSV row {row_number} has unexpected cells")
        row = cast(dict[str, str], raw_row)
        direction = _parse_direction(row["direction"], row_number)
        if row["outcome_type"] == "response":
            responses[direction].append(_response_from_csv_row(row, row_number, direction))
        elif row["outcome_type"] == "exclusion":
            exclusions[direction].append(_exclusion_from_csv_row(row, row_number, direction))
        else:
            raise CrossPoolContractError(f"event CSV row {row_number} has invalid outcome type")
    studies = tuple(
        ShortHorizonStudy(
            direction=direction,
            detected_shock_count=(
                len({row.shock_timestamp_ms for row in responses[direction]})
                + len(exclusions[direction])
            ),
            eligible_shock_count=len({row.shock_timestamp_ms for row in responses[direction]}),
            responses=tuple(responses[direction]),
            exclusions=tuple(exclusions[direction]),
        )
        for direction in _DIRECTION_ORDER
    )
    if short_horizon_events_csv_bytes(studies) != raw:
        raise CrossPoolContractError("short-horizon event CSV bytes are not canonical")
    return studies


def short_horizon_summaries_csv_bytes(
    inferences: Sequence[ShortHorizonInference],
) -> bytes:
    materialized = tuple(inferences)
    profiles = tuple((value.direction, value.cohort) for value in materialized)
    if profiles != tuple(
        (direction, cohort) for direction in _DIRECTION_ORDER for cohort in _COHORT_ORDER
    ):
        raise CrossPoolContractError("summary reporting requires canonical profiles")
    rows: list[dict[str, str]] = []
    for inference in materialized:
        bands = {family.metric: family for family in inference.simultaneous_bands}
        for summary in inference.summaries:
            row = {
                "direction": summary.direction,
                "cohort": inference.cohort,
                "horizon_ms": str(summary.horizon_ms),
                "eligible_event_count": str(summary.eligible_event_count),
                "shock_day_count": str(summary.shock_day_count),
                "unconditional_mean_response_bps": _float_text(
                    summary.unconditional_mean_response_bps.point
                ),
                "unconditional_mean_lower_bps": _float_text(
                    summary.unconditional_mean_response_bps.lower
                ),
                "unconditional_mean_upper_bps": _float_text(
                    summary.unconditional_mean_response_bps.upper
                ),
                "unconditional_median_response_bps": _float_text(
                    summary.unconditional_median_response_bps.point
                ),
                "unconditional_median_lower_bps": _float_text(
                    summary.unconditional_median_response_bps.lower
                ),
                "unconditional_median_upper_bps": _float_text(
                    summary.unconditional_median_response_bps.upper
                ),
                "direction_agreement_share": _float_text(summary.direction_agreement_share),
                "zero_response_share": _float_text(summary.zero_response_share),
                "update_count": str(summary.update_count),
                "right_censored_count": str(summary.right_censored_count),
                "update_incidence": _float_text(summary.update_incidence),
                "observed_update_day_count": str(summary.observed_update_day_count),
                "conditional_first_update_mean_response_bps": _optional_float_text(
                    summary.conditional_first_update_mean_response_bps
                ),
                "conditional_first_update_median_response_bps": _optional_float_text(
                    summary.conditional_first_update_median_response_bps
                ),
                "conditional_first_update_median_delay_ms": _optional_float_text(
                    summary.conditional_first_update_median_delay_ms
                ),
                "conditional_first_update_p95_delay_ms": _optional_float_text(
                    summary.conditional_first_update_p95_delay_ms
                ),
                "target_start_median_age_ms": _float_text(summary.target_start_median_age_ms),
                "target_start_p95_age_ms": _float_text(summary.target_start_p95_age_ms),
                "target_end_median_age_ms": _float_text(summary.target_end_median_age_ms),
                "target_end_p95_age_ms": _float_text(summary.target_end_p95_age_ms),
                "mean_fee_gap_start_bps": _float_text(summary.mean_fee_gap_start_bps),
                "mean_fee_gap_end_bps": _float_text(summary.mean_fee_gap_end_bps),
                "positive_fee_gap_start_share": _float_text(summary.positive_fee_gap_start_share),
                "positive_fee_gap_end_share": _float_text(summary.positive_fee_gap_end_share),
                "mean_fee_gap_closure_bps": _float_text(summary.mean_fee_gap_closure_bps),
            }
            row.update(
                _band_csv_fields(
                    "unconditional_mean",
                    bands["unconditional_mean_response_bps"],
                    summary.horizon_ms,
                )
            )
            row.update(
                _band_csv_fields(
                    "update_incidence",
                    bands["update_incidence"],
                    summary.horizon_ms,
                )
            )
            row.update(
                _band_csv_fields(
                    "conditional_first_update_mean",
                    bands["conditional_first_update_mean_response_bps"],
                    summary.horizon_ms,
                )
            )
            rows.append(row)
    return _csv_bytes(_SUMMARY_FIELDS, rows)


def short_horizon_report_bytes(evidence: ShortHorizonEvidence) -> bytes:
    lines = [
        "# Cross-Pool Short-Horizon Response Study",
        "",
        "This is a post-hoc exploratory response study. It does not change the "
        "reviewed parent conclusion: aggregate cross-pool leadership remains unresolved.",
        "",
        "All seven horizons are shown for both directions. The pointwise UTC-day "
        "bootstrap intervals are descriptive; within-direction max-z simultaneous "
        "bands address the seven-horizon family when available.",
        "The update-profile figure contains point estimates; inference is reported in "
        "short_horizon_summaries.csv.",
        "",
        "## Primary response and update profile",
        "",
        "| Direction | Horizon | Mean response (bps) | Pointwise 95% interval | "
        "Update incidence | Median observed-update delay |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for inference in evidence.inferences:
        if inference.cohort != "primary":
            continue
        for summary in inference.summaries:
            delay = _display_optional(summary.conditional_first_update_median_delay_ms)
            direction_label = _direction_label(inference.direction)
            horizon_label = _horizon_label(summary.horizon_ms)
            lines.append(
                f"| {direction_label} | {horizon_label} | "
                f"{summary.unconditional_mean_response_bps.point:.6f} | "
                f"[{summary.unconditional_mean_response_bps.lower:.6f}, "
                f"{summary.unconditional_mean_response_bps.upper:.6f}] | "
                f"{summary.update_incidence:.3f} | {delay} |"
            )
    lines.extend(
        [
            "",
            "An unchanged as-of state is a valid unconditional zero. A right-censored "
            "first update is not encoded as a conditional zero; conditional response "
            "and latency summarize observed updates only and are not survival estimates.",
            "",
            "## Simultaneous-band availability",
            "",
        ]
    )
    for inference in evidence.inferences:
        if inference.cohort != "primary":
            continue
        states = ", ".join(
            f"{family.metric}={family.status}"
            + (f" ({family.reason})" if family.reason is not None else "")
            for family in inference.simultaneous_bands
        )
        lines.append(f"- {_direction_label(inference.direction)}: {states}.")
    lines.extend(
        [
            "",
            "## Timestamp-tie sensitivity",
            "",
            "The fixed sensitivity removes complete shock families whose target had an "
            "observation at the same timestamp. Cross-chain timestamps do not establish "
            "within-timestamp ordering.",
            "",
            "| Direction | Horizon | Primary mean response (bps) | "
            "Tie-excluded mean response (bps) | Primary update incidence | "
            "Tie-excluded update incidence |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    profiles = {
        (inference.direction, inference.cohort): inference for inference in evidence.inferences
    }
    for direction in _DIRECTION_ORDER:
        primary = profiles[(direction, "primary")]
        sensitivity = profiles[(direction, "exclude_same_timestamp")]
        for primary_summary, sensitivity_summary in zip(
            primary.summaries,
            sensitivity.summaries,
            strict=True,
        ):
            lines.append(
                f"| {_direction_label(direction)} | "
                f"{_horizon_label(primary_summary.horizon_ms)} | "
                f"{primary_summary.unconditional_mean_response_bps.point:.6f} | "
                f"{sensitivity_summary.unconditional_mean_response_bps.point:.6f} | "
                f"{primary_summary.update_incidence:.3f} | "
                f"{sensitivity_summary.update_incidence:.3f} |"
            )
    three_minute_means = {
        direction: next(
            summary.unconditional_mean_response_bps.point
            for summary in profiles[(direction, "primary")].summaries
            if summary.horizon_ms == 180_000
        )
        for direction in _DIRECTION_ORDER
    }
    signs_match_girum = all(value > 0.0 for value in three_minute_means.values())
    reproduces_magnitude_order = abs(three_minute_means["base_to_bsc"]) > abs(
        three_minute_means["bsc_to_base"]
    )
    sign_comparison = (
        "Both response signs match Girum's"
        if signs_match_girum
        else "The two response signs do not both match Girum's"
    )
    ordering_comparison = (
        "reproduces Girum's directional magnitude ordering"
        if reproduces_magnitude_order
        else "does not reproduce Girum's directional magnitude ordering"
    )
    lines.extend(
        [
            "",
            "## Fee-only gap",
            "",
            "The signed fee-only bid/ask gap includes both pool fees under an explicit "
            "USDC/USDT parity assumption. It excludes gas, slippage, latency, inventory "
            "constraints, and stablecoin basis; it is not net executable profit.",
            "",
            "## Reconciliation with Girum",
            "",
            "Girum uses per-swap moves of at least 10 bps, 3- and 30-minute responses, "
            "own-trade exclusions, a 10-minute de-clustering sensitivity, and "
            "circular-shift inference. This extension uses the parent cumulative "
            "5-bps/15-minute shock and paired UTC-day bootstrap.",
            "",
            "Girum's de-clustered three-minute means are Base to BSC +3.4 bps and "
            "BSC to Base +0.4 bps. This extension's three-minute means are "
            f"Base to BSC {three_minute_means['base_to_bsc']:+.6f} bps and "
            f"BSC to Base {three_minute_means['bsc_to_base']:+.6f} bps. "
            f"{sign_comparison}, but the extension {ordering_comparison}. Any sign "
            "agreement is descriptive, not corroborating evidence of Base leadership; "
            "the estimands, samples, clustering, and inferential procedures differ.",
            "",
            "No result here establishes causal price discovery, a unique directional "
            "leader, deployable alpha, toxic flow, external LP profitability, or net "
            "executable profit.",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _qa_payload(evidence: ShortHorizonEvidence) -> dict[str, JsonValue]:
    return {
        "schema_version": "1.0.0",
        "status": "pass",
        "parent_anchor_reconciled": evidence.parent_anchor_reconciled,
        "bootstrap": {
            "resamples": SHORT_HORIZON_BOOTSTRAP_RESAMPLES,
            "seed": SHORT_HORIZON_BOOTSTRAP_SEED,
            "confidence_level": SHORT_HORIZON_BOOTSTRAP_CONFIDENCE_LEVEL,
            "unit": "shock_utc_day",
        },
        "counts": [
            {
                "direction": count.direction,
                "detected_shock_count": count.detected_shock_count,
                "eligible_shock_count": count.eligible_shock_count,
                "exclusion_count": count.exclusion_count,
                "same_timestamp_shock_count": count.same_timestamp_shock_count,
            }
            for count in evidence.qa_counts
        ],
        "simultaneous_bands": [
            {
                "direction": inference.direction,
                "cohort": inference.cohort,
                "families": [
                    {
                        "metric": family.metric,
                        "status": family.status,
                        "reason": family.reason,
                        "valid_joint_resamples": family.valid_joint_resamples,
                    }
                    for family in inference.simultaneous_bands
                ],
            }
            for inference in evidence.inferences
        ],
        "semantic_gates": {
            "all_horizons_present": True,
            "counts_reconciled": True,
            "derived_fields_recomputed": True,
            "same_timestamp_sensitivity_present": True,
        },
    }


def _response_csv_row(row: ShortHorizonResponse) -> dict[str, str]:
    return {
        "outcome_type": "response",
        "direction": row.direction,
        "shock_timestamp_ms": str(row.shock_timestamp_ms),
        "shock_day_utc": row.shock_day_utc.isoformat(),
        "horizon_ms": str(row.horizon_ms),
        "source_pool": row.source_pool,
        "target_pool": row.target_pool,
        "source_move_bps": _float_text(row.source_move_bps),
        "source_move_sign": str(row.source_move_sign),
        "source_shock_raw_mid": _decimal_text(row.source_shock_raw_mid),
        "source_shock_bid": _decimal_text(row.source_shock_bid),
        "source_shock_ask": _decimal_text(row.source_shock_ask),
        "source_end_timestamp_ms": str(row.source_end_timestamp_ms),
        "source_end_age_ms": str(row.source_end_age_ms),
        "source_end_raw_mid": _decimal_text(row.source_end_raw_mid),
        "source_end_bid": _decimal_text(row.source_end_bid),
        "source_end_ask": _decimal_text(row.source_end_ask),
        "target_start_timestamp_ms": str(row.target_start_timestamp_ms),
        "target_start_age_ms": str(row.target_start_age_ms),
        "target_start_raw_mid": _decimal_text(row.target_start_raw_mid),
        "target_start_bid": _decimal_text(row.target_start_bid),
        "target_start_ask": _decimal_text(row.target_start_ask),
        "target_end_timestamp_ms": str(row.target_end_timestamp_ms),
        "target_end_age_ms": str(row.target_end_age_ms),
        "target_end_raw_mid": _decimal_text(row.target_end_raw_mid),
        "target_end_bid": _decimal_text(row.target_end_bid),
        "target_end_ask": _decimal_text(row.target_end_ask),
        "target_response_bps": _float_text(row.target_response_bps),
        "zero_response": _bool_text(row.zero_response),
        "direction_agrees": _bool_text(row.direction_agrees),
        "target_same_timestamp": _bool_text(row.target_same_timestamp),
        "first_update_status": row.first_update_status,
        "first_update_timestamp_ms": _optional_int_text(row.first_update_timestamp_ms),
        "first_update_delay_ms": _optional_int_text(row.first_update_delay_ms),
        "first_update_raw_mid": _optional_decimal_text(row.first_update_raw_mid),
        "first_update_response_bps": _optional_float_text(row.first_update_response_bps),
        "first_update_direction_agrees": _optional_bool_text(row.first_update_direction_agrees),
        "fee_gap_start_bps": _float_text(row.fee_gap_start_bps),
        "fee_gap_end_bps": _float_text(row.fee_gap_end_bps),
        "fee_gap_start_positive_bps": _float_text(row.fee_gap_start_positive_bps),
        "fee_gap_end_positive_bps": _float_text(row.fee_gap_end_positive_bps),
        "fee_gap_closure_bps": _float_text(row.fee_gap_closure_bps),
        "exclusion_reason": "",
    }


def _exclusion_csv_row(row: ShortHorizonExclusion) -> dict[str, str]:
    payload = {field: "" for field in _EVENT_FIELDS}
    payload.update(
        {
            "outcome_type": "exclusion",
            "direction": row.direction,
            "shock_timestamp_ms": str(row.shock_timestamp_ms),
            "exclusion_reason": row.reason,
        }
    )
    return payload


def _response_from_csv_row(
    row: Mapping[str, str],
    row_number: int,
    direction: Direction,
) -> ShortHorizonResponse:
    if row["exclusion_reason"]:
        raise CrossPoolContractError(f"event CSV row {row_number} mixes response and exclusion")
    return ShortHorizonResponse(
        direction=direction,
        source_pool=_parse_pool(row["source_pool"], row_number),
        target_pool=_parse_pool(row["target_pool"], row_number),
        shock_timestamp_ms=_parse_int(row["shock_timestamp_ms"], row_number),
        shock_day_utc=_parse_date(row["shock_day_utc"], row_number),
        horizon_ms=_parse_int(row["horizon_ms"], row_number),
        source_move_bps=_parse_float(row["source_move_bps"], row_number),
        source_move_sign=cast(
            Literal[-1, 1],
            _parse_int(row["source_move_sign"], row_number),
        ),
        source_shock_raw_mid=_parse_decimal(row["source_shock_raw_mid"], row_number),
        source_shock_bid=_parse_decimal(row["source_shock_bid"], row_number),
        source_shock_ask=_parse_decimal(row["source_shock_ask"], row_number),
        source_end_timestamp_ms=_parse_int(row["source_end_timestamp_ms"], row_number),
        source_end_age_ms=_parse_int(row["source_end_age_ms"], row_number),
        source_end_raw_mid=_parse_decimal(row["source_end_raw_mid"], row_number),
        source_end_bid=_parse_decimal(row["source_end_bid"], row_number),
        source_end_ask=_parse_decimal(row["source_end_ask"], row_number),
        target_start_timestamp_ms=_parse_int(row["target_start_timestamp_ms"], row_number),
        target_start_age_ms=_parse_int(row["target_start_age_ms"], row_number),
        target_start_raw_mid=_parse_decimal(row["target_start_raw_mid"], row_number),
        target_start_bid=_parse_decimal(row["target_start_bid"], row_number),
        target_start_ask=_parse_decimal(row["target_start_ask"], row_number),
        target_end_timestamp_ms=_parse_int(row["target_end_timestamp_ms"], row_number),
        target_end_age_ms=_parse_int(row["target_end_age_ms"], row_number),
        target_end_raw_mid=_parse_decimal(row["target_end_raw_mid"], row_number),
        target_end_bid=_parse_decimal(row["target_end_bid"], row_number),
        target_end_ask=_parse_decimal(row["target_end_ask"], row_number),
        target_response_bps=_parse_float(row["target_response_bps"], row_number),
        zero_response=_parse_bool(row["zero_response"], row_number),
        direction_agrees=_parse_bool(row["direction_agrees"], row_number),
        target_same_timestamp=_parse_bool(row["target_same_timestamp"], row_number),
        first_update_status=_parse_update_status(row["first_update_status"], row_number),
        first_update_timestamp_ms=_parse_optional_int(row["first_update_timestamp_ms"], row_number),
        first_update_delay_ms=_parse_optional_int(row["first_update_delay_ms"], row_number),
        first_update_raw_mid=_parse_optional_decimal(row["first_update_raw_mid"], row_number),
        first_update_response_bps=_parse_optional_float(
            row["first_update_response_bps"], row_number
        ),
        first_update_direction_agrees=_parse_optional_bool(
            row["first_update_direction_agrees"], row_number
        ),
        fee_gap_start_bps=_parse_float(row["fee_gap_start_bps"], row_number),
        fee_gap_end_bps=_parse_float(row["fee_gap_end_bps"], row_number),
        fee_gap_start_positive_bps=_parse_float(row["fee_gap_start_positive_bps"], row_number),
        fee_gap_end_positive_bps=_parse_float(row["fee_gap_end_positive_bps"], row_number),
        fee_gap_closure_bps=_parse_float(row["fee_gap_closure_bps"], row_number),
    )


def _exclusion_from_csv_row(
    row: Mapping[str, str],
    row_number: int,
    direction: Direction,
) -> ShortHorizonExclusion:
    allowed = {"outcome_type", "direction", "shock_timestamp_ms", "exclusion_reason"}
    if any(value for key, value in row.items() if key not in allowed):
        raise CrossPoolContractError(f"event CSV row {row_number} has exclusion-only values")
    return ShortHorizonExclusion(
        direction=direction,
        shock_timestamp_ms=_parse_int(row["shock_timestamp_ms"], row_number),
        reason=cast(
            Literal[
                "missing_target_start_state",
                "missing_target_end_state",
                "missing_source_end_state",
            ],
            row["exclusion_reason"],
        ),
    )


def _band_csv_fields(
    prefix: str,
    family: SimultaneousBandFamily,
    horizon_ms: int,
) -> dict[str, str]:
    base = {
        f"{prefix}_band_status": family.status,
        f"{prefix}_band_reason": family.reason or "",
        f"{prefix}_band_valid_joint_resamples": str(family.valid_joint_resamples),
        f"{prefix}_band_critical_value": _optional_float_text(family.critical_value),
        f"{prefix}_band_lower" + ("_bps" if prefix != "update_incidence" else ""): "",
        f"{prefix}_band_upper" + ("_bps" if prefix != "update_incidence" else ""): "",
        f"{prefix}_band_bootstrap_standard_deviation": "",
    }
    if family.status == "available":
        point = next(item for item in family.points if item.horizon_ms == horizon_ms)
        lower_key = f"{prefix}_band_lower" + ("_bps" if prefix != "update_incidence" else "")
        upper_key = f"{prefix}_band_upper" + ("_bps" if prefix != "update_incidence" else "")
        base[lower_key] = _float_text(point.lower)
        base[upper_key] = _float_text(point.upper)
        base[f"{prefix}_band_bootstrap_standard_deviation"] = _float_text(
            point.bootstrap_standard_deviation
        )
    return base


def _response_figure(inferences: Sequence[ShortHorizonInference]) -> bytes:
    primary = tuple(value for value in inferences if value.cohort == "primary")
    with rc_context(_FIGURE_RC):
        figure = Figure(figsize=(8.4, 3.5), constrained_layout=True)
        axes = figure.subplots(1, 2, squeeze=False)[0]
        for axis, inference in zip(axes, primary, strict=True):
            x = [value / 60_000 for value in SHORT_HORIZONS_MS]
            summaries = inference.summaries
            points = [item.unconditional_mean_response_bps.point for item in summaries]
            lowers = [item.unconditional_mean_response_bps.lower for item in summaries]
            uppers = [item.unconditional_mean_response_bps.upper for item in summaries]
            axis.plot(x, points, marker="o", color="#1f5a94", label="Mean response")
            axis.fill_between(x, lowers, uppers, color="#8fb9dd", alpha=0.45, label="Pointwise 95%")
            band = inference.simultaneous_bands[0]
            if band.status == "available":
                axis.fill_between(
                    x,
                    [point.lower for point in band.points],
                    [point.upper for point in band.points],
                    color="#e69f00",
                    alpha=0.22,
                    label="Max-z 95%",
                )
            else:
                axis.text(
                    0.02,
                    0.98,
                    f"Max-z unavailable: {band.reason}",
                    transform=axis.transAxes,
                    va="top",
                    fontsize=6.5,
                )
            axis.axhline(0.0, color="#555555", linewidth=0.7)
            axis.set_title(_direction_label(inference.direction))
            axis.set_xlabel("Horizon (minutes)")
            axis.set_ylabel("Response (bps)")
            axis.legend(loc="best")
        figure.suptitle("Unconditional cross-pool response profile")
        return _figure_png(figure)


def _updates_figure(inferences: Sequence[ShortHorizonInference]) -> bytes:
    primary = tuple(value for value in inferences if value.cohort == "primary")
    colors = ("#1f5a94", "#b44b39")
    with rc_context(_FIGURE_RC):
        figure = Figure(figsize=(8.4, 6.2), constrained_layout=True)
        axes = figure.subplots(2, 2, squeeze=False)
        for inference, color in zip(primary, colors, strict=True):
            x = [value / 60_000 for value in SHORT_HORIZONS_MS]
            label = _direction_label(inference.direction)
            summaries = inference.summaries
            axes[0][0].plot(
                x,
                [item.update_incidence for item in summaries],
                marker="o",
                color=color,
                label=label,
            )
            axes[0][1].plot(
                x,
                [
                    math.nan
                    if item.conditional_first_update_mean_response_bps is None
                    else item.conditional_first_update_mean_response_bps
                    for item in summaries
                ],
                marker="o",
                color=color,
                label=label,
            )
            axes[1][0].plot(
                x,
                [
                    math.nan
                    if item.conditional_first_update_median_delay_ms is None
                    else item.conditional_first_update_median_delay_ms / 1_000
                    for item in summaries
                ],
                marker="o",
                color=color,
                label=label,
            )
            axes[1][1].plot(
                x,
                [item.target_start_median_age_ms / 1_000 for item in summaries],
                linestyle="--",
                color=color,
                label=f"{label} start",
            )
            axes[1][1].plot(
                x,
                [item.target_end_median_age_ms / 1_000 for item in summaries],
                marker="o",
                color=color,
                label=f"{label} end",
            )
        titles = (
            ("Update incidence", "Incidence"),
            ("Conditional first-update response", "Response (bps)"),
            ("Observed-update median delay", "Seconds"),
            ("Median target-state staleness", "Seconds"),
        )
        for axis, (title, ylabel) in zip(axes.flat, titles, strict=True):
            axis.set_title(title)
            axis.set_xlabel("Horizon (minutes)")
            axis.set_ylabel(ylabel)
            axis.legend(loc="best")
        figure.suptitle(
            "Target update, latency, and staleness diagnostics "
            "(point estimates; inference in summaries CSV)"
        )
        return _figure_png(figure)


def _fee_gap_figure(inferences: Sequence[ShortHorizonInference]) -> bytes:
    primary = tuple(value for value in inferences if value.cohort == "primary")
    with rc_context(_FIGURE_RC):
        figure = Figure(figsize=(8.4, 3.5), constrained_layout=True)
        axes = figure.subplots(1, 2, squeeze=False)[0]
        for axis, inference in zip(axes, primary, strict=True):
            x = [value / 60_000 for value in SHORT_HORIZONS_MS]
            summaries = inference.summaries
            axis.plot(
                x,
                [item.mean_fee_gap_start_bps for item in summaries],
                linestyle="--",
                color="#666666",
                label="At shock",
            )
            axis.plot(
                x,
                [item.mean_fee_gap_end_bps for item in summaries],
                marker="o",
                color="#1f5a94",
                label="At horizon",
            )
            axis.plot(
                x,
                [item.mean_fee_gap_closure_bps for item in summaries],
                marker="s",
                color="#b44b39",
                label="Closure",
            )
            axis.axhline(0.0, color="#555555", linewidth=0.7)
            axis.set_title(_direction_label(inference.direction))
            axis.set_xlabel("Horizon (minutes)")
            axis.set_ylabel("Fee-only gap (bps)")
            axis.legend(loc="best")
        figure.suptitle("Fee-only cross-venue gap (not net executable profit)")
        return _figure_png(figure)


def _figure_png(figure: Figure) -> bytes:
    buffer = io.BytesIO()
    FigureCanvasAgg(figure).print_figure(
        buffer,
        format="png",
        metadata={"Software": "automated-infra short-horizon v1"},
    )
    return buffer.getvalue()


def _csv_bytes(fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=tuple(fieldnames),
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        if set(row) != set(fieldnames):
            raise CrossPoolContractError("CSV row fields do not match the frozen header")
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _parse_direction(value: str, row_number: int) -> Direction:
    if value not in _DIRECTION_ORDER:
        raise CrossPoolContractError(f"event CSV row {row_number} has invalid direction")
    return value


def _parse_pool(value: str, row_number: int) -> PoolName:
    if value not in ("uni-base", "uni-bsc"):
        raise CrossPoolContractError(f"event CSV row {row_number} has invalid pool")
    return cast(PoolName, value)


def _parse_update_status(value: str, row_number: int) -> FirstUpdateStatus:
    if value not in ("observed", "right_censored"):
        raise CrossPoolContractError(f"event CSV row {row_number} has invalid update status")
    return cast(FirstUpdateStatus, value)


def _parse_int(value: str, row_number: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CrossPoolContractError(f"event CSV row {row_number} has invalid integer") from exc
    if str(parsed) != value:
        raise CrossPoolContractError(f"event CSV row {row_number} has noncanonical integer")
    return parsed


def _parse_float(value: str, row_number: int) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise CrossPoolContractError(f"event CSV row {row_number} has invalid float") from exc
    if not math.isfinite(parsed) or _float_text(parsed) != value:
        raise CrossPoolContractError(f"event CSV row {row_number} has noncanonical float")
    return parsed


def _parse_decimal(value: str, row_number: int) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CrossPoolContractError(f"event CSV row {row_number} has invalid decimal") from exc
    if not parsed.is_finite() or _decimal_text(parsed) != value:
        raise CrossPoolContractError(f"event CSV row {row_number} has noncanonical decimal")
    return parsed


def _parse_date(value: str, row_number: int) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CrossPoolContractError(f"event CSV row {row_number} has invalid UTC date") from exc
    if parsed.isoformat() != value:
        raise CrossPoolContractError(f"event CSV row {row_number} has noncanonical UTC date")
    return parsed


def _parse_bool(value: str, row_number: int) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise CrossPoolContractError(f"event CSV row {row_number} has invalid boolean")


def _parse_optional_int(value: str, row_number: int) -> int | None:
    return None if value == "" else _parse_int(value, row_number)


def _parse_optional_float(value: str, row_number: int) -> float | None:
    return None if value == "" else _parse_float(value, row_number)


def _parse_optional_decimal(value: str, row_number: int) -> Decimal | None:
    return None if value == "" else _parse_decimal(value, row_number)


def _parse_optional_bool(value: str, row_number: int) -> bool | None:
    return None if value == "" else _parse_bool(value, row_number)


def _float_text(value: float) -> str:
    if not math.isfinite(value):
        raise CrossPoolContractError("reported floats must be finite")
    return "0" if value == 0.0 else repr(value)


def _optional_float_text(value: float | None) -> str:
    return "" if value is None else _float_text(value)


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise CrossPoolContractError("reported decimals must be finite")
    return "0" if value == 0 else format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str:
    return "" if value is None else _decimal_text(value)


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _optional_bool_text(value: bool | None) -> str:
    return "" if value is None else _bool_text(value)


def _optional_int_text(value: int | None) -> str:
    return "" if value is None else str(value)


def _direction_label(direction: Direction) -> str:
    return "BSC to Base" if direction == "bsc_to_base" else "Base to BSC"


def _horizon_label(horizon_ms: int) -> str:
    seconds = horizon_ms // 1_000
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m"


def _display_optional(value: float | None) -> str:
    return "unavailable" if value is None else f"{value / 1_000:.3f}s"
