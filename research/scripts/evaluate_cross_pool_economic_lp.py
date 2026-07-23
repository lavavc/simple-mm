"""Evaluate and atomically publish the frozen forecast-gated Base LP policy."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Literal, TypeAlias, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.web3_utils import redact_rpc_credentials
from research.backtester.clmm_math import sqrt_price_x96_to_native_price
from research.backtester.data import Event, V4Event, load_v4_events
from research.backtester.params import BacktestParams
from research.backtester.pool_price_semantics import token_decimals
from research.backtester.run import (
    WindowSlice,
    WindowSpec,
    _build_pool_state,
    _iter_window_slices,
)
from research.backtester.simulator import SimResult, simulate_pool
from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.figures import lp_performance_png
from research.cross_pool.forecast import (
    DirectionalArchetype,
    ForecastAgreementOverlay,
    ForecastPoint,
    ForecastTimeline,
)
from research.cross_pool.manifest import (
    EconomicDecisionMetrics,
    EconomicInvalidManifestInput,
    EconomicInvalidReason,
    EconomicManifestInput,
    EconomicStrategySummaries,
    ForecastCoverage,
    JsonValue,
    RuntimeEnvironment,
    StrategySummary,
    merge_economic_invalid_manifest,
    merge_economic_manifest,
    select_economic_class,
)
from research.cross_pool.provenance import (
    ImmutableInputSnapshot,
    capture_git_state,
    collect_runtime_environment,
    snapshot_regular_file,
)
from research.cross_pool.publication import (
    OutputPublicationError,
    augment_evidence_directory,
    output_lock,
    validate_evidence_directory,
)
from research.cross_pool.reporting import RenderedArtifact
from research.scripts.evaluate_directional_paper_lp import (
    DirectionalArchetypeConfig,
    directional_archetype_configs,
    directional_policy_profiles,
    route_directional_windows,
)
from research.scripts.evaluate_flow_gated_lp import build_entry_states
from research.scripts.evaluate_frozen_family_lp import (
    POOL_EXPERIMENTS,
    static_lp_configs,
)

EconomicStrategy: TypeAlias = Literal[
    "original",
    "gated",
    "static",
    "pool_mark_hold_cngn",
    "cash",
]
PositionStatus: TypeAlias = Literal["active", "cash"]
ForecastGateStatus: TypeAlias = Literal[
    "not_applied",
    "not_routed",
    "agreement",
    "disagreement",
]
RoutedArchetype: TypeAlias = DirectionalArchetype | Literal["no_position"]

FROZEN_POLICY_PROFILE = "upside_tight_v1"
FROZEN_STATIC_CONFIG = "static_spot_w0025"
FROZEN_EXCLUDED_WINDOWS = (0, 1, 2)
FROZEN_EVALUATION_WINDOWS = tuple(range(3, 26))
FROZEN_ENTRY_STATE_SHA256 = (
    "61d9f7977c9806bf25c01a3f2db9bc5ecb56bb7612fcc6c72bf68a94a31f118b"
)
FROZEN_PARAMETER_SHA256 = (
    "abe886b69c909010a1e1a27eb16d740448cde2993c2a5ec7835e1b03a864888f"
)
FROZEN_INITIAL_CAPITAL_USD = Decimal("1200")
FROZEN_MINT_GAS_USD = Decimal("0.073")
FROZEN_REMOVE_GAS_USD = Decimal("0.022")
FROZEN_FORECAST_AGREEMENT_THRESHOLD_BPS = 5.0
FROZEN_ACTIVE_ROUTES: Mapping[int, DirectionalArchetype] = MappingProxyType(
    {
        7: "upside_capture",
        18: "fee_box",
        23: "upside_capture",
        25: "dip_accumulator",
    }
)

_HOUR_MS = 3_600_000
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
_ENTRY_FINGERPRINT_FIELDS = (
    "window_index",
    "entry_timestamp_ms",
    "validation_start_timestamp_ms",
    "validation_end_timestamp_ms",
    "directional_archetype",
    "directional_route_reason",
    "gate_directional_active",
)
_STRATEGIES: tuple[EconomicStrategy, ...] = (
    "original",
    "gated",
    "static",
    "pool_mark_hold_cngn",
    "cash",
)
_WINDOW_FIELDS = (
    "window_index",
    "validation_start_timestamp_ms",
    "validation_end_timestamp_ms",
    "strategy",
    "config",
    "archetype",
    "route_reason",
    "position_status",
    "forecast_gate",
    "net_return_fraction",
    "worst_within_window_drawdown_magnitude_fraction",
    "total_fees_usd",
    "total_transaction_cost_usd",
    "rebalance_count",
)
_SUMMARY_FIELDS = (
    "strategy",
    "window_count",
    "active_window_count",
    "positive_window_count",
    "positive_active_window_count",
    "aggregate_net_return_fraction",
    "worst_window_return_fraction",
    "worst_within_window_drawdown_magnitude_fraction",
    "all_window_positive_rate_fraction",
    "active_window_positive_rate_fraction",
    "total_fees_usd",
    "total_transaction_cost_usd",
    "fee_to_transaction_cost_ratio",
    "rebalance_count",
)
_EXCLUSION_FIELDS = ("window_index", "reason")
_BASE_EXPERIMENT = POOL_EXPERIMENTS["uni-base"]


class EconomicQaError(CrossPoolContractError):
    """A typed, terminal reason that prevents economic adjudication."""

    def __init__(self, reason_code: EconomicInvalidReason, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class FrozenEntryState:
    window_index: int
    entry_timestamp_ms: int
    validation_start_timestamp_ms: int
    validation_end_timestamp_ms: int
    archetype: RoutedArchetype
    route_reason: str


@dataclass(frozen=True)
class FrozenEconomicPlan:
    entries: tuple[FrozenEntryState, ...]
    excluded_windows: tuple[int, ...]
    evaluation_windows: tuple[int, ...]
    active_routes: Mapping[int, DirectionalArchetype]
    directional_configs: Mapping[str, DirectionalArchetypeConfig]
    static_params: BacktestParams
    entry_state_contract_sha256: str
    parameter_contract_sha256: str
    initial_capital_usd: Decimal
    mint_gas_usd: Decimal
    remove_gas_usd: Decimal


@dataclass(frozen=True)
class EconomicInputSnapshots:
    predictions: ImmutableInputSnapshot
    base_features: ImmutableInputSnapshot
    flow_markout_features: ImmutableInputSnapshot
    base_replay: ImmutableInputSnapshot


@dataclass(frozen=True)
class EconomicWindowRow:
    window_index: int
    validation_start_timestamp_ms: int
    validation_end_timestamp_ms: int
    strategy: EconomicStrategy
    config: str
    archetype: str
    route_reason: str
    position_status: PositionStatus
    forecast_gate: ForecastGateStatus
    net_return: Decimal
    worst_within_window_drawdown_magnitude: Decimal
    total_fees_usd: Decimal
    total_transaction_cost_usd: Decimal
    rebalance_count: int

    def __post_init__(self) -> None:
        if self.window_index not in FROZEN_EVALUATION_WINDOWS:
            raise CrossPoolContractError("economic row uses an unfrozen window")
        if self.strategy not in _STRATEGIES or not self.config:
            raise CrossPoolContractError("economic row strategy or config is invalid")
        if (
            self.validation_start_timestamp_ms <= 0
            or self.validation_end_timestamp_ms
            <= self.validation_start_timestamp_ms
        ):
            raise CrossPoolContractError("economic row interval is invalid")
        for label, value in (
            ("net return", self.net_return),
            (
                "within-window drawdown",
                self.worst_within_window_drawdown_magnitude,
            ),
            ("fees", self.total_fees_usd),
            ("transaction cost", self.total_transaction_cost_usd),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise CrossPoolContractError(
                    f"economic row {label} must be a finite Decimal"
                )
        if (
            self.worst_within_window_drawdown_magnitude < 0
            or self.total_fees_usd < 0
            or self.total_transaction_cost_usd < 0
            or isinstance(self.rebalance_count, bool)
            or not isinstance(self.rebalance_count, int)
            or self.rebalance_count < 0
        ):
            raise CrossPoolContractError("economic row costs or counts are invalid")
        if self.position_status not in ("active", "cash"):
            raise CrossPoolContractError("economic row position status is invalid")
        if self.position_status == "cash" and (
            self.net_return != 0
            or self.worst_within_window_drawdown_magnitude != 0
            or self.total_fees_usd != 0
            or self.total_transaction_cost_usd != 0
            or self.rebalance_count != 0
        ):
            raise CrossPoolContractError(
                "economic cash row must be an exact zero observation"
            )
        if self.strategy == "cash" and self.position_status != "cash":
            raise CrossPoolContractError("cash comparator cannot be active")
        if self.strategy in ("static", "pool_mark_hold_cngn") and not self.active:
            raise CrossPoolContractError(
                "static and hold comparators must remain active"
            )

    @property
    def active(self) -> bool:
        return self.position_status == "active"


@dataclass(frozen=True)
class RenderedEconomicEvidence:
    artifacts: tuple[RenderedArtifact, ...]
    manifest_input: EconomicManifestInput
    strategies: EconomicStrategySummaries
    decision_metrics: EconomicDecisionMetrics


def canonical_payload_sha256(payload: object) -> str:
    """Hash one exact compact, sorted, ASCII JSON payload without a newline."""
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CrossPoolContractError(
            "frozen contract payload must be canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def frozen_parameter_payload() -> dict[str, object]:
    directional = directional_policy_profiles(
        directional_archetype_configs(
            initial_capital_usd=float(FROZEN_INITIAL_CAPITAL_USD),
            mint_gas_usd=float(FROZEN_MINT_GAS_USD),
            remove_gas_usd=float(FROZEN_REMOVE_GAS_USD),
        )
    )[FROZEN_POLICY_PROFILE]
    static = dict(
        static_lp_configs(
            initial_capital_usd=float(FROZEN_INITIAL_CAPITAL_USD),
            mint_gas_usd=float(FROZEN_MINT_GAS_USD),
            remove_gas_usd=float(FROZEN_REMOVE_GAS_USD),
        )
    )[FROZEN_STATIC_CONFIG]
    return {
        "profile": FROZEN_POLICY_PROFILE,
        "directional": {
            archetype: asdict(config.params)
            for archetype, config in directional.items()
        },
        "static": {FROZEN_STATIC_CONFIG: asdict(static)},
        "mint_gas_usd": float(FROZEN_MINT_GAS_USD),
        "remove_gas_usd": float(FROZEN_REMOVE_GAS_USD),
        "forecast_agreement_threshold_bps": (
            FROZEN_FORECAST_AGREEMENT_THRESHOLD_BPS
        ),
    }


def load_base_entry_states(
    feature_csv: Path,
    flow_markout_feature_csv: Path,
) -> list[dict[str, str]]:
    """Rebuild causal Base entry states from explicit immutable inputs."""
    feature_rows = _read_csv(feature_csv)
    qts_rows = _read_csv(flow_markout_feature_csv)
    return route_directional_windows(
        build_entry_states(
            feature_rows,
            qts_rows=qts_rows,
            train_swaps=_BASE_EXPERIMENT.train_swaps,
            val_swaps=_BASE_EXPERIMENT.val_swaps,
            stride_swaps=_BASE_EXPERIMENT.val_swaps,
            flow_threshold=Decimal("0.90"),
            train_return_max=Decimal("0"),
            max_windows=None,
        )
    )


def current_base_entry_states() -> list[dict[str, str]]:
    """Rebuild the frozen contract vector from the canonical local inputs."""
    return load_base_entry_states(
        _BASE_EXPERIMENT.feature_csv,
        _BASE_EXPERIMENT.qts_feature_csv,
    )


def build_frozen_economic_plan(
    entry_states: Sequence[Mapping[str, str]],
) -> FrozenEconomicPlan:
    """Validate the exact Base windows, routes, parameters, and fingerprints."""
    materialized = tuple(dict(state) for state in entry_states)
    indexed: dict[int, dict[str, str]] = {}
    for state in materialized:
        window_index = _required_int_text(state, "window_index")
        if window_index in indexed:
            raise CrossPoolContractError("duplicate economic entry-state window")
        indexed[window_index] = state
    if tuple(sorted(indexed)) != tuple(range(26)):
        raise CrossPoolContractError(
            "frozen economic entry states require windows 0 through 25 exactly once"
        )

    fingerprint_payload: list[dict[str, str]] = []
    entries: list[FrozenEntryState] = []
    active_routes: dict[int, DirectionalArchetype] = {}
    for window_index in FROZEN_EVALUATION_WINDOWS:
        state = indexed[window_index]
        exact = {
            field: _required_exact_text(state, field)
            for field in _ENTRY_FINGERPRINT_FIELDS
        }
        fingerprint_payload.append(exact)
        if int(exact["window_index"]) != window_index:
            raise CrossPoolContractError("entry-state window ordering is inconsistent")
        archetype_text = exact["directional_archetype"]
        if archetype_text not in (
            "upside_capture",
            "dip_accumulator",
            "fee_box",
            "no_position",
        ):
            raise CrossPoolContractError("entry-state route is unsupported")
        archetype = cast(RoutedArchetype, archetype_text)
        active_text = exact["gate_directional_active"]
        if active_text != ("0" if archetype == "no_position" else "1"):
            raise CrossPoolContractError("entry-state active gate conflicts with route")
        if archetype != "no_position":
            active_routes[window_index] = archetype
        entry_timestamp_ms = int(exact["entry_timestamp_ms"])
        validation_start_timestamp_ms = int(
            exact["validation_start_timestamp_ms"]
        )
        validation_end_timestamp_ms = int(exact["validation_end_timestamp_ms"])
        if not (
            0 < entry_timestamp_ms
            < validation_start_timestamp_ms
            < validation_end_timestamp_ms
        ):
            raise CrossPoolContractError("entry-state timestamps are not causal")
        entries.append(
            FrozenEntryState(
                window_index=window_index,
                entry_timestamp_ms=entry_timestamp_ms,
                validation_start_timestamp_ms=validation_start_timestamp_ms,
                validation_end_timestamp_ms=validation_end_timestamp_ms,
                archetype=archetype,
                route_reason=exact["directional_route_reason"],
            )
        )

    entry_digest = canonical_payload_sha256(fingerprint_payload)
    if entry_digest != FROZEN_ENTRY_STATE_SHA256:
        raise EconomicQaError(
            "ENTRY_STATE_CONTRACT_MISMATCH",
            "economic entry-state fingerprint does not match the frozen vector",
        )
    if active_routes != dict(FROZEN_ACTIVE_ROUTES):
        raise EconomicQaError(
            "ENTRY_STATE_CONTRACT_MISMATCH",
            "economic active routes do not match the frozen vector",
        )

    directional_configs = directional_policy_profiles(
        directional_archetype_configs(
            initial_capital_usd=float(FROZEN_INITIAL_CAPITAL_USD),
            mint_gas_usd=float(FROZEN_MINT_GAS_USD),
            remove_gas_usd=float(FROZEN_REMOVE_GAS_USD),
        )
    )[FROZEN_POLICY_PROFILE]
    static_params = dict(
        static_lp_configs(
            initial_capital_usd=float(FROZEN_INITIAL_CAPITAL_USD),
            mint_gas_usd=float(FROZEN_MINT_GAS_USD),
            remove_gas_usd=float(FROZEN_REMOVE_GAS_USD),
        )
    )[FROZEN_STATIC_CONFIG]
    parameter_digest = canonical_payload_sha256(frozen_parameter_payload())
    if parameter_digest != FROZEN_PARAMETER_SHA256:
        raise EconomicQaError(
            "PARAMETER_CONTRACT_MISMATCH",
            "economic parameter fingerprint does not match the frozen vector",
        )
    _validate_parameter_identity(directional_configs, static_params)

    return FrozenEconomicPlan(
        entries=tuple(entries),
        excluded_windows=FROZEN_EXCLUDED_WINDOWS,
        evaluation_windows=FROZEN_EVALUATION_WINDOWS,
        active_routes=MappingProxyType(dict(active_routes)),
        directional_configs=MappingProxyType(dict(directional_configs)),
        static_params=static_params,
        entry_state_contract_sha256=entry_digest,
        parameter_contract_sha256=parameter_digest,
        initial_capital_usd=FROZEN_INITIAL_CAPITAL_USD,
        mint_gas_usd=FROZEN_MINT_GAS_USD,
        remove_gas_usd=FROZEN_REMOVE_GAS_USD,
    )


def load_primary_forecast_timeline(path: Path) -> ForecastTimeline:
    """Load only the frozen BSC-to-Base one-hour forecast origin series."""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != _PREDICTION_FIELDS:
                raise CrossPoolContractError(
                    "prediction CSV header does not match the frozen contract"
                )
            points: list[ForecastPoint] = []
            for row in reader:
                timestamp_ms = _required_csv_int(row, "timestamp_ms")
                target_timestamp_ms = _required_csv_int(
                    row,
                    "target_timestamp_ms",
                )
                horizon_ms = _required_csv_int(row, "horizon_ms")
                refit_timestamp_ms = _required_csv_int(
                    row,
                    "refit_timestamp_ms",
                )
                fold_index = _required_csv_int(row, "fold_index")
                direction = _required_csv_text(row, "direction")
                if direction not in ("bsc_to_base", "base_to_bsc"):
                    raise CrossPoolContractError(
                        "prediction direction is unsupported"
                    )
                if horizon_ms not in (900_000, _HOUR_MS, 14_400_000):
                    raise CrossPoolContractError("prediction horizon is unsupported")
                if target_timestamp_ms != timestamp_ms + horizon_ms:
                    raise CrossPoolContractError(
                        "prediction target timestamp does not match its horizon"
                    )
                if (
                    timestamp_ms <= 0
                    or timestamp_ms % horizon_ms != 0
                    or refit_timestamp_ms <= 0
                    or refit_timestamp_ms > timestamp_ms
                    or fold_index < 0
                ):
                    raise CrossPoolContractError(
                        "prediction timing or fold identity is invalid"
                    )
                actual_bps = _required_csv_float(row, "actual_bps")
                baseline_bps = _required_csv_float(
                    row,
                    "baseline_prediction_bps",
                )
                cross_bps = _required_csv_float(row, "cross_prediction_bps")
                if not all(
                    math.isfinite(value)
                    for value in (actual_bps, baseline_bps, cross_bps)
                ):
                    raise CrossPoolContractError(
                        "prediction values must be finite"
                    )
                if direction == "bsc_to_base" and horizon_ms == _HOUR_MS:
                    points.append(
                        ForecastPoint(
                            origin_time=_utc_from_ms(timestamp_ms),
                            refit_time=_utc_from_ms(refit_timestamp_ms),
                            horizon=timedelta(hours=1),
                            forecast_bps=cross_bps,
                        )
                    )
    except (csv.Error, UnicodeError) as exc:
        raise CrossPoolContractError(
            "prediction artifact is not a valid UTF-8 CSV"
        ) from exc
    except OSError as exc:
        raise OutputPublicationError("prediction artifact could not be read") from exc
    if not points:
        raise CrossPoolContractError(
            "prediction artifact has no primary one-hour forecasts"
        )
    return ForecastTimeline(tuple(sorted(points, key=lambda point: point.origin_time)))


def pool_mark_hold_metrics(events: Sequence[Event]) -> tuple[Decimal, Decimal]:
    """Return exact sqrt-mid hold return and within-window drawdown."""
    swaps = tuple(
        event
        for event in events
        if isinstance(event, V4Event) and event.event_type == "swap"
    )
    if not swaps:
        raise CrossPoolContractError(
            "pool-mark hold requires at least one V4 validation swap"
        )
    mids = tuple(_event_raw_sqrt_mid(event) for event in swaps)
    with localcontext() as context:
        context.prec = 60
        path = tuple(mid / mids[0] for mid in mids)
        return +(path[-1] - Decimal("1")), +_maximum_drawdown(path)


def evaluate_frozen_windows(
    plan: FrozenEconomicPlan,
    timeline: ForecastTimeline,
    events: Sequence[Event],
) -> tuple[tuple[EconomicWindowRow, ...], ForecastCoverage]:
    """Evaluate original, gated, static, hold, and cash on identical slices."""
    if not isinstance(plan, FrozenEconomicPlan) or not isinstance(
        timeline,
        ForecastTimeline,
    ):
        raise CrossPoolContractError("economic evaluation requires typed inputs")
    slices = _frozen_window_slices(events)
    entries = {entry.window_index: entry for entry in plan.entries}
    try:
        coverage = _forecast_coverage(entries, slices, timeline)
    except CrossPoolContractError as exc:
        raise EconomicQaError(
            "FORECAST_COVERAGE_INVALID",
            "frozen routed windows do not have complete causal forecast coverage",
        ) from exc

    rows: list[EconomicWindowRow] = []
    for window_index in FROZEN_EVALUATION_WINDOWS:
        entry = entries[window_index]
        window_slice = slices[window_index]
        _validate_slice_identity(entry, window_slice)
        initial_pool_state = _build_pool_state(window_slice.train_events)
        if entry.archetype == "no_position":
            rows.extend(
                (
                    _cash_row(entry, "original", FROZEN_POLICY_PROFILE, "not_routed"),
                    _cash_row(entry, "gated", FROZEN_POLICY_PROFILE, "not_routed"),
                )
            )
        else:
            directional = plan.directional_configs[entry.archetype]
            original = simulate_pool(
                window_slice.val_events,
                directional.params,
                _BASE_EXPERIMENT.pool_config,
                float(plan.initial_capital_usd),
                initial_pool_state=initial_pool_state,
                settle_to_cash=False,
            )
            rows.append(
                _simulation_row(
                    entry,
                    strategy="original",
                    config=FROZEN_POLICY_PROFILE,
                    sim=original,
                    forecast_gate="not_applied",
                )
            )
            gated = simulate_pool(
                window_slice.val_events,
                directional.params,
                _BASE_EXPERIMENT.pool_config,
                float(plan.initial_capital_usd),
                initial_pool_state=initial_pool_state,
                entry_eligibility=ForecastAgreementOverlay(
                    routed_archetype=entry.archetype,
                    timeline=timeline,
                    threshold_bps=FROZEN_FORECAST_AGREEMENT_THRESHOLD_BPS,
                ),
                settle_to_cash=False,
            )
            rows.append(
                _simulation_row(
                    entry,
                    strategy="gated",
                    config=FROZEN_POLICY_PROFILE,
                    sim=gated,
                    forecast_gate=("agreement" if gated.episodes else "disagreement"),
                )
            )

        static = simulate_pool(
            window_slice.val_events,
            plan.static_params,
            _BASE_EXPERIMENT.pool_config,
            float(plan.initial_capital_usd),
            initial_pool_state=initial_pool_state,
            settle_to_cash=False,
        )
        rows.append(
            _simulation_row(
                entry,
                strategy="static",
                config=FROZEN_STATIC_CONFIG,
                sim=static,
                forecast_gate="not_applied",
            )
        )
        hold_return, hold_drawdown = pool_mark_hold_metrics(
            window_slice.val_events
        )
        rows.append(
            EconomicWindowRow(
                window_index=entry.window_index,
                validation_start_timestamp_ms=(
                    entry.validation_start_timestamp_ms
                ),
                validation_end_timestamp_ms=entry.validation_end_timestamp_ms,
                strategy="pool_mark_hold_cngn",
                config="mark_to_pool_sqrt_mid",
                archetype="",
                route_reason="",
                position_status="active",
                forecast_gate="not_applied",
                net_return=hold_return,
                worst_within_window_drawdown_magnitude=hold_drawdown,
                total_fees_usd=Decimal("0"),
                total_transaction_cost_usd=Decimal("0"),
                rebalance_count=0,
            )
        )
        rows.append(_cash_row(entry, "cash", "idle_cash", "not_applied"))
    return tuple(rows), coverage


def summarize_economic_rows(
    rows: Sequence[EconomicWindowRow],
) -> tuple[EconomicStrategySummaries, EconomicDecisionMetrics]:
    """Aggregate isolated windows without constructing a synthetic equity path."""
    materialized = tuple(rows)
    identities = tuple((row.window_index, row.strategy) for row in materialized)
    expected = {
        (window_index, strategy)
        for window_index in FROZEN_EVALUATION_WINDOWS
        for strategy in _STRATEGIES
    }
    if len(identities) != len(expected) or set(identities) != expected:
        raise CrossPoolContractError(
            "economic rows require one observation per strategy and frozen window"
        )
    summaries = {
        strategy: _strategy_summary(
            tuple(row for row in materialized if row.strategy == strategy)
        )
        for strategy in _STRATEGIES
    }
    typed = EconomicStrategySummaries(
        original=summaries["original"],
        gated=summaries["gated"],
        static=summaries["static"],
        pool_mark_hold_cngn=summaries["pool_mark_hold_cngn"],
        cash=summaries["cash"],
    )
    metrics = EconomicDecisionMetrics(
        inputs_valid=True,
        original_net_return=typed.original.aggregate_net_return,
        gated_net_return=typed.gated.aggregate_net_return,
        original_worst_window_return=typed.original.worst_window_return,
        gated_worst_window_return=typed.gated.worst_window_return,
        original_worst_drawdown_magnitude=(
            typed.original.worst_within_window_drawdown_magnitude
        ),
        gated_worst_drawdown_magnitude=(
            typed.gated.worst_within_window_drawdown_magnitude
        ),
    )
    return typed, metrics


def render_economic_artifacts(
    plan: FrozenEconomicPlan,
    rows: Sequence[EconomicWindowRow],
    coverage: ForecastCoverage,
    *,
    prediction_sha256: str,
    flow_markout_feature_sha256: str,
) -> RenderedEconomicEvidence:
    """Render the five deterministic economic artifacts and typed merge input."""
    strategies, decision_metrics = summarize_economic_rows(rows)
    summary_pairs = _strategy_summary_pairs(strategies)
    figure = lp_performance_png(
        tuple(
            (
                label,
                float(summary.aggregate_net_return),
                float(summary.worst_within_window_drawdown_magnitude),
            )
            for label, summary in summary_pairs
        )
    )
    artifacts = (
        RenderedArtifact(
            "frozen_policy_windows.csv",
            _economic_windows_csv_bytes(rows),
        ),
        RenderedArtifact(
            "frozen_policy_summary.csv",
            _economic_summary_csv_bytes(summary_pairs),
        ),
        RenderedArtifact(
            "frozen_policy_exclusions.csv",
            _economic_exclusions_csv_bytes(),
        ),
        RenderedArtifact(
            "frozen_policy_report.md",
            _economic_report_bytes(summary_pairs, decision_metrics),
        ),
        RenderedArtifact("lp_performance.png", figure),
    )
    artifact_hashes = {
        artifact.relative_name: artifact.sha256 for artifact in artifacts
    }
    manifest_input = EconomicManifestInput(
        entry_state_contract_sha256=plan.entry_state_contract_sha256,
        parameter_contract_sha256=plan.parameter_contract_sha256,
        prediction_sha256=prediction_sha256,
        flow_markout_feature_sha256=flow_markout_feature_sha256,
        forecast_coverage=coverage,
        decision_metrics=decision_metrics,
        strategies=strategies,
        lp_performance_figure={
            "artifact": "lp_performance.png",
            "sha256": artifact_hashes["lp_performance.png"],
        },
        artifacts=artifact_hashes,
    )
    return RenderedEconomicEvidence(
        artifacts=artifacts,
        manifest_input=manifest_input,
        strategies=strategies,
        decision_metrics=decision_metrics,
    )


def _validate_parameter_identity(
    directional_configs: Mapping[str, DirectionalArchetypeConfig],
    static_params: BacktestParams,
) -> None:
    if set(directional_configs) != {
        "upside_capture",
        "dip_accumulator",
        "fee_box",
    }:
        raise EconomicQaError(
            "PARAMETER_CONTRACT_MISMATCH",
            "frozen directional profile is incomplete",
        )
    params = tuple(config.params for config in directional_configs.values()) + (
        static_params,
    )
    for parameter in params:
        costs = parameter.transaction_costs
        if (
            Decimal(str(parameter.initial_capital_usd))
            != FROZEN_INITIAL_CAPITAL_USD
            or Decimal(str(costs.mint_gas_usd)) != FROZEN_MINT_GAS_USD
            or Decimal(str(costs.remove_gas_usd)) != FROZEN_REMOVE_GAS_USD
        ):
            raise EconomicQaError(
                "PARAMETER_CONTRACT_MISMATCH",
                "frozen capital or gas parameters changed",
            )


def _frozen_window_slices(events: Sequence[Event]) -> dict[int, WindowSlice]:
    spec = WindowSpec(
        mode="swap_count",
        train_swaps=_BASE_EXPERIMENT.train_swaps,
        val_swaps=_BASE_EXPERIMENT.val_swaps,
        stride_swaps=_BASE_EXPERIMENT.val_swaps,
        min_train_swaps=_BASE_EXPERIMENT.train_swaps,
        min_train_liquidity_events=0,
        min_val_swaps=_BASE_EXPERIMENT.val_swaps,
    )
    materialized = _iter_window_slices(list(events), spec, max_windows=None)
    indexed = {window_slice.window.index: window_slice for window_slice in materialized}
    if len(indexed) != len(materialized) or tuple(sorted(indexed)) != tuple(range(26)):
        raise CrossPoolContractError(
            "Base replay does not produce exactly frozen windows 0 through 25"
        )
    if any(window_slice.skipped_reason is not None for window_slice in materialized):
        raise CrossPoolContractError("Base replay contains an incomplete frozen window")
    return indexed


def _forecast_coverage(
    entries: Mapping[int, FrozenEntryState],
    slices: Mapping[int, WindowSlice],
    timeline: ForecastTimeline,
) -> ForecastCoverage:
    routed = tuple(
        entries[window_index]
        for window_index in sorted(FROZEN_ACTIVE_ROUTES)
    )
    selected_ages: list[int] = []
    for entry in routed:
        decision_time = _utc_from_ms(entry.entry_timestamp_ms)
        point = timeline.as_of(decision_time)
        selected_ages.append(
            entry.entry_timestamp_ms - _datetime_ms(point.origin_time)
        )
        for event in slices[entry.window_index].val_events:
            if isinstance(event, V4Event) and event.event_type == "swap":
                timeline.as_of(event.block_time)
    return ForecastCoverage(
        routed_entry_decision_count=len(routed),
        forecast_covered_entry_decision_count=len(routed),
        maximum_selected_forecast_age_ms=max(selected_ages),
        first_routed_entry_timestamp_ms=routed[0].entry_timestamp_ms,
        last_routed_entry_timestamp_ms=routed[-1].entry_timestamp_ms,
    )


def _validate_slice_identity(
    entry: FrozenEntryState,
    window_slice: WindowSlice,
) -> None:
    replay_start_ms = _datetime_ms(window_slice.window.val_start)
    replay_end_ms = _datetime_ms(window_slice.window.val_end)
    # Feature rows add millisecond ordinals when several swaps share an
    # on-chain second; replay events retain the chain's second-level timestamp.
    if (
        entry.window_index != window_slice.window.index
        or not replay_start_ms
        <= entry.validation_start_timestamp_ms
        < replay_start_ms + 1_000
        or not replay_end_ms
        <= entry.validation_end_timestamp_ms
        < replay_end_ms + 1_000
    ):
        raise CrossPoolContractError(
            "Base replay window does not match the frozen feature-state bounds"
        )


def _simulation_row(
    entry: FrozenEntryState,
    *,
    strategy: EconomicStrategy,
    config: str,
    sim: SimResult,
    forecast_gate: ForecastGateStatus,
) -> EconomicWindowRow:
    active = bool(sim.episodes)
    if not active:
        return _cash_row(entry, strategy, config, forecast_gate)
    net_return = Decimal(
        str(sim.final_value / float(FROZEN_INITIAL_CAPITAL_USD) - 1.0)
    )
    equity_path = (
        FROZEN_INITIAL_CAPITAL_USD,
        *(Decimal(str(value)) for _timestamp, value in sim.value_samples),
        Decimal(str(sim.final_value)),
    )
    return EconomicWindowRow(
        window_index=entry.window_index,
        validation_start_timestamp_ms=entry.validation_start_timestamp_ms,
        validation_end_timestamp_ms=entry.validation_end_timestamp_ms,
        strategy=strategy,
        config=config,
        archetype=entry.archetype if strategy in ("original", "gated") else "",
        route_reason=(
            entry.route_reason if strategy in ("original", "gated") else ""
        ),
        position_status="active",
        forecast_gate=forecast_gate,
        net_return=net_return,
        worst_within_window_drawdown_magnitude=_maximum_drawdown(equity_path),
        total_fees_usd=Decimal(str(sim.total_fees)),
        total_transaction_cost_usd=Decimal(str(sim.total_transaction_cost)),
        rebalance_count=sim.rebalance_count,
    )


def _cash_row(
    entry: FrozenEntryState,
    strategy: EconomicStrategy,
    config: str,
    forecast_gate: ForecastGateStatus,
) -> EconomicWindowRow:
    return EconomicWindowRow(
        window_index=entry.window_index,
        validation_start_timestamp_ms=entry.validation_start_timestamp_ms,
        validation_end_timestamp_ms=entry.validation_end_timestamp_ms,
        strategy=strategy,
        config=config,
        archetype=(
            entry.archetype if strategy in ("original", "gated") else ""
        ),
        route_reason=(
            entry.route_reason if strategy in ("original", "gated") else ""
        ),
        position_status="cash",
        forecast_gate=forecast_gate,
        net_return=Decimal("0"),
        worst_within_window_drawdown_magnitude=Decimal("0"),
        total_fees_usd=Decimal("0"),
        total_transaction_cost_usd=Decimal("0"),
        rebalance_count=0,
    )


def _strategy_summary(rows: Sequence[EconomicWindowRow]) -> StrategySummary:
    if len(rows) != len(FROZEN_EVALUATION_WINDOWS):
        raise CrossPoolContractError("economic strategy has incomplete windows")
    active = tuple(row for row in rows if row.active)
    positive = tuple(row for row in rows if row.net_return > 0)
    positive_active = tuple(row for row in active if row.net_return > 0)
    return StrategySummary(
        window_count=len(rows),
        active_window_count=len(active),
        positive_window_count=len(positive),
        positive_active_window_count=(
            len(positive_active) if active else None
        ),
        aggregate_net_return=sum(
            (row.net_return for row in rows),
            Decimal("0"),
        ),
        worst_window_return=min(row.net_return for row in rows),
        worst_within_window_drawdown_magnitude=max(
            row.worst_within_window_drawdown_magnitude for row in rows
        ),
        total_fees_usd=sum(
            (row.total_fees_usd for row in rows),
            Decimal("0"),
        ),
        total_transaction_cost_usd=sum(
            (row.total_transaction_cost_usd for row in rows),
            Decimal("0"),
        ),
        rebalance_count=sum(row.rebalance_count for row in rows),
    )


def _event_raw_sqrt_mid(event: V4Event) -> Decimal:
    token0_decimals = token_decimals(event.token0_symbol, event.chain)
    token1_decimals = token_decimals(event.token1_symbol, event.chain)
    native = sqrt_price_x96_to_native_price(
        event.sqrt_price_x96,
        token0_decimals,
        token1_decimals,
    )
    if native <= 0:
        raise CrossPoolContractError("sqrt-derived hold price must be positive")
    if event.token1_symbol.strip().upper() == "CNGN":
        with localcontext() as context:
            context.prec = 60
            return Decimal("1") / native
    return native


def _maximum_drawdown(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise CrossPoolContractError("drawdown path must not be empty")
    if any(not value.is_finite() or value < 0 for value in values):
        raise CrossPoolContractError("drawdown path must be finite and nonnegative")
    peak = values[0]
    drawdown = Decimal("0")
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak)
    return drawdown


def _strategy_summary_pairs(
    summaries: EconomicStrategySummaries,
) -> tuple[tuple[str, StrategySummary], ...]:
    return (
        ("Original", summaries.original),
        ("Forecast-gated", summaries.gated),
        ("Static LP", summaries.static),
        ("Pool-mark hold", summaries.pool_mark_hold_cngn),
        ("Cash", summaries.cash),
    )


def _economic_windows_csv_bytes(rows: Sequence[EconomicWindowRow]) -> bytes:
    ordered = sorted(rows, key=lambda row: (row.window_index, _STRATEGIES.index(row.strategy)))
    csv_rows = tuple(
        (
            row.window_index,
            row.validation_start_timestamp_ms,
            row.validation_end_timestamp_ms,
            row.strategy,
            row.config,
            row.archetype,
            row.route_reason,
            row.position_status,
            row.forecast_gate,
            _decimal_text(row.net_return),
            _decimal_text(row.worst_within_window_drawdown_magnitude),
            _decimal_text(row.total_fees_usd),
            _decimal_text(row.total_transaction_cost_usd),
            row.rebalance_count,
        )
        for row in ordered
    )
    return _csv_bytes(_WINDOW_FIELDS, csv_rows)


def _economic_summary_csv_bytes(
    pairs: Sequence[tuple[str, StrategySummary]],
) -> bytes:
    rows = tuple(
        (
            label,
            summary.window_count,
            summary.active_window_count,
            summary.positive_window_count,
            (
                ""
                if summary.positive_active_window_count is None
                else summary.positive_active_window_count
            ),
            _decimal_text(summary.aggregate_net_return),
            _decimal_text(summary.worst_window_return),
            _decimal_text(summary.worst_within_window_drawdown_magnitude),
            _decimal_text(summary.all_window_positive_rate),
            (
                ""
                if summary.active_window_positive_rate is None
                else _decimal_text(summary.active_window_positive_rate)
            ),
            _decimal_text(summary.total_fees_usd),
            _decimal_text(summary.total_transaction_cost_usd),
            (
                ""
                if summary.fee_to_transaction_cost_ratio is None
                else _decimal_text(summary.fee_to_transaction_cost_ratio)
            ),
            summary.rebalance_count,
        )
        for label, summary in pairs
    )
    return _csv_bytes(_SUMMARY_FIELDS, rows)


def _economic_exclusions_csv_bytes() -> bytes:
    return _csv_bytes(
        _EXCLUSION_FIELDS,
        tuple(
            (window_index, "frozen_warmup_exclusion")
            for window_index in FROZEN_EXCLUDED_WINDOWS
        ),
    )


def _economic_report_bytes(
    pairs: Sequence[tuple[str, StrategySummary]],
    metrics: EconomicDecisionMetrics,
) -> bytes:
    lines = [
        "# Frozen Base LP Economic Report",
        "",
        "Status: generated and unreviewed.",
        "",
        "The evaluation uses windows 3–25, the frozen `upside_tight_v1` routes, "
        "the primary one-hour BSC-to-Base forecast, and `raw_sqrt_mid` for the "
        "pool-mark hold comparator.",
        "",
        "Window returns are summed because each validation window resets capital. "
        "Worst drawdown is the maximum within-window drawdown; reset windows are "
        "never stitched into a synthetic equity path.",
        "",
        f"Economic classification: `{select_economic_class(metrics)}`.",
        "",
        "| Strategy | Active windows | Aggregate net return | Worst window | "
        "Worst within-window drawdown | Fees / transaction costs |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, summary in pairs:
        ratio = summary.fee_to_transaction_cost_ratio
        lines.append(
            "| "
            f"{label} | {summary.active_window_count} | "
            f"{_decimal_text(summary.aggregate_net_return)} | "
            f"{_decimal_text(summary.worst_window_return)} | "
            f"{_decimal_text(summary.worst_within_window_drawdown_magnitude)} | "
            f"{'n/a' if ratio is None else _decimal_text(ratio)} |"
        )
    lines.extend(
        [
            "",
            "This report is an aggregate research artifact, not a deployable signal. "
            "It does not authorize leverage, sizing, or execution changes.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _csv_bytes(
    fieldnames: Sequence[str],
    rows: Sequence[Sequence[object]],
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fieldnames)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise CrossPoolContractError("economic decimal must be finite")
    if value == 0:
        return "0"
    return format(value, "f")


def _required_exact_text(row: Mapping[str, str], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or value == "":
        raise CrossPoolContractError(
            f"entry-state fingerprint field {field} must be a nonempty raw string"
        )
    return value


def _required_int_text(row: Mapping[str, str], field: str) -> int:
    value = _required_exact_text(row, field)
    try:
        return int(value)
    except ValueError as exc:
        raise CrossPoolContractError(
            f"entry-state field {field} must be an integer"
        ) from exc


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise CrossPoolContractError("frozen input CSV is missing its header")
            return [dict(row) for row in reader]
    except OSError as exc:
        raise OutputPublicationError("frozen economic input could not be read") from exc


def _required_csv_text(row: Mapping[str, str | None], field: str) -> str:
    value = row.get(field)
    if value is None or value.strip() == "":
        raise CrossPoolContractError(f"prediction field {field} is required")
    return value.strip()


def _required_csv_int(row: Mapping[str, str | None], field: str) -> int:
    value = _required_csv_text(row, field)
    try:
        return int(value)
    except ValueError as exc:
        raise CrossPoolContractError(
            f"prediction field {field} must be an integer"
        ) from exc


def _required_csv_float(row: Mapping[str, str | None], field: str) -> float:
    value = _required_csv_text(row, field)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CrossPoolContractError(
            f"prediction field {field} must be numeric"
        ) from exc
    if not parsed.is_finite():
        raise CrossPoolContractError(f"prediction field {field} must be finite")
    return float(parsed)


def _utc_from_ms(timestamp_ms: int) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise CrossPoolContractError("timestamp is outside the UTC datetime range") from exc


def _datetime_ms(value: datetime) -> int:
    if value.utcoffset() != timedelta(0) or value.microsecond % 1_000 != 0:
        raise CrossPoolContractError("economic event time must use exact UTC milliseconds")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value - epoch
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000
        + delta.microseconds // 1_000
    )


def _require_pending_statistical_manifest(
    manifest: Mapping[str, JsonValue],
) -> None:
    qa = manifest.get("qa")
    economics = manifest.get("economics")
    review = manifest.get("review")
    if (
        manifest.get("artifact_status") != "generated_unreviewed"
        or not isinstance(qa, dict)
        or qa.get("status") != "pass"
        or not isinstance(economics, dict)
        or economics.get("status") != "pending"
        or not isinstance(review, dict)
        or review.get("status") != "pending"
    ):
        raise OutputPublicationError(
            "economic runner requires unreviewed QA-pass statistics with economics pending"
        )


def _manifest_artifact_sha256(
    manifest: Mapping[str, JsonValue],
    name: str,
) -> str:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get(name), str):
        raise OutputPublicationError("statistical manifest artifact map is invalid")
    return cast(str, artifacts[name])


def _manifest_input_sha256(
    manifest: Mapping[str, JsonValue],
    name: str,
) -> str:
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise OutputPublicationError("statistical manifest provenance is invalid")
    input_sha256 = provenance.get("input_sha256")
    if not isinstance(input_sha256, dict) or not isinstance(
        input_sha256.get(name),
        str,
    ):
        raise OutputPublicationError("statistical manifest input hashes are invalid")
    return cast(str, input_sha256[name])


def require_matching_execution_provenance(
    manifest: Mapping[str, JsonValue],
    *,
    code_commit: str,
    source_diff_sha256: str,
    runtime: RuntimeEnvironment,
) -> None:
    """Require the economic process to match the sealed statistical process."""
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise OutputPublicationError("statistical manifest provenance is invalid")
    if (
        provenance.get("code_commit") != code_commit
        or provenance.get("source_diff_sha256") != source_diff_sha256
        or provenance.get("runtime") != asdict(runtime)
    ):
        raise OutputPublicationError(
            "economic source/runtime provenance does not match sealed statistics"
        )


def _require_current_execution_provenance(
    manifest: Mapping[str, JsonValue],
) -> None:
    runtime = collect_runtime_environment()
    code_commit, source_diff_sha256 = capture_git_state(REPO_ROOT)
    require_matching_execution_provenance(
        manifest,
        code_commit=code_commit,
        source_diff_sha256=source_diff_sha256,
        runtime=runtime,
    )


def snapshot_economic_inputs(
    predictions: Path,
    snapshot_dir: Path,
) -> EconomicInputSnapshots:
    """Capture every economic input before parsing or simulation."""
    sources = (
        (predictions, snapshot_dir / "predictions.csv"),
        (_BASE_EXPERIMENT.feature_csv, snapshot_dir / "base_features.csv"),
        (
            _BASE_EXPERIMENT.qts_feature_csv,
            snapshot_dir / "base_flow_markout_features.csv",
        ),
        (_BASE_EXPERIMENT.history_csv, snapshot_dir / "base_replay.csv"),
    )
    captured: list[ImmutableInputSnapshot] = []
    for source, destination in sources:
        snapshot = snapshot_regular_file(source, destination)
        if snapshot is None:
            raise OutputPublicationError(
                "economic inputs must be regular non-symlink files"
            )
        captured.append(snapshot)
    return EconomicInputSnapshots(
        predictions=captured[0],
        base_features=captured[1],
        flow_markout_features=captured[2],
        base_replay=captured[3],
    )


def _record_invalid_economics(
    out_dir: Path,
    manifest: Mapping[str, JsonValue],
    reason_code: EconomicInvalidReason,
) -> int:
    _require_current_execution_provenance(manifest)
    merged = merge_economic_invalid_manifest(
        manifest,
        EconomicInvalidManifestInput(reason_codes=(reason_code,)),
    )
    augment_evidence_directory(out_dir, (), merged)
    return 2


def _run_locked(predictions: Path, out_dir: Path) -> int:
    manifest = validate_evidence_directory(out_dir)
    _require_pending_statistical_manifest(manifest)
    _require_current_execution_provenance(manifest)
    snapshot_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{out_dir.name}.economic-inputs-",
            dir=out_dir.parent,
        )
    )
    try:
        snapshots = snapshot_economic_inputs(predictions, snapshot_dir)
        expected_prediction_sha256 = _manifest_artifact_sha256(
            manifest,
            "predictive_predictions.csv",
        )
        if snapshots.predictions.sha256 != expected_prediction_sha256:
            return _record_invalid_economics(
                out_dir,
                manifest,
                "PREDICTION_ARTIFACT_HASH_MISMATCH",
            )
        try:
            timeline = load_primary_forecast_timeline(snapshots.predictions.path)
        except CrossPoolContractError:
            return _record_invalid_economics(
                out_dir,
                manifest,
                "PREDICTION_ROWS_INVALID",
            )

        if (
            snapshots.base_features.sha256
            != _manifest_input_sha256(manifest, "base_features")
            or snapshots.base_replay.sha256
            != _manifest_input_sha256(manifest, "base_replay")
        ):
            return _record_invalid_economics(
                out_dir,
                manifest,
                "ECONOMIC_INPUT_INVALID",
            )
        try:
            plan = build_frozen_economic_plan(
                load_base_entry_states(
                    snapshots.base_features.path,
                    snapshots.flow_markout_features.path,
                )
            )
        except EconomicQaError as exc:
            return _record_invalid_economics(out_dir, manifest, exc.reason_code)
        except CrossPoolContractError:
            return _record_invalid_economics(
                out_dir,
                manifest,
                "ECONOMIC_INPUT_INVALID",
            )

        try:
            events = load_v4_events(
                str(snapshots.base_replay.path),
                pool_id=_BASE_EXPERIMENT.pool_config.pool_address,
            )
            rows, coverage = evaluate_frozen_windows(plan, timeline, events)
            rendered = render_economic_artifacts(
                plan,
                rows,
                coverage,
                prediction_sha256=snapshots.predictions.sha256,
                flow_markout_feature_sha256=(
                    snapshots.flow_markout_features.sha256
                ),
            )
        except EconomicQaError as exc:
            return _record_invalid_economics(out_dir, manifest, exc.reason_code)
        except (CrossPoolContractError, ValueError):
            return _record_invalid_economics(
                out_dir,
                manifest,
                "ECONOMIC_INPUT_INVALID",
            )

        _require_current_execution_provenance(manifest)
        merged = merge_economic_manifest(manifest, rendered.manifest_input)
        augment_evidence_directory(out_dir, rendered.artifacts, merged)
        return 0
    finally:
        shutil.rmtree(snapshot_dir)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the frozen forecast-gated Base LP policy",
    )
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        with output_lock(args.out_dir):
            result = _run_locked(args.predictions, args.out_dir)
    except Exception as exc:  # CLI boundary must never expose RPC credentials.
        message = redact_rpc_credentials(str(exc))
        print(f"economic run failed: {message}", file=sys.stderr)
        return 1
    if result == 0:
        print("wrote frozen economic evidence")
    else:
        print("economic evidence was marked not adjudicable", file=sys.stderr)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
