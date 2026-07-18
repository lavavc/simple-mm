"""Shared immutable contracts for cross-pool research."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from types import MappingProxyType
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
EventExclusionReason: TypeAlias = Literal[
    "missing_target_start_state",
    "missing_target_end_state",
]

PREDICTIVE_BOOTSTRAP_RESAMPLES = 2_000
PREDICTIVE_BOOTSTRAP_SEED = 20_260_715
PREDICTIVE_BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
PREDICTIVE_MIN_TARGET_DAYS = 20
PREDICTIVE_MIN_CONDITIONAL_TARGET_DAYS = 10
PREDICTIVE_CONDITIONAL_MOVE_THRESHOLD_BPS = 10.0
PREDICTIVE_POSITIVE_ACCURACY_LOWER_BOUND = 0.50
PREDICTIVE_NULL_MAE_UPPER_BOUND_BPS = 1.0
PREDICTIVE_NULL_DIRECTIONAL_GAIN_UPPER_BOUND = 0.05
EVENT_SHOCK_LOOKBACK_MS = 900_000
EVENT_SHOCK_THRESHOLD_BPS = 5.0
EVENT_SHOCK_CLUSTER_MS = 900_000
EVENT_RESPONSE_HORIZONS_MS = (900_000, 3_600_000, 14_400_000)
EVENT_BOOTSTRAP_RESAMPLES = 2_000
EVENT_BOOTSTRAP_SEED = 20_260_715
EVENT_BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
DTW_GRID_MS = 900_000
DTW_BAND_STEPS = (1, 4, 16)
DTW_PRIMARY_BAND_STEPS = 4
DTW_DAY_MS = 86_400_000
DTW_WEEK_MS = 7 * DTW_DAY_MS
DTW_POINTS_PER_WEEK = DTW_WEEK_MS // DTW_GRID_MS
DTW_MONDAY_EPOCH_MS = 4 * DTW_DAY_MS


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
class ShockConfig:
    lookback_ms: int = EVENT_SHOCK_LOOKBACK_MS
    threshold_bps: float = EVENT_SHOCK_THRESHOLD_BPS
    cluster_ms: int = EVENT_SHOCK_CLUSTER_MS
    response_horizons_ms: tuple[int, ...] = EVENT_RESPONSE_HORIZONS_MS

    def __post_init__(self) -> None:
        _require_positive_int(self.lookback_ms, "shock lookback_ms")
        if (
            isinstance(self.threshold_bps, bool)
            or not math.isfinite(self.threshold_bps)
            or self.threshold_bps <= 0.0
        ):
            raise CrossPoolContractError(
                "shock threshold_bps must be positive and finite"
            )
        _require_positive_int(self.cluster_ms, "shock cluster_ms")
        if not isinstance(self.response_horizons_ms, tuple):
            raise CrossPoolContractError("response horizons must be an immutable tuple")
        if not self.response_horizons_ms:
            raise CrossPoolContractError("response horizons must be positive")
        for horizon_ms in self.response_horizons_ms:
            _require_positive_int(horizon_ms, "response horizon_ms")
        if any(
            current <= previous
            for previous, current in zip(
                self.response_horizons_ms,
                self.response_horizons_ms[1:],
                strict=False,
            )
        ):
            raise CrossPoolContractError(
                "response horizons must be strictly increasing and unique"
            )


@dataclass(frozen=True)
class ShockEvent:
    direction: Direction
    source_pool: PoolName
    target_pool: PoolName
    source_start_timestamp_ms: int
    shock_timestamp_ms: int
    source_move_bps: float
    source_move_sign: Literal[-1, 1]
    shock_day_utc: date

    def __post_init__(self) -> None:
        _validate_event_direction_pools(
            self.direction,
            self.source_pool,
            self.target_pool,
        )
        _require_positive_int(
            self.source_start_timestamp_ms,
            "shock source_start_timestamp_ms",
        )
        _require_positive_int(self.shock_timestamp_ms, "shock timestamp_ms")
        if self.source_start_timestamp_ms >= self.shock_timestamp_ms:
            raise CrossPoolContractError(
                "shock source start must be positive and precede the crossing"
        )
        _require_finite(self.source_move_bps, "shock source_move_bps")
        expected_sign = 1 if self.source_move_bps > 0.0 else -1
        if (
            self.source_move_bps == 0.0
            or not isinstance(self.source_move_sign, int)
            or isinstance(self.source_move_sign, bool)
            or self.source_move_sign != expected_sign
        ):
            raise CrossPoolContractError("shock sign must match a nonzero source move")
        if self.shock_day_utc != utc_day_from_timestamp_ms(self.shock_timestamp_ms):
            raise CrossPoolContractError("shock UTC day must match its timestamp")


@dataclass(frozen=True)
class EventResponse:
    direction: Direction
    source_pool: PoolName
    target_pool: PoolName
    shock_timestamp_ms: int
    shock_day_utc: date
    horizon_ms: int
    source_move_bps: float
    target_start_timestamp_ms: int
    target_end_timestamp_ms: int
    target_response_bps: float
    direction_agrees: bool

    def __post_init__(self) -> None:
        _validate_event_direction_pools(
            self.direction,
            self.source_pool,
            self.target_pool,
        )
        _validate_direction_horizon(self.direction, self.horizon_ms)
        _require_positive_int(self.shock_timestamp_ms, "event shock_timestamp_ms")
        if self.shock_day_utc != utc_day_from_timestamp_ms(self.shock_timestamp_ms):
            raise CrossPoolContractError("event UTC day must match its shock timestamp")
        _require_finite(self.source_move_bps, "event source_move_bps")
        if self.source_move_bps == 0.0:
            raise CrossPoolContractError("event source move must be nonzero")
        _require_positive_int(
            self.target_start_timestamp_ms,
            "event target_start_timestamp_ms",
        )
        _require_positive_int(
            self.target_end_timestamp_ms,
            "event target_end_timestamp_ms",
        )
        if self.target_start_timestamp_ms > self.shock_timestamp_ms:
            raise CrossPoolContractError("event target start must be as of the shock")
        if not (
            self.target_start_timestamp_ms
            <= self.target_end_timestamp_ms
            <= self.shock_timestamp_ms + self.horizon_ms
        ):
            raise CrossPoolContractError("event target end must be as of the horizon")
        _require_finite(self.target_response_bps, "event target_response_bps")
        if not isinstance(self.direction_agrees, bool):
            raise CrossPoolContractError(
                "event direction agreement must be a boolean"
            )
        expected_agreement = (
            self.target_response_bps > 0.0 and self.source_move_bps > 0.0
        ) or (self.target_response_bps < 0.0 and self.source_move_bps < 0.0)
        if self.direction_agrees != expected_agreement:
            raise CrossPoolContractError(
                "event direction agreement must match the nonzero response signs"
            )


@dataclass(frozen=True)
class EventExclusion:
    direction: Direction
    shock_timestamp_ms: int
    shock_day_utc: date
    horizon_ms: int
    reason: EventExclusionReason

    def __post_init__(self) -> None:
        _validate_direction_horizon(self.direction, self.horizon_ms)
        _require_positive_int(self.shock_timestamp_ms, "exclusion shock_timestamp_ms")
        if self.shock_day_utc != utc_day_from_timestamp_ms(self.shock_timestamp_ms):
            raise CrossPoolContractError(
                "exclusion UTC day must match its shock timestamp"
            )
        if self.reason not in (
            "missing_target_start_state",
            "missing_target_end_state",
        ):
            raise CrossPoolContractError("unsupported event exclusion reason")


@dataclass(frozen=True)
class EventStudyResult:
    responses: tuple[EventResponse, ...]
    exclusions: tuple[EventExclusion, ...]

    def __post_init__(self) -> None:
        response_keys = tuple(_event_outcome_key(row) for row in self.responses)
        exclusion_keys = tuple(_event_outcome_key(row) for row in self.exclusions)
        if response_keys != tuple(sorted(response_keys)):
            raise CrossPoolContractError("event responses must use canonical ordering")
        if exclusion_keys != tuple(sorted(exclusion_keys)):
            raise CrossPoolContractError("event exclusions must use canonical ordering")
        if len(set(response_keys)) != len(response_keys):
            raise CrossPoolContractError("event response keys must be unique")
        if len(set(exclusion_keys)) != len(exclusion_keys):
            raise CrossPoolContractError("event exclusion keys must be unique")
        if set(response_keys) & set(exclusion_keys):
            raise CrossPoolContractError(
                "an event outcome cannot be both a response and an exclusion"
            )


@dataclass(frozen=True)
class EventSummary:
    direction: Direction
    horizon_ms: int
    event_count: int
    event_day_count: int
    mean_response_bps: ConfidenceInterval
    median_response_bps: ConfidenceInterval
    direction_agreement: ConfidenceInterval

    def __post_init__(self) -> None:
        _validate_direction_horizon(self.direction, self.horizon_ms)
        _require_positive_int(self.event_count, "event_count")
        _require_positive_int(self.event_day_count, "event_day_count")
        if self.event_day_count > self.event_count:
            raise CrossPoolContractError("event_day_count cannot exceed event_count")
        if any(
            not 0.0 <= value <= 1.0
            for value in (
                self.direction_agreement.point,
                self.direction_agreement.lower,
                self.direction_agreement.upper,
            )
        ):
            raise CrossPoolContractError(
                "event direction agreement interval must be between 0 and 1"
            )


@dataclass(frozen=True)
class DtwConfig:
    grid_ms: int = DTW_GRID_MS
    band_steps: tuple[int, ...] = DTW_BAND_STEPS
    primary_band_steps: int = DTW_PRIMARY_BAND_STEPS

    def __post_init__(self) -> None:
        _require_positive_int(self.grid_ms, "DTW grid_ms")
        if not isinstance(self.band_steps, tuple):
            raise CrossPoolContractError("DTW band_steps must be an immutable tuple")
        for band_steps in self.band_steps:
            _require_positive_int(band_steps, "DTW band_steps value")
        _require_positive_int(self.primary_band_steps, "DTW primary_band_steps")
        if (
            self.grid_ms != DTW_GRID_MS
            or self.band_steps != DTW_BAND_STEPS
            or self.primary_band_steps != DTW_PRIMARY_BAND_STEPS
        ):
            raise CrossPoolContractError("DTW configuration is frozen")


@dataclass(frozen=True)
class DtwPath:
    source_length: int
    target_length: int
    band_steps: int
    matches: tuple[tuple[int, int], ...]
    total_cost: float
    normalized_cost: float
    median_signed_lag_steps: float

    def __post_init__(self) -> None:
        _require_positive_int(self.source_length, "DTW source_length")
        _require_positive_int(self.target_length, "DTW target_length")
        _require_nonnegative_int(self.band_steps, "DTW band_steps")
        signed_lags = _validate_dtw_matches(
            self.matches,
            source_length=self.source_length,
            target_length=self.target_length,
            band_steps=self.band_steps,
        )
        _require_nonnegative_finite(self.total_cost, "DTW total_cost")
        _require_nonnegative_finite(self.normalized_cost, "DTW normalized_cost")
        _require_exact(
            self.normalized_cost,
            self.total_cost / len(self.matches),
            "DTW normalized cost",
        )
        _require_finite(
            self.median_signed_lag_steps,
            "DTW median_signed_lag_steps",
        )
        _require_exact(
            self.median_signed_lag_steps,
            float(median(signed_lags)),
            "DTW median signed lag",
        )


@dataclass(frozen=True)
class DtwWeekResult:
    week_start_timestamp_ms: int
    direction: Direction
    band_steps: int
    path_length: int
    total_cost: float
    normalized_cost: float
    median_signed_lag_steps: float
    matches: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        _validate_utc_week_start(self.week_start_timestamp_ms)
        _validate_direction(self.direction)
        _require_positive_int(self.band_steps, "DTW week band_steps")
        if self.band_steps not in DTW_BAND_STEPS:
            raise CrossPoolContractError("DTW week requires a frozen band")
        _require_positive_int(self.path_length, "DTW week path_length")
        signed_lags = _validate_dtw_matches(
            self.matches,
            source_length=DTW_POINTS_PER_WEEK,
            target_length=DTW_POINTS_PER_WEEK,
            band_steps=self.band_steps,
        )
        if self.path_length != len(self.matches):
            raise CrossPoolContractError("DTW week path_length must match its path")
        _require_nonnegative_finite(self.total_cost, "DTW week total_cost")
        _require_nonnegative_finite(
            self.normalized_cost,
            "DTW week normalized_cost",
        )
        _require_exact(
            self.normalized_cost,
            self.total_cost / self.path_length,
            "DTW week normalized cost",
        )
        _require_finite(
            self.median_signed_lag_steps,
            "DTW week median_signed_lag_steps",
        )
        _require_exact(
            self.median_signed_lag_steps,
            float(median(signed_lags)),
            "DTW week median signed lag",
        )


@dataclass(frozen=True)
class DtwNullResult:
    week_start_timestamp_ms: int
    direction: Direction
    band_steps: int
    rotation_days: int
    path_length: int
    total_cost: float
    normalized_cost: float
    observed_normalized_cost: float
    observed_cost_improvement: float
    median_signed_lag_steps: float
    observed_median_signed_lag_steps: float
    observed_signed_lag_difference_steps: float

    def __post_init__(self) -> None:
        _validate_utc_week_start(self.week_start_timestamp_ms)
        _validate_direction(self.direction)
        _require_positive_int(self.band_steps, "DTW null band_steps")
        if self.band_steps not in DTW_BAND_STEPS:
            raise CrossPoolContractError("DTW null requires a frozen band")
        _require_positive_int(self.rotation_days, "DTW null rotation_days")
        if self.rotation_days > 6:
            raise CrossPoolContractError("DTW null rotation_days must be between 1 and 6")
        _require_positive_int(self.path_length, "DTW null path_length")
        if not DTW_POINTS_PER_WEEK <= self.path_length <= 2 * DTW_POINTS_PER_WEEK - 1:
            raise CrossPoolContractError("DTW null path_length is inconsistent")
        _require_nonnegative_finite(self.total_cost, "DTW null total_cost")
        _require_nonnegative_finite(
            self.normalized_cost,
            "DTW null normalized_cost",
        )
        _require_exact(
            self.normalized_cost,
            self.total_cost / self.path_length,
            "DTW null normalized cost",
        )
        _require_nonnegative_finite(
            self.observed_normalized_cost,
            "DTW null observed_normalized_cost",
        )
        _require_finite(
            self.observed_cost_improvement,
            "DTW null observed_cost_improvement",
        )
        _require_exact(
            self.observed_cost_improvement,
            self.normalized_cost - self.observed_normalized_cost,
            "DTW null observed cost improvement",
        )
        for name, value in (
            ("median_signed_lag_steps", self.median_signed_lag_steps),
            (
                "observed_median_signed_lag_steps",
                self.observed_median_signed_lag_steps,
            ),
        ):
            _require_finite(value, f"DTW null {name}")
            if abs(value) > self.band_steps:
                raise CrossPoolContractError(f"DTW null {name} exceeds its band")
        _require_finite(
            self.observed_signed_lag_difference_steps,
            "DTW null observed_signed_lag_difference_steps",
        )
        _require_exact(
            self.observed_signed_lag_difference_steps,
            self.median_signed_lag_steps
            - self.observed_median_signed_lag_steps,
            "DTW null observed signed lag difference",
        )


@dataclass(frozen=True)
class DtwStability:
    direction: Direction
    aggregate_median_lag_by_band: Mapping[int, float]
    weekly_median_lags_by_band: Mapping[int, tuple[tuple[int, float], ...]]
    primary_band_same_sign_week_share: float
    band_unstable: bool

    def __post_init__(self) -> None:
        _validate_direction(self.direction)
        expected_bands = DTW_BAND_STEPS
        if tuple(self.aggregate_median_lag_by_band) != expected_bands or tuple(
            self.weekly_median_lags_by_band
        ) != expected_bands:
            raise CrossPoolContractError(
                "DTW stability requires the frozen bands in canonical order"
            )

        weekly_support: dict[int, tuple[tuple[int, float], ...]] = {}
        common_weeks: tuple[int, ...] | None = None
        aggregates: dict[int, float] = {}
        for band_steps in expected_bands:
            support = self.weekly_median_lags_by_band[band_steps]
            if not isinstance(support, tuple) or not support:
                raise CrossPoolContractError(
                    "DTW stability requires immutable nonempty weekly support"
                )
            weeks: list[int] = []
            lags: list[float] = []
            for row in support:
                if not isinstance(row, tuple) or len(row) != 2:
                    raise CrossPoolContractError(
                        "DTW stability weekly support must contain week-lag pairs"
                    )
                week_start_timestamp_ms, lag = row
                _validate_utc_week_start(week_start_timestamp_ms)
                _require_finite(lag, "DTW stability weekly median lag")
                if abs(lag) > band_steps:
                    raise CrossPoolContractError(
                        "DTW stability weekly median lag exceeds its band"
                    )
                weeks.append(week_start_timestamp_ms)
                lags.append(lag)
            week_tuple = tuple(weeks)
            if len(set(week_tuple)) != len(week_tuple) or week_tuple != tuple(
                sorted(week_tuple)
            ):
                raise CrossPoolContractError(
                    "DTW stability weeks must be unique and canonically ordered"
                )
            if any(
                current != previous + DTW_WEEK_MS
                for previous, current in zip(
                    week_tuple,
                    week_tuple[1:],
                    strict=False,
                )
            ):
                raise CrossPoolContractError(
                    "DTW stability requires consecutive complete weeks"
                )
            if common_weeks is None:
                common_weeks = week_tuple
            elif week_tuple != common_weeks:
                raise CrossPoolContractError(
                    "DTW stability bands must use identical week support"
                )
            aggregate = self.aggregate_median_lag_by_band[band_steps]
            _require_finite(aggregate, "DTW stability aggregate median lag")
            _require_exact(
                aggregate,
                float(median(lags)),
                f"DTW stability aggregate band {band_steps}",
            )
            weekly_support[band_steps] = support
            aggregates[band_steps] = aggregate

        assert common_weeks is not None
        _require_finite(
            self.primary_band_same_sign_week_share,
            "DTW stability primary-band same-sign share",
        )
        if not 0.0 <= self.primary_band_same_sign_week_share <= 1.0:
            raise CrossPoolContractError(
                "DTW stability primary-band same-sign share must be between 0 and 1"
            )
        primary_aggregate = aggregates[DTW_PRIMARY_BAND_STEPS]
        primary_sign = _dtw_sign(primary_aggregate)
        primary_lags = (
            lag for _, lag in weekly_support[DTW_PRIMARY_BAND_STEPS]
        )
        same_sign_count = sum(
            1
            for lag in primary_lags
            if _dtw_sign(lag) != 0 and _dtw_sign(lag) == primary_sign
        )
        expected_share = (
            0.0 if primary_sign == 0 else same_sign_count / len(common_weeks)
        )
        _require_exact(
            self.primary_band_same_sign_week_share,
            expected_share,
            "DTW stability primary-band same-sign share",
        )
        aggregate_signs = {_dtw_sign(value) for value in aggregates.values()}
        opposing_nonzero_signs = -1 in aggregate_signs and 1 in aggregate_signs
        expected_unstable = (
            primary_sign == 0
            or opposing_nonzero_signs
            or same_sign_count * 3 < 2 * len(common_weeks)
        )
        if not isinstance(self.band_unstable, bool) or (
            self.band_unstable != expected_unstable
        ):
            raise CrossPoolContractError(
                "DTW stability band_unstable must match its weekly support"
            )
        object.__setattr__(
            self,
            "aggregate_median_lag_by_band",
            MappingProxyType(aggregates),
        )
        object.__setattr__(
            self,
            "weekly_median_lags_by_band",
            MappingProxyType(weekly_support),
        )


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
    _validate_direction(direction)
    _require_positive_int(horizon_ms, "horizon_ms")


def _validate_direction(direction: Direction) -> None:
    if direction not in ("bsc_to_base", "base_to_bsc"):
        raise CrossPoolContractError("unsupported direction")


def _validate_utc_week_start(timestamp_ms: int) -> None:
    _require_positive_int(timestamp_ms, "DTW week_start_timestamp_ms")
    if (timestamp_ms - DTW_MONDAY_EPOCH_MS) % DTW_WEEK_MS != 0:
        raise CrossPoolContractError("DTW week must start at Monday 00:00 UTC")


def _validate_event_direction_pools(
    direction: Direction,
    source_pool: PoolName,
    target_pool: PoolName,
) -> None:
    expected = {
        "bsc_to_base": ("uni-bsc", "uni-base"),
        "base_to_bsc": ("uni-base", "uni-bsc"),
    }
    if expected.get(direction) != (source_pool, target_pool):
        raise CrossPoolContractError(
            "event direction must match its source and target pools"
        )


def _event_outcome_key(
    row: EventResponse | EventExclusion,
) -> tuple[int, int, int]:
    direction_order = {"bsc_to_base": 0, "base_to_bsc": 1}
    return (
        direction_order[row.direction],
        row.shock_timestamp_ms,
        row.horizon_ms,
    )


def utc_day_from_timestamp_ms(timestamp_ms: int) -> date:
    _require_positive_int(timestamp_ms, "event timestamp_ms")
    try:
        return date(1970, 1, 1) + timedelta(days=timestamp_ms // 86_400_000)
    except OverflowError as exc:
        raise CrossPoolContractError(
            "event timestamp_ms is outside the supported date range"
        ) from exc


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


def _dtw_sign(value: float) -> Literal[-1, 0, 1]:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def _require_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value):
        raise CrossPoolContractError(f"{name} must be finite")


def _require_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CrossPoolContractError(f"{name} must be a positive integer")


def _require_nonnegative_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrossPoolContractError(f"{name} must be a nonnegative integer")


def _validate_dtw_matches(
    matches: tuple[tuple[int, int], ...],
    *,
    source_length: int,
    target_length: int,
    band_steps: int,
) -> tuple[int, ...]:
    if not isinstance(matches, tuple) or not matches:
        raise CrossPoolContractError("DTW matches must be a nonempty immutable tuple")
    for match in matches:
        if not isinstance(match, tuple) or len(match) != 2:
            raise CrossPoolContractError("DTW matches must contain index pairs")
        source_index, target_index = match
        _require_nonnegative_int(source_index, "DTW source index")
        _require_nonnegative_int(target_index, "DTW target index")
        if source_index >= source_length or target_index >= target_length:
            raise CrossPoolContractError("DTW match index exceeds its sequence length")
        if abs(source_index - target_index) > band_steps:
            raise CrossPoolContractError("DTW match exceeds the requested band")
    if matches[0] != (0, 0):
        raise CrossPoolContractError("DTW path must start at the fixed origin")
    if matches[-1] != (source_length - 1, target_length - 1):
        raise CrossPoolContractError("DTW path must reach the fixed terminal indices")
    if len(set(matches)) != len(matches):
        raise CrossPoolContractError("DTW matches must be unique")
    if any(
        (next_source - source_index, next_target - target_index)
        not in {(1, 1), (1, 0), (0, 1)}
        for (source_index, target_index), (next_source, next_target) in zip(
            matches,
            matches[1:],
            strict=False,
        )
    ):
        raise CrossPoolContractError("DTW path must use only legal steps")
    return tuple(target_index - source_index for source_index, target_index in matches)


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
