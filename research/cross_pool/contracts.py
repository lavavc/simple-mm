"""Shared immutable contracts for cross-pool research."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypeAlias

PoolName: TypeAlias = Literal["uni-base", "uni-bsc"]
StoredPriceModel: TypeAlias = Literal["sqrt_mid"]
Regime: TypeAlias = Literal["early", "mixed", "late"]
Direction: TypeAlias = Literal["bsc_to_base", "base_to_bsc"]
EvidenceClass: TypeAlias = Literal[
    "positive_evidence",
    "suggestive",
    "affirmative_null",
    "inconclusive",
]
ImprovementSign: TypeAlias = Literal[-1, 0, 1]
UnavailableReason: TypeAlias = Literal["no_rows"]

PREDICTIVE_BOOTSTRAP_RESAMPLES = 2_000
PREDICTIVE_BOOTSTRAP_SEED = 20_260_715
PREDICTIVE_BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
PREDICTIVE_MIN_TARGET_DAYS = 20
PREDICTIVE_MIN_CONDITIONAL_TARGET_DAYS = 10
PREDICTIVE_CONDITIONAL_MOVE_THRESHOLD_BPS = 10.0
PREDICTIVE_POSITIVE_ACCURACY_LOWER_BOUND = 0.50
PREDICTIVE_NULL_MAE_UPPER_BOUND_BPS = 1.0
PREDICTIVE_NULL_DIRECTIONAL_GAIN_UPPER_BOUND = 0.05


class CrossPoolContractError(ValueError):
    """Raised when an input violates a frozen causal research contract."""


@dataclass(frozen=True)
class PoolEvent:
    pool: PoolName
    timestamp_ms: int
    block_number: int
    tx_hash: str
    log_index: int
    raw_mid: Decimal
    fee_adjusted_bid: Decimal
    fee_adjusted_ask: Decimal
    stored_cngn_usd_price: Decimal
    stored_price_model: StoredPriceModel


@dataclass(frozen=True)
class GapQuantiles:
    p50: int
    p95: int
    p99: int


@dataclass(frozen=True)
class StreamQuality:
    pool: PoolName
    rows: int
    first_timestamp_ms: int
    last_timestamp_ms: int
    update_gap_quantiles_ms: GapQuantiles | None
    pre_transition_rows: int
    post_transition_rows: int


@dataclass(frozen=True)
class PanelConfig:
    horizon_ms: int
    base_transition_block: int = 45_848_255
    bsc_transition_block: int = 97_799_490


@dataclass(frozen=True)
class PanelRow:
    timestamp_ms: int
    horizon_ms: int
    base_state_timestamp_ms: int
    bsc_state_timestamp_ms: int
    base_forward_state_timestamp_ms: int
    bsc_forward_state_timestamp_ms: int
    base_lag_block_number: int
    bsc_lag_block_number: int
    base_state_block_number: int
    bsc_state_block_number: int
    base_forward_block_number: int
    bsc_forward_block_number: int
    base_age_ms: int
    bsc_age_ms: int
    base_mid: float
    bsc_mid: float
    base_trailing_return_bps: float
    bsc_trailing_return_bps: float
    base_minus_bsc_gap_bps: float
    base_forward_return_bps: float
    bsc_forward_return_bps: float
    base_regime: Regime
    bsc_regime: Regime


@dataclass(frozen=True)
class CausalPanel:
    common_interval_start_ms: int
    common_interval_end_ms: int
    horizon_ms: int
    rows: tuple[PanelRow, ...]


@dataclass(frozen=True)
class WalkForwardConfig:
    direction: Direction
    initial_train_days: int = 14
    refit_weekday: int = 0
    maximum_condition_number: float = 1e12


@dataclass(frozen=True)
class PredictionRow:
    timestamp_ms: int
    target_timestamp_ms: int
    horizon_ms: int
    refit_timestamp_ms: int
    fold_index: int
    direction: Direction
    target_regime: Regime
    source_regime: Regime
    actual_bps: float
    baseline_prediction_bps: float
    cross_prediction_bps: float


@dataclass(frozen=True)
class FoldAudit:
    fold_index: int
    refit_timestamp_ms: int
    max_training_target_timestamp_ms: int
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]


@dataclass(frozen=True)
class WalkForwardResult:
    predictions: tuple[PredictionRow, ...]
    audits: tuple[FoldAudit, ...]


@dataclass(frozen=True)
class PredictiveMetrics:
    direction: Direction
    horizon_ms: int
    rows: int
    target_day_count: int
    conditional_rows: int
    conditional_target_day_count: int
    baseline_mae_bps: float
    cross_mae_bps: float
    mae_improvement_bps: float
    baseline_mse_bps2: float
    cross_mse_bps2: float
    mse_improvement_bps2: float
    baseline_rmse_bps: float
    cross_rmse_bps: float
    relative_oos_r2: float | None
    baseline_conditional_directional_accuracy: float | None
    cross_conditional_directional_accuracy: float | None
    conditional_directional_accuracy_gain: float | None

    def __post_init__(self) -> None:
        _validate_direction_horizon(self.direction, self.horizon_ms)
        if self.rows <= 0:
            raise CrossPoolContractError("predictive metrics require positive rows")
        if not 1 <= self.target_day_count <= self.rows:
            raise CrossPoolContractError("target_day_count must be between 1 and rows")
        if not 0 <= self.conditional_rows <= self.rows:
            raise CrossPoolContractError("conditional_rows must be between 0 and rows")
        if not 0 <= self.conditional_target_day_count <= self.target_day_count:
            raise CrossPoolContractError(
                "conditional_target_day_count must be between 0 and target_day_count"
            )
        if self.conditional_target_day_count > self.conditional_rows:
            raise CrossPoolContractError(
                "conditional target-day support cannot exceed conditional rows"
            )
        if (self.conditional_rows == 0) != (self.conditional_target_day_count == 0):
            raise CrossPoolContractError(
                "conditional row and target-day support must be absent together"
            )

        nonnegative_fields = (
            ("baseline_mae_bps", self.baseline_mae_bps),
            ("cross_mae_bps", self.cross_mae_bps),
            ("baseline_mse_bps2", self.baseline_mse_bps2),
            ("cross_mse_bps2", self.cross_mse_bps2),
            ("baseline_rmse_bps", self.baseline_rmse_bps),
            ("cross_rmse_bps", self.cross_rmse_bps),
        )
        for name, value in nonnegative_fields:
            _require_nonnegative_finite(value, name)
        _require_finite(self.mae_improvement_bps, "mae_improvement_bps")
        _require_finite(self.mse_improvement_bps2, "mse_improvement_bps2")
        _require_exact(
            self.mae_improvement_bps,
            self.baseline_mae_bps - self.cross_mae_bps,
            "MAE improvement",
        )
        _require_exact(
            self.mse_improvement_bps2,
            self.baseline_mse_bps2 - self.cross_mse_bps2,
            "MSE improvement",
        )
        _require_exact(
            self.baseline_rmse_bps,
            math.sqrt(self.baseline_mse_bps2),
            "baseline RMSE",
        )
        _require_exact(
            self.cross_rmse_bps,
            math.sqrt(self.cross_mse_bps2),
            "cross RMSE",
        )
        if self.baseline_mse_bps2 == 0.0:
            if self.relative_oos_r2 is not None:
                raise CrossPoolContractError(
                    "relative OOS R-squared must be unavailable when baseline MSE is zero"
                )
        else:
            if self.relative_oos_r2 is None:
                raise CrossPoolContractError(
                    "relative OOS R-squared must be present when baseline MSE is nonzero"
                )
            _require_finite(self.relative_oos_r2, "relative_oos_r2")
            _require_exact(
                self.relative_oos_r2,
                1.0 - self.cross_mse_bps2 / self.baseline_mse_bps2,
                "relative OOS R-squared",
            )

        directional = (
            self.baseline_conditional_directional_accuracy,
            self.cross_conditional_directional_accuracy,
            self.conditional_directional_accuracy_gain,
        )
        if self.conditional_rows == 0:
            if any(value is not None for value in directional):
                raise CrossPoolContractError(
                    "directional metrics must be unavailable without conditional rows"
                )
            return
        if any(value is None for value in directional):
            raise CrossPoolContractError(
                "directional metrics must be present with conditional rows"
            )
        baseline_accuracy = self.baseline_conditional_directional_accuracy
        cross_accuracy = self.cross_conditional_directional_accuracy
        gain = self.conditional_directional_accuracy_gain
        assert baseline_accuracy is not None and cross_accuracy is not None and gain is not None
        for name, value in (
            ("baseline_conditional_directional_accuracy", baseline_accuracy),
            ("cross_conditional_directional_accuracy", cross_accuracy),
        ):
            _require_finite(value, name)
            if not 0.0 <= value <= 1.0:
                raise CrossPoolContractError(f"{name} must be between 0 and 1")
        _require_finite(gain, "conditional_directional_accuracy_gain")
        if not -1.0 <= gain <= 1.0:
            raise CrossPoolContractError(
                "conditional_directional_accuracy_gain must be between -1 and 1"
            )
        _require_exact(
            gain,
            cross_accuracy - baseline_accuracy,
            "conditional directional accuracy gain",
        )


@dataclass(frozen=True)
class ConfidenceInterval:
    point: float
    lower: float
    upper: float

    def __post_init__(self) -> None:
        _require_finite(self.point, "confidence interval point")
        _require_finite(self.lower, "confidence interval lower")
        _require_finite(self.upper, "confidence interval upper")
        if self.lower > self.upper:
            raise CrossPoolContractError("confidence interval lower must not exceed upper")


@dataclass(frozen=True)
class PredictiveBootstrap:
    direction: Direction
    horizon_ms: int
    resamples: int
    seed: int
    confidence_level: float
    mae_improvement_bps: ConfidenceInterval
    mse_improvement_bps2: ConfidenceInterval
    cross_conditional_directional_accuracy: ConfidenceInterval | None
    conditional_directional_accuracy_gain: ConfidenceInterval | None
    valid_conditional_resamples: int

    def __post_init__(self) -> None:
        _validate_direction_horizon(self.direction, self.horizon_ms)
        if self.resamples <= 0:
            raise CrossPoolContractError("resamples must be positive")
        if self.seed < 0:
            raise CrossPoolContractError("seed must be nonnegative")
        if not math.isfinite(self.confidence_level) or not 0.0 < self.confidence_level < 1.0:
            raise CrossPoolContractError("confidence_level must be finite and between 0 and 1")
        if not 0 <= self.valid_conditional_resamples <= self.resamples:
            raise CrossPoolContractError(
                "valid_conditional_resamples must be between 0 and resamples"
            )
        conditional_intervals = (
            self.cross_conditional_directional_accuracy,
            self.conditional_directional_accuracy_gain,
        )
        if self.valid_conditional_resamples == self.resamples:
            if any(interval is None for interval in conditional_intervals):
                raise CrossPoolContractError(
                    "conditional intervals must be present when every draw is valid"
                )
        elif any(interval is not None for interval in conditional_intervals):
            raise CrossPoolContractError(
                "conditional intervals must be unavailable unless every draw is valid"
            )
        accuracy = self.cross_conditional_directional_accuracy
        if accuracy is not None and any(
            not 0.0 <= value <= 1.0 for value in (accuracy.point, accuracy.lower, accuracy.upper)
        ):
            raise CrossPoolContractError(
                "conditional accuracy interval values must be between 0 and 1"
            )
        gain = self.conditional_directional_accuracy_gain
        if gain is not None and any(
            not -1.0 <= value <= 1.0 for value in (gain.point, gain.lower, gain.upper)
        ):
            raise CrossPoolContractError(
                "conditional directional gain interval values must be between -1 and 1"
            )


@dataclass(frozen=True)
class AdequacyAudit:
    target_day_count: int
    conditional_target_day_count: int

    def __post_init__(self) -> None:
        if self.target_day_count < 0:
            raise CrossPoolContractError("target_day_count must be nonnegative")
        if not 0 <= self.conditional_target_day_count <= self.target_day_count:
            raise CrossPoolContractError(
                "conditional_target_day_count must be between 0 and target_day_count"
            )

    @property
    def underpowered(self) -> bool:
        return (
            self.target_day_count < PREDICTIVE_MIN_TARGET_DAYS
            or self.conditional_target_day_count < PREDICTIVE_MIN_CONDITIONAL_TARGET_DAYS
        )


@dataclass(frozen=True)
class PredictiveInference:
    metrics: PredictiveMetrics
    bootstrap: PredictiveBootstrap
    adequacy: AdequacyAudit
    evidence_class: EvidenceClass

    def __post_init__(self) -> None:
        if (
            self.bootstrap.resamples,
            self.bootstrap.seed,
            self.bootstrap.confidence_level,
        ) != (
            PREDICTIVE_BOOTSTRAP_RESAMPLES,
            PREDICTIVE_BOOTSTRAP_SEED,
            PREDICTIVE_BOOTSTRAP_CONFIDENCE_LEVEL,
        ):
            raise CrossPoolContractError(
                "predictive inference requires the frozen bootstrap configuration"
            )
        if self.adequacy != AdequacyAudit(
            target_day_count=self.metrics.target_day_count,
            conditional_target_day_count=self.metrics.conditional_target_day_count,
        ):
            raise CrossPoolContractError("adequacy support must match predictive metrics")
        _validate_evidence_class(self.evidence_class)
        if self.evidence_class != _derive_evidence_class(self.metrics, self.bootstrap):
            raise CrossPoolContractError(
                "predictive evidence class must match metrics and bootstrap intervals"
            )


@dataclass(frozen=True)
class OmissionResult:
    unit: str
    mae_improvement_sign: ImprovementSign
    evidence_class: EvidenceClass

    def __post_init__(self) -> None:
        if not self.unit:
            raise CrossPoolContractError("omission unit must be nonempty")
        _validate_improvement_sign(self.mae_improvement_sign)
        _validate_evidence_class(self.evidence_class)


@dataclass(frozen=True)
class InfluenceReport:
    direction: Direction
    horizon_ms: int
    full_mae_improvement_sign: ImprovementSign
    full_evidence_class: EvidenceClass
    leave_one_day: tuple[OmissionResult, ...]
    leave_one_fold: tuple[OmissionResult, ...]
    unit_dependent: bool

    def __post_init__(self) -> None:
        _validate_direction_horizon(self.direction, self.horizon_ms)
        _validate_improvement_sign(self.full_mae_improvement_sign)
        _validate_evidence_class(self.full_evidence_class)
        if not self.leave_one_day or not self.leave_one_fold:
            raise CrossPoolContractError("influence report requires day and fold omissions")
        for label, omissions in (
            ("day", self.leave_one_day),
            ("fold", self.leave_one_fold),
        ):
            units = tuple(omission.unit for omission in omissions)
            if len(set(units)) != len(units):
                raise CrossPoolContractError(f"leave-one-{label} units must be unique")
        expected = any(
            omission.mae_improvement_sign != self.full_mae_improvement_sign
            or omission.evidence_class != self.full_evidence_class
            for omission in self.leave_one_day + self.leave_one_fold
        )
        if self.unit_dependent != expected:
            raise CrossPoolContractError(
                "unit_dependent must match the omission sign and class comparisons"
            )


@dataclass(frozen=True)
class RegimeSubsetInference:
    regime: Regime
    row_count: int
    inference: PredictiveInference | None
    unavailable_reason: UnavailableReason | None

    def __post_init__(self) -> None:
        _validate_regime(self.regime)
        if self.row_count < 0:
            raise CrossPoolContractError("regime row_count must be nonnegative")
        if self.row_count == 0:
            if self.inference is not None or self.unavailable_reason != "no_rows":
                raise CrossPoolContractError(
                    "empty regime subset requires no_rows and no inference"
                )
            return
        if self.inference is None or self.unavailable_reason is not None:
            raise CrossPoolContractError(
                "nonempty regime subset requires inference without an unavailable reason"
            )
        if self.inference.metrics.rows != self.row_count:
            raise CrossPoolContractError("regime row_count must match inference metrics")


@dataclass(frozen=True)
class RegimeSensitivity:
    direction: Direction
    horizon_ms: int
    early: RegimeSubsetInference
    mixed: RegimeSubsetInference
    late: RegimeSubsetInference
    regime_unstable: bool
    regime_not_adjudicable: bool

    def __post_init__(self) -> None:
        _validate_direction_horizon(self.direction, self.horizon_ms)
        if (self.early.regime, self.mixed.regime, self.late.regime) != (
            "early",
            "mixed",
            "late",
        ):
            raise CrossPoolContractError("regime subset labels must be early, mixed, and late")
        if self.regime_unstable and self.regime_not_adjudicable:
            raise CrossPoolContractError(
                "regime_unstable and regime_not_adjudicable cannot both be true"
            )
        for subset in (self.early, self.mixed, self.late):
            if subset.inference is None:
                continue
            metrics = subset.inference.metrics
            if metrics.direction != self.direction or metrics.horizon_ms != self.horizon_ms:
                raise CrossPoolContractError(
                    "regime inference direction and horizon must match the report"
                )
        adjudicable = (
            self.early.inference is not None
            and self.late.inference is not None
            and not self.early.inference.adequacy.underpowered
            and not self.late.inference.adequacy.underpowered
        )
        expected_not_adjudicable = not adjudicable
        expected_unstable = False
        if adjudicable:
            assert self.early.inference is not None and self.late.inference is not None
            early_sign = _improvement_sign(self.early.inference.metrics.mae_improvement_bps)
            late_sign = _improvement_sign(self.late.inference.metrics.mae_improvement_bps)
            opposite_signs = early_sign != 0 and early_sign == -late_sign
            expected_unstable = (
                opposite_signs
                or self.early.inference.evidence_class != self.late.inference.evidence_class
            )
        if (
            self.regime_not_adjudicable != expected_not_adjudicable
            or self.regime_unstable != expected_unstable
        ):
            raise CrossPoolContractError(
                "regime flags must match nested support, signs, and evidence classes"
            )


@dataclass(frozen=True)
class DirectionalPredictiveAudit:
    direction: Direction
    horizon_ms: int
    inference: PredictiveInference
    influence: InfluenceReport
    regime_sensitivity: RegimeSensitivity

    def __post_init__(self) -> None:
        _validate_direction_horizon(self.direction, self.horizon_ms)
        for direction, horizon_ms in (
            (self.inference.metrics.direction, self.inference.metrics.horizon_ms),
            (self.inference.bootstrap.direction, self.inference.bootstrap.horizon_ms),
            (self.influence.direction, self.influence.horizon_ms),
            (self.regime_sensitivity.direction, self.regime_sensitivity.horizon_ms),
        ):
            if direction != self.direction or horizon_ms != self.horizon_ms:
                raise CrossPoolContractError(
                    "directional audit nested direction and horizon must match"
                )
        expected_sign = _improvement_sign(self.inference.metrics.mae_improvement_bps)
        if self.influence.full_mae_improvement_sign != expected_sign:
            raise CrossPoolContractError("influence full MAE sign must match predictive inference")
        if self.influence.full_evidence_class != self.inference.evidence_class:
            raise CrossPoolContractError(
                "influence full evidence class must match predictive inference"
            )
        subset_rows = sum(
            subset.row_count
            for subset in (
                self.regime_sensitivity.early,
                self.regime_sensitivity.mixed,
                self.regime_sensitivity.late,
            )
        )
        if subset_rows != self.inference.metrics.rows:
            raise CrossPoolContractError(
                "regime subset rows must partition the full predictive inference"
            )


def _validate_direction_horizon(direction: Direction, horizon_ms: int) -> None:
    if direction not in ("bsc_to_base", "base_to_bsc"):
        raise CrossPoolContractError("unsupported predictive direction")
    if horizon_ms <= 0:
        raise CrossPoolContractError("predictive horizon_ms must be positive")


def _validate_regime(regime: Regime) -> None:
    if regime not in ("early", "mixed", "late"):
        raise CrossPoolContractError("unsupported regime")


def _validate_evidence_class(evidence_class: EvidenceClass) -> None:
    if evidence_class not in (
        "positive_evidence",
        "suggestive",
        "affirmative_null",
        "inconclusive",
    ):
        raise CrossPoolContractError("unsupported evidence class")


def _validate_improvement_sign(sign: ImprovementSign) -> None:
    if sign not in (-1, 0, 1):
        raise CrossPoolContractError("MAE improvement sign must be -1, 0, or 1")


def _improvement_sign(value: float) -> ImprovementSign:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def _require_finite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise CrossPoolContractError(f"{name} must be finite")


def _require_nonnegative_finite(value: float, name: str) -> None:
    _require_finite(value, name)
    if value < 0.0:
        raise CrossPoolContractError(f"{name} must be nonnegative")


def _require_exact(actual: float, expected: float, name: str) -> None:
    if actual != expected:
        raise CrossPoolContractError(f"{name} is inconsistent with its components")


def _derive_evidence_class(
    metrics: PredictiveMetrics,
    bootstrap: PredictiveBootstrap,
) -> EvidenceClass:
    if metrics.direction != bootstrap.direction or metrics.horizon_ms != bootstrap.horizon_ms:
        raise CrossPoolContractError("metrics and bootstrap direction and horizon must match")
    expected_points = (
        (bootstrap.mae_improvement_bps.point, metrics.mae_improvement_bps),
        (bootstrap.mse_improvement_bps2.point, metrics.mse_improvement_bps2),
    )
    if any(interval_point != metric_point for interval_point, metric_point in expected_points):
        raise CrossPoolContractError("bootstrap interval point must match predictive metrics")
    if bootstrap.cross_conditional_directional_accuracy is not None:
        if (
            metrics.cross_conditional_directional_accuracy is None
            or bootstrap.cross_conditional_directional_accuracy.point
            != metrics.cross_conditional_directional_accuracy
        ):
            raise CrossPoolContractError("bootstrap interval point must match predictive metrics")
    if bootstrap.conditional_directional_accuracy_gain is not None:
        if (
            metrics.conditional_directional_accuracy_gain is None
            or bootstrap.conditional_directional_accuracy_gain.point
            != metrics.conditional_directional_accuracy_gain
        ):
            raise CrossPoolContractError("bootstrap interval point must match predictive metrics")
    if AdequacyAudit(
        target_day_count=metrics.target_day_count,
        conditional_target_day_count=metrics.conditional_target_day_count,
    ).underpowered:
        return "inconclusive"

    cross_accuracy = bootstrap.cross_conditional_directional_accuracy
    directional_gain = bootstrap.conditional_directional_accuracy_gain
    if (
        metrics.mae_improvement_bps > 0.0
        and metrics.mse_improvement_bps2 > 0.0
        and bootstrap.mae_improvement_bps.lower > 0.0
        and bootstrap.mse_improvement_bps2.lower > 0.0
        and cross_accuracy is not None
        and cross_accuracy.lower > PREDICTIVE_POSITIVE_ACCURACY_LOWER_BOUND
    ):
        return "positive_evidence"
    if (
        bootstrap.mae_improvement_bps.upper < PREDICTIVE_NULL_MAE_UPPER_BOUND_BPS
        and directional_gain is not None
        and directional_gain.upper < PREDICTIVE_NULL_DIRECTIONAL_GAIN_UPPER_BOUND
    ):
        return "affirmative_null"
    if metrics.mae_improvement_bps > 0.0 and metrics.mse_improvement_bps2 > 0.0:
        return "suggestive"
    return "inconclusive"
