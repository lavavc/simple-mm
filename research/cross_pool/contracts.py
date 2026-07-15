"""Shared immutable contracts for cross-pool research."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypeAlias

PoolName: TypeAlias = Literal["uni-base", "uni-bsc"]
StoredPriceModel: TypeAlias = Literal["sqrt_mid"]
Regime: TypeAlias = Literal["early", "mixed", "late"]
Direction: TypeAlias = Literal["bsc_to_base", "base_to_bsc"]


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
