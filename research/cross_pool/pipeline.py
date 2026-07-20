"""Typed orchestration for the frozen cross-pool statistical analysis."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from research.cross_pool.bootstrap import infer_predictions
from research.cross_pool.contracts import (
    CausalPanel,
    CrossPoolContractError,
    Direction,
    DirectionalPredictiveAudit,
    DtwConfig,
    DtwNullResult,
    DtwStability,
    DtwWeekResult,
    EventExclusion,
    EventResponse,
    EventStudyResult,
    EventSummary,
    PanelConfig,
    PoolEvent,
    PredictiveInference,
    ShockConfig,
    StreamQuality,
    WalkForwardConfig,
    WalkForwardResult,
)
from research.cross_pool.dtw import (
    assess_dtw_stability,
    build_day_rotation_nulls,
    evaluate_weekly_dtw,
)
from research.cross_pool.event_study import (
    bootstrap_event_responses,
    detect_shocks,
    measure_event_responses,
)
from research.cross_pool.market_structure import MarketStructureAnalysis
from research.cross_pool.panel import build_causal_panel
from research.cross_pool.predictive import expanding_weekly_predictions
from research.cross_pool.qa import audit_predictions, validate_stream

_PANEL_HORIZONS_MS = (900_000, 3_600_000, 14_400_000)
_BASE_TRANSITION_BLOCK = 45_848_255
_BSC_TRANSITION_BLOCK = 97_799_490


@dataclass(frozen=True)
class DirectionalAnalysis:
    direction: Direction
    walk_forward_15m: WalkForwardResult
    walk_forward_1h: WalkForwardResult
    walk_forward_4h: WalkForwardResult
    inference_15m: PredictiveInference
    audit_1h: DirectionalPredictiveAudit
    inference_4h: PredictiveInference
    event_study: EventStudyResult
    event_summaries: tuple[EventSummary, ...]
    dtw_weeks: tuple[DtwWeekResult, ...]
    dtw_nulls: tuple[DtwNullResult, ...]
    dtw_stability: DtwStability

    def __post_init__(self) -> None:
        if not isinstance(self.audit_1h, DirectionalPredictiveAudit):
            raise CrossPoolContractError(
                "directional analysis requires a one-hour predictive audit"
            )
        expected = (
            (900_000, self.walk_forward_15m, self.inference_15m),
            (3_600_000, self.walk_forward_1h, self.audit_1h.inference),
            (14_400_000, self.walk_forward_4h, self.inference_4h),
        )
        for horizon_ms, walk_forward, inference in expected:
            predictions = walk_forward.predictions
            if not predictions or any(
                row.direction != self.direction or row.horizon_ms != horizon_ms
                for row in predictions
            ):
                raise CrossPoolContractError(
                    "directional walk-forward results must match direction and horizon"
                )
            if (
                inference.metrics.direction != self.direction
                or inference.metrics.horizon_ms != horizon_ms
                or inference.metrics.rows != len(predictions)
            ):
                raise CrossPoolContractError(
                    "directional inference must match its walk-forward result"
                )
        if self.audit_1h.direction != self.direction:
            raise CrossPoolContractError(
                "one-hour predictive audit direction is inconsistent"
            )

        event_rows: tuple[EventResponse | EventExclusion, ...] = (
            self.event_study.responses + self.event_study.exclusions
        )
        if not event_rows or any(row.direction != self.direction for row in event_rows):
            raise CrossPoolContractError(
                "directional event study requires outcomes for one direction"
            )
        event_horizons: dict[int, set[int]] = {}
        for row in event_rows:
            event_horizons.setdefault(row.shock_timestamp_ms, set()).add(
                row.horizon_ms
            )
        if any(
            horizons != {900_000, 3_600_000, 14_400_000}
            for horizons in event_horizons.values()
        ):
            raise CrossPoolContractError(
                "each shock requires one event outcome per frozen horizon"
            )
        summary_keys = tuple(
            (row.direction, row.horizon_ms) for row in self.event_summaries
        )
        if summary_keys != tuple(
            (self.direction, horizon_ms)
            for horizon_ms in (900_000, 3_600_000, 14_400_000)
        ):
            raise CrossPoolContractError(
                "directional event summaries require every frozen horizon"
            )
        for summary in self.event_summaries:
            responses = tuple(
                row
                for row in self.event_study.responses
                if row.horizon_ms == summary.horizon_ms
            )
            if (
                summary.event_count != len(responses)
                or summary.event_day_count
                != len({row.shock_day_utc for row in responses})
            ):
                raise CrossPoolContractError(
                    "event summary support must match measured responses"
                )

        if (
            not self.dtw_weeks
            or not self.dtw_nulls
            or any(row.direction != self.direction for row in self.dtw_weeks)
            or any(row.direction != self.direction for row in self.dtw_nulls)
            or self.dtw_stability.direction != self.direction
        ):
            raise CrossPoolContractError(
                "directional DTW results must be nonempty and direction-consistent"
            )
        weekly_keys = tuple(
            (row.week_start_timestamp_ms, row.band_steps) for row in self.dtw_weeks
        )
        expected_weekly_keys = tuple(
            (week_start, band_steps)
            for week_start in sorted({key[0] for key in weekly_keys})
            for band_steps in (1, 4, 16)
        )
        if weekly_keys != expected_weekly_keys:
            raise CrossPoolContractError(
                "directional DTW weeks require the complete frozen band matrix"
            )
        null_keys = tuple(
            (row.week_start_timestamp_ms, row.band_steps, row.rotation_days)
            for row in self.dtw_nulls
        )
        expected_null_keys = tuple(
            (week_start, band_steps, rotation_days)
            for week_start, band_steps in expected_weekly_keys
            for rotation_days in range(1, 7)
        )
        if null_keys != expected_null_keys:
            raise CrossPoolContractError(
                "directional DTW nulls require six rotations per week and band"
            )
        for band_steps in (1, 4, 16):
            stability_weeks = tuple(
                week_start
                for week_start, _ in self.dtw_stability.weekly_median_lags_by_band[
                    band_steps
                ]
            )
            result_weeks = tuple(
                row.week_start_timestamp_ms
                for row in self.dtw_weeks
                if row.band_steps == band_steps
            )
            if stability_weeks != result_weeks:
                raise CrossPoolContractError(
                    "DTW stability support must match weekly path results"
                )


@dataclass(frozen=True)
class StatisticalAnalysis:
    base_quality: StreamQuality
    bsc_quality: StreamQuality
    panel_15m: CausalPanel
    panel_1h: CausalPanel
    panel_4h: CausalPanel
    primary: DirectionalAnalysis
    reverse: DirectionalAnalysis
    market_structure: MarketStructureAnalysis

    def __post_init__(self) -> None:
        if (self.base_quality.pool, self.bsc_quality.pool) != (
            "uni-base",
            "uni-bsc",
        ):
            raise CrossPoolContractError(
                "statistical analysis requires ordered Base and BSC stream quality"
            )
        for panel, horizon_ms in (
            (self.panel_15m, 900_000),
            (self.panel_1h, 3_600_000),
            (self.panel_4h, 14_400_000),
        ):
            if panel.horizon_ms != horizon_ms or not panel.rows:
                raise CrossPoolContractError(
                    "statistical analysis requires every nonempty frozen panel"
                )
        intervals = {
            (panel.common_interval_start_ms, panel.common_interval_end_ms)
            for panel in (self.panel_15m, self.panel_1h, self.panel_4h)
        }
        if len(intervals) != 1:
            raise CrossPoolContractError(
                "frozen causal panels must share one raw common interval"
            )
        if (self.primary.direction, self.reverse.direction) != (
            "bsc_to_base",
            "base_to_bsc",
        ):
            raise CrossPoolContractError(
                "statistical analysis requires ordered primary and reverse directions"
            )


def analyze_cross_pool(
    base_events: Sequence[PoolEvent],
    bsc_events: Sequence[PoolEvent],
    market_structure: MarketStructureAnalysis,
) -> StatisticalAnalysis:
    """Execute the frozen estimators after input and coverage validation."""
    base = tuple(base_events)
    bsc = tuple(bsc_events)
    base_quality = validate_stream(base, transition_block=_BASE_TRANSITION_BLOCK)
    bsc_quality = validate_stream(bsc, transition_block=_BSC_TRANSITION_BLOCK)
    panels = (
        _build_panel(base, bsc, _PANEL_HORIZONS_MS[0]),
        _build_panel(base, bsc, _PANEL_HORIZONS_MS[1]),
        _build_panel(base, bsc, _PANEL_HORIZONS_MS[2]),
    )
    panel_15m, panel_1h, panel_4h = panels
    return StatisticalAnalysis(
        base_quality=base_quality,
        bsc_quality=bsc_quality,
        panel_15m=panel_15m,
        panel_1h=panel_1h,
        panel_4h=panel_4h,
        primary=_analyze_direction(
            direction="bsc_to_base",
            panels=panels,
            source_events=bsc,
            target_events=base,
        ),
        reverse=_analyze_direction(
            direction="base_to_bsc",
            panels=panels,
            source_events=base,
            target_events=bsc,
        ),
        market_structure=market_structure,
    )


def _analyze_direction(
    *,
    direction: Direction,
    panels: tuple[CausalPanel, CausalPanel, CausalPanel],
    source_events: tuple[PoolEvent, ...],
    target_events: tuple[PoolEvent, ...],
) -> DirectionalAnalysis:
    panel_15m, panel_1h, panel_4h = panels
    walks = tuple(
        expanding_weekly_predictions(
            panel,
            WalkForwardConfig(direction=direction),
        )
        for panel in (panel_15m, panel_1h, panel_4h)
    )
    walk_15m, walk_1h, walk_4h = walks

    shock_config = ShockConfig()
    shocks = detect_shocks(source_events, shock_config)
    event_study = measure_event_responses(shocks, target_events, shock_config)
    event_summaries = bootstrap_event_responses(event_study.responses)

    dtw_config = DtwConfig()
    dtw_weeks = evaluate_weekly_dtw(
        panel_15m.rows,
        dtw_config,
        direction=direction,
    )
    dtw_nulls = build_day_rotation_nulls(
        panel_15m.rows,
        dtw_config,
        direction=direction,
    )
    return DirectionalAnalysis(
        direction=direction,
        walk_forward_15m=walk_15m,
        walk_forward_1h=walk_1h,
        walk_forward_4h=walk_4h,
        inference_15m=infer_predictions(walk_15m.predictions),
        audit_1h=audit_predictions(walk_1h.predictions),
        inference_4h=infer_predictions(walk_4h.predictions),
        event_study=event_study,
        event_summaries=event_summaries,
        dtw_weeks=dtw_weeks,
        dtw_nulls=dtw_nulls,
        dtw_stability=assess_dtw_stability(dtw_weeks, dtw_config),
    )


def _build_panel(
    base: tuple[PoolEvent, ...],
    bsc: tuple[PoolEvent, ...],
    horizon_ms: int,
) -> CausalPanel:
    return build_causal_panel(
        base,
        bsc,
        PanelConfig(
            horizon_ms=horizon_ms,
            base_transition_block=_BASE_TRANSITION_BLOCK,
            bsc_transition_block=_BSC_TRANSITION_BLOCK,
        ),
    )
