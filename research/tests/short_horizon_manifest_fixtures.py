from __future__ import annotations

import math
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from functools import lru_cache

from research.cross_pool.contracts import Direction
from research.cross_pool.short_horizon import (
    SHORT_HORIZONS_MS,
    ShortHorizonResponse,
    ShortHorizonStudy,
)
from research.cross_pool.short_horizon_artifacts import ShortHorizonArtifact
from research.cross_pool.short_horizon_inference import (
    PointwiseInterval,
    ShortHorizonInference,
    ShortHorizonSummary,
    SimultaneousBandFamily,
    SimultaneousBandPoint,
    infer_short_horizon,
)
from research.cross_pool.short_horizon_manifest import (
    BASE_FEATURES_SHA256,
    BSC_FEATURES_SHA256,
    GIRUM_NOTE_SHA256,
    PARENT_MANIFEST_SHA256,
    SHORT_HORIZON_SOURCE_DEPENDENCIES,
    DirectionQaCounts,
    GeneratedManifestInput,
    InputIdentity,
    ShortHorizonProvenance,
    SourceDependencyIdentity,
    build_generated_short_horizon_manifest,
)
from research.cross_pool.short_horizon_reporting import (
    ShortHorizonEvidence,
    render_short_horizon_artifacts,
)

ARTIFACT_NAMES = (
    "short_horizon_events.csv",
    "short_horizon_summaries.csv",
    "short_horizon_qa.json",
    "short_horizon_report.md",
    "short_horizon_response.png",
    "short_horizon_updates.png",
    "short_horizon_fee_gap.png",
)


def generated_manifest() -> dict[str, object]:
    return build_generated_short_horizon_manifest(generated_manifest_input())


def generated_manifest_input() -> GeneratedManifestInput:
    return GeneratedManifestInput(
        provenance=manifest_provenance(),
        qa_counts=(
            DirectionQaCounts(
                direction="bsc_to_base",
                detected_shock_count=123,
                eligible_shock_count=123,
                exclusion_count=0,
                same_timestamp_shock_count=0,
            ),
            DirectionQaCounts(
                direction="base_to_bsc",
                detected_shock_count=197,
                eligible_shock_count=197,
                exclusion_count=0,
                same_timestamp_shock_count=0,
            ),
        ),
        inferences=inference_profiles(),
        artifacts=rendered_artifacts(),
        parent_anchor_reconciled=True,
    )


def manifest_provenance() -> ShortHorizonProvenance:
    return ShortHorizonProvenance(
        parent_manifest=InputIdentity(
            sha256=PARENT_MANIFEST_SHA256,
            byte_count=101,
        ),
        base_features=InputIdentity(
            sha256=BASE_FEATURES_SHA256,
            byte_count=102,
        ),
        bsc_features=InputIdentity(
            sha256=BSC_FEATURES_SHA256,
            byte_count=103,
        ),
        girum_note=InputIdentity(
            sha256=GIRUM_NOTE_SHA256,
            byte_count=104,
        ),
        code_commit="a" * 40,
        source_dependencies=tuple(
            SourceDependencyIdentity(
                relative_path=relative_path,
                sha256=f"{index:064x}",
            )
            for index, relative_path in enumerate(
                SHORT_HORIZON_SOURCE_DEPENDENCIES,
                start=1,
            )
        ),
        runtime=(
            ("numpy", "2.3.1"),
            ("python", "3.12.10"),
        ),
    )


def rendered_artifacts() -> tuple[ShortHorizonArtifact, ...]:
    return tuple(
        ShortHorizonArtifact(relative_name=name, content=f"fixture:{name}\n".encode())
        for name in ARTIFACT_NAMES
    )


def sealed_manifest() -> dict[str, object]:
    manifest, _ = _sealed_package()
    return deepcopy(manifest)


def sealed_artifacts() -> tuple[ShortHorizonArtifact, ...]:
    _, artifacts = _sealed_package()
    return artifacts


@lru_cache(maxsize=1)
def _sealed_package() -> tuple[dict[str, object], tuple[ShortHorizonArtifact, ...]]:
    studies = (
        _anchored_study("bsc_to_base", event_count=123, day_count=59),
        _anchored_study("base_to_bsc", event_count=197, day_count=70),
    )
    inferences = tuple(
        infer_short_horizon(study, exclude_same_timestamp=exclude)
        for study in studies
        for exclude in (False, True)
    )
    evidence = ShortHorizonEvidence(
        studies=studies,
        inferences=inferences,
        parent_anchor_reconciled=True,
    )
    artifacts = render_short_horizon_artifacts(evidence)
    manifest = build_generated_short_horizon_manifest(
        GeneratedManifestInput(
            provenance=manifest_provenance(),
            qa_counts=evidence.qa_counts,
            inferences=inferences,
            artifacts=artifacts,
            parent_anchor_reconciled=True,
        )
    )
    return manifest, artifacts


