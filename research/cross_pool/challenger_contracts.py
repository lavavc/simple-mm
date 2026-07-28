"""Immutable contracts for the post-hoc cross-pool challenger study."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, TypeAlias

from research.cross_pool.contracts import Direction, PanelRow, Regime

ModelFamily: TypeAlias = Literal["ols", "arx2", "gam", "huber", "two_part"]
FeatureVariant: TypeAlias = Literal["target_only", "source_age", "full_source"]
FreshnessSupport: TypeAlias = Literal["all", "both_age_le_4h", "both_age_le_1h"]
MetricEndpoint: TypeAlias = Literal[
    "mae",
    "mse",
    "brier",
    "conditional_update_mae",
]
MetricStatus: TypeAlias = Literal["available", "model_fit_failed", "no_rows"]
ContrastKind: TypeAlias = Literal["source_age", "source_price", "total_source"]
ContrastStatus: TypeAlias = Literal["adjudicable", "not_adjudicable"]

MODEL_FAMILIES: tuple[ModelFamily, ...] = ("ols", "arx2", "gam", "huber", "two_part")
FEATURE_VARIANTS: tuple[FeatureVariant, ...] = (
    "target_only",
    "source_age",
    "full_source",
)
FRESHNESS_SUPPORTS: tuple[FreshnessSupport, ...] = (
    "all",
    "both_age_le_4h",
    "both_age_le_1h",
)
_MODEL_ORDER = {value: index for index, value in enumerate(MODEL_FAMILIES)}
_VARIANT_ORDER = {value: index for index, value in enumerate(FEATURE_VARIANTS)}
_DIRECTION_ORDER = {"bsc_to_base": 0, "base_to_bsc": 1}


@dataclass(frozen=True)
class ParentAnchor:
    artifact_sha256: tuple[tuple[str, str], ...]
    article_branch: str
    review_status: str


@dataclass(frozen=True)
class ParentPanel:
    horizon_ms: int
    rows: tuple[PanelRow, ...]


@dataclass(frozen=True)
class ParentPrediction:
    timestamp_ms: int
    target_timestamp_ms: int
    horizon_ms: int
    refit_timestamp_ms: int
    fold_index: int
    direction: Direction
    actual_bps: float
    baseline_prediction_bps: float
    cross_prediction_bps: float


@dataclass(frozen=True)
class ProjectedParentRow:
    timestamp_ms: int
    target_timestamp_ms: int
    horizon_ms: int
    direction: Direction
    target_return_bps: float
    previous_target_return_bps: float | None
    source_return_bps: float
    gap_bps: float
    target_age_ms: int
    source_age_ms: int
    actual_bps: float
    updated: bool
    target_regime: Regime
    source_regime: Regime


@dataclass(frozen=True)
class ChallengerParent:
    anchor: ParentAnchor
    panels: tuple[ParentPanel, ...]
    predictions: tuple[ParentPrediction, ...]
    target_days_utc: tuple[date, ...]


@dataclass(frozen=True)
class ChallengerPrediction:
    timestamp_ms: int
    target_timestamp_ms: int
    horizon_ms: int
    refit_timestamp_ms: int
    fold_index: int
    direction: Direction
    family: ModelFamily
    variant: FeatureVariant
    target_regime: Regime
    source_regime: Regime
    target_age_ms: int
    source_age_ms: int
    actual_bps: float
    updated: bool
    prediction_bps: float
    update_probability: float | None
    conditional_prediction_bps: float | None

    @property
    def sort_key(self) -> tuple[int, int, int, int, int]:
        return (
            _MODEL_ORDER[self.family],
            _VARIANT_ORDER[self.variant],
            _DIRECTION_ORDER[self.direction],
            self.horizon_ms,
            self.timestamp_ms,
        )


@dataclass(frozen=True)
class ChallengerFoldAudit:
    family: ModelFamily
    variant: FeatureVariant
    direction: Direction
    horizon_ms: int
    fold_index: int
    refit_timestamp_ms: int
    max_training_target_timestamp_ms: int
    training_rows: int
    training_update_rows: int
    condition_number: float
    iterations: int
    robust_scale: float | None
    downweighted_fraction: float | None
    logistic_condition_number: float | None
    logistic_iterations: int | None
    spline_knots: tuple[tuple[float, float], ...]
    spline_extrapolation_count: int


@dataclass(frozen=True)
class ChallengerFitFailure:
    family: ModelFamily
    variant: FeatureVariant
    direction: Direction
    horizon_ms: int
    fold_index: int
    refit_timestamp_ms: int
    reason: str


@dataclass(frozen=True)
class ChallengerStudy:
    predictions: tuple[ChallengerPrediction, ...]
    fold_audits: tuple[ChallengerFoldAudit, ...]
    failures: tuple[ChallengerFitFailure, ...]
    target_days_utc: tuple[date, ...]


@dataclass(frozen=True)
class ChallengerMetric:
    family: ModelFamily
    variant: FeatureVariant
    direction: Direction
    horizon_ms: int
    support: FreshnessSupport
    status: MetricStatus
    reason: str | None
    rows: int
    target_days: int
    update_rows: int
    update_days: int
    update_incidence: float | None
    mae_bps: float | None
    mse_bps2: float | None
    rmse_bps: float | None
    directional_rows: int
    directional_accuracy: float | None
    brier_loss: float | None
    log_loss: float | None
    calibration_error: float | None
    conditional_update_mae_bps: float | None


@dataclass(frozen=True)
class ChallengerContrast:
    family: ModelFamily
    direction: Direction
    horizon_ms: int
    support: FreshnessSupport
    endpoint: MetricEndpoint
    contrast: ContrastKind
    comparator_variant: FeatureVariant
    candidate_variant: FeatureVariant
    status: ContrastStatus
    reason: str | None
    rows: int
    target_days: int
    complete_blocks: int
    update_days: int
    point: float | None
    raw_lower: float | None
    raw_upper: float | None
    bootstrap_standard_error: float | None
    simultaneous_lower: float | None
    simultaneous_upper: float | None
    adjusted_p_value: float | None


@dataclass(frozen=True)
class ReliabilityGroup:
    family: Literal["two_part"]
    variant: FeatureVariant
    direction: Direction
    horizon_ms: int
    support: FreshnessSupport
    group_index: int
    rows: int
    mean_probability: float
    observed_incidence: float

    @property
    def sort_key(self) -> tuple[int, int, int, int, int]:
        return (
            _VARIANT_ORDER[self.variant],
            _DIRECTION_ORDER[self.direction],
            self.horizon_ms,
            FRESHNESS_SUPPORTS.index(self.support),
            self.group_index,
        )


@dataclass(frozen=True)
class ChallengerInference:
    metrics: tuple[ChallengerMetric, ...]
    contrasts: tuple[ChallengerContrast, ...]
    reliability: tuple[ReliabilityGroup, ...]
    bootstrap_draws: int
    bootstrap_seed: int
    block_days: int
    simultaneous_critical_value: float | None
