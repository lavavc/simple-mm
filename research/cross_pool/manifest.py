"""Typed article-manifest decisions and canonical serialization."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypeAlias, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from research.backtester.lp_ledger_attribution import (
    LEDGER_COVERAGE_SCHEMA_VERSION,
    POOL_ATTRIBUTION_ORIENTATIONS,
    RpcLedgerCoverage,
    VerifiedLedgerCoverageEvidence,
    rpc_ledger_coverage_bytes,
    rpc_ledger_coverage_from_payload,
    rpc_ledger_coverage_payload,
)
from research.cross_pool.contracts import (
    DTW_BAND_STEPS,
    DTW_GRID_MS,
    DTW_PRIMARY_BAND_STEPS,
    EVENT_BOOTSTRAP_CONFIDENCE_LEVEL,
    EVENT_BOOTSTRAP_RESAMPLES,
    EVENT_BOOTSTRAP_SEED,
    EVENT_RESPONSE_HORIZONS_MS,
    EVENT_SHOCK_CLUSTER_MS,
    EVENT_SHOCK_LOOKBACK_MS,
    EVENT_SHOCK_THRESHOLD_BPS,
    PREDICTIVE_BOOTSTRAP_CONFIDENCE_LEVEL,
    PREDICTIVE_BOOTSTRAP_RESAMPLES,
    PREDICTIVE_BOOTSTRAP_SEED,
    PREDICTIVE_CONDITIONAL_MOVE_THRESHOLD_BPS,
    PREDICTIVE_MIN_CONDITIONAL_TARGET_DAYS,
    PREDICTIVE_MIN_TARGET_DAYS,
    PREDICTIVE_NULL_DIRECTIONAL_GAIN_UPPER_BOUND,
    PREDICTIVE_NULL_MAE_UPPER_BOUND_BPS,
    PREDICTIVE_POSITIVE_ACCURACY_LOWER_BOUND,
    AdequacyAudit,
    ConfidenceInterval,
    CrossPoolContractError,
    Direction,
    DirectionalPredictiveAudit,
    DtwStability,
    EvidenceClass,
    ImprovementSign,
    InfluenceReport,
    OmissionResult,
    PredictiveBootstrap,
    PredictiveInference,
    PredictiveMetrics,
    Regime,
    RegimeSensitivity,
    RegimeSubsetInference,
    UnavailableReason,
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
QaStatus: TypeAlias = Literal["pass", "blocked"]
GeneratedArtifactStatus: TypeAlias = Literal[
    "generated_unreviewed",
    "qa_blocked",
]
RobustnessFlag: TypeAlias = Literal[
    "underpowered",
    "unit_dependent",
    "regime_unstable",
    "regime_not_adjudicable",
    "dtw_band_unstable",
]
ArticleBranch: TypeAlias = Literal[
    "bsc_to_base_incremental",
    "base_to_bsc_incremental",
    "bidirectional_incremental_no_unique_leader",
    "no_material_incremental_lead",
    "leadership_unresolved",
    "not_adjudicable_qa",
]
EconomicClass: TypeAlias = Literal[
    "unavailable",
    "pareto_improvement",
    "return_risk_tradeoff",
    "no_net_return_improvement",
    "not_adjudicable_qa",
]
EconomicInvalidReason: TypeAlias = Literal[
    "PREDICTION_ARTIFACT_HASH_MISMATCH",
    "PREDICTION_ROWS_INVALID",
    "FORECAST_COVERAGE_INVALID",
    "ENTRY_STATE_CONTRACT_MISMATCH",
    "PARAMETER_CONTRACT_MISMATCH",
    "ECONOMIC_INPUT_INVALID",
]
QaReasonCode: TypeAlias = Literal[
    "FEATURE_BASE_INVALID",
    "FEATURE_BSC_INVALID",
    "REPLAY_BASE_INVALID",
    "REPLAY_BSC_INVALID",
    "LEDGER_BASE_COVERAGE_INVALID",
    "LEDGER_BSC_COVERAGE_INVALID",
    "CAUSAL_ALIGNMENT_INVALID",
    "ANALYSIS_CONTRACT_INVALID",
]

_SCHEMA_VERSION = "2.0.0"
_SCHEMA_PATH = Path(__file__).with_name("article_manifest.schema.json")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_BLOCK_HASH_PATTERN = re.compile(r"0x[0-9a-f]{64}\Z")
_POOL_ID_PATTERN = re.compile(r"0x[0-9a-f]{64}\Z")
_PRIMARY_INPUT_NAMES = (
    "base_features",
    "bsc_features",
    "base_replay",
    "bsc_replay",
    "base_ledger",
    "bsc_ledger",
)
_ARTICLE_MANIFEST_SOURCE_PATH = (
    "research/results/cross_pool_lead_lag/article_manifest.json"
)
_EVIDENCE_EDITORIAL_KEYS = (
    "CPL_EDITORIAL_STATUS",
    "CPL_PRIMARY_CLASS",
    "CPL_REVERSE_CLASS",
    "CPL_ARTICLE_BRANCH",
    "CPL_ECONOMIC_CLASS",
    "CPL_ROBUSTNESS_STATUS",
    "CPL_ROBUSTNESS_FLAGS",
    "CPL_SOURCE_MANIFEST",
)
_EVIDENCE_PROVENANCE_KEYS = (
    "CPL_MANIFEST_SHA256",
    "CPL_REVIEWED_BY",
    "CPL_REVIEWED_AT_UTC",
    "CPL_CODE_COMMIT",
    "CPL_SCHEMA_VERSION",
    "CPL_SOURCE_DIFF_SHA256",
)
_MISSING_INPUT_REASON_CODES: Mapping[str, QaReasonCode] = {
    "base_features": "FEATURE_BASE_INVALID",
    "bsc_features": "FEATURE_BSC_INVALID",
    "base_replay": "REPLAY_BASE_INVALID",
    "bsc_replay": "REPLAY_BSC_INVALID",
    "base_ledger": "LEDGER_BASE_COVERAGE_INVALID",
    "bsc_ledger": "LEDGER_BSC_COVERAGE_INVALID",
}
_EVIDENCE_INPUT_KEYS = frozenset(
    key
    for input_name in _PRIMARY_INPUT_NAMES
    for key in (
        f"CPL_INPUT_SHA256_{input_name.upper()}",
        f"CPL_INPUT_MISSING_{input_name.upper()}",
    )
)
_EVIDENCE_KEYS = frozenset(
    (*_EVIDENCE_EDITORIAL_KEYS, *_EVIDENCE_PROVENANCE_KEYS)
) | _EVIDENCE_INPUT_KEYS
_EVIDENCE_LINE_PATTERN = re.compile(
    r"(?P<key>CPL_[A-Z0-9_]+): (?P<value>\S(?:.*\S)?)\Z"
)
_FIGURE_NAMES = (
    "price_gap",
    "event_response",
    "dtw_lag",
    "diagnostic_predictive_performance",
    "lp_performance",
)
_STATISTICAL_ARTIFACT_NAMES = (
    "data_quality.json",
    "market_structure.json",
    "panel_15m.csv",
    "panel_1h.csv",
    "panel_4h.csv",
    "predictive_predictions.csv",
    "predictive_metrics.json",
    "event_study.csv",
    "dtw_weekly_paths.csv",
    "dtw_nulls.csv",
    "statistical_report.md",
    "price_gap.png",
    "predictive_performance.png",
    "event_response.png",
    "dtw_lag.png",
)
_ECONOMIC_ARTIFACT_NAMES = (
    "frozen_policy_windows.csv",
    "frozen_policy_summary.csv",
    "frozen_policy_exclusions.csv",
    "frozen_policy_report.md",
    "lp_performance.png",
)
_ENTRY_STATE_CONTRACT_SHA256 = (
    "61d9f7977c9806bf25c01a3f2db9bc5ecb56bb7612fcc6c72bf68a94a31f118b"
)
_PARAMETER_CONTRACT_SHA256 = (
    "abe886b69c909010a1e1a27eb16d740448cde2993c2a5ec7835e1b03a864888f"
)
_ECONOMIC_WINDOW_COUNT = 23
_ECONOMIC_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_ECONOMIC_INVALID_REASONS = frozenset(
    (
        "PREDICTION_ARTIFACT_HASH_MISMATCH",
        "PREDICTION_ROWS_INVALID",
        "FORECAST_COVERAGE_INVALID",
        "ENTRY_STATE_CONTRACT_MISMATCH",
        "PARAMETER_CONTRACT_MISMATCH",
        "ECONOMIC_INPUT_INVALID",
    )
)
_QA_REASON_CODES = frozenset(
    (
        "FEATURE_BASE_INVALID",
        "FEATURE_BSC_INVALID",
        "REPLAY_BASE_INVALID",
        "REPLAY_BSC_INVALID",
        "LEDGER_BASE_COVERAGE_INVALID",
        "LEDGER_BSC_COVERAGE_INVALID",
        "CAUSAL_ALIGNMENT_INVALID",
        "ANALYSIS_CONTRACT_INVALID",
    )
)

_ROBUSTNESS_FLAGS = frozenset(
    (
        "underpowered",
        "unit_dependent",
        "regime_unstable",
        "regime_not_adjudicable",
        "dtw_band_unstable",
    )
)
_ARTICLE_BRANCHES = frozenset(
    (
        "bsc_to_base_incremental",
        "base_to_bsc_incremental",
        "bidirectional_incremental_no_unique_leader",
        "no_material_incremental_lead",
        "leadership_unresolved",
        "not_adjudicable_qa",
    )
)
_DIRECTIONAL_CLAIMS = {
    "bsc_to_base_incremental": "aggregate_bsc_to_base_incremental_evidence",
    "base_to_bsc_incremental": "aggregate_base_to_bsc_incremental_evidence",
    "bidirectional_incremental_no_unique_leader": (
        "aggregate_bidirectional_incremental_evidence_without_unique_leader"
    ),
    "no_material_incremental_lead": "aggregate_no_material_incremental_lead",
    "leadership_unresolved": "aggregate_leadership_unresolved",
}
_PERMANENTLY_FORBIDDEN_CLAIMS = (
    "causal_price_discovery",
    "toxic_flow_attribution",
    "external_lp_profitability",
    "deployable_alpha",
    "operational_signal_coefficients_leverage_or_sizing",
)


@dataclass(frozen=True)
class InputFileProvenance:
    sha256: str
    rows: int
    first_block: int
    last_block: int
    first_timestamp_ms: int
    last_timestamp_ms: int

    def __post_init__(self) -> None:
        _require_sha256(self.sha256, "input file")
        if isinstance(self.rows, bool) or self.rows <= 0:
            raise CrossPoolContractError("input file rows must be positive")
        if (
            isinstance(self.first_block, bool)
            or isinstance(self.last_block, bool)
            or self.first_block <= 0
            or self.last_block < self.first_block
        ):
            raise CrossPoolContractError(
                "input file block interval must be positive and ordered"
            )
        if (
            isinstance(self.first_timestamp_ms, bool)
            or isinstance(self.last_timestamp_ms, bool)
            or self.first_timestamp_ms <= 0
            or self.last_timestamp_ms < self.first_timestamp_ms
        ):
            raise CrossPoolContractError(
                "input file interval must be a positive ordered UTC interval"
            )


@dataclass(frozen=True)
class RunConfiguration:
    panel_horizons_ms: tuple[int, ...] = EVENT_RESPONSE_HORIZONS_MS
    base_transition_block: int = 45_848_255
    bsc_transition_block: int = 97_799_490
    walk_forward_initial_train_days: int = 14
    walk_forward_refit_weekday: int = 0
    walk_forward_maximum_condition_number: float = 1e12
    predictive_bootstrap_resamples: int = PREDICTIVE_BOOTSTRAP_RESAMPLES
    predictive_bootstrap_seed: int = PREDICTIVE_BOOTSTRAP_SEED
    predictive_bootstrap_confidence_level: float = (
        PREDICTIVE_BOOTSTRAP_CONFIDENCE_LEVEL
    )
    predictive_min_target_days: int = PREDICTIVE_MIN_TARGET_DAYS
    predictive_min_conditional_target_days: int = (
        PREDICTIVE_MIN_CONDITIONAL_TARGET_DAYS
    )
    predictive_conditional_move_threshold_bps: float = (
        PREDICTIVE_CONDITIONAL_MOVE_THRESHOLD_BPS
    )
    predictive_positive_accuracy_lower_bound: float = (
        PREDICTIVE_POSITIVE_ACCURACY_LOWER_BOUND
    )
    predictive_null_mae_upper_bound_bps: float = (
        PREDICTIVE_NULL_MAE_UPPER_BOUND_BPS
    )
    predictive_null_directional_gain_upper_bound: float = (
        PREDICTIVE_NULL_DIRECTIONAL_GAIN_UPPER_BOUND
    )
    event_shock_lookback_ms: int = EVENT_SHOCK_LOOKBACK_MS
    event_shock_threshold_bps: float = EVENT_SHOCK_THRESHOLD_BPS
    event_shock_cluster_ms: int = EVENT_SHOCK_CLUSTER_MS
    event_response_horizons_ms: tuple[int, ...] = EVENT_RESPONSE_HORIZONS_MS
    event_bootstrap_resamples: int = EVENT_BOOTSTRAP_RESAMPLES
    event_bootstrap_seed: int = EVENT_BOOTSTRAP_SEED
    event_bootstrap_confidence_level: float = EVENT_BOOTSTRAP_CONFIDENCE_LEVEL
    dtw_grid_ms: int = DTW_GRID_MS
    dtw_band_steps: tuple[int, ...] = DTW_BAND_STEPS
    dtw_primary_band_steps: int = DTW_PRIMARY_BAND_STEPS

    def __post_init__(self) -> None:
        expected = _frozen_run_configuration_values()
        actual = _run_configuration_json(self)
        if actual != expected:
            raise CrossPoolContractError("run configuration must match the frozen design")


@dataclass(frozen=True)
class RuntimeEnvironment:
    python_version: str
    numpy_version: str
    numpy_build_sha256: str
    machine_architecture: str
    byte_order: Literal["little", "big"]
    bit_generator: Literal["PCG64"]
    draw_dtype: Literal["int64"]
    matplotlib_version: str
    matplotlib_backend: Literal["Agg"]
    freetype_version: str
    font_name: str
    font_file_sha256: str
    figure_rcparams_sha256: str

    def __post_init__(self) -> None:
        for label, value in (
            ("python_version", self.python_version),
            ("numpy_version", self.numpy_version),
            ("machine_architecture", self.machine_architecture),
            ("matplotlib_version", self.matplotlib_version),
            ("freetype_version", self.freetype_version),
            ("font_name", self.font_name),
        ):
            if not isinstance(value, str) or not value.strip():
                raise CrossPoolContractError(f"runtime {label} must be nonempty")
        for label, value in (
            ("numpy_build", self.numpy_build_sha256),
            ("font_file", self.font_file_sha256),
            ("figure_rcparams", self.figure_rcparams_sha256),
        ):
            _require_sha256(value, f"runtime {label}")
        if self.byte_order not in ("little", "big"):
            raise CrossPoolContractError("runtime byte_order is unsupported")
        if self.bit_generator != "PCG64" or self.draw_dtype != "int64":
            raise CrossPoolContractError(
                "runtime bootstrap generator and draw dtype must remain frozen"
            )
        if self.matplotlib_backend != "Agg":
            raise CrossPoolContractError("runtime Matplotlib backend must be Agg")


@dataclass(frozen=True)
class RunProvenance:
    code_commit: str
    source_diff_sha256: str
    base_features: InputFileProvenance | None
    bsc_features: InputFileProvenance | None
    base_replay: InputFileProvenance | None
    bsc_replay: InputFileProvenance | None
    base_ledger: InputFileProvenance | None
    bsc_ledger: InputFileProvenance | None
    base_ledger_coverage: VerifiedLedgerCoverageEvidence | None
    bsc_ledger_coverage: VerifiedLedgerCoverageEvidence | None
    config: RunConfiguration
    runtime: RuntimeEnvironment

    def __post_init__(self) -> None:
        if not _COMMIT_PATTERN.fullmatch(self.code_commit):
            raise CrossPoolContractError("code commit must be 40 lowercase hex characters")
        _require_sha256(self.source_diff_sha256, "source diff")
        for name in _PRIMARY_INPUT_NAMES:
            value = getattr(self, name)
            if value is not None and not isinstance(value, InputFileProvenance):
                raise CrossPoolContractError(
                    f"{name} provenance must use InputFileProvenance"
                )
        _validate_coverage_slot(
            input_file=self.base_ledger,
            replay_file=self.base_replay,
            coverage=self.base_ledger_coverage,
            expected_pool="uni-base",
        )
        _validate_coverage_slot(
            input_file=self.bsc_ledger,
            replay_file=self.bsc_replay,
            coverage=self.bsc_ledger_coverage,
            expected_pool="uni-bsc",
        )


@dataclass(frozen=True)
class QaBlockedManifestInput:
    provenance: RunProvenance
    reason_codes: tuple[QaReasonCode, ...]

    def __post_init__(self) -> None:
        _require_qa_reason_codes(self.reason_codes)


@dataclass(frozen=True)
class PredictiveSensitivityInput:
    primary_15m: PredictiveInference
    reverse_15m: PredictiveInference
    primary_4h: PredictiveInference
    reverse_4h: PredictiveInference

    def __post_init__(self) -> None:
        checks: tuple[tuple[str, PredictiveInference, Direction, int], ...] = (
            ("primary 15-minute", self.primary_15m, "bsc_to_base", 900_000),
            ("reverse 15-minute", self.reverse_15m, "base_to_bsc", 900_000),
            ("primary four-hour", self.primary_4h, "bsc_to_base", 14_400_000),
            ("reverse four-hour", self.reverse_4h, "base_to_bsc", 14_400_000),
        )
        for label, inference, direction, horizon_ms in checks:
            _validate_predictive_inference_identity(
                inference,
                expected_direction=direction,
                expected_horizon_ms=horizon_ms,
                label=label,
            )


@dataclass(frozen=True)
class StatisticalManifestInput:
    provenance: RunProvenance
    primary_predictive_audit: DirectionalPredictiveAudit
    reverse_predictive_audit: DirectionalPredictiveAudit
    primary_dtw_stability: DtwStability
    reverse_dtw_stability: DtwStability
    predictive_sensitivity: PredictiveSensitivityInput
    causal_audit_counts: Mapping[str, object]
    event_study: Mapping[str, object]
    dtw_null_summary: Mapping[str, object]
    market_structure: Mapping[str, object]
    figures: Mapping[str, object]
    artifacts: Mapping[str, object]

    def __post_init__(self) -> None:
        _validate_data_valid_provenance(self.provenance)
        if not isinstance(self.predictive_sensitivity, PredictiveSensitivityInput):
            raise CrossPoolContractError(
                "predictive sensitivity must use PredictiveSensitivityInput"
            )
        for label, value in (
            ("causal audit counts", self.causal_audit_counts),
            ("event study", self.event_study),
            ("DTW null summary", self.dtw_null_summary),
            ("market structure", self.market_structure),
            ("figures", self.figures),
            ("artifacts", self.artifacts),
        ):
            _copy_json_mapping(value, label)


@dataclass(frozen=True)
class RobustnessStatus:
    flags: tuple[RobustnessFlag, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.flags, tuple):
            raise CrossPoolContractError("robustness flags must be an immutable tuple")
        if any(flag not in _ROBUSTNESS_FLAGS for flag in self.flags):
            raise CrossPoolContractError("unsupported robustness flag")
        if self.flags != tuple(sorted(set(self.flags))):
            raise CrossPoolContractError(
                "robustness flags must be sorted and unique"
            )

    @property
    def blocks_directional_claim(self) -> bool:
        return any(flag != "regime_not_adjudicable" for flag in self.flags)


@dataclass(frozen=True)
class EconomicDecisionMetrics:
    inputs_valid: bool
    original_net_return: Decimal
    gated_net_return: Decimal
    original_worst_window_return: Decimal
    gated_worst_window_return: Decimal
    original_worst_drawdown_magnitude: Decimal
    gated_worst_drawdown_magnitude: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.inputs_valid, bool):
            raise CrossPoolContractError("economic inputs_valid must be boolean")
        for label, value in (
            ("original_net_return", self.original_net_return),
            ("gated_net_return", self.gated_net_return),
            (
                "original_worst_window_return",
                self.original_worst_window_return,
            ),
            ("gated_worst_window_return", self.gated_worst_window_return),
            (
                "original_worst_drawdown_magnitude",
                self.original_worst_drawdown_magnitude,
            ),
            (
                "gated_worst_drawdown_magnitude",
                self.gated_worst_drawdown_magnitude,
            ),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise CrossPoolContractError(
                    f"economic {label} must be a finite Decimal"
                )
        if (
            self.original_worst_drawdown_magnitude < 0
            or self.gated_worst_drawdown_magnitude < 0
        ):
            raise CrossPoolContractError(
                "economic drawdown magnitudes must be nonnegative"
            )


@dataclass(frozen=True)
class ForecastCoverage:
    routed_entry_decision_count: int
    forecast_covered_entry_decision_count: int
    maximum_selected_forecast_age_ms: int
    first_routed_entry_timestamp_ms: int
    last_routed_entry_timestamp_ms: int

    def __post_init__(self) -> None:
        integer_values = (
            self.routed_entry_decision_count,
            self.forecast_covered_entry_decision_count,
            self.maximum_selected_forecast_age_ms,
            self.first_routed_entry_timestamp_ms,
            self.last_routed_entry_timestamp_ms,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in integer_values
        ):
            raise CrossPoolContractError(
                "forecast coverage values must be nonnegative integers"
            )
        if self.routed_entry_decision_count <= 0:
            raise CrossPoolContractError(
                "forecast coverage requires at least one routed entry"
            )
        if (
            self.forecast_covered_entry_decision_count
            != self.routed_entry_decision_count
        ):
            raise CrossPoolContractError(
                "every routed entry decision must have forecast coverage"
            )
        if self.maximum_selected_forecast_age_ms >= 3_600_000:
            raise CrossPoolContractError(
                "selected forecast age must remain below one hour"
            )
        if (
            self.first_routed_entry_timestamp_ms <= 0
            or self.last_routed_entry_timestamp_ms
            < self.first_routed_entry_timestamp_ms
        ):
            raise CrossPoolContractError(
                "forecast coverage routed timestamps must be positive and ordered"
            )


@dataclass(frozen=True)
class StrategySummary:
    window_count: int
    active_window_count: int
    positive_window_count: int
    positive_active_window_count: int | None
    aggregate_net_return: Decimal
    worst_window_return: Decimal
    worst_within_window_drawdown_magnitude: Decimal
    total_fees_usd: Decimal
    total_transaction_cost_usd: Decimal
    rebalance_count: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.window_count, bool)
            or not isinstance(self.window_count, int)
            or self.window_count != _ECONOMIC_WINDOW_COUNT
        ):
            raise CrossPoolContractError(
                "economic strategy summary requires 23 evaluation windows"
            )
        if (
            isinstance(self.active_window_count, bool)
            or not isinstance(self.active_window_count, int)
            or not 0 <= self.active_window_count <= self.window_count
            or isinstance(self.rebalance_count, bool)
            or not isinstance(self.rebalance_count, int)
            or self.rebalance_count < 0
        ):
            raise CrossPoolContractError(
                "economic strategy counts are inconsistent"
            )
        if (
            isinstance(self.positive_window_count, bool)
            or not isinstance(self.positive_window_count, int)
            or not 0 <= self.positive_window_count <= self.window_count
        ):
            raise CrossPoolContractError(
                "economic positive-window count is inconsistent"
            )
        if self.active_window_count == 0:
            if self.positive_active_window_count is not None:
                raise CrossPoolContractError(
                    "inactive strategy cannot report positive active windows"
                )
        elif (
            isinstance(self.positive_active_window_count, bool)
            or not isinstance(self.positive_active_window_count, int)
            or not 0
            <= self.positive_active_window_count
            <= self.active_window_count
        ):
            raise CrossPoolContractError(
                "active strategy requires a valid positive-window count"
            )
        if (
            self.positive_active_window_count is not None
            and self.positive_active_window_count != self.positive_window_count
        ):
            raise CrossPoolContractError(
                "positive economic windows must be active windows"
            )
        if self.active_window_count == 0 and self.positive_window_count != 0:
            raise CrossPoolContractError(
                "inactive strategy cannot report positive windows"
            )
        for label, value in (
            ("aggregate net return", self.aggregate_net_return),
            ("worst window return", self.worst_window_return),
            (
                "worst within-window drawdown magnitude",
                self.worst_within_window_drawdown_magnitude,
            ),
            ("total fees", self.total_fees_usd),
            ("total transaction cost", self.total_transaction_cost_usd),
        ):
            _require_finite_decimal(value, f"economic strategy {label}")
        for label, value in (
            (
                "worst within-window drawdown magnitude",
                self.worst_within_window_drawdown_magnitude,
            ),
            ("total fees", self.total_fees_usd),
            ("total transaction cost", self.total_transaction_cost_usd),
        ):
            if value < 0:
                raise CrossPoolContractError(
                    f"economic strategy {label} must be nonnegative"
                )
        if self.active_window_count == 0 and (
            self.aggregate_net_return != 0
            or self.worst_window_return != 0
            or self.worst_within_window_drawdown_magnitude != 0
            or self.total_fees_usd != 0
            or self.total_transaction_cost_usd != 0
            or self.rebalance_count != 0
        ):
            raise CrossPoolContractError(
                "inactive strategy and cash comparator must be an exact zero observation"
            )

    @property
    def all_window_positive_rate(self) -> Decimal:
        return _fixed_decimal_ratio(
            Decimal(self.positive_window_count),
            Decimal(self.window_count),
        )

    @property
    def active_window_positive_rate(self) -> Decimal | None:
        if self.positive_active_window_count is None:
            return None
        return _fixed_decimal_ratio(
            Decimal(self.positive_active_window_count),
            Decimal(self.active_window_count),
        )

    @property
    def fee_to_transaction_cost_ratio(self) -> Decimal | None:
        if self.total_transaction_cost_usd == 0:
            return None
        return _fixed_decimal_ratio(
            self.total_fees_usd,
            self.total_transaction_cost_usd,
        )


@dataclass(frozen=True)
class EconomicStrategySummaries:
    original: StrategySummary
    gated: StrategySummary
    static: StrategySummary
    pool_mark_hold_cngn: StrategySummary
    cash: StrategySummary

    def __post_init__(self) -> None:
        if any(
            not isinstance(summary, StrategySummary)
            for summary in (
                self.original,
                self.gated,
                self.static,
                self.pool_mark_hold_cngn,
                self.cash,
            )
        ):
            raise CrossPoolContractError(
                "economic strategies must use typed strategy summaries"
            )
        if self.original.active_window_count != 4:
            raise CrossPoolContractError(
                "original frozen policy requires four routed active windows"
            )
        if self.gated.active_window_count > self.original.active_window_count:
            raise CrossPoolContractError(
                "gated policy cannot create active windows"
            )
        if self.static.active_window_count != _ECONOMIC_WINDOW_COUNT:
            raise CrossPoolContractError(
                "static comparator must cover every evaluation window"
            )
        if (
            self.pool_mark_hold_cngn.active_window_count
            != _ECONOMIC_WINDOW_COUNT
            or self.pool_mark_hold_cngn.total_fees_usd != 0
            or self.pool_mark_hold_cngn.total_transaction_cost_usd != 0
            or self.pool_mark_hold_cngn.rebalance_count != 0
        ):
            raise CrossPoolContractError(
                "pool-mark hold comparator cannot contain trading activity"
            )
        cash = self.cash
        if (
            cash.active_window_count != 0
            or cash.positive_window_count != 0
            or cash.aggregate_net_return != 0
            or cash.worst_window_return != 0
            or cash.worst_within_window_drawdown_magnitude != 0
            or cash.total_fees_usd != 0
            or cash.total_transaction_cost_usd != 0
            or cash.rebalance_count != 0
        ):
            raise CrossPoolContractError(
                "cash comparator must remain an inactive zero-return observation"
            )


@dataclass(frozen=True)
class EconomicManifestInput:
    entry_state_contract_sha256: str
    parameter_contract_sha256: str
    prediction_sha256: str
    flow_markout_feature_sha256: str
    forecast_coverage: ForecastCoverage
    decision_metrics: EconomicDecisionMetrics
    strategies: EconomicStrategySummaries
    lp_performance_figure: Mapping[str, object]
    artifacts: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_sha256(
            self.entry_state_contract_sha256,
            "economic entry-state fingerprint",
        )
        if self.entry_state_contract_sha256 != _ENTRY_STATE_CONTRACT_SHA256:
            raise CrossPoolContractError(
                "economic entry-state fingerprint does not match the frozen vector"
            )
        _require_sha256(
            self.parameter_contract_sha256,
            "economic parameter fingerprint",
        )
        if self.parameter_contract_sha256 != _PARAMETER_CONTRACT_SHA256:
            raise CrossPoolContractError(
                "economic parameter fingerprint does not match the frozen vector"
            )
        _require_sha256(self.prediction_sha256, "economic prediction artifact")
        _require_sha256(
            self.flow_markout_feature_sha256,
            "economic flow-markout feature input",
        )
        if not isinstance(self.forecast_coverage, ForecastCoverage):
            raise CrossPoolContractError(
                "economic forecast coverage must use ForecastCoverage"
            )
        if not isinstance(self.decision_metrics, EconomicDecisionMetrics):
            raise CrossPoolContractError(
                "economic decision metrics must use EconomicDecisionMetrics"
            )
        if not self.decision_metrics.inputs_valid:
            raise CrossPoolContractError(
                "valid economic merge requires adjudicable inputs"
            )
        if not isinstance(self.strategies, EconomicStrategySummaries):
            raise CrossPoolContractError(
                "economic strategies must use EconomicStrategySummaries"
            )
        _validate_decision_metrics_match(self.decision_metrics, self.strategies)
        _copy_json_mapping(self.lp_performance_figure, "LP performance figure")
        _copy_json_mapping(self.artifacts, "economic artifacts")


@dataclass(frozen=True)
class EconomicInvalidManifestInput:
    reason_codes: tuple[EconomicInvalidReason, ...]

    def __post_init__(self) -> None:
        _require_economic_invalid_reason_codes(self.reason_codes)


def select_article_branch(
    qa_status: QaStatus,
    primary: EvidenceClass,
    reverse: EvidenceClass,
    robustness: RobustnessStatus,
) -> ArticleBranch:
    if qa_status not in ("pass", "blocked"):
        raise CrossPoolContractError("unsupported QA status")
    _require_evidence_class(primary)
    _require_evidence_class(reverse)
    if qa_status == "blocked":
        return "not_adjudicable_qa"
    if robustness.blocks_directional_claim:
        return "leadership_unresolved"
    primary_positive = primary == "positive_evidence"
    reverse_positive = reverse == "positive_evidence"
    if primary_positive and reverse_positive:
        return "bidirectional_incremental_no_unique_leader"
    if primary_positive:
        return "bsc_to_base_incremental"
    if reverse_positive:
        return "base_to_bsc_incremental"
    if primary == reverse == "affirmative_null":
        return "no_material_incremental_lead"
    return "leadership_unresolved"


def derive_robustness_status(
    primary: DirectionalPredictiveAudit,
    reverse: DirectionalPredictiveAudit,
    *,
    primary_dtw: DtwStability,
    reverse_dtw: DtwStability,
) -> RobustnessStatus:
    for label, audit, expected_direction in (
        ("primary", primary, "bsc_to_base"),
        ("reverse", reverse, "base_to_bsc"),
    ):
        if not isinstance(audit, DirectionalPredictiveAudit):
            raise CrossPoolContractError(
                f"{label} predictive audit must use DirectionalPredictiveAudit"
            )
        if audit.direction != expected_direction:
            raise CrossPoolContractError(
                f"{label} direction must be {expected_direction}"
            )
        if audit.horizon_ms != 3_600_000:
            raise CrossPoolContractError(
                f"{label} predictive audit must use the one-hour horizon"
            )
        bootstrap = audit.inference.bootstrap
        if (
            bootstrap.resamples,
            bootstrap.seed,
            bootstrap.confidence_level,
        ) != (
            PREDICTIVE_BOOTSTRAP_RESAMPLES,
            PREDICTIVE_BOOTSTRAP_SEED,
            PREDICTIVE_BOOTSTRAP_CONFIDENCE_LEVEL,
        ):
            raise CrossPoolContractError(
                f"{label} predictive audit must use the frozen bootstrap"
            )

    for label, stability, expected_direction in (
        ("primary", primary_dtw, "bsc_to_base"),
        ("reverse", reverse_dtw, "base_to_bsc"),
    ):
        if not isinstance(stability, DtwStability):
            raise CrossPoolContractError(
                f"{label} DTW stability must use DtwStability"
            )
        if stability.direction != expected_direction:
            raise CrossPoolContractError(
                f"{label} DTW direction must be {expected_direction}"
            )

    flags: set[RobustnessFlag] = set()
    for audit in (primary, reverse):
        if audit.inference.adequacy.underpowered:
            flags.add("underpowered")
        if audit.influence.unit_dependent:
            flags.add("unit_dependent")
        if audit.regime_sensitivity.regime_unstable:
            flags.add("regime_unstable")
        if audit.regime_sensitivity.regime_not_adjudicable:
            flags.add("regime_not_adjudicable")
    if primary_dtw.band_unstable or reverse_dtw.band_unstable:
        flags.add("dtw_band_unstable")
    return RobustnessStatus(flags=tuple(sorted(flags)))


def _validate_predictive_inference_identity(
    inference: PredictiveInference,
    *,
    expected_direction: Literal["bsc_to_base", "base_to_bsc"],
    expected_horizon_ms: int,
    label: str,
) -> None:
    if not isinstance(inference, PredictiveInference):
        raise CrossPoolContractError(
            f"{label} sensitivity must use PredictiveInference"
        )
    for direction, horizon_ms in (
        (inference.metrics.direction, inference.metrics.horizon_ms),
        (inference.bootstrap.direction, inference.bootstrap.horizon_ms),
    ):
        if direction != expected_direction or horizon_ms != expected_horizon_ms:
            raise CrossPoolContractError(
                f"{label} sensitivity direction or horizon is inconsistent"
            )


def claims_for_article_branch(
    branch: ArticleBranch,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if branch not in _ARTICLE_BRANCHES:
        raise CrossPoolContractError("unsupported article branch")
    directional_claims = tuple(sorted(_DIRECTIONAL_CLAIMS.values()))
    if branch == "not_adjudicable_qa":
        return (), tuple(sorted(directional_claims + _PERMANENTLY_FORBIDDEN_CLAIMS))
    allowed = (_DIRECTIONAL_CLAIMS[branch],)
    forbidden = tuple(
        sorted(
            claim
            for claim in directional_claims + _PERMANENTLY_FORBIDDEN_CLAIMS
            if claim not in allowed
        )
    )
    return allowed, forbidden


def select_economic_class(metrics: EconomicDecisionMetrics) -> EconomicClass:
    if not metrics.inputs_valid:
        return "not_adjudicable_qa"
    return_improves = metrics.gated_net_return > metrics.original_net_return
    worst_window_improves = (
        metrics.gated_worst_window_return
        > metrics.original_worst_window_return
    )
    drawdown_improves = (
        metrics.gated_worst_drawdown_magnitude
        < metrics.original_worst_drawdown_magnitude
    )
    risk_is_nonworse = (
        metrics.gated_worst_window_return
        >= metrics.original_worst_window_return
        and metrics.gated_worst_drawdown_magnitude
        <= metrics.original_worst_drawdown_magnitude
    )
    if return_improves and risk_is_nonworse:
        return "pareto_improvement"
    if return_improves or worst_window_improves or drawdown_improves:
        return "return_risk_tradeoff"
    return "no_net_return_improvement"


def canonical_manifest_bytes(payload: Mapping[str, JsonValue]) -> bytes:
    _require_json_value(payload, path="$", allow_mapping=True)
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - recursive guard owns this.
        raise CrossPoolContractError("manifest must contain canonical JSON values") from exc
    return (text + "\n").encode("utf-8")


def predictive_audit_payload(
    audit: DirectionalPredictiveAudit,
) -> dict[str, JsonValue]:
    """Return the canonical manifest representation of a typed audit."""
    return _predictive_audit_json(audit)


def predictive_inference_payload(
    inference: PredictiveInference,
) -> dict[str, JsonValue]:
    """Return the canonical manifest representation of typed inference."""
    return _predictive_inference_json(inference)


def build_article_manifest(
    inputs: StatisticalManifestInput,
    *,
    artifact_status: GeneratedArtifactStatus = "generated_unreviewed",
) -> dict[str, JsonValue]:
    if not isinstance(inputs, StatisticalManifestInput):
        raise CrossPoolContractError(
            "statistical manifest input must use StatisticalManifestInput"
        )
    if artifact_status != "generated_unreviewed":
        raise CrossPoolContractError(
            "generated code cannot mark evidence reviewed"
        )

    robustness = derive_robustness_status(
        inputs.primary_predictive_audit,
        inputs.reverse_predictive_audit,
        primary_dtw=inputs.primary_dtw_stability,
        reverse_dtw=inputs.reverse_dtw_stability,
    )
    branch = select_article_branch(
        "pass",
        inputs.primary_predictive_audit.inference.evidence_class,
        inputs.reverse_predictive_audit.inference.evidence_class,
        robustness,
    )
    allowed_claims, forbidden_claims = claims_for_article_branch(branch)
    artifacts = _copy_json_mapping(inputs.artifacts, "artifacts")
    figures = _copy_json_mapping(inputs.figures, "figures")
    _validate_statistical_artifacts_and_figures(artifacts, figures)

    primary = inputs.primary_predictive_audit
    reverse = inputs.reverse_predictive_audit
    manifest: dict[str, JsonValue] = {
        "schema_version": _SCHEMA_VERSION,
        "artifact_status": "generated_unreviewed",
        "provenance": _run_provenance_json(inputs.provenance),
        "qa": {
            "status": "pass",
            "reasons": [],
            "causal_audit_counts": _copy_json_mapping(
                inputs.causal_audit_counts,
                "causal audit counts",
            ),
        },
        "robustness": {
            "status": "complete",
            "flags": list(robustness.flags),
            "support_counts": {
                "primary_target_day_count": primary.inference.adequacy.target_day_count,
                "primary_conditional_target_day_count": (
                    primary.inference.adequacy.conditional_target_day_count
                ),
                "reverse_target_day_count": reverse.inference.adequacy.target_day_count,
                "reverse_conditional_target_day_count": (
                    reverse.inference.adequacy.conditional_target_day_count
                ),
            },
            "influence_audit": {
                "primary": _influence_audit_summary(primary),
                "reverse": _influence_audit_summary(reverse),
            },
            "regime_audit": {
                "primary": _regime_audit_summary(primary),
                "reverse": _regime_audit_summary(reverse),
            },
        },
        "predictive": {
            "status": "complete",
            "primary": _predictive_audit_json(primary),
            "reverse": _predictive_audit_json(reverse),
            "sensitivity": {
                "panel_15m": {
                    "primary": _predictive_inference_json(
                        inputs.predictive_sensitivity.primary_15m
                    ),
                    "reverse": _predictive_inference_json(
                        inputs.predictive_sensitivity.reverse_15m
                    ),
                },
                "panel_4h": {
                    "primary": _predictive_inference_json(
                        inputs.predictive_sensitivity.primary_4h
                    ),
                    "reverse": _predictive_inference_json(
                        inputs.predictive_sensitivity.reverse_4h
                    ),
                },
            },
        },
        "event_study": _copy_json_mapping(inputs.event_study, "event study"),
        "dtw": {
            "status": "complete",
            "primary": _dtw_stability_json(inputs.primary_dtw_stability),
            "reverse": _dtw_stability_json(inputs.reverse_dtw_stability),
            "null_summary": _copy_json_mapping(
                inputs.dtw_null_summary,
                "DTW null summary",
            ),
        },
        "market_structure": _copy_json_mapping(
            inputs.market_structure,
            "market structure",
        ),
        "economics": {
            "status": "pending",
            "frozen_contract": None,
            "decision_metrics": None,
            "strategies": None,
        },
        "publication": {
            "article_branch": branch,
            "economic_class": "unavailable",
            "allowed_claims": list(allowed_claims),
            "forbidden_claims": list(forbidden_claims),
        },
        "figures": figures,
        "artifacts": artifacts,
        "review": {
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at_utc": None,
        },
    }
    validate_article_manifest(manifest)
    return manifest


def merge_economic_manifest(
    manifest: Mapping[str, JsonValue],
    inputs: EconomicManifestInput,
) -> dict[str, JsonValue]:
    """Merge typed frozen economics without changing statistical evidence."""
    validate_article_manifest(manifest)
    if not isinstance(inputs, EconomicManifestInput):
        raise CrossPoolContractError(
            "economic manifest input must use EconomicManifestInput"
        )
    qa = cast(dict[str, JsonValue], manifest["qa"])
    economics = cast(dict[str, JsonValue], manifest["economics"])
    review = cast(dict[str, JsonValue], manifest["review"])
    if (
        manifest["artifact_status"] != "generated_unreviewed"
        or qa["status"] != "pass"
        or economics["status"] != "pending"
        or review["status"] != "pending"
    ):
        raise CrossPoolContractError(
            "economic merge requires unreviewed QA-pass statistics with economics pending"
        )

    existing_artifacts = cast(dict[str, JsonValue], manifest["artifacts"])
    prediction_digest = existing_artifacts.get("predictive_predictions.csv")
    if prediction_digest != inputs.prediction_sha256:
        raise CrossPoolContractError(
            "economic prediction hash must bind the statistical artifact"
        )
    economic_artifacts = _copy_json_mapping(inputs.artifacts, "economic artifacts")
    if set(economic_artifacts) != set(_ECONOMIC_ARTIFACT_NAMES):
        raise CrossPoolContractError(
            "economic merge requires the frozen economic artifact set"
        )
    if set(economic_artifacts) & set(existing_artifacts):
        raise CrossPoolContractError(
            "economic artifacts cannot replace statistical artifacts"
        )
    for name, digest in economic_artifacts.items():
        if not isinstance(digest, str):
            raise CrossPoolContractError(
                f"economic artifact {name} must contain a SHA-256"
            )
        _require_sha256(digest, f"economic artifact {name}")

    figure = _copy_json_mapping(
        inputs.lp_performance_figure,
        "LP performance figure",
    )
    if (
        set(figure) != {"artifact", "sha256"}
        or figure["artifact"] != "lp_performance.png"
        or figure["sha256"] != economic_artifacts["lp_performance.png"]
    ):
        raise CrossPoolContractError(
            "LP performance figure must bind its economic artifact"
        )

    merged = _plain_manifest_copy(manifest)
    merged["economics"] = {
        "status": "complete",
        "frozen_contract": _frozen_economic_contract_json(inputs),
        "decision_metrics": _economic_decision_metrics_json(
            inputs.decision_metrics
        ),
        "strategies": _economic_strategies_json(inputs.strategies),
    }
    merged_publication = cast(dict[str, JsonValue], merged["publication"])
    merged_publication["economic_class"] = select_economic_class(
        inputs.decision_metrics
    )
    merged_figures = cast(dict[str, JsonValue], merged["figures"])
    merged_figures["lp_performance"] = figure
    merged_artifacts = cast(dict[str, JsonValue], merged["artifacts"])
    merged_artifacts.update(economic_artifacts)
    validate_article_manifest(merged)
    return merged


def merge_economic_invalid_manifest(
    manifest: Mapping[str, JsonValue],
    inputs: EconomicInvalidManifestInput,
) -> dict[str, JsonValue]:
    """Record a typed economic QA failure without fabricating results."""
    validate_article_manifest(manifest)
    if not isinstance(inputs, EconomicInvalidManifestInput):
        raise CrossPoolContractError(
            "invalid economic input must use EconomicInvalidManifestInput"
        )
    qa = cast(dict[str, JsonValue], manifest["qa"])
    economics = cast(dict[str, JsonValue], manifest["economics"])
    review = cast(dict[str, JsonValue], manifest["review"])
    if (
        manifest["artifact_status"] != "generated_unreviewed"
        or qa["status"] != "pass"
        or economics["status"] != "pending"
        or review["status"] != "pending"
    ):
        raise CrossPoolContractError(
            "economic merge requires unreviewed QA-pass statistics with economics pending"
        )

    merged = _plain_manifest_copy(manifest)
    merged["economics"] = {
        "status": "not_adjudicable_qa",
        "reason_codes": list(inputs.reason_codes),
        "frozen_contract": None,
        "decision_metrics": None,
        "strategies": None,
    }
    merged_publication = cast(dict[str, JsonValue], merged["publication"])
    merged_publication["economic_class"] = "not_adjudicable_qa"
    validate_article_manifest(merged)
    return merged


def build_qa_blocked_manifest(
    inputs: QaBlockedManifestInput,
) -> dict[str, JsonValue]:
    if not isinstance(inputs, QaBlockedManifestInput):
        raise CrossPoolContractError(
            "QA-blocked manifest input must use QaBlockedManifestInput"
        )
    allowed_claims, forbidden_claims = claims_for_article_branch(
        "not_adjudicable_qa"
    )
    manifest: dict[str, JsonValue] = {
        "schema_version": _SCHEMA_VERSION,
        "artifact_status": "qa_blocked",
        "provenance": _run_provenance_json(inputs.provenance),
        "qa": {
            "status": "blocked",
            "reasons": list(inputs.reason_codes),
            "causal_audit_counts": None,
        },
        "robustness": {
            "status": "unavailable",
            "flags": [],
            "support_counts": None,
            "influence_audit": None,
            "regime_audit": None,
        },
        "predictive": {
            "status": "unavailable",
            "primary": None,
            "reverse": None,
            "sensitivity": None,
        },
        "event_study": {
            "status": "unavailable",
            "primary": None,
            "reverse": None,
            "exclusion_counts": None,
        },
        "dtw": {
            "status": "unavailable",
            "primary": None,
            "reverse": None,
            "null_summary": None,
        },
        "market_structure": {
            "status": "unavailable",
            "venues": None,
        },
        "economics": {
            "status": "unavailable",
            "frozen_contract": None,
            "decision_metrics": None,
            "strategies": None,
        },
        "publication": {
            "article_branch": "not_adjudicable_qa",
            "economic_class": "not_adjudicable_qa",
            "allowed_claims": list(allowed_claims),
            "forbidden_claims": list(forbidden_claims),
        },
        "figures": {name: None for name in _FIGURE_NAMES},
        "artifacts": {},
        "review": {
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at_utc": None,
        },
    }
    validate_article_manifest(manifest)
    return manifest


def validate_article_manifest(payload: Mapping[str, JsonValue]) -> None:
    if not isinstance(payload, dict):
        raise CrossPoolContractError("manifest schema requires a plain JSON object")
    _require_json_value(payload, path="$", allow_mapping=True)
    errors = sorted(
        _manifest_validator().iter_errors(payload),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    if errors:
        error = errors[0]
        location = "$" + "".join(f"[{part!r}]" for part in error.absolute_path)
        raise CrossPoolContractError(
            f"manifest schema validation failed at {location}"
        ) from error
    _validate_manifest_semantics(payload)


def load_and_validate_article_manifest(path: Path) -> dict[str, JsonValue]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CrossPoolContractError("manifest file could not be read") from exc
    return _load_and_validate_article_manifest_bytes(raw)


def validate_evidence_provenance_block(
    evidence_text: str,
    manifest_path: Path,
) -> None:
    """Verify the durable evidence ledger against one reviewed manifest."""
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise CrossPoolContractError("manifest file could not be read") from exc
    manifest = _load_and_validate_article_manifest_bytes(raw)
    review = cast(dict[str, JsonValue], manifest["review"])
    if (
        manifest["artifact_status"] != "reviewed"
        or review["status"] != "reviewed"
    ):
        raise CrossPoolContractError(
            "evidence provenance requires a reviewed manifest"
        )

    fields = _parse_evidence_provenance_fields(evidence_text)
    for key in (*_EVIDENCE_EDITORIAL_KEYS, *_EVIDENCE_PROVENANCE_KEYS):
        if key not in fields:
            raise CrossPoolContractError(
                f"evidence provenance block is missing required key {key}"
            )

    provenance = cast(dict[str, JsonValue], manifest["provenance"])
    expected_fixed_values = {
        **_expected_evidence_editorial_values(manifest),
        "CPL_MANIFEST_SHA256": hashlib.sha256(raw).hexdigest(),
        "CPL_REVIEWED_BY": cast(str, review["reviewed_by"]),
        "CPL_REVIEWED_AT_UTC": cast(str, review["reviewed_at_utc"]),
        "CPL_CODE_COMMIT": cast(str, provenance["code_commit"]),
        "CPL_SCHEMA_VERSION": cast(str, manifest["schema_version"]),
        "CPL_SOURCE_DIFF_SHA256": cast(
            str,
            provenance["source_diff_sha256"],
        ),
    }
    for key, expected in expected_fixed_values.items():
        if fields[key] != expected:
            raise CrossPoolContractError(
                f"evidence provenance value for {key} does not match manifest"
            )

    input_sha256 = cast(dict[str, JsonValue], provenance["input_sha256"])
    expected_input_keys: set[str] = set()
    for input_name in _PRIMARY_INPUT_NAMES:
        prefix = (
            "CPL_INPUT_MISSING_"
            if input_sha256[input_name] is None
            else "CPL_INPUT_SHA256_"
        )
        expected_input_keys.add(f"{prefix}{input_name.upper()}")
    observed_input_keys = set(fields) & _EVIDENCE_INPUT_KEYS
    if observed_input_keys != expected_input_keys:
        raise CrossPoolContractError(
            "evidence provenance input key sets do not match manifest availability"
        )

    qa = cast(dict[str, JsonValue], manifest["qa"])
    reason_codes = cast(list[JsonValue], qa["reasons"])
    for input_name in _PRIMARY_INPUT_NAMES:
        digest = input_sha256[input_name]
        suffix = input_name.upper()
        if digest is None:
            required_reason = _MISSING_INPUT_REASON_CODES[input_name]
            key = f"CPL_INPUT_MISSING_{suffix}"
            if required_reason not in reason_codes or fields[key] != required_reason:
                raise CrossPoolContractError(
                    f"evidence provenance missing {input_name} requires "
                    f"{required_reason}"
                )
        else:
            key = f"CPL_INPUT_SHA256_{suffix}"
            if fields[key] != digest:
                raise CrossPoolContractError(
                    f"evidence provenance value for {key} does not match manifest"
                )


def _expected_evidence_editorial_values(
    manifest: Mapping[str, JsonValue],
) -> dict[str, str]:
    qa = cast(dict[str, JsonValue], manifest["qa"])
    robustness = cast(dict[str, JsonValue], manifest["robustness"])
    flags = cast(list[str], robustness["flags"])
    expected = {
        "CPL_EDITORIAL_STATUS": "EVIDENCE_REVIEWED",
        "CPL_ROBUSTNESS_STATUS": cast(str, robustness["status"]),
        "CPL_ROBUSTNESS_FLAGS": ",".join(flags) if flags else "NONE",
        "CPL_SOURCE_MANIFEST": _ARTICLE_MANIFEST_SOURCE_PATH,
    }
    if qa["status"] == "blocked":
        expected.update(
            {
                "CPL_PRIMARY_CLASS": "UNAVAILABLE",
                "CPL_REVERSE_CLASS": "UNAVAILABLE",
                "CPL_ARTICLE_BRANCH": "NOT_ADJUDICABLE_QA",
                "CPL_ECONOMIC_CLASS": "NOT_ADJUDICABLE_QA",
            }
        )
        return expected

    predictive = cast(dict[str, JsonValue], manifest["predictive"])
    primary = cast(dict[str, JsonValue], predictive["primary"])
    reverse = cast(dict[str, JsonValue], predictive["reverse"])
    publication = cast(dict[str, JsonValue], manifest["publication"])
    expected.update(
        {
            "CPL_PRIMARY_CLASS": cast(str, primary["evidence_class"]),
            "CPL_REVERSE_CLASS": cast(str, reverse["evidence_class"]),
            "CPL_ARTICLE_BRANCH": cast(str, publication["article_branch"]),
            "CPL_ECONOMIC_CLASS": cast(str, publication["economic_class"]),
        }
    )
    return expected


def _parse_evidence_provenance_fields(evidence_text: str) -> dict[str, str]:
    if not isinstance(evidence_text, str):
        raise CrossPoolContractError("evidence provenance requires text")
    fields: dict[str, str] = {}
    for line in evidence_text.splitlines():
        candidate = line.strip()
        if not candidate.startswith("CPL_"):
            continue
        match = _EVIDENCE_LINE_PATTERN.fullmatch(candidate)
        if match is None:
            raise CrossPoolContractError(
                "evidence provenance block has malformed CPL line"
            )
        key = match.group("key")
        if key in fields:
            raise CrossPoolContractError(
                f"evidence provenance block contains duplicate key {key}"
            )
        if key not in _EVIDENCE_KEYS:
            raise CrossPoolContractError(
                f"evidence provenance block contains unsupported key {key}"
            )
        fields[key] = match.group("value")
    return fields


def _load_and_validate_article_manifest_bytes(
    raw: bytes,
) -> dict[str, JsonValue]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CrossPoolContractError("manifest must use canonical UTF-8 JSON") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except CrossPoolContractError:
        raise
    except json.JSONDecodeError as exc:
        raise CrossPoolContractError("manifest must use canonical JSON") from exc
    if not isinstance(parsed, dict):
        raise CrossPoolContractError("manifest schema requires a JSON object")
    payload = cast(dict[str, JsonValue], parsed)
    if raw != canonical_manifest_bytes(payload):
        raise CrossPoolContractError("manifest bytes are not canonical")
    validate_article_manifest(payload)
    return payload


def _require_evidence_class(value: str) -> None:
    if value not in (
        "positive_evidence",
        "suggestive",
        "affirmative_null",
        "inconclusive",
    ):
        raise CrossPoolContractError("unsupported predictive evidence class")


def _require_json_value(
    value: object,
    *,
    path: str,
    allow_mapping: bool = False,
) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CrossPoolContractError(
                f"manifest JSON number at {path} must be finite"
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        if not allow_mapping and not isinstance(value, dict):
            raise CrossPoolContractError(
                f"manifest JSON object at {path} must be a plain dictionary"
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise CrossPoolContractError(
                    f"manifest JSON object key at {path} must be a string"
                )
            _require_json_value(item, path=f"{path}.{key}")
        return
    raise CrossPoolContractError(
        f"manifest value at {path} is not a supported JSON value"
    )


def _require_qa_reason_codes(reason_codes: object) -> None:
    if not isinstance(reason_codes, tuple) or not reason_codes:
        raise CrossPoolContractError(
            "QA reason codes must be a nonempty immutable tuple"
        )
    if any(not isinstance(code, str) or code not in _QA_REASON_CODES for code in reason_codes):
        raise CrossPoolContractError("unsupported QA reason code")
    if reason_codes != tuple(sorted(set(reason_codes))):
        raise CrossPoolContractError("QA reason codes must be sorted and unique")


def _require_economic_invalid_reason_codes(reason_codes: object) -> None:
    if not isinstance(reason_codes, tuple) or not reason_codes:
        raise CrossPoolContractError(
            "economic invalid reasons must be a nonempty immutable tuple"
        )
    if any(
        not isinstance(code, str) or code not in _ECONOMIC_INVALID_REASONS
        for code in reason_codes
    ):
        raise CrossPoolContractError("unsupported economic invalid reason")
    if reason_codes != tuple(sorted(set(reason_codes))):
        raise CrossPoolContractError(
            "economic invalid reasons must be sorted and unique"
        )


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise CrossPoolContractError(
            f"{label} SHA-256 must be 64 lowercase hex characters"
        )


def _copy_json_mapping(
    value: Mapping[str, object],
    label: str,
) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise CrossPoolContractError(f"manifest {label} must be a mapping")
    plain = dict(value)
    _require_json_value(plain, path=f"$.{label}", allow_mapping=True)
    encoded = json.dumps(
        plain,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):  # pragma: no cover - encoded root is fixed above.
        raise CrossPoolContractError(f"manifest {label} must be a JSON object")
    return cast(dict[str, JsonValue], parsed)


def _validate_data_valid_provenance(provenance: RunProvenance) -> None:
    if not isinstance(provenance, RunProvenance):
        raise CrossPoolContractError(
            "data-valid manifest provenance must use RunProvenance"
        )
    inputs = tuple(getattr(provenance, name) for name in _PRIMARY_INPUT_NAMES)
    if any(item is None for item in inputs):
        raise CrossPoolContractError(
            "data-valid manifest requires all six captured inputs"
        )
    if (
        provenance.base_ledger_coverage is None
        or provenance.bsc_ledger_coverage is None
    ):
        raise CrossPoolContractError(
            "data-valid manifest requires both verified ledger sidecars"
        )
    if (
        provenance.base_ledger_coverage.required_end_timestamp_ms
        != provenance.bsc_ledger_coverage.required_end_timestamp_ms
    ):
        raise CrossPoolContractError(
            "verified ledger sidecars must bind one common replay cutoff"
        )


def _validate_statistical_artifacts_and_figures(
    artifacts: Mapping[str, JsonValue],
    figures: Mapping[str, JsonValue],
) -> None:
    if set(artifacts) != set(_STATISTICAL_ARTIFACT_NAMES):
        raise CrossPoolContractError(
            "statistical manifest requires the frozen artifact set"
        )
    for name, digest in artifacts.items():
        if not isinstance(digest, str):
            raise CrossPoolContractError(
                f"statistical artifact {name} must contain a SHA-256"
            )
        _require_sha256(digest, f"statistical artifact {name}")

    if set(figures) != set(_FIGURE_NAMES):
        raise CrossPoolContractError(
            "statistical manifest requires the frozen figure slots"
        )
    expected_artifacts = {
        "price_gap": "price_gap.png",
        "event_response": "event_response.png",
        "dtw_lag": "dtw_lag.png",
        "diagnostic_predictive_performance": "predictive_performance.png",
    }
    for slot, artifact_name in expected_artifacts.items():
        value = figures[slot]
        if not isinstance(value, dict) or set(value) != {"artifact", "sha256"}:
            raise CrossPoolContractError(
                f"statistical figure {slot} must bind one artifact and hash"
            )
        if (
            value["artifact"] != artifact_name
            or value["sha256"] != artifacts[artifact_name]
        ):
            raise CrossPoolContractError(
                f"statistical figure {slot} does not bind its artifact hash"
            )
    if figures["lp_performance"] is not None:
        raise CrossPoolContractError(
            "LP performance must remain unavailable before economic evaluation"
        )


def _confidence_interval_json(
    interval: ConfidenceInterval | None,
) -> dict[str, JsonValue] | None:
    if interval is None:
        return None
    return {
        "point": interval.point,
        "lower": interval.lower,
        "upper": interval.upper,
    }


def _predictive_inference_json(
    inference: PredictiveInference,
) -> dict[str, JsonValue]:
    metrics = inference.metrics
    bootstrap = inference.bootstrap
    return {
        "metrics": {
            "direction": metrics.direction,
            "horizon_ms": metrics.horizon_ms,
            "rows": metrics.rows,
            "target_day_count": metrics.target_day_count,
            "conditional_rows": metrics.conditional_rows,
            "conditional_target_day_count": metrics.conditional_target_day_count,
            "baseline_mae_bps": metrics.baseline_mae_bps,
            "cross_mae_bps": metrics.cross_mae_bps,
            "mae_improvement_bps": metrics.mae_improvement_bps,
            "baseline_mse_bps2": metrics.baseline_mse_bps2,
            "cross_mse_bps2": metrics.cross_mse_bps2,
            "mse_improvement_bps2": metrics.mse_improvement_bps2,
            "baseline_rmse_bps": metrics.baseline_rmse_bps,
            "cross_rmse_bps": metrics.cross_rmse_bps,
            "relative_oos_r2": metrics.relative_oos_r2,
            "baseline_conditional_directional_accuracy": (
                metrics.baseline_conditional_directional_accuracy
            ),
            "cross_conditional_directional_accuracy": (
                metrics.cross_conditional_directional_accuracy
            ),
            "conditional_directional_accuracy_gain": (
                metrics.conditional_directional_accuracy_gain
            ),
        },
        "bootstrap": {
            "direction": bootstrap.direction,
            "horizon_ms": bootstrap.horizon_ms,
            "resamples": bootstrap.resamples,
            "seed": bootstrap.seed,
            "confidence_level": bootstrap.confidence_level,
            "mae_improvement_bps": _confidence_interval_json(
                bootstrap.mae_improvement_bps
            ),
            "mse_improvement_bps2": _confidence_interval_json(
                bootstrap.mse_improvement_bps2
            ),
            "cross_conditional_directional_accuracy": _confidence_interval_json(
                bootstrap.cross_conditional_directional_accuracy
            ),
            "conditional_directional_accuracy_gain": _confidence_interval_json(
                bootstrap.conditional_directional_accuracy_gain
            ),
            "valid_conditional_resamples": bootstrap.valid_conditional_resamples,
        },
        "adequacy": {
            "target_day_count": inference.adequacy.target_day_count,
            "conditional_target_day_count": (
                inference.adequacy.conditional_target_day_count
            ),
            "underpowered": inference.adequacy.underpowered,
        },
        "evidence_class": inference.evidence_class,
    }


def _regime_subset_json(
    subset: RegimeSubsetInference,
) -> dict[str, JsonValue]:
    return {
        "regime": subset.regime,
        "row_count": subset.row_count,
        "inference": (
            _predictive_inference_json(subset.inference)
            if subset.inference is not None
            else None
        ),
        "unavailable_reason": subset.unavailable_reason,
    }


def _predictive_audit_json(
    audit: DirectionalPredictiveAudit,
) -> dict[str, JsonValue]:
    influence = audit.influence
    regime = audit.regime_sensitivity
    return {
        "direction": audit.direction,
        "horizon_ms": audit.horizon_ms,
        "evidence_class": audit.inference.evidence_class,
        "inference": _predictive_inference_json(audit.inference),
        "influence": {
            "direction": influence.direction,
            "horizon_ms": influence.horizon_ms,
            "full_mae_improvement_sign": influence.full_mae_improvement_sign,
            "full_evidence_class": influence.full_evidence_class,
            "leave_one_day": [
                {
                    "unit": omission.unit,
                    "mae_improvement_sign": omission.mae_improvement_sign,
                    "evidence_class": omission.evidence_class,
                }
                for omission in influence.leave_one_day
            ],
            "leave_one_fold": [
                {
                    "unit": omission.unit,
                    "mae_improvement_sign": omission.mae_improvement_sign,
                    "evidence_class": omission.evidence_class,
                }
                for omission in influence.leave_one_fold
            ],
            "unit_dependent": influence.unit_dependent,
        },
        "regime_sensitivity": {
            "direction": regime.direction,
            "horizon_ms": regime.horizon_ms,
            "early": _regime_subset_json(regime.early),
            "mixed": _regime_subset_json(regime.mixed),
            "late": _regime_subset_json(regime.late),
            "regime_unstable": regime.regime_unstable,
            "regime_not_adjudicable": regime.regime_not_adjudicable,
        },
    }


def _influence_audit_summary(
    audit: DirectionalPredictiveAudit,
) -> dict[str, JsonValue]:
    return {
        "leave_one_day_count": len(audit.influence.leave_one_day),
        "leave_one_fold_count": len(audit.influence.leave_one_fold),
        "unit_dependent": audit.influence.unit_dependent,
    }


def _regime_audit_summary(
    audit: DirectionalPredictiveAudit,
) -> dict[str, JsonValue]:
    regime = audit.regime_sensitivity
    return {
        "early_rows": regime.early.row_count,
        "mixed_rows": regime.mixed.row_count,
        "late_rows": regime.late.row_count,
        "regime_unstable": regime.regime_unstable,
        "regime_not_adjudicable": regime.regime_not_adjudicable,
    }


def _dtw_stability_json(stability: DtwStability) -> dict[str, JsonValue]:
    return {
        "direction": stability.direction,
        "aggregate_median_lag_by_band": [
            {
                "band_steps": band_steps,
                "median_signed_lag_steps": stability.aggregate_median_lag_by_band[
                    band_steps
                ],
            }
            for band_steps in DTW_BAND_STEPS
        ],
        "weekly_median_lags_by_band": [
            {
                "band_steps": band_steps,
                "weeks": [
                    {
                        "week_start_timestamp_ms": week_start_timestamp_ms,
                        "median_signed_lag_steps": lag_steps,
                    }
                    for week_start_timestamp_ms, lag_steps in (
                        stability.weekly_median_lags_by_band[band_steps]
                    )
                ],
            }
            for band_steps in DTW_BAND_STEPS
        ],
        "primary_band_same_sign_week_share": (
            stability.primary_band_same_sign_week_share
        ),
        "band_unstable": stability.band_unstable,
    }


def _validate_coverage_slot(
    *,
    input_file: InputFileProvenance | None,
    replay_file: InputFileProvenance | None,
    coverage: VerifiedLedgerCoverageEvidence | None,
    expected_pool: Literal["uni-base", "uni-bsc"],
) -> None:
    if coverage is None:
        return
    if not isinstance(coverage, VerifiedLedgerCoverageEvidence):
        raise CrossPoolContractError(
            f"{expected_pool} coverage must use verified ledger evidence"
        )
    if input_file is None:
        raise CrossPoolContractError(
            f"{expected_pool} coverage requires captured ledger provenance"
        )
    if replay_file is None:
        raise CrossPoolContractError(
            f"{expected_pool} coverage requires captured replay provenance"
        )
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[expected_pool]
    if (
        coverage.schema_version != LEDGER_COVERAGE_SCHEMA_VERSION
        or coverage.pool != expected_pool
        or coverage.chain != orientation.chain
        or coverage.chain_id != orientation.chain_id
        or coverage.pool_id != orientation.pool_id
        or coverage.covered_start_block != orientation.inception_block
    ):
        raise CrossPoolContractError(
            f"{expected_pool} coverage identity or verification mode is invalid"
        )
    typed_coverage = _as_rpc_coverage(coverage)
    for label, digest in (
        ("sidecar", coverage.sidecar_sha256),
        ("ledger", coverage.ledger_sha256),
        ("attestation", coverage.attestation_sha256),
    ):
        _require_sha256(digest, f"{expected_pool} coverage {label}")
    if hashlib.sha256(rpc_ledger_coverage_bytes(typed_coverage)).hexdigest() != (
        coverage.sidecar_sha256
    ):
        raise CrossPoolContractError(
            f"{expected_pool} coverage sidecar hash does not bind its payload"
        )
    if (
        coverage.ledger_sha256 != input_file.sha256
        or coverage.ledger_rows != input_file.rows
        or coverage.ledger_first_block != input_file.first_block
        or coverage.ledger_last_block != input_file.last_block
    ):
        raise CrossPoolContractError(
            f"{expected_pool} coverage does not bind the captured ledger"
        )
    replay = coverage.evidence.replay_input
    if (
        replay.sha256 != replay_file.sha256
        or replay.row_count != replay_file.rows
        or replay.first_block != replay_file.first_block
        or replay.last_block != replay_file.last_block
        or replay.first_timestamp_ms != replay_file.first_timestamp_ms
        or replay.last_timestamp_ms != replay_file.last_timestamp_ms
    ):
        raise CrossPoolContractError(
            f"{expected_pool} coverage does not bind the captured replay"
        )
    if (
        not _BLOCK_HASH_PATTERN.fullmatch(coverage.covered_start_block_hash)
        or not _BLOCK_HASH_PATTERN.fullmatch(coverage.covered_end_block_hash)
        or not _POOL_ID_PATTERN.fullmatch(coverage.pool_id)
    ):
        raise CrossPoolContractError(
            f"{expected_pool} coverage contains a noncanonical chain identifier"
        )
    integer_fields = (
        coverage.covered_start_block,
        coverage.covered_start_timestamp_ms,
        coverage.covered_end_block,
        coverage.covered_end_timestamp_ms,
        coverage.required_end_block,
        coverage.required_end_timestamp_ms,
        coverage.ledger_rows,
        coverage.ledger_first_block,
        coverage.ledger_last_block,
    )
    if any(isinstance(value, bool) or value < 0 for value in integer_fields):
        raise CrossPoolContractError(
            f"{expected_pool} coverage numeric fields must be nonnegative integers"
        )
    if (
        coverage.covered_end_block < coverage.covered_start_block
        or coverage.required_end_block < coverage.covered_start_block
        or coverage.required_end_block > coverage.covered_end_block
        or coverage.covered_end_timestamp_ms
        < coverage.covered_start_timestamp_ms
        or coverage.required_end_timestamp_ms
        < coverage.covered_start_timestamp_ms
        or coverage.required_end_timestamp_ms
        > coverage.covered_end_timestamp_ms
    ):
        raise CrossPoolContractError(
            f"{expected_pool} coverage does not bracket the required cutoff"
        )


def _as_rpc_coverage(
    coverage: VerifiedLedgerCoverageEvidence,
) -> RpcLedgerCoverage:
    return RpcLedgerCoverage(
        schema_version=cast(Literal["2.0.0"], coverage.schema_version),
        verification_mode="rpc_verified",
        pool=coverage.pool,
        chain=coverage.chain,
        chain_id=coverage.chain_id,
        pool_id=coverage.pool_id,
        covered_start_block=coverage.covered_start_block,
        covered_start_block_hash=coverage.covered_start_block_hash,
        covered_start_timestamp_ms=coverage.covered_start_timestamp_ms,
        covered_end_block=coverage.covered_end_block,
        covered_end_block_hash=coverage.covered_end_block_hash,
        covered_end_timestamp_ms=coverage.covered_end_timestamp_ms,
        ledger_sha256=coverage.ledger_sha256,
        ledger_rows=coverage.ledger_rows,
        ledger_first_block=coverage.ledger_first_block,
        ledger_last_block=coverage.ledger_last_block,
        evidence=coverage.evidence,
        attestation_sha256=coverage.attestation_sha256,
    )


def _rpc_coverage_from_manifest_json(
    coverage: Mapping[str, JsonValue],
) -> RpcLedgerCoverage:
    payload = dict(coverage)
    for consumer_field in (
        "sidecar_sha256",
        "required_end_block",
        "required_end_timestamp_ms",
    ):
        payload.pop(consumer_field, None)
    pool = payload.get("pool")
    if pool not in ("uni-base", "uni-bsc"):
        raise CrossPoolContractError("manifest ledger coverage pool is invalid")
    return rpc_ledger_coverage_from_payload(
        cast(Literal["uni-base", "uni-bsc"], pool),
        cast(Mapping[str, object], payload),
    )


def _run_provenance_json(provenance: RunProvenance) -> dict[str, JsonValue]:
    if not isinstance(provenance, RunProvenance):
        raise CrossPoolContractError("manifest provenance must use RunProvenance")
    input_files = {
        name: cast(InputFileProvenance | None, getattr(provenance, name))
        for name in _PRIMARY_INPUT_NAMES
    }
    return {
        "code_commit": provenance.code_commit,
        "source_diff_sha256": provenance.source_diff_sha256,
        "input_sha256": {
            name: item.sha256 if item is not None else None
            for name, item in input_files.items()
        },
        "input_intervals": {
            name: (
                {
                    "rows": item.rows,
                    "first_block": item.first_block,
                    "last_block": item.last_block,
                    "first_timestamp_ms": item.first_timestamp_ms,
                    "last_timestamp_ms": item.last_timestamp_ms,
                }
                if item is not None
                else None
            )
            for name, item in input_files.items()
        },
        "ledger_coverage": {
            "base_ledger": _ledger_coverage_json(provenance.base_ledger_coverage),
            "bsc_ledger": _ledger_coverage_json(provenance.bsc_ledger_coverage),
        },
        "config": _run_configuration_json(provenance.config),
        "runtime": _runtime_environment_json(provenance.runtime),
    }


def _ledger_coverage_json(
    coverage: VerifiedLedgerCoverageEvidence | None,
) -> dict[str, JsonValue] | None:
    if coverage is None:
        return None
    payload = _copy_json_mapping(
        rpc_ledger_coverage_payload(_as_rpc_coverage(coverage)),
        "ledger coverage",
    )
    payload["sidecar_sha256"] = coverage.sidecar_sha256
    payload["required_end_block"] = coverage.required_end_block
    payload["required_end_timestamp_ms"] = coverage.required_end_timestamp_ms
    return payload


def _run_configuration_json(config: RunConfiguration) -> dict[str, JsonValue]:
    return {
        "panel_horizons_ms": list(config.panel_horizons_ms),
        "base_transition_block": config.base_transition_block,
        "bsc_transition_block": config.bsc_transition_block,
        "walk_forward_initial_train_days": config.walk_forward_initial_train_days,
        "walk_forward_refit_weekday": config.walk_forward_refit_weekday,
        "walk_forward_maximum_condition_number": (
            config.walk_forward_maximum_condition_number
        ),
        "predictive_bootstrap_resamples": config.predictive_bootstrap_resamples,
        "predictive_bootstrap_seed": config.predictive_bootstrap_seed,
        "predictive_bootstrap_confidence_level": (
            config.predictive_bootstrap_confidence_level
        ),
        "predictive_min_target_days": config.predictive_min_target_days,
        "predictive_min_conditional_target_days": (
            config.predictive_min_conditional_target_days
        ),
        "predictive_conditional_move_threshold_bps": (
            config.predictive_conditional_move_threshold_bps
        ),
        "predictive_positive_accuracy_lower_bound": (
            config.predictive_positive_accuracy_lower_bound
        ),
        "predictive_null_mae_upper_bound_bps": (
            config.predictive_null_mae_upper_bound_bps
        ),
        "predictive_null_directional_gain_upper_bound": (
            config.predictive_null_directional_gain_upper_bound
        ),
        "event_shock_lookback_ms": config.event_shock_lookback_ms,
        "event_shock_threshold_bps": config.event_shock_threshold_bps,
        "event_shock_cluster_ms": config.event_shock_cluster_ms,
        "event_response_horizons_ms": list(config.event_response_horizons_ms),
        "event_bootstrap_resamples": config.event_bootstrap_resamples,
        "event_bootstrap_seed": config.event_bootstrap_seed,
        "event_bootstrap_confidence_level": config.event_bootstrap_confidence_level,
        "dtw_grid_ms": config.dtw_grid_ms,
        "dtw_band_steps": list(config.dtw_band_steps),
        "dtw_primary_band_steps": config.dtw_primary_band_steps,
    }


def _frozen_run_configuration_values() -> dict[str, JsonValue]:
    return {
        "panel_horizons_ms": list(EVENT_RESPONSE_HORIZONS_MS),
        "base_transition_block": 45_848_255,
        "bsc_transition_block": 97_799_490,
        "walk_forward_initial_train_days": 14,
        "walk_forward_refit_weekday": 0,
        "walk_forward_maximum_condition_number": 1e12,
        "predictive_bootstrap_resamples": PREDICTIVE_BOOTSTRAP_RESAMPLES,
        "predictive_bootstrap_seed": PREDICTIVE_BOOTSTRAP_SEED,
        "predictive_bootstrap_confidence_level": (
            PREDICTIVE_BOOTSTRAP_CONFIDENCE_LEVEL
        ),
        "predictive_min_target_days": PREDICTIVE_MIN_TARGET_DAYS,
        "predictive_min_conditional_target_days": (
            PREDICTIVE_MIN_CONDITIONAL_TARGET_DAYS
        ),
        "predictive_conditional_move_threshold_bps": (
            PREDICTIVE_CONDITIONAL_MOVE_THRESHOLD_BPS
        ),
        "predictive_positive_accuracy_lower_bound": (
            PREDICTIVE_POSITIVE_ACCURACY_LOWER_BOUND
        ),
        "predictive_null_mae_upper_bound_bps": PREDICTIVE_NULL_MAE_UPPER_BOUND_BPS,
        "predictive_null_directional_gain_upper_bound": (
            PREDICTIVE_NULL_DIRECTIONAL_GAIN_UPPER_BOUND
        ),
        "event_shock_lookback_ms": EVENT_SHOCK_LOOKBACK_MS,
        "event_shock_threshold_bps": EVENT_SHOCK_THRESHOLD_BPS,
        "event_shock_cluster_ms": EVENT_SHOCK_CLUSTER_MS,
        "event_response_horizons_ms": list(EVENT_RESPONSE_HORIZONS_MS),
        "event_bootstrap_resamples": EVENT_BOOTSTRAP_RESAMPLES,
        "event_bootstrap_seed": EVENT_BOOTSTRAP_SEED,
        "event_bootstrap_confidence_level": EVENT_BOOTSTRAP_CONFIDENCE_LEVEL,
        "dtw_grid_ms": DTW_GRID_MS,
        "dtw_band_steps": list(DTW_BAND_STEPS),
        "dtw_primary_band_steps": DTW_PRIMARY_BAND_STEPS,
    }


def _runtime_environment_json(runtime: RuntimeEnvironment) -> dict[str, JsonValue]:
    return {
        "python_version": runtime.python_version,
        "numpy_version": runtime.numpy_version,
        "numpy_build_sha256": runtime.numpy_build_sha256,
        "machine_architecture": runtime.machine_architecture,
        "byte_order": runtime.byte_order,
        "bit_generator": runtime.bit_generator,
        "draw_dtype": runtime.draw_dtype,
        "matplotlib_version": runtime.matplotlib_version,
        "matplotlib_backend": runtime.matplotlib_backend,
        "freetype_version": runtime.freetype_version,
        "font_name": runtime.font_name,
        "font_file_sha256": runtime.font_file_sha256,
        "figure_rcparams_sha256": runtime.figure_rcparams_sha256,
    }


@lru_cache(maxsize=1)
def _manifest_validator() -> Draft202012Validator:
    try:
        schema_raw = _SCHEMA_PATH.read_text(encoding="utf-8")
        schema = json.loads(
            schema_raw,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise CrossPoolContractError("article manifest schema is unavailable or invalid") from exc
    if not isinstance(schema, dict):
        raise CrossPoolContractError("article manifest schema root must be an object")
    return Draft202012Validator(schema)


def _validate_manifest_semantics(payload: Mapping[str, JsonValue]) -> None:
    provenance = cast(dict[str, JsonValue], payload["provenance"])
    _validate_input_provenance_semantics(provenance)
    qa = cast(dict[str, JsonValue], payload["qa"])
    publication = cast(dict[str, JsonValue], payload["publication"])
    if qa["status"] == "blocked":
        reason_values = cast(list[JsonValue], qa["reasons"])
        _require_qa_reason_codes(
            tuple(cast(str, value) for value in reason_values)
        )
        allowed, forbidden = claims_for_article_branch("not_adjudicable_qa")
        if (
            publication["article_branch"] != "not_adjudicable_qa"
            or publication["economic_class"] != "not_adjudicable_qa"
            or publication["allowed_claims"] != list(allowed)
            or publication["forbidden_claims"] != list(forbidden)
        ):
            raise CrossPoolContractError(
                "manifest publication decisions do not match blocked QA evidence"
            )
    else:
        _validate_data_valid_manifest_semantics(payload, provenance, publication)

    review = cast(dict[str, JsonValue], payload["review"])
    if review["status"] == "reviewed":
        reviewed_at = cast(str, review["reviewed_at_utc"])
        try:
            parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CrossPoolContractError(
                "manifest reviewed_at_utc must be a valid UTC timestamp"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise CrossPoolContractError(
                "manifest reviewed_at_utc must use UTC"
            )


def _validate_data_valid_manifest_semantics(
    payload: Mapping[str, JsonValue],
    provenance: Mapping[str, JsonValue],
    publication: Mapping[str, JsonValue],
) -> None:
    input_sha256 = cast(dict[str, JsonValue], provenance["input_sha256"])
    input_intervals = cast(dict[str, JsonValue], provenance["input_intervals"])
    if any(
        input_sha256[name] is None or input_intervals[name] is None
        for name in _PRIMARY_INPUT_NAMES
    ):
        raise CrossPoolContractError(
            "data-valid manifest requires all six captured inputs"
        )
    coverage_group = cast(dict[str, JsonValue], provenance["ledger_coverage"])
    base_coverage = cast(dict[str, JsonValue], coverage_group["base_ledger"])
    bsc_coverage = cast(dict[str, JsonValue], coverage_group["bsc_ledger"])
    if not isinstance(base_coverage, dict) or not isinstance(bsc_coverage, dict):
        raise CrossPoolContractError(
            "data-valid manifest requires both verified ledger sidecars"
        )
    common_replay_cutoff_ms = cast(
        int,
        base_coverage["required_end_timestamp_ms"],
    )
    if cast(int, bsc_coverage["required_end_timestamp_ms"]) != common_replay_cutoff_ms:
        raise CrossPoolContractError(
            "verified ledger sidecars must bind one common replay cutoff"
        )
    market_structure = cast(dict[str, JsonValue], payload["market_structure"])
    venues = cast(list[JsonValue], market_structure["venues"])
    if any(
        cast(dict[str, JsonValue], venue)["activity_end_timestamp_ms"]
        != common_replay_cutoff_ms
        for venue in venues
    ):
        raise CrossPoolContractError(
            "market structure must use the verified common replay cutoff"
        )

    predictive = cast(dict[str, JsonValue], payload["predictive"])
    primary = cast(dict[str, JsonValue], predictive["primary"])
    reverse = cast(dict[str, JsonValue], predictive["reverse"])
    primary_class = _validate_serialized_predictive_audit(
        primary,
        expected_direction="bsc_to_base",
    )
    reverse_class = _validate_serialized_predictive_audit(
        reverse,
        expected_direction="base_to_bsc",
    )
    sensitivity = cast(dict[str, JsonValue], predictive["sensitivity"])
    for panel_name, horizon_ms in (
        ("panel_15m", 900_000),
        ("panel_4h", 14_400_000),
    ):
        pair = cast(dict[str, JsonValue], sensitivity[panel_name])
        _validate_serialized_predictive_inference_identity(
            cast(dict[str, JsonValue], pair["primary"]),
            expected_direction="bsc_to_base",
            expected_horizon_ms=horizon_ms,
        )
        _validate_serialized_predictive_inference_identity(
            cast(dict[str, JsonValue], pair["reverse"]),
            expected_direction="base_to_bsc",
            expected_horizon_ms=horizon_ms,
        )
    robustness = cast(dict[str, JsonValue], payload["robustness"])
    flag_values = cast(list[JsonValue], robustness["flags"])
    flags = tuple(cast(RobustnessFlag, value) for value in flag_values)
    status = RobustnessStatus(flags=flags)
    expected_flags = _derive_serialized_robustness_flags(payload)
    if status.flags != expected_flags:
        raise CrossPoolContractError(
            "manifest robustness flags do not match serialized audits"
        )

    branch = select_article_branch(
        "pass",
        primary_class,
        reverse_class,
        status,
    )
    allowed, forbidden = claims_for_article_branch(branch)
    economics = cast(dict[str, JsonValue], payload["economics"])
    if economics["status"] == "pending":
        expected_economic_class: EconomicClass = "unavailable"
    elif economics["status"] == "not_adjudicable_qa":
        reason_values = cast(list[JsonValue], economics["reason_codes"])
        _require_economic_invalid_reason_codes(
            tuple(cast(str, value) for value in reason_values)
        )
        expected_economic_class = "not_adjudicable_qa"
    else:
        _validate_economic_complete_semantics(economics)
        metrics = cast(dict[str, JsonValue], economics["decision_metrics"])
        expected_economic_class = select_economic_class(
            _economic_decision_metrics_from_json(metrics)
        )
    if (
        publication["article_branch"] != branch
        or publication["economic_class"] != expected_economic_class
        or publication["allowed_claims"] != list(allowed)
        or publication["forbidden_claims"] != list(forbidden)
    ):
        raise CrossPoolContractError(
            "manifest publication decisions do not match serialized evidence"
        )
    _validate_manifest_artifact_bindings(payload, economics)


def _validate_serialized_predictive_audit(
    audit: Mapping[str, JsonValue],
    *,
    expected_direction: Literal["bsc_to_base", "base_to_bsc"],
) -> EvidenceClass:
    typed_audit = _directional_predictive_audit_from_json(audit)
    inference = cast(dict[str, JsonValue], audit["inference"])
    metrics = cast(dict[str, JsonValue], inference["metrics"])
    bootstrap = cast(dict[str, JsonValue], inference["bootstrap"])
    influence = cast(dict[str, JsonValue], audit["influence"])
    regime = cast(dict[str, JsonValue], audit["regime_sensitivity"])
    evidence_class = typed_audit.inference.evidence_class
    _require_evidence_class(evidence_class)
    if any(
        group["direction"] != expected_direction
        for group in (audit, metrics, bootstrap, influence, regime)
    ) or any(
        group["horizon_ms"] != 3_600_000
        for group in (audit, metrics, bootstrap, influence, regime)
    ):
        raise CrossPoolContractError(
            "serialized predictive audit direction or horizon is inconsistent"
        )
    if (
        inference["evidence_class"] != evidence_class
        or influence["full_evidence_class"] != evidence_class
    ):
        raise CrossPoolContractError(
            "serialized predictive evidence classes are inconsistent"
        )
    adequacy = cast(dict[str, JsonValue], inference["adequacy"])
    expected_underpowered = (
        cast(int, adequacy["target_day_count"]) < PREDICTIVE_MIN_TARGET_DAYS
        or cast(int, adequacy["conditional_target_day_count"])
        < PREDICTIVE_MIN_CONDITIONAL_TARGET_DAYS
    )
    if adequacy["underpowered"] != expected_underpowered:
        raise CrossPoolContractError(
            "serialized predictive adequacy flag is inconsistent"
        )
    return evidence_class


def _validate_serialized_predictive_inference_identity(
    inference: Mapping[str, JsonValue],
    *,
    expected_direction: Literal["bsc_to_base", "base_to_bsc"],
    expected_horizon_ms: int,
) -> None:
    _predictive_inference_from_json(inference)
    metrics = cast(dict[str, JsonValue], inference["metrics"])
    bootstrap = cast(dict[str, JsonValue], inference["bootstrap"])
    if any(
        group["direction"] != expected_direction
        or group["horizon_ms"] != expected_horizon_ms
        for group in (metrics, bootstrap)
    ):
        raise CrossPoolContractError(
            "serialized predictive sensitivity direction or horizon is inconsistent"
        )


def _directional_predictive_audit_from_json(
    value: Mapping[str, JsonValue],
) -> DirectionalPredictiveAudit:
    inference = _predictive_inference_from_json(
        cast(dict[str, JsonValue], value["inference"])
    )
    influence_value = cast(dict[str, JsonValue], value["influence"])
    influence = InfluenceReport(
        direction=cast(Direction, influence_value["direction"]),
        horizon_ms=cast(int, influence_value["horizon_ms"]),
        full_mae_improvement_sign=cast(
            ImprovementSign,
            influence_value["full_mae_improvement_sign"],
        ),
        full_evidence_class=cast(
            EvidenceClass,
            influence_value["full_evidence_class"],
        ),
        leave_one_day=_omission_results_from_json(
            cast(list[JsonValue], influence_value["leave_one_day"])
        ),
        leave_one_fold=_omission_results_from_json(
            cast(list[JsonValue], influence_value["leave_one_fold"])
        ),
        unit_dependent=cast(bool, influence_value["unit_dependent"]),
    )
    regime_value = cast(dict[str, JsonValue], value["regime_sensitivity"])
    regime = RegimeSensitivity(
        direction=cast(Direction, regime_value["direction"]),
        horizon_ms=cast(int, regime_value["horizon_ms"]),
        early=_regime_subset_from_json(
            cast(dict[str, JsonValue], regime_value["early"])
        ),
        mixed=_regime_subset_from_json(
            cast(dict[str, JsonValue], regime_value["mixed"])
        ),
        late=_regime_subset_from_json(
            cast(dict[str, JsonValue], regime_value["late"])
        ),
        regime_unstable=cast(bool, regime_value["regime_unstable"]),
        regime_not_adjudicable=cast(
            bool,
            regime_value["regime_not_adjudicable"],
        ),
    )
    typed = DirectionalPredictiveAudit(
        direction=cast(Direction, value["direction"]),
        horizon_ms=cast(int, value["horizon_ms"]),
        inference=inference,
        influence=influence,
        regime_sensitivity=regime,
    )
    if value["evidence_class"] != typed.inference.evidence_class:
        raise CrossPoolContractError(
            "serialized predictive evidence class is inconsistent"
        )
    return typed


def _predictive_inference_from_json(
    value: Mapping[str, JsonValue],
) -> PredictiveInference:
    metrics_value = cast(dict[str, JsonValue], value["metrics"])
    metrics = PredictiveMetrics(
        direction=cast(Direction, metrics_value["direction"]),
        horizon_ms=cast(int, metrics_value["horizon_ms"]),
        rows=cast(int, metrics_value["rows"]),
        target_day_count=cast(int, metrics_value["target_day_count"]),
        conditional_rows=cast(int, metrics_value["conditional_rows"]),
        conditional_target_day_count=cast(
            int,
            metrics_value["conditional_target_day_count"],
        ),
        baseline_mae_bps=cast(float, metrics_value["baseline_mae_bps"]),
        cross_mae_bps=cast(float, metrics_value["cross_mae_bps"]),
        mae_improvement_bps=cast(float, metrics_value["mae_improvement_bps"]),
        baseline_mse_bps2=cast(float, metrics_value["baseline_mse_bps2"]),
        cross_mse_bps2=cast(float, metrics_value["cross_mse_bps2"]),
        mse_improvement_bps2=cast(
            float,
            metrics_value["mse_improvement_bps2"],
        ),
        baseline_rmse_bps=cast(float, metrics_value["baseline_rmse_bps"]),
        cross_rmse_bps=cast(float, metrics_value["cross_rmse_bps"]),
        relative_oos_r2=cast(float | None, metrics_value["relative_oos_r2"]),
        baseline_conditional_directional_accuracy=cast(
            float | None,
            metrics_value["baseline_conditional_directional_accuracy"],
        ),
        cross_conditional_directional_accuracy=cast(
            float | None,
            metrics_value["cross_conditional_directional_accuracy"],
        ),
        conditional_directional_accuracy_gain=cast(
            float | None,
            metrics_value["conditional_directional_accuracy_gain"],
        ),
    )
    bootstrap_value = cast(dict[str, JsonValue], value["bootstrap"])
    bootstrap = PredictiveBootstrap(
        direction=cast(Direction, bootstrap_value["direction"]),
        horizon_ms=cast(int, bootstrap_value["horizon_ms"]),
        resamples=cast(int, bootstrap_value["resamples"]),
        seed=cast(int, bootstrap_value["seed"]),
        confidence_level=cast(float, bootstrap_value["confidence_level"]),
        mae_improvement_bps=_required_confidence_interval_from_json(
            bootstrap_value["mae_improvement_bps"]
        ),
        mse_improvement_bps2=_required_confidence_interval_from_json(
            bootstrap_value["mse_improvement_bps2"]
        ),
        cross_conditional_directional_accuracy=_confidence_interval_from_json(
            bootstrap_value["cross_conditional_directional_accuracy"]
        ),
        conditional_directional_accuracy_gain=_confidence_interval_from_json(
            bootstrap_value["conditional_directional_accuracy_gain"]
        ),
        valid_conditional_resamples=cast(
            int,
            bootstrap_value["valid_conditional_resamples"],
        ),
    )
    adequacy_value = cast(dict[str, JsonValue], value["adequacy"])
    adequacy = AdequacyAudit(
        target_day_count=cast(int, adequacy_value["target_day_count"]),
        conditional_target_day_count=cast(
            int,
            adequacy_value["conditional_target_day_count"],
        ),
    )
    if adequacy_value["underpowered"] != adequacy.underpowered:
        raise CrossPoolContractError(
            "serialized predictive adequacy flag is inconsistent"
        )
    return PredictiveInference(
        metrics=metrics,
        bootstrap=bootstrap,
        adequacy=adequacy,
        evidence_class=cast(EvidenceClass, value["evidence_class"]),
    )


def _confidence_interval_from_json(
    value: JsonValue,
) -> ConfidenceInterval | None:
    if value is None:
        return None
    interval = cast(dict[str, JsonValue], value)
    return ConfidenceInterval(
        point=cast(float, interval["point"]),
        lower=cast(float, interval["lower"]),
        upper=cast(float, interval["upper"]),
    )


def _required_confidence_interval_from_json(
    value: JsonValue,
) -> ConfidenceInterval:
    interval = _confidence_interval_from_json(value)
    if interval is None:
        raise CrossPoolContractError(
            "serialized predictive bootstrap interval is required"
        )
    return interval


def _omission_results_from_json(
    values: list[JsonValue],
) -> tuple[OmissionResult, ...]:
    return tuple(
        OmissionResult(
            unit=cast(str, omission["unit"]),
            mae_improvement_sign=cast(
                ImprovementSign,
                omission["mae_improvement_sign"],
            ),
            evidence_class=cast(EvidenceClass, omission["evidence_class"]),
        )
        for value in values
        for omission in (cast(dict[str, JsonValue], value),)
    )


def _regime_subset_from_json(
    value: Mapping[str, JsonValue],
) -> RegimeSubsetInference:
    inference_value = value["inference"]
    return RegimeSubsetInference(
        regime=cast(Regime, value["regime"]),
        row_count=cast(int, value["row_count"]),
        inference=(
            _predictive_inference_from_json(
                cast(dict[str, JsonValue], inference_value)
            )
            if inference_value is not None
            else None
        ),
        unavailable_reason=cast(
            UnavailableReason | None,
            value["unavailable_reason"],
        ),
    )


def _derive_serialized_robustness_flags(
    payload: Mapping[str, JsonValue],
) -> tuple[RobustnessFlag, ...]:
    predictive = cast(dict[str, JsonValue], payload["predictive"])
    flags: set[RobustnessFlag] = set()
    for direction_key in ("primary", "reverse"):
        audit = cast(dict[str, JsonValue], predictive[direction_key])
        inference = cast(dict[str, JsonValue], audit["inference"])
        adequacy = cast(dict[str, JsonValue], inference["adequacy"])
        influence = cast(dict[str, JsonValue], audit["influence"])
        regime = cast(dict[str, JsonValue], audit["regime_sensitivity"])
        if adequacy["underpowered"] is True:
            flags.add("underpowered")
        if influence["unit_dependent"] is True:
            flags.add("unit_dependent")
        if regime["regime_unstable"] is True:
            flags.add("regime_unstable")
        if regime["regime_not_adjudicable"] is True:
            flags.add("regime_not_adjudicable")
    dtw = cast(dict[str, JsonValue], payload["dtw"])
    for direction_key in ("primary", "reverse"):
        stability = cast(dict[str, JsonValue], dtw[direction_key])
        if stability["band_unstable"] is True:
            flags.add("dtw_band_unstable")
    return tuple(sorted(flags))


def _frozen_economic_contract_json(
    inputs: EconomicManifestInput,
) -> dict[str, JsonValue]:
    coverage = inputs.forecast_coverage
    return {
        "base_pool": "uni-base",
        "policy_profile": "upside_tight_v1",
        "static_config": "static_spot_w0025",
        "initial_capital_usd": "1200",
        "prediction_artifact": "predictive_predictions.csv",
        "prediction_sha256": inputs.prediction_sha256,
        "prediction_direction": "bsc_to_base",
        "prediction_horizon_ms": 3_600_000,
        "forecast_agreement_threshold_bps": "5",
        "flow_markout_feature_sha256": inputs.flow_markout_feature_sha256,
        "entry_state_contract_sha256": inputs.entry_state_contract_sha256,
        "parameter_contract_sha256": inputs.parameter_contract_sha256,
        "mint_gas_usd": "0.073",
        "remove_gas_usd": "0.022",
        "excluded_window_indices": [0, 1, 2],
        "evaluation_window_indices": list(range(3, 26)),
        "active_routes": {
            "7": "upside_capture",
            "18": "fee_box",
            "23": "upside_capture",
            "25": "dip_accumulator",
        },
        "forecast_coverage": {
            "routed_entry_decision_count": coverage.routed_entry_decision_count,
            "forecast_covered_entry_decision_count": (
                coverage.forecast_covered_entry_decision_count
            ),
            "maximum_selected_forecast_age_ms": (
                coverage.maximum_selected_forecast_age_ms
            ),
            "first_routed_entry_timestamp_ms": (
                coverage.first_routed_entry_timestamp_ms
            ),
            "last_routed_entry_timestamp_ms": (
                coverage.last_routed_entry_timestamp_ms
            ),
        },
    }


def _economic_decision_metrics_json(
    metrics: EconomicDecisionMetrics,
) -> dict[str, JsonValue]:
    return {
        "inputs_valid": metrics.inputs_valid,
        "original_net_return_fraction": _decimal_text(
            metrics.original_net_return
        ),
        "gated_net_return_fraction": _decimal_text(metrics.gated_net_return),
        "original_worst_window_return_fraction": _decimal_text(
            metrics.original_worst_window_return
        ),
        "gated_worst_window_return_fraction": _decimal_text(
            metrics.gated_worst_window_return
        ),
        "original_worst_within_window_drawdown_magnitude_fraction": (
            _decimal_text(metrics.original_worst_drawdown_magnitude)
        ),
        "gated_worst_within_window_drawdown_magnitude_fraction": (
            _decimal_text(metrics.gated_worst_drawdown_magnitude)
        ),
    }


def _economic_strategies_json(
    strategies: EconomicStrategySummaries,
) -> dict[str, JsonValue]:
    return {
        "original": _strategy_summary_json(strategies.original),
        "gated": _strategy_summary_json(strategies.gated),
        "static": _strategy_summary_json(strategies.static),
        "pool_mark_hold_cngn": _strategy_summary_json(
            strategies.pool_mark_hold_cngn
        ),
        "cash": _strategy_summary_json(strategies.cash),
    }


def _strategy_summary_json(summary: StrategySummary) -> dict[str, JsonValue]:
    return {
        "window_count": summary.window_count,
        "active_window_count": summary.active_window_count,
        "positive_window_count": summary.positive_window_count,
        "positive_active_window_count": summary.positive_active_window_count,
        "aggregate_net_return_fraction": _decimal_text(
            summary.aggregate_net_return
        ),
        "worst_window_return_fraction": _decimal_text(
            summary.worst_window_return
        ),
        "worst_within_window_drawdown_magnitude_fraction": _decimal_text(
            summary.worst_within_window_drawdown_magnitude
        ),
        "all_window_positive_rate_fraction": _decimal_text(
            summary.all_window_positive_rate
        ),
        "active_window_positive_rate_fraction": (
            None
            if summary.active_window_positive_rate is None
            else _decimal_text(summary.active_window_positive_rate)
        ),
        "total_fees_usd": _decimal_text(summary.total_fees_usd),
        "total_transaction_cost_usd": _decimal_text(
            summary.total_transaction_cost_usd
        ),
        "fee_to_transaction_cost_ratio": (
            None
            if summary.fee_to_transaction_cost_ratio is None
            else _decimal_text(summary.fee_to_transaction_cost_ratio)
        ),
        "rebalance_count": summary.rebalance_count,
    }


def _plain_manifest_copy(
    manifest: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    parsed = json.loads(canonical_manifest_bytes(manifest))
    if not isinstance(parsed, dict):  # pragma: no cover - manifest root is validated.
        raise CrossPoolContractError("manifest copy requires an object")
    return cast(dict[str, JsonValue], parsed)


def _economic_decision_metrics_from_json(
    metrics: Mapping[str, JsonValue],
) -> EconomicDecisionMetrics:
    try:
        return EconomicDecisionMetrics(
            inputs_valid=cast(bool, metrics["inputs_valid"]),
            original_net_return=Decimal(
                cast(str, metrics["original_net_return_fraction"])
            ),
            gated_net_return=Decimal(
                cast(str, metrics["gated_net_return_fraction"])
            ),
            original_worst_window_return=Decimal(
                cast(str, metrics["original_worst_window_return_fraction"])
            ),
            gated_worst_window_return=Decimal(
                cast(str, metrics["gated_worst_window_return_fraction"])
            ),
            original_worst_drawdown_magnitude=Decimal(
                cast(
                    str,
                    metrics[
                        "original_worst_within_window_drawdown_magnitude_fraction"
                    ],
                )
            ),
            gated_worst_drawdown_magnitude=Decimal(
                cast(
                    str,
                    metrics[
                        "gated_worst_within_window_drawdown_magnitude_fraction"
                    ],
                )
            ),
        )
    except (KeyError, ArithmeticError) as exc:
        raise CrossPoolContractError(
            "manifest economic decision metrics are invalid"
        ) from exc


def _validate_manifest_artifact_bindings(
    payload: Mapping[str, JsonValue],
    economics: Mapping[str, JsonValue],
) -> None:
    artifacts = cast(dict[str, JsonValue], payload["artifacts"])
    figures = cast(dict[str, JsonValue], payload["figures"])
    expected_names = set(_STATISTICAL_ARTIFACT_NAMES)
    if economics["status"] == "complete":
        expected_names.update(_ECONOMIC_ARTIFACT_NAMES)
    if set(artifacts) != expected_names:
        raise CrossPoolContractError(
            "manifest artifacts do not match the frozen run state"
        )
    figure_artifacts = {
        "price_gap": "price_gap.png",
        "event_response": "event_response.png",
        "dtw_lag": "dtw_lag.png",
        "diagnostic_predictive_performance": "predictive_performance.png",
    }
    if economics["status"] == "complete":
        figure_artifacts["lp_performance"] = "lp_performance.png"
    elif figures["lp_performance"] is not None:
        raise CrossPoolContractError(
            "LP performance must remain unavailable before economic evaluation"
        )
    for slot, artifact_name in figure_artifacts.items():
        binding = cast(dict[str, JsonValue], figures[slot])
        if (
            binding["artifact"] != artifact_name
            or binding["sha256"] != artifacts[artifact_name]
        ):
            raise CrossPoolContractError(
                f"manifest figure {slot} does not bind its artifact hash"
            )
    if economics["status"] == "complete":
        frozen_contract = cast(dict[str, JsonValue], economics["frozen_contract"])
        if (
            frozen_contract["prediction_sha256"]
            != artifacts["predictive_predictions.csv"]
        ):
            raise CrossPoolContractError(
                "economic forecast contract must bind predictive predictions"
            )


def _validate_input_provenance_semantics(
    provenance: Mapping[str, JsonValue],
) -> None:
    input_sha256 = cast(dict[str, JsonValue], provenance["input_sha256"])
    input_intervals = cast(dict[str, JsonValue], provenance["input_intervals"])
    for name in _PRIMARY_INPUT_NAMES:
        digest = input_sha256[name]
        interval_value = input_intervals[name]
        if (digest is None) != (interval_value is None):
            raise CrossPoolContractError(
                f"manifest {name} hash and interval availability must match"
            )
        if interval_value is None:
            continue
        interval = cast(dict[str, JsonValue], interval_value)
        first_block = cast(int, interval["first_block"])
        last_block = cast(int, interval["last_block"])
        first_timestamp_ms = cast(int, interval["first_timestamp_ms"])
        last_timestamp_ms = cast(int, interval["last_timestamp_ms"])
        if last_block < first_block or last_timestamp_ms < first_timestamp_ms:
            raise CrossPoolContractError(
                f"manifest {name} input block/time interval must be ordered"
            )

    ledger_coverage = cast(dict[str, JsonValue], provenance["ledger_coverage"])
    for ledger_name in ("base_ledger", "bsc_ledger"):
        coverage_value = ledger_coverage[ledger_name]
        if coverage_value is None:
            continue
        ledger_digest = input_sha256[ledger_name]
        coverage = cast(dict[str, JsonValue], coverage_value)
        typed_coverage = _rpc_coverage_from_manifest_json(coverage)
        ledger_interval = cast(dict[str, JsonValue], input_intervals[ledger_name])
        if ledger_digest is None or (
            typed_coverage.ledger_sha256 != ledger_digest
            or typed_coverage.ledger_rows != ledger_interval["rows"]
            or typed_coverage.ledger_first_block != ledger_interval["first_block"]
            or typed_coverage.ledger_last_block != ledger_interval["last_block"]
        ):
            raise CrossPoolContractError(
                f"manifest {ledger_name} coverage must bind its exact input"
            )
        replay_name = (
            "base_replay" if ledger_name == "base_ledger" else "bsc_replay"
        )
        replay_digest = input_sha256[replay_name]
        replay_interval = cast(dict[str, JsonValue], input_intervals[replay_name])
        replay = typed_coverage.evidence.replay_input
        if replay_digest is None or (
            replay.sha256 != replay_digest
            or replay.row_count != replay_interval["rows"]
            or replay.first_block != replay_interval["first_block"]
            or replay.last_block != replay_interval["last_block"]
            or replay.first_timestamp_ms != replay_interval["first_timestamp_ms"]
            or replay.last_timestamp_ms != replay_interval["last_timestamp_ms"]
        ):
            raise CrossPoolContractError(
                f"manifest {ledger_name} coverage must bind its exact replay"
            )
        sidecar_sha256 = coverage["sidecar_sha256"]
        if not isinstance(sidecar_sha256, str):
            raise CrossPoolContractError(
                f"manifest {ledger_name} sidecar SHA-256 is invalid"
            )
        _require_sha256(sidecar_sha256, f"manifest {ledger_name} sidecar")
        if hashlib.sha256(
            rpc_ledger_coverage_bytes(typed_coverage)
        ).hexdigest() != sidecar_sha256:
            raise CrossPoolContractError(
                f"manifest {ledger_name} sidecar hash does not bind its payload"
            )
        if (
            typed_coverage.covered_end_block
            < typed_coverage.covered_start_block
            or cast(int, coverage["required_end_block"])
            < typed_coverage.covered_start_block
            or cast(int, coverage["required_end_block"])
            > typed_coverage.covered_end_block
            or typed_coverage.covered_end_timestamp_ms
            < typed_coverage.covered_start_timestamp_ms
            or cast(int, coverage["required_end_timestamp_ms"])
            < typed_coverage.covered_start_timestamp_ms
            or cast(int, coverage["required_end_timestamp_ms"])
            > typed_coverage.covered_end_timestamp_ms
        ):
            raise CrossPoolContractError(
                f"manifest {ledger_name} coverage must bracket the required cutoff"
            )


def _validate_economic_complete_semantics(
    economics: Mapping[str, JsonValue],
) -> None:
    contract = cast(dict[str, JsonValue], economics["frozen_contract"])
    if (
        contract["entry_state_contract_sha256"]
        != _ENTRY_STATE_CONTRACT_SHA256
        or contract["parameter_contract_sha256"]
        != _PARAMETER_CONTRACT_SHA256
    ):
        raise CrossPoolContractError(
            "economic manifest contract fingerprints do not match the frozen vectors"
        )
    coverage = cast(dict[str, JsonValue], contract["forecast_coverage"])
    try:
        ForecastCoverage(
            routed_entry_decision_count=cast(
                int,
                coverage["routed_entry_decision_count"],
            ),
            forecast_covered_entry_decision_count=cast(
                int,
                coverage["forecast_covered_entry_decision_count"],
            ),
            maximum_selected_forecast_age_ms=cast(
                int,
                coverage["maximum_selected_forecast_age_ms"],
            ),
            first_routed_entry_timestamp_ms=cast(
                int,
                coverage["first_routed_entry_timestamp_ms"],
            ),
            last_routed_entry_timestamp_ms=cast(
                int,
                coverage["last_routed_entry_timestamp_ms"],
            ),
        )
        strategy_payload = cast(dict[str, JsonValue], economics["strategies"])
        strategies = EconomicStrategySummaries(
            original=_strategy_summary_from_json(
                cast(dict[str, JsonValue], strategy_payload["original"])
            ),
            gated=_strategy_summary_from_json(
                cast(dict[str, JsonValue], strategy_payload["gated"])
            ),
            static=_strategy_summary_from_json(
                cast(dict[str, JsonValue], strategy_payload["static"])
            ),
            pool_mark_hold_cngn=_strategy_summary_from_json(
                cast(
                    dict[str, JsonValue],
                    strategy_payload["pool_mark_hold_cngn"],
                )
            ),
            cash=_strategy_summary_from_json(
                cast(dict[str, JsonValue], strategy_payload["cash"])
            ),
        )
        metrics = _economic_decision_metrics_from_json(
            cast(dict[str, JsonValue], economics["decision_metrics"])
        )
        _validate_decision_metrics_match(metrics, strategies)
    except (KeyError, TypeError, ArithmeticError) as exc:
        raise CrossPoolContractError(
            "economic manifest cross-field semantics are invalid"
        ) from exc


def _strategy_summary_from_json(
    payload: Mapping[str, JsonValue],
) -> StrategySummary:
    active_rate = payload["active_window_positive_rate_fraction"]
    fee_cost_ratio = payload["fee_to_transaction_cost_ratio"]
    summary = StrategySummary(
        window_count=cast(int, payload["window_count"]),
        active_window_count=cast(int, payload["active_window_count"]),
        positive_window_count=cast(int, payload["positive_window_count"]),
        positive_active_window_count=cast(
            int | None,
            payload["positive_active_window_count"],
        ),
        aggregate_net_return=Decimal(
            cast(str, payload["aggregate_net_return_fraction"])
        ),
        worst_window_return=Decimal(
            cast(str, payload["worst_window_return_fraction"])
        ),
        worst_within_window_drawdown_magnitude=Decimal(
            cast(
                str,
                payload[
                    "worst_within_window_drawdown_magnitude_fraction"
                ],
            )
        ),
        total_fees_usd=Decimal(cast(str, payload["total_fees_usd"])),
        total_transaction_cost_usd=Decimal(
            cast(str, payload["total_transaction_cost_usd"])
        ),
        rebalance_count=cast(int, payload["rebalance_count"]),
    )
    serialized_all_rate = Decimal(
        cast(str, payload["all_window_positive_rate_fraction"])
    )
    serialized_active_rate = (
        None if active_rate is None else Decimal(cast(str, active_rate))
    )
    serialized_fee_cost_ratio = (
        None if fee_cost_ratio is None else Decimal(cast(str, fee_cost_ratio))
    )
    if (
        serialized_all_rate != summary.all_window_positive_rate
        or serialized_active_rate != summary.active_window_positive_rate
        or serialized_fee_cost_ratio != summary.fee_to_transaction_cost_ratio
    ):
        raise CrossPoolContractError(
            "economic strategy derived rates are inconsistent"
        )
    return summary


def _validate_decision_metrics_match(
    metrics: EconomicDecisionMetrics,
    strategies: EconomicStrategySummaries,
) -> None:
    original = strategies.original
    gated = strategies.gated
    if (
        metrics.original_net_return != original.aggregate_net_return
        or metrics.gated_net_return != gated.aggregate_net_return
        or metrics.original_worst_window_return != original.worst_window_return
        or metrics.gated_worst_window_return != gated.worst_window_return
        or metrics.original_worst_drawdown_magnitude
        != original.worst_within_window_drawdown_magnitude
        or metrics.gated_worst_drawdown_magnitude
        != gated.worst_within_window_drawdown_magnitude
    ):
        raise CrossPoolContractError(
            "economic decision metrics must match original and gated strategies"
        )


def _require_finite_decimal(value: object, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CrossPoolContractError(f"{label} must be a finite Decimal")


def _fixed_decimal_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    _require_finite_decimal(numerator, "economic ratio numerator")
    _require_finite_decimal(denominator, "economic ratio denominator")
    if denominator <= 0:
        raise CrossPoolContractError(
            "economic ratio denominator must be positive"
        )
    with localcontext(_ECONOMIC_DECIMAL_CONTEXT):
        return numerator / denominator


def _decimal_text(value: Decimal) -> str:
    _require_finite_decimal(value, "serialized economic decimal")
    if value == 0:
        return "0"
    return format(value, "f")


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CrossPoolContractError(f"manifest contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise CrossPoolContractError(
        f"manifest contains non-finite JSON constant {value}"
    )