def _anchored_study(
    direction: Direction,
    *,
    event_count: int,
    day_count: int,
) -> ShortHorizonStudy:
    anchor_response_bps = (
        Decimal("0.8782298255072702")
        if direction == "bsc_to_base"
        else Decimal("0.03955408492685122")
    )
    source_pool = "uni-bsc" if direction == "bsc_to_base" else "uni-base"
    target_pool = "uni-base" if direction == "bsc_to_base" else "uni-bsc"
    source_fee_multiplier = Decimal("0.9988") if source_pool == "uni-bsc" else Decimal("0.9985")
    target_fee_multiplier = Decimal("0.9988") if target_pool == "uni-bsc" else Decimal("0.9985")
    source_mid = Decimal(1)
    target_start_mid = Decimal(1)
    source_bid = source_mid * source_fee_multiplier
    source_ask = source_mid / source_fee_multiplier
    target_start_bid = target_start_mid * target_fee_multiplier
    target_start_ask = target_start_mid / target_fee_multiplier
    start_gap = 10_000 * math.log(float(source_bid / target_start_ask))
    epoch_ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1_000)
    rows: list[ShortHorizonResponse] = []
    for index in range(event_count):
        response_bps = (
            anchor_response_bps
            if index == event_count - 1
            else anchor_response_bps + (Decimal("0.01") if index % 2 == 0 else Decimal("-0.01"))
        )
        target_end_mid = Decimal(1) + response_bps / Decimal(10_000)
        target_end_bid = target_end_mid * target_fee_multiplier
        target_end_ask = target_end_mid / target_fee_multiplier
        end_gap = 10_000 * math.log(float(source_bid / target_end_ask))
        with localcontext() as context:
            context.prec = 80
            response_float = float(
                (target_end_mid / target_start_mid - Decimal(1)) * Decimal(10_000)
            )
        day_index = index % day_count
        intraday_slot = index // day_count + 1
        shock_timestamp_ms = epoch_ms + day_index * 86_400_000 + intraday_slot * 3_600_000
        shock_day = datetime.fromtimestamp(
            shock_timestamp_ms / 1_000,
            tz=UTC,
        ).date()
        for horizon_ms in SHORT_HORIZONS_MS:
            first_update_delay_ms = 1_000
            rows.append(
                ShortHorizonResponse(
                    direction=direction,
                    source_pool=source_pool,
                    target_pool=target_pool,
                    shock_timestamp_ms=shock_timestamp_ms,
                    shock_day_utc=shock_day,
                    horizon_ms=horizon_ms,
                    source_move_bps=10.0,
                    source_move_sign=1,
                    source_shock_raw_mid=source_mid,
                    source_shock_bid=source_bid,
                    source_shock_ask=source_ask,
                    source_end_timestamp_ms=shock_timestamp_ms + horizon_ms,
                    source_end_age_ms=0,
                    source_end_raw_mid=source_mid,
                    source_end_bid=source_bid,
                    source_end_ask=source_ask,
                    target_start_timestamp_ms=shock_timestamp_ms - 1_000,
                    target_start_age_ms=1_000,
                    target_start_raw_mid=target_start_mid,
                    target_start_bid=target_start_bid,
                    target_start_ask=target_start_ask,
                    target_end_timestamp_ms=shock_timestamp_ms + horizon_ms,
                    target_end_age_ms=0,
                    target_end_raw_mid=target_end_mid,
                    target_end_bid=target_end_bid,
                    target_end_ask=target_end_ask,
                    target_response_bps=response_float,
                    zero_response=False,
                    direction_agrees=True,
                    target_same_timestamp=False,
                    first_update_status="observed",
                    first_update_timestamp_ms=(shock_timestamp_ms + first_update_delay_ms),
                    first_update_delay_ms=first_update_delay_ms,
                    first_update_raw_mid=target_end_mid,
                    first_update_response_bps=response_float,
                    first_update_direction_agrees=True,
                    fee_gap_start_bps=start_gap,
                    fee_gap_end_bps=end_gap,
                    fee_gap_start_positive_bps=max(start_gap, 0.0),
                    fee_gap_end_positive_bps=max(end_gap, 0.0),
                    fee_gap_closure_bps=start_gap - end_gap,
                )
            )
    ordered = tuple(sorted(rows, key=lambda row: (row.shock_timestamp_ms, row.horizon_ms)))
    return ShortHorizonStudy(
        direction=direction,
        detected_shock_count=event_count,
        eligible_shock_count=event_count,
        responses=ordered,
        exclusions=(),
    )


def inference_profiles() -> tuple[ShortHorizonInference, ...]:
    return tuple(
        _inference(direction, cohort)
        for direction in ("bsc_to_base", "base_to_bsc")
        for cohort in ("primary", "exclude_same_timestamp")
    )


def _inference(direction: str, cohort: str) -> ShortHorizonInference:
    typed_direction = "bsc_to_base" if direction == "bsc_to_base" else "base_to_bsc"
    typed_cohort = "primary" if cohort == "primary" else "exclude_same_timestamp"
    event_count = 123 if typed_direction == "bsc_to_base" else 197
    day_count = 59 if typed_direction == "bsc_to_base" else 70
    summaries = tuple(
        _summary(
            typed_direction,
            horizon_ms,
            event_count=event_count,
            day_count=day_count,
        )
        for horizon_ms in SHORT_HORIZONS_MS
    )
    bands = (
        _available_band(
            "unconditional_mean_response_bps",
            tuple(summary.unconditional_mean_response_bps.point for summary in summaries),
        ),
        _available_band(
            "update_incidence",
            tuple(summary.update_incidence for summary in summaries),
            probability=True,
        ),
        (
            _available_band(
                "conditional_first_update_mean_response_bps",
                tuple(
                    float(summary.conditional_first_update_mean_response_bps or 0.0)
                    for summary in summaries
                ),
            )
            if typed_direction == "bsc_to_base"
            else SimultaneousBandFamily(
                metric="conditional_first_update_mean_response_bps",
                status="unavailable",
                reason="insufficient_observed_update_days",
                valid_joint_resamples=2_000,
                critical_value=None,
                points=(),
            )
        ),
    )
    return ShortHorizonInference(
        direction=typed_direction,
        cohort=typed_cohort,
        eligible_event_count=event_count,
        shock_day_count=day_count,
        resamples=2_000,
        seed=20_260_715,
        confidence_level=0.95,
        summaries=summaries,
        simultaneous_bands=bands,
    )


def _summary(
    direction: str,
    horizon_ms: int,
    *,
    event_count: int,
    day_count: int,
) -> ShortHorizonSummary:
    if horizon_ms == 900_000:
        point = 0.8782298255072702 if direction == "bsc_to_base" else 0.03955408492685122
    else:
        point = horizon_ms / 1_000_000
    update_count = min(20, event_count)
    return ShortHorizonSummary(
        direction="bsc_to_base" if direction == "bsc_to_base" else "base_to_bsc",
        horizon_ms=horizon_ms,
        eligible_event_count=event_count,
        shock_day_count=day_count,
        unconditional_mean_response_bps=PointwiseInterval(
            point=point,
            lower=point - 1.0,
            upper=point + 1.0,
        ),
        unconditional_median_response_bps=PointwiseInterval(
            point=point / 2.0,
            lower=point / 2.0 - 1.0,
            upper=point / 2.0 + 1.0,
        ),
        direction_agreement_share=0.5,
        zero_response_share=0.25,
        update_count=update_count,
        right_censored_count=event_count - update_count,
        update_incidence=update_count / event_count,
        observed_update_day_count=min(12, day_count, update_count),
        conditional_first_update_mean_response_bps=point,
        conditional_first_update_median_response_bps=point / 2.0,
        conditional_first_update_median_delay_ms=10_000.0,
        conditional_first_update_p95_delay_ms=20_000.0,
        target_start_median_age_ms=1_000.0,
        target_start_p95_age_ms=2_000.0,
        target_end_median_age_ms=3_000.0,
        target_end_p95_age_ms=4_000.0,
        mean_fee_gap_start_bps=1.0,
        mean_fee_gap_end_bps=0.5,
        positive_fee_gap_start_share=0.6,
        positive_fee_gap_end_share=0.4,
        mean_fee_gap_closure_bps=0.5,
    )


def _available_band(
    metric: str,
    points: tuple[float, ...],
    *,
    probability: bool = False,
) -> SimultaneousBandFamily:
    typed_metric = (
        "unconditional_mean_response_bps"
        if metric == "unconditional_mean_response_bps"
        else (
            "update_incidence"
            if metric == "update_incidence"
            else "conditional_first_update_mean_response_bps"
        )
    )
    return SimultaneousBandFamily(
        metric=typed_metric,
        status="available",
        reason=None,
        valid_joint_resamples=2_000,
        critical_value=2.0,
        points=tuple(
            SimultaneousBandPoint(
                horizon_ms=horizon_ms,
                point=point,
                lower=max(0.0, point - 0.1) if probability else point - 0.1,
                upper=min(1.0, point + 0.1) if probability else point + 0.1,
                bootstrap_standard_deviation=0.05,
            )
            for horizon_ms, point in zip(SHORT_HORIZONS_MS, points, strict=True)
        ),
    )
