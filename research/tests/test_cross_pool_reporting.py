from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal, localcontext
from functools import lru_cache
from pathlib import Path
from typing import cast

import matplotlib
import pytest

import research.cross_pool.publication as publication
import research.scripts.evaluate_cross_pool_economic_lp as economic_cli
from research.backtester.lp_ledger_attribution import (
    POOL_ATTRIBUTION_ORIENTATIONS,
    AcquisitionPolicyCoverage,
    BundleDigestCoverage,
    EligibleBundleCoverage,
    FROZEN_REPLAY_PARSER_VERSION,
    FrozenTokenCoverage,
    FullTransferCoverage,
    ReconciliationCoverage,
    ReplayCoverage,
    RpcLedgerEvidence,
    TransferChunkCoverage,
    VerifiedLedgerCoverageEvidence,
    WitnessSetCoverage,
    build_rpc_ledger_coverage_bytes,
    frozen_replay_header_sha256,
    frozen_replay_parser_contract_sha256,
    frozen_replay_price_semantics_sha256,
    rpc_ledger_coverage_from_payload,
)
from research.cross_pool.contracts import (
    CausalPanel,
    ConfidenceInterval,
    CrossPoolContractError,
    Direction,
    DirectionalPredictiveAudit,
    DtwNullResult,
    DtwStability,
    DtwWeekResult,
    EventExclusion,
    EventResponse,
    EventStudyResult,
    EventSummary,
    PanelRow,
    PredictionRow,
)
from research.cross_pool.figures import (
    dtw_lag_png,
    event_response_png,
    predictive_performance_png,
    price_gap_png,
)
from research.cross_pool.manifest import (
    EconomicDecisionMetrics,
    EconomicInvalidManifestInput,
    EconomicManifestInput,
    EconomicStrategySummaries,
    ForecastCoverage,
    GeneratedArtifactStatus,
    InputFileProvenance,
    PredictiveSensitivityInput,
    QaBlockedManifestInput,
    RobustnessStatus,
    RunConfiguration,
    RunProvenance,
    RuntimeEnvironment,
    StatisticalManifestInput,
    StrategySummary,
    build_article_manifest,
    build_qa_blocked_manifest,
    canonical_manifest_bytes,
    claims_for_article_branch,
    derive_robustness_status,
    load_and_validate_article_manifest,
    merge_economic_invalid_manifest,
    merge_economic_manifest,
    select_article_branch,
    select_economic_class,
    validate_article_manifest,
)
from research.cross_pool.provenance import (
    ProvenanceCaptureError,
    capture_git_state,
    collect_runtime_environment,
    csv_file_provenance,
)
from research.cross_pool.publication import (
    OutputPublicationError,
    augment_evidence_directory,
    classify_output,
    output_lock,
    publish_or_verify_candidate,
    write_sealed_candidate,
)
from research.cross_pool.qa import audit_predictions
from research.cross_pool.reporting import (
    RenderedArtifact,
    canonical_json_bytes,
    dtw_nulls_csv_bytes,
    dtw_weekly_paths_csv_bytes,
    event_study_csv_bytes,
    panel_csv_bytes,
    prediction_csv_bytes,
)

_HOUR_MS = 3_600_000
_DAY_MS = 86_400_000
_WEEK_MS = 7 * _DAY_MS
_FIRST_REFIT_MS = 1_767_571_200_000
_MONDAY_START_MS = 1_767_571_200_000


@pytest.mark.parametrize(
    ("qa_status", "primary", "reverse", "flags", "expected"),
    (
        (
            "blocked",
            "positive_evidence",
            "positive_evidence",
            (),
            "not_adjudicable_qa",
        ),
        (
            "pass",
            "positive_evidence",
            "inconclusive",
            ("underpowered",),
            "leadership_unresolved",
        ),
        (
            "pass",
            "positive_evidence",
            "inconclusive",
            ("regime_not_adjudicable",),
            "bsc_to_base_incremental",
        ),
        (
            "pass",
            "positive_evidence",
            "inconclusive",
            (),
            "bsc_to_base_incremental",
        ),
        (
            "pass",
            "suggestive",
            "positive_evidence",
            (),
            "base_to_bsc_incremental",
        ),
        (
            "pass",
            "positive_evidence",
            "positive_evidence",
            (),
            "bidirectional_incremental_no_unique_leader",
        ),
        (
            "pass",
            "affirmative_null",
            "affirmative_null",
            (),
            "no_material_incremental_lead",
        ),
        (
            "pass",
            "suggestive",
            "inconclusive",
            (),
            "leadership_unresolved",
        ),
    ),
)
def test_publication_branch_uses_the_frozen_decision_table(
    qa_status,
    primary,
    reverse,
    flags,
    expected,
) -> None:
    robustness = RobustnessStatus(flags=flags)

    assert (
        select_article_branch(qa_status, primary, reverse, robustness)
        == expected
    )


@pytest.mark.parametrize(
    "flag",
    ("underpowered", "unit_dependent", "regime_unstable", "dtw_band_unstable"),
)
def test_reverse_robustness_failure_blocks_a_one_way_primary_claim(flag) -> None:
    assert (
        select_article_branch(
            "pass",
            "positive_evidence",
            "inconclusive",
            RobustnessStatus(flags=(flag,)),
        )
        == "leadership_unresolved"
    )


def test_derive_robustness_status_unions_both_audits_and_dtw() -> None:
    status = derive_robustness_status(
        _predictive_audit("bsc_to_base", day_count=28),
        _predictive_audit("base_to_bsc", day_count=8),
        primary_dtw=_dtw_stability("bsc_to_base", lag_steps=1.0),
        reverse_dtw=_dtw_stability("base_to_bsc", lag_steps=0.0),
    )

    assert status.flags == (
        "dtw_band_unstable",
        "regime_not_adjudicable",
        "underpowered",
    )


def test_derive_robustness_status_rejects_direction_or_horizon_mismatch() -> None:
    primary = _predictive_audit("bsc_to_base", day_count=28)
    reverse = _predictive_audit("base_to_bsc", day_count=28)

    with pytest.raises(CrossPoolContractError, match="primary direction"):
        derive_robustness_status(
            reverse,
            primary,
            primary_dtw=_dtw_stability("bsc_to_base", lag_steps=1.0),
            reverse_dtw=_dtw_stability("base_to_bsc", lag_steps=1.0),
        )
    with pytest.raises(CrossPoolContractError, match="one-hour horizon"):
        derive_robustness_status(
            _predictive_audit(
                "bsc_to_base",
                day_count=28,
                horizon_ms=900_000,
            ),
            reverse,
            primary_dtw=_dtw_stability("bsc_to_base", lag_steps=1.0),
            reverse_dtw=_dtw_stability("base_to_bsc", lag_steps=1.0),
        )
    with pytest.raises(CrossPoolContractError, match="DTW direction"):
        derive_robustness_status(
            primary,
            reverse,
            primary_dtw=_dtw_stability("base_to_bsc", lag_steps=1.0),
            reverse_dtw=_dtw_stability("base_to_bsc", lag_steps=1.0),
        )


@pytest.mark.parametrize(
    ("metrics", "expected"),
    (
        (
            EconomicDecisionMetrics(
                inputs_valid=False,
                original_net_return=Decimal("1"),
                gated_net_return=Decimal("2"),
                original_worst_window_return=Decimal("-1"),
                gated_worst_window_return=Decimal("0"),
                original_worst_drawdown_magnitude=Decimal("2"),
                gated_worst_drawdown_magnitude=Decimal("1"),
            ),
            "not_adjudicable_qa",
        ),
        (
            EconomicDecisionMetrics(
                inputs_valid=True,
                original_net_return=Decimal("1"),
                gated_net_return=Decimal("2"),
                original_worst_window_return=Decimal("-1"),
                gated_worst_window_return=Decimal("-1"),
                original_worst_drawdown_magnitude=Decimal("2"),
                gated_worst_drawdown_magnitude=Decimal("2"),
            ),
            "pareto_improvement",
        ),
        (
            EconomicDecisionMetrics(
                inputs_valid=True,
                original_net_return=Decimal("1"),
                gated_net_return=Decimal("1"),
                original_worst_window_return=Decimal("-1"),
                gated_worst_window_return=Decimal("0"),
                original_worst_drawdown_magnitude=Decimal("2"),
                gated_worst_drawdown_magnitude=Decimal("2"),
            ),
            "return_risk_tradeoff",
        ),
        (
            EconomicDecisionMetrics(
                inputs_valid=True,
                original_net_return=Decimal("1"),
                gated_net_return=Decimal("0"),
                original_worst_window_return=Decimal("-1"),
                gated_worst_window_return=Decimal("0"),
                original_worst_drawdown_magnitude=Decimal("2"),
                gated_worst_drawdown_magnitude=Decimal("1"),
            ),
            "return_risk_tradeoff",
        ),
        (
            EconomicDecisionMetrics(
                inputs_valid=True,
                original_net_return=Decimal("1"),
                gated_net_return=Decimal("1"),
                original_worst_window_return=Decimal("-1"),
                gated_worst_window_return=Decimal("-1"),
                original_worst_drawdown_magnitude=Decimal("2"),
                gated_worst_drawdown_magnitude=Decimal("2"),
            ),
            "no_net_return_improvement",
        ),
    ),
)
def test_economic_class_uses_exact_ordered_boundaries(metrics, expected) -> None:
    assert select_economic_class(metrics) == expected


def test_claim_sets_are_disjoint_and_qa_blocks_every_performance_claim() -> None:
    allowed, forbidden = claims_for_article_branch("bsc_to_base_incremental")
    blocked_allowed, blocked_forbidden = claims_for_article_branch(
        "not_adjudicable_qa"
    )

    assert allowed
    assert not set(allowed).intersection(forbidden)
    assert blocked_allowed == ()
    assert set(allowed).issubset(blocked_forbidden)


def test_statistical_manifest_is_complete_unreviewed_and_economics_pending() -> None:
    manifest = build_article_manifest(_statistical_manifest_input())

    assert manifest["artifact_status"] == "generated_unreviewed"
    assert manifest["qa"]["status"] == "pass"
    assert manifest["robustness"]["flags"] == ["regime_not_adjudicable"]
    assert manifest["predictive"]["primary"]["evidence_class"] == (
        "positive_evidence"
    )
    assert manifest["predictive"]["reverse"]["evidence_class"] == (
        "positive_evidence"
    )
    assert manifest["predictive"]["sensitivity"]["panel_15m"]["primary"][
        "metrics"
    ]["horizon_ms"] == 900_000
    assert manifest["predictive"]["sensitivity"]["panel_4h"]["reverse"][
        "metrics"
    ]["horizon_ms"] == 14_400_000
    assert manifest["economics"] == {
        "status": "pending",
        "frozen_contract": None,
        "decision_metrics": None,
        "strategies": None,
    }
    assert manifest["publication"]["article_branch"] == (
        "bidirectional_incremental_no_unique_leader"
    )
    assert manifest["publication"]["economic_class"] == "unavailable"
    assert set(manifest["artifacts"]) == set(_statistical_artifacts())
    assert manifest["figures"]["lp_performance"] is None
    validate_article_manifest(manifest)


def test_generated_builder_cannot_mark_its_manifest_reviewed() -> None:
    with pytest.raises(
        CrossPoolContractError,
        match="generated code cannot mark evidence reviewed",
    ):
        build_article_manifest(
            _statistical_manifest_input(),
            artifact_status=cast(GeneratedArtifactStatus, "reviewed"),
        )


def test_economic_merge_is_typed_narrow_and_binds_frozen_contracts() -> None:
    statistical = build_article_manifest(_statistical_manifest_input())
    original = _strategy_summary(
        net="0.01",
        worst="-0.02",
        drawdown="0.03",
    )
    gated = _strategy_summary(
        net="0.02",
        worst="-0.01",
        drawdown="0.02",
    )
    economic_artifacts = {
        name: _digest(name)
        for name in (
            "frozen_policy_windows.csv",
            "frozen_policy_summary.csv",
            "frozen_policy_exclusions.csv",
            "frozen_policy_report.md",
            "lp_performance.png",
        )
    }
    economic_input = EconomicManifestInput(
            entry_state_contract_sha256=(
                "61d9f7977c9806bf25c01a3f2db9bc5ecb56bb7612fcc6c72bf68a94a31f118b"
            ),
            parameter_contract_sha256=(
                "abe886b69c909010a1e1a27eb16d740448cde2993c2a5ec7835e1b03a864888f"
            ),
            prediction_sha256=cast(
                str,
                statistical["artifacts"]["predictive_predictions.csv"],
            ),
            flow_markout_feature_sha256="f" * 64,
            forecast_coverage=ForecastCoverage(
                routed_entry_decision_count=4,
                forecast_covered_entry_decision_count=4,
                maximum_selected_forecast_age_ms=3_599_999,
                first_routed_entry_timestamp_ms=100,
                last_routed_entry_timestamp_ms=200,
            ),
            decision_metrics=EconomicDecisionMetrics(
                inputs_valid=True,
                original_net_return=Decimal("0.01"),
                gated_net_return=Decimal("0.02"),
                original_worst_window_return=Decimal("-0.02"),
                gated_worst_window_return=Decimal("-0.01"),
                original_worst_drawdown_magnitude=Decimal("0.03"),
                gated_worst_drawdown_magnitude=Decimal("0.02"),
            ),
            strategies=EconomicStrategySummaries(
                original=original,
                gated=gated,
                static=_strategy_summary(
                    net="0",
                    worst="0",
                    drawdown="0",
                    active_window_count=23,
                ),
                pool_mark_hold_cngn=_strategy_summary(
                    net="0",
                    worst="0",
                    drawdown="0",
                    active_window_count=23,
                    total_fees="0",
                    total_transaction_cost="0",
                    rebalance_count=0,
                ),
                cash=_strategy_summary(
                    net="0",
                    worst="0",
                    drawdown="0",
                    active_window_count=0,
                    positive_window_count=0,
                    positive_active_window_count=None,
                    total_fees="0",
                    total_transaction_cost="0",
                    rebalance_count=0,
                ),
            ),
            lp_performance_figure={
                "artifact": "lp_performance.png",
                "sha256": economic_artifacts["lp_performance.png"],
            },
            artifacts=economic_artifacts,
        )
    merged = merge_economic_manifest(
        statistical,
        economic_input,
    )

    assert statistical["economics"]["status"] == "pending"
    assert merged["economics"]["status"] == "complete"
    assert merged["economics"]["frozen_contract"][
        "entry_state_contract_sha256"
    ] == "61d9f7977c9806bf25c01a3f2db9bc5ecb56bb7612fcc6c72bf68a94a31f118b"
    assert merged["economics"]["frozen_contract"][
        "parameter_contract_sha256"
    ] == "abe886b69c909010a1e1a27eb16d740448cde2993c2a5ec7835e1b03a864888f"
    assert merged["economics"]["frozen_contract"][
        "forecast_agreement_threshold_bps"
    ] == "5"
    assert merged["publication"]["economic_class"] == "pareto_improvement"
    validate_article_manifest(merged)

    with pytest.raises(
        CrossPoolContractError,
        match="entry-state fingerprint",
    ):
        replace(economic_input, entry_state_contract_sha256="0" * 64)


def test_economic_counts_are_strict_and_rates_are_derived_deterministically() -> None:
    with pytest.raises(CrossPoolContractError, match="nonnegative integers"):
        ForecastCoverage(
            routed_entry_decision_count=4.0,  # type: ignore[arg-type]
            forecast_covered_entry_decision_count=4,
            maximum_selected_forecast_age_ms=1,
            first_routed_entry_timestamp_ms=100,
            last_routed_entry_timestamp_ms=200,
        )

    with localcontext() as context:
        context.prec = 3
        summary = StrategySummary(
            window_count=23,
            active_window_count=3,
            positive_window_count=1,
            positive_active_window_count=1,
            aggregate_net_return=Decimal("0.01"),
            worst_window_return=Decimal("-0.02"),
            worst_within_window_drawdown_magnitude=Decimal("0.03"),
            total_fees_usd=Decimal("1"),
            total_transaction_cost_usd=Decimal("3"),
            rebalance_count=1,
        )
        assert summary.all_window_positive_rate == Decimal(
            "0.043478260869565217391304347826086956521739130434783"
        )
        assert summary.active_window_positive_rate == Decimal(
            "0.33333333333333333333333333333333333333333333333333"
        )
        assert summary.fee_to_transaction_cost_ratio == Decimal(
            "0.33333333333333333333333333333333333333333333333333"
        )

    with pytest.raises(CrossPoolContractError, match="23 evaluation windows"):
        replace(summary, window_count=23.0)  # type: ignore[arg-type]
    with pytest.raises(CrossPoolContractError, match="counts are inconsistent"):
        replace(summary, active_window_count=3.0)  # type: ignore[arg-type]


def test_inactive_strategy_must_be_an_exact_zero_cash_observation() -> None:
    inactive = _strategy_summary(
        net="0",
        worst="0",
        drawdown="0",
        active_window_count=0,
        positive_window_count=0,
        positive_active_window_count=None,
        total_fees="0",
        total_transaction_cost="0",
        rebalance_count=0,
    )

    with pytest.raises(CrossPoolContractError, match="inactive strategy"):
        replace(inactive, aggregate_net_return=Decimal("0.01"))
    with pytest.raises(CrossPoolContractError, match="inactive strategy"):
        replace(inactive, total_transaction_cost_usd=Decimal("1"))


def test_invalid_economic_inputs_are_recorded_without_fabricated_results() -> None:
    statistical = build_article_manifest(_statistical_manifest_input())

    merged = merge_economic_invalid_manifest(
        statistical,
        EconomicInvalidManifestInput(
            reason_codes=("FORECAST_COVERAGE_INVALID",),
        ),
    )

    assert statistical["economics"]["status"] == "pending"
    assert merged["economics"] == {
        "status": "not_adjudicable_qa",
        "reason_codes": ["FORECAST_COVERAGE_INVALID"],
        "frozen_contract": None,
        "decision_metrics": None,
        "strategies": None,
    }
    assert merged["publication"]["economic_class"] == "not_adjudicable_qa"
    assert merged["figures"] == statistical["figures"]
    assert merged["artifacts"] == statistical["artifacts"]
    validate_article_manifest(merged)


def test_economic_comparators_reject_trading_activity_for_cash_and_hold() -> None:
    original = _strategy_summary(net="0", worst="0", drawdown="0")
    static = _strategy_summary(
        net="0",
        worst="0",
        drawdown="0",
        active_window_count=23,
    )
    hold = _strategy_summary(
        net="0",
        worst="0",
        drawdown="0",
        active_window_count=23,
        total_fees="0",
        total_transaction_cost="0",
        rebalance_count=0,
    )
    cash = _strategy_summary(
        net="0",
        worst="0",
        drawdown="0",
        active_window_count=0,
        positive_window_count=0,
        positive_active_window_count=None,
        total_fees="0",
        total_transaction_cost="0",
        rebalance_count=0,
    )

    with pytest.raises(CrossPoolContractError, match="cash comparator"):
        EconomicStrategySummaries(
            original=original,
            gated=original,
            static=static,
            pool_mark_hold_cngn=hold,
            cash=replace(cash, total_fees_usd=Decimal("1")),
        )
    with pytest.raises(CrossPoolContractError, match="pool-mark hold comparator"):
        EconomicStrategySummaries(
            original=original,
            gated=original,
            static=static,
            pool_mark_hold_cngn=replace(hold, rebalance_count=1),
            cash=cash,
        )


def test_economic_semantics_reject_cross_field_contradictions() -> None:
    manifest = build_article_manifest(_statistical_manifest_input())
    mutated = deepcopy(manifest)
    mutated["economics"] = {
        "status": "complete",
        "frozen_contract": {
            "base_pool": "uni-base",
            "policy_profile": "upside_tight_v1",
            "static_config": "static_spot_w0025",
            "initial_capital_usd": "1200",
            "prediction_artifact": "predictive_predictions.csv",
            "prediction_sha256": manifest["artifacts"]["predictive_predictions.csv"],
            "prediction_direction": "bsc_to_base",
            "prediction_horizon_ms": 3_600_000,
            "forecast_agreement_threshold_bps": "5",
            "flow_markout_feature_sha256": "f" * 64,
            "entry_state_contract_sha256": "a" * 64,
            "parameter_contract_sha256": "b" * 64,
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
                "routed_entry_decision_count": 1,
                "forecast_covered_entry_decision_count": 2,
                "maximum_selected_forecast_age_ms": 1,
                "first_routed_entry_timestamp_ms": 200,
                "last_routed_entry_timestamp_ms": 100,
            },
        },
        "decision_metrics": {
            "inputs_valid": True,
            "original_net_return_fraction": "0",
            "gated_net_return_fraction": "0",
            "original_worst_window_return_fraction": "0",
            "gated_worst_window_return_fraction": "0",
            "original_worst_within_window_drawdown_magnitude_fraction": "0",
            "gated_worst_within_window_drawdown_magnitude_fraction": "0",
        },
        "strategies": {
            name: _strategy_summary_payload()
            for name in (
                "original",
                "gated",
                "static",
                "pool_mark_hold_cngn",
                "cash",
            )
        },
    }
    mutated["publication"]["economic_class"] = "no_net_return_improvement"
    for name in (
        "frozen_policy_windows.csv",
        "frozen_policy_summary.csv",
        "frozen_policy_exclusions.csv",
        "frozen_policy_report.md",
        "lp_performance.png",
    ):
        mutated["artifacts"][name] = _digest(name)
    mutated["figures"]["lp_performance"] = {
        "artifact": "lp_performance.png",
        "sha256": _digest("lp_performance.png"),
    }

    with pytest.raises(CrossPoolContractError):
        validate_article_manifest(mutated)


def test_panel_csv_uses_contract_field_order_and_unix_newlines() -> None:
    panel = CausalPanel(
        common_interval_start_ms=100,
        common_interval_end_ms=400,
        horizon_ms=900_000,
        rows=(_panel_row(),),
    )

    rendered = panel_csv_bytes(panel)

    assert rendered.splitlines()[0].decode() == (
        "timestamp_ms,horizon_ms,base_state_timestamp_ms,bsc_state_timestamp_ms,"
        "base_forward_state_timestamp_ms,bsc_forward_state_timestamp_ms,"
        "base_lag_block_number,bsc_lag_block_number,base_state_block_number,"
        "bsc_state_block_number,base_forward_block_number,bsc_forward_block_number,"
        "base_age_ms,bsc_age_ms,base_mid,bsc_mid,base_trailing_return_bps,"
        "bsc_trailing_return_bps,base_minus_bsc_gap_bps,base_forward_return_bps,"
        "bsc_forward_return_bps,base_regime,bsc_regime"
    )
    assert b"\r\n" not in rendered
    assert rendered.endswith(b"\n")


def test_prediction_csv_uses_the_frozen_economic_handoff_header() -> None:
    rows = (
        PredictionRow(
            timestamp_ms=100,
            target_timestamp_ms=200,
            horizon_ms=_HOUR_MS,
            refit_timestamp_ms=50,
            fold_index=0,
            direction="bsc_to_base",
            target_regime="early",
            source_regime="early",
            actual_bps=-0.0,
            baseline_prediction_bps=1.25,
            cross_prediction_bps=1.5,
        ),
    )

    rendered = prediction_csv_bytes(rows)

    assert rendered.splitlines() == [
        (
            b"timestamp_ms,target_timestamp_ms,horizon_ms,refit_timestamp_ms,"
            b"fold_index,direction,actual_bps,baseline_prediction_bps,"
            b"cross_prediction_bps"
        ),
        b"100,200,3600000,50,0,bsc_to_base,0,1.25,1.5",
    ]


def test_reporting_json_is_sorted_finite_and_has_one_trailing_newline() -> None:
    assert canonical_json_bytes({"z": 1, "a": [2.5]}) == b'{"a":[2.5],"z":1}\n'
    with pytest.raises(CrossPoolContractError, match="finite"):
        canonical_json_bytes({"bad": float("inf")})


def test_event_study_csv_uses_one_closed_union_schema() -> None:
    response = EventResponse(
        direction="bsc_to_base",
        source_pool="uni-bsc",
        target_pool="uni-base",
        shock_timestamp_ms=_MONDAY_START_MS,
        shock_day_utc=date(2026, 1, 5),
        horizon_ms=900_000,
        source_move_bps=6.0,
        target_start_timestamp_ms=_MONDAY_START_MS,
        target_end_timestamp_ms=_MONDAY_START_MS + 900_000,
        target_response_bps=2.0,
        direction_agrees=True,
    )
    exclusion = EventExclusion(
        direction="base_to_bsc",
        shock_timestamp_ms=_MONDAY_START_MS + _DAY_MS,
        shock_day_utc=date(2026, 1, 6),
        horizon_ms=_HOUR_MS,
        reason="missing_target_end_state",
    )

    rendered = event_study_csv_bytes(
        (
            EventStudyResult(responses=(response,), exclusions=()),
            EventStudyResult(responses=(), exclusions=(exclusion,)),
        )
    )

    assert rendered.splitlines()[0] == (
        b"outcome_type,direction,shock_timestamp_ms,shock_day_utc,horizon_ms,"
        b"source_pool,target_pool,source_move_bps,target_start_timestamp_ms,"
        b"target_end_timestamp_ms,target_response_bps,direction_agrees,"
        b"exclusion_reason"
    )
    assert len(rendered.splitlines()) == 3


def test_dtw_serializers_expand_paths_and_use_stable_tie_ranks() -> None:
    matches = tuple((index, index) for index in range(672))
    week = DtwWeekResult(
        week_start_timestamp_ms=_MONDAY_START_MS,
        direction="bsc_to_base",
        band_steps=4,
        path_length=len(matches),
        total_cost=0.0,
        normalized_cost=0.0,
        median_signed_lag_steps=0.0,
        matches=matches,
    )
    nulls = tuple(
        DtwNullResult(
            week_start_timestamp_ms=_MONDAY_START_MS,
            direction="bsc_to_base",
            band_steps=4,
            rotation_days=rotation_days,
            path_length=672,
            total_cost=float(672 * (1 if rotation_days <= 2 else 2)),
            normalized_cost=float(1 if rotation_days <= 2 else 2),
            observed_normalized_cost=1.0,
            observed_cost_improvement=float(0 if rotation_days <= 2 else 1),
            median_signed_lag_steps=float(0 if rotation_days <= 2 else 1),
            observed_median_signed_lag_steps=0.0,
            observed_signed_lag_difference_steps=float(
                0 if rotation_days <= 2 else 1
            ),
        )
        for rotation_days in range(1, 7)
    )

    paths_csv = dtw_weekly_paths_csv_bytes((week,))
    nulls_csv = dtw_nulls_csv_bytes(nulls)

    assert len(paths_csv.splitlines()) == 673
    assert paths_csv.splitlines()[1].endswith(b",0,0")
    assert nulls_csv.splitlines()[0].endswith(
        b",observed_cost_rank,observed_absolute_lag_rank"
    )
    assert {
        tuple(line.rsplit(b",", 2)[1:]) for line in nulls_csv.splitlines()[1:]
    } == {
        (b"1", b"1")
    }


def test_statistical_figures_are_nonempty_and_byte_stable() -> None:
    panel = CausalPanel(
        common_interval_start_ms=100,
        common_interval_end_ms=400,
        horizon_ms=_HOUR_MS,
        rows=(replace(_panel_row(), horizon_ms=_HOUR_MS),),
    )
    inferences = tuple(
        _predictive_audit(
            direction,
            day_count=28,
            horizon_ms=horizon_ms,
        ).inference
        for horizon_ms in (900_000, _HOUR_MS, 14_400_000)
        for direction in ("bsc_to_base", "base_to_bsc")
    )
    summaries = tuple(
        EventSummary(
            direction=direction,
            horizon_ms=horizon_ms,
            event_count=12,
            event_day_count=6,
            mean_response_bps=ConfidenceInterval(2.0, 1.0, 3.0),
            median_response_bps=ConfidenceInterval(1.5, 0.5, 2.5),
            direction_agreement=ConfidenceInterval(0.75, 0.6, 0.9),
        )
        for horizon_ms in (900_000, _HOUR_MS, 14_400_000)
        for direction in ("bsc_to_base", "base_to_bsc")
    )
    stabilities = (
        _dtw_stability("bsc_to_base", lag_steps=1.0),
        _dtw_stability("base_to_bsc", lag_steps=-1.0),
    )
    renderers = (
        lambda: price_gap_png(panel),
        lambda: predictive_performance_png(inferences),
        lambda: event_response_png(summaries),
        lambda: dtw_lag_png(stabilities),
    )

    for render in renderers:
        first = render()
        second = render()
        assert first.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(first) > 1_000
        assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()

    assert dtw_lag_png(stabilities) == dtw_lag_png(tuple(reversed(stabilities)))


def test_price_gap_figure_isolated_from_caller_rc_state() -> None:
    panel = CausalPanel(
        common_interval_start_ms=100,
        common_interval_end_ms=400,
        horizon_ms=_HOUR_MS,
        rows=(replace(_panel_row(), horizon_ms=_HOUR_MS),),
    )
    original = matplotlib.rcParams["text.antialiased"]
    try:
        first = price_gap_png(panel)
        matplotlib.rcParams["text.antialiased"] = not original
        second = price_gap_png(panel)
    finally:
        matplotlib.rcParams["text.antialiased"] = original

    assert first == second


def test_price_gap_figure_rejects_nonfinite_or_nonmonotonic_rows() -> None:
    row = replace(_panel_row(), horizon_ms=_HOUR_MS)
    with pytest.raises(CrossPoolContractError, match="finite log gaps"):
        price_gap_png(
            CausalPanel(
                common_interval_start_ms=100,
                common_interval_end_ms=400,
                horizon_ms=_HOUR_MS,
                rows=(replace(row, base_minus_bsc_gap_bps=float("nan")),),
            )
        )
    with pytest.raises(CrossPoolContractError, match="regular increasing"):
        price_gap_png(
            CausalPanel(
                common_interval_start_ms=100,
                common_interval_end_ms=400,
                horizon_ms=_HOUR_MS,
                rows=(row, replace(row, timestamp_ms=row.timestamp_ms - _HOUR_MS)),
            )
        )


def test_runtime_environment_collection_is_repeatable_and_complete() -> None:
    first = collect_runtime_environment()
    second = collect_runtime_environment()

    assert first == second
    assert first.matplotlib_backend == "Agg"
    assert first.font_name == "DejaVu Sans"
    assert len(first.numpy_build_sha256) == 64
    assert len(first.font_file_sha256) == 64
    assert len(first.figure_rcparams_sha256) == 64


def test_csv_file_provenance_hashes_bytes_and_parses_timestamp_kinds(
    tmp_path: Path,
) -> None:
    milliseconds = tmp_path / "milliseconds.csv"
    milliseconds.write_bytes(
        b"block_number,timestamp_ms,value\n1,100,a\n2,250,b\n"
    )
    block_time = tmp_path / "block_time.csv"
    block_time.write_bytes(
        b"block_number,block_time,value\n1,1970-01-01T00:00:00.100Z,a\n"
        b"2,1970-01-01T00:00:00.250Z,b\n"
    )

    millisecond_result = csv_file_provenance(
        milliseconds,
        timestamp_field="timestamp_ms",
    )
    block_time_result = csv_file_provenance(
        block_time,
        timestamp_field="block_time",
    )

    assert millisecond_result.rows == block_time_result.rows == 2
    assert millisecond_result.first_block == 1
    assert block_time_result.last_block == 2
    assert millisecond_result.first_timestamp_ms == 100
    assert block_time_result.last_timestamp_ms == 250
    assert millisecond_result.sha256 == hashlib.sha256(milliseconds.read_bytes()).hexdigest()


def test_git_provenance_rejects_dirty_or_untracked_executable_source(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repository, check=True)
    subprocess.run(
        ("git", "config", "user.email", "research@example.invalid"),
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Research Test"),
        cwd=repository,
        check=True,
    )
    (repository / "engine").mkdir()
    (repository / "research").mkdir()
    source = repository / "research" / "runner.py"
    source.write_text("VALUE = 1\n")
    (repository / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-qm", "fixture"), cwd=repository, check=True)

    commit, diff_sha256 = capture_git_state(repository)
    assert len(commit) == 40
    assert diff_sha256 == hashlib.sha256(b"").hexdigest()

    source.write_text("VALUE = 2\n")
    with pytest.raises(ProvenanceCaptureError, match="tracked and clean"):
        capture_git_state(repository)
    subprocess.run(("git", "restore", "research/runner.py"), cwd=repository, check=True)
    (repository / "engine" / "untracked.py").write_text("VALUE = 3\n")
    with pytest.raises(ProvenanceCaptureError, match="tracked and clean"):
        capture_git_state(repository)


def test_sealed_candidate_publishes_once_then_verifies_without_mutation(
    tmp_path: Path,
) -> None:
    manifest = build_article_manifest(_statistical_manifest_input())
    artifacts = tuple(
        RenderedArtifact(name, name.encode("utf-8"))
        for name in _statistical_artifacts()
    )
    out_dir = tmp_path / "evidence"
    first_candidate = tmp_path / "first-candidate"
    write_sealed_candidate(first_candidate, artifacts, manifest)

    assert classify_output(out_dir) == "publish_new"
    publish_or_verify_candidate(
        out_dir,
        first_candidate,
        mode="publish_new",
    )
    original = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in out_dir.iterdir()
    }

    second_candidate = tmp_path / "second-candidate"
    write_sealed_candidate(second_candidate, artifacts, manifest)
    assert classify_output(out_dir) == "verify_existing"
    publish_or_verify_candidate(
        out_dir,
        second_candidate,
        mode="verify_existing",
    )

    assert not second_candidate.exists()
    assert original == {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in out_dir.iterdir()
    }


def test_atomic_directory_publish_never_replaces_an_existing_target(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "candidate.txt").write_text("candidate")
    target = tmp_path / "target"
    target.mkdir()
    (target / "sentinel.txt").write_text("existing")

    with pytest.raises(OutputPublicationError, match="no longer absent"):
        publication._rename_directory_no_replace(candidate, target)

    assert (candidate / "candidate.txt").read_text() == "candidate"
    assert (target / "sentinel.txt").read_text() == "existing"


def test_atomic_economic_augmentation_preserves_sealed_statistics(
    tmp_path: Path,
) -> None:
    statistical = build_article_manifest(_statistical_manifest_input())
    artifacts = tuple(
        RenderedArtifact(name, name.encode("utf-8"))
        for name in _statistical_artifacts()
    )
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    write_sealed_candidate(candidate, artifacts, statistical)
    publish_or_verify_candidate(out_dir, candidate, mode="publish_new")
    statistical_bytes = {
        artifact.relative_name: (out_dir / artifact.relative_name).read_bytes()
        for artifact in artifacts
    }
    merged = merge_economic_invalid_manifest(
        statistical,
        EconomicInvalidManifestInput(
            reason_codes=("FORECAST_COVERAGE_INVALID",),
        ),
    )

    with output_lock(out_dir):
        augment_evidence_directory(out_dir, (), merged)

    final = publication.validate_evidence_directory(out_dir)
    assert final["economics"]["status"] == "not_adjudicable_qa"
    assert {
        name: (out_dir / name).read_bytes() for name in statistical_bytes
    } == statistical_bytes


def test_economic_augmentation_rejects_changes_to_sealed_manifest_groups(
    tmp_path: Path,
) -> None:
    statistical = build_article_manifest(_statistical_manifest_input())
    artifacts = tuple(
        RenderedArtifact(name, name.encode("utf-8"))
        for name in _statistical_artifacts()
    )
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    write_sealed_candidate(candidate, artifacts, statistical)
    publish_or_verify_candidate(out_dir, candidate, mode="publish_new")
    merged = merge_economic_invalid_manifest(
        statistical,
        EconomicInvalidManifestInput(
            reason_codes=("FORECAST_COVERAGE_INVALID",),
        ),
    )
    cast(dict[str, object], merged["provenance"])["runtime"] = {
        **cast(dict[str, object], cast(dict[str, object], merged["provenance"])["runtime"]),
        "python_version": "9.9.9",
    }

    with output_lock(out_dir):
        with pytest.raises(
            OutputPublicationError,
            match="cannot change sealed statistical manifest fields",
        ):
            augment_evidence_directory(out_dir, (), merged)


def test_failed_economic_directory_exchange_leaves_old_evidence_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statistical = build_article_manifest(_statistical_manifest_input())
    artifacts = tuple(
        RenderedArtifact(name, name.encode("utf-8"))
        for name in _statistical_artifacts()
    )
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    write_sealed_candidate(candidate, artifacts, statistical)
    publish_or_verify_candidate(out_dir, candidate, mode="publish_new")
    before = {path.name: path.read_bytes() for path in out_dir.iterdir()}
    merged = merge_economic_invalid_manifest(
        statistical,
        EconomicInvalidManifestInput(
            reason_codes=("FORECAST_COVERAGE_INVALID",),
        ),
    )

    def fail_exchange(_source: Path, _target: Path) -> None:
        raise OutputPublicationError("injected exchange failure")

    monkeypatch.setattr(publication, "_rename_directory_exchange", fail_exchange)
    with output_lock(out_dir):
        with pytest.raises(OutputPublicationError, match="injected"):
            augment_evidence_directory(out_dir, (), merged)

    assert {path.name: path.read_bytes() for path in out_dir.iterdir()} == before
    assert not tuple(tmp_path.glob(".evidence.economic-*"))


def test_economic_runner_records_prediction_hash_mismatch_without_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statistical = build_article_manifest(_statistical_manifest_input())
    artifacts = tuple(
        RenderedArtifact(name, name.encode("utf-8"))
        for name in _statistical_artifacts()
    )
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    write_sealed_candidate(candidate, artifacts, statistical)
    publish_or_verify_candidate(out_dir, candidate, mode="publish_new")
    predictions = tmp_path / "wrong-predictions.csv"
    predictions.write_text("not the sealed prediction artifact\n")

    _patch_economic_execution_provenance(monkeypatch)

    assert economic_cli.main(
        ("--predictions", str(predictions), "--out-dir", str(out_dir))
    ) == 2

    final = publication.validate_evidence_directory(out_dir)
    assert final["economics"] == {
        "status": "not_adjudicable_qa",
        "reason_codes": ["PREDICTION_ARTIFACT_HASH_MISMATCH"],
        "frozen_contract": None,
        "decision_metrics": None,
        "strategies": None,
    }
    assert final["figures"]["lp_performance"] is None
    assert set(final["artifacts"]) == set(_statistical_artifacts())


def test_economic_runner_records_malformed_entry_states_as_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statistical = build_article_manifest(_statistical_manifest_input())
    artifacts = tuple(
        RenderedArtifact(name, name.encode("utf-8"))
        for name in _statistical_artifacts()
    )
    out_dir = tmp_path / "evidence"
    candidate = tmp_path / "candidate"
    write_sealed_candidate(candidate, artifacts, statistical)
    publish_or_verify_candidate(out_dir, candidate, mode="publish_new")
    predictions = out_dir / "predictive_predictions.csv"
    _patch_economic_execution_provenance(monkeypatch)
    monkeypatch.setattr(
        economic_cli,
        "_manifest_input_sha256",
        lambda _manifest, name: hashlib.sha256(
            (
                economic_cli._BASE_EXPERIMENT.history_csv
                if name == "base_replay"
                else economic_cli._BASE_EXPERIMENT.feature_csv
            ).read_bytes()
        ).hexdigest(),
    )
    monkeypatch.setattr(
        economic_cli,
        "load_primary_forecast_timeline",
        lambda _path: object(),
    )
    monkeypatch.setattr(
        economic_cli,
        "load_base_entry_states",
        lambda _features, _qts: (_ for _ in ()).throw(
            CrossPoolContractError("malformed entry state")
        ),
    )

    assert economic_cli.main(
        ("--predictions", str(predictions), "--out-dir", str(out_dir))
    ) == 2

    final = publication.validate_evidence_directory(out_dir)
    assert final["economics"]["reason_codes"] == ["ECONOMIC_INPUT_INVALID"]


def test_output_classifier_refuses_existing_qa_or_corrupt_evidence(
    tmp_path: Path,
) -> None:
    qa_dir = tmp_path / "qa"
    qa_dir.mkdir()
    (qa_dir / "article_manifest.json").write_bytes(
        canonical_manifest_bytes(
            build_qa_blocked_manifest(
                QaBlockedManifestInput(
                    provenance=_blocked_provenance(),
                    reason_codes=("ANALYSIS_CONTRACT_INVALID",),
                )
            )
        )
    )
    with pytest.raises(OutputPublicationError, match="immutable"):
        classify_output(qa_dir)

    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "article_manifest.json").write_text("not-json")
    with pytest.raises(OutputPublicationError, match="valid evidence"):
        classify_output(corrupt)


def test_manifest_json_is_canonical_and_rejects_nonfinite_values() -> None:
    assert canonical_manifest_bytes({"z": 1, "a": "é"}) == (
        '{"a":"é","z":1}\n'.encode()
    )
    with pytest.raises(CrossPoolContractError, match="finite"):
        canonical_manifest_bytes({"bad": float("nan")})


def test_qa_blocked_manifest_has_only_stable_failure_evidence() -> None:
    manifest = build_qa_blocked_manifest(
        QaBlockedManifestInput(
            provenance=_blocked_provenance(),
            reason_codes=("CAUSAL_ALIGNMENT_INVALID", "FEATURE_BASE_INVALID"),
        )
    )

    assert manifest["artifact_status"] == "qa_blocked"
    assert manifest["qa"] == {
        "status": "blocked",
        "reasons": ["CAUSAL_ALIGNMENT_INVALID", "FEATURE_BASE_INVALID"],
        "causal_audit_counts": None,
    }
    assert manifest["artifacts"] == {}
    assert set(manifest["figures"].values()) == {None}
    for group in ("predictive", "event_study", "dtw", "market_structure"):
        assert manifest[group]["status"] == "unavailable"
    assert manifest["economics"]["status"] == "unavailable"
    assert manifest["publication"]["allowed_claims"] == []
    assert manifest["publication"]["article_branch"] == "not_adjudicable_qa"
    assert manifest["publication"]["economic_class"] == "not_adjudicable_qa"
    assert "/Users/" not in canonical_manifest_bytes(manifest).decode()


def test_qa_blocked_builder_rejects_free_form_or_unsorted_reasons() -> None:
    with pytest.raises(CrossPoolContractError, match="sorted and unique"):
        build_qa_blocked_manifest(
            QaBlockedManifestInput(
                provenance=_blocked_provenance(),
                reason_codes=("FEATURE_BASE_INVALID", "CAUSAL_ALIGNMENT_INVALID"),
            )
        )
    with pytest.raises(CrossPoolContractError, match="unsupported QA reason"):
        build_qa_blocked_manifest(
            QaBlockedManifestInput(
                provenance=_blocked_provenance(),
                reason_codes=("/private/tmp/input.csv failed",),
            )
        )


def test_manifest_schema_rejects_unknown_and_missing_nested_fields() -> None:
    manifest = build_qa_blocked_manifest(
        QaBlockedManifestInput(
            provenance=_blocked_provenance(),
            reason_codes=("FEATURE_BASE_INVALID",),
        )
    )
    extra = {**manifest, "unexpected": 1}
    missing_review = dict(manifest)
    del missing_review["review"]
    nested_extra = dict(manifest)
    nested_extra["qa"] = {**manifest["qa"], "raw_error": "secret path"}

    for mutation in (extra, missing_review, nested_extra):
        with pytest.raises(CrossPoolContractError, match="schema"):
            validate_article_manifest(mutation)


def test_reviewed_qa_blocked_shape_requires_review_identity_and_time() -> None:
    manifest = build_qa_blocked_manifest(
        QaBlockedManifestInput(
            provenance=_blocked_provenance(),
            reason_codes=("FEATURE_BASE_INVALID",),
        )
    )
    reviewed = dict(manifest)
    reviewed["artifact_status"] = "reviewed"
    reviewed["review"] = {
        "status": "reviewed",
        "reviewed_by": "sol_ultra",
        "reviewed_at_utc": "2026-07-17T12:00:00Z",
    }
    validate_article_manifest(reviewed)

    reviewed["review"] = {
        "status": "reviewed",
        "reviewed_by": None,
        "reviewed_at_utc": None,
    }
    with pytest.raises(CrossPoolContractError, match="schema"):
        validate_article_manifest(reviewed)


def test_manifest_loader_requires_exact_canonical_bytes(tmp_path: Path) -> None:
    manifest = build_qa_blocked_manifest(
        QaBlockedManifestInput(
            provenance=_blocked_provenance(),
            reason_codes=("FEATURE_BASE_INVALID",),
        )
    )
    path = tmp_path / "article_manifest.json"
    path.write_bytes(canonical_manifest_bytes(manifest))
    assert load_and_validate_article_manifest(path) == manifest

    path.write_text("{\n  \"artifact_status\": \"qa_blocked\"\n}\n")
    with pytest.raises(CrossPoolContractError, match="canonical"):
        load_and_validate_article_manifest(path)

    path.write_text('{"x":1,"x":2}\n')
    with pytest.raises(CrossPoolContractError, match="duplicate"):
        load_and_validate_article_manifest(path)

    path.write_text('{"x":NaN}\n')
    with pytest.raises(CrossPoolContractError, match="non-finite"):
        load_and_validate_article_manifest(path)


def test_manifest_semantics_bind_input_hash_and_interval_availability() -> None:
    manifest = build_qa_blocked_manifest(
        QaBlockedManifestInput(
            provenance=_blocked_provenance(),
            reason_codes=("FEATURE_BASE_INVALID",),
        )
    )
    mutated = deepcopy(manifest)
    mutated["provenance"]["input_sha256"]["base_features"] = "f" * 64

    with pytest.raises(CrossPoolContractError, match="hash and interval"):
        validate_article_manifest(mutated)


def test_manifest_semantics_reject_reversed_input_interval() -> None:
    manifest = build_qa_blocked_manifest(
        QaBlockedManifestInput(
            provenance=_blocked_provenance(),
            reason_codes=("FEATURE_BASE_INVALID",),
        )
    )
    mutated = deepcopy(manifest)
    mutated["provenance"]["input_sha256"]["base_features"] = "f" * 64
    mutated["provenance"]["input_intervals"]["base_features"] = {
        "rows": 1,
        "first_block": 2,
        "last_block": 1,
        "first_timestamp_ms": 2,
        "last_timestamp_ms": 1,
    }

    with pytest.raises(CrossPoolContractError, match="ordered"):
        validate_article_manifest(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("sha256", "f" * 64),
        ("rows", 11),
        ("first_block", POOL_ATTRIBUTION_ORIENTATIONS["uni-base"].inception_block + 1),
        (
            "last_block",
            POOL_ATTRIBUTION_ORIENTATIONS["uni-base"].inception_block + 1_998,
        ),
        ("first_timestamp_ms", 101),
        ("last_timestamp_ms", 299),
    ),
)
def test_typed_manifest_provenance_requires_exact_replay_identity(
    field: str,
    value: object,
) -> None:
    provenance = _valid_provenance()
    assert provenance.base_replay is not None
    changed_replay = replace(provenance.base_replay, **{field: value})

    with pytest.raises(CrossPoolContractError, match="captured replay"):
        replace(provenance, base_replay=changed_replay)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("sha256", "f" * 64),
        ("rows", 11),
        ("first_block", POOL_ATTRIBUTION_ORIENTATIONS["uni-base"].inception_block + 1),
        (
            "last_block",
            POOL_ATTRIBUTION_ORIENTATIONS["uni-base"].inception_block + 1_998,
        ),
        ("first_timestamp_ms", 101),
        ("last_timestamp_ms", 299),
    ),
)
def test_serialized_manifest_requires_exact_replay_identity(
    field: str,
    value: object,
) -> None:
    manifest = build_article_manifest(_statistical_manifest_input())
    mutated = deepcopy(manifest)
    if field == "sha256":
        mutated["provenance"]["input_sha256"]["base_replay"] = value
    else:
        mutated["provenance"]["input_intervals"]["base_replay"][field] = value

    with pytest.raises(CrossPoolContractError, match="exact replay"):
        validate_article_manifest(mutated)


def test_typed_and_serialized_manifest_bind_exact_sidecar_bytes() -> None:
    provenance = _valid_provenance()
    assert provenance.base_ledger_coverage is not None
    changed_coverage = replace(
        provenance.base_ledger_coverage,
        sidecar_sha256="f" * 64,
    )
    with pytest.raises(CrossPoolContractError, match="sidecar hash"):
        replace(provenance, base_ledger_coverage=changed_coverage)

    manifest = build_article_manifest(_statistical_manifest_input())
    mutated = deepcopy(manifest)
    mutated["provenance"]["ledger_coverage"]["base_ledger"][
        "sidecar_sha256"
    ] = "f" * 64
    with pytest.raises(CrossPoolContractError, match="sidecar hash"):
        validate_article_manifest(mutated)


def test_manifest_recomputes_predictive_class_from_metrics_and_bootstrap() -> None:
    manifest = build_article_manifest(_statistical_manifest_input())
    mutated = deepcopy(manifest)
    for direction_key in ("primary", "reverse"):
        audit = mutated["predictive"][direction_key]
        audit["evidence_class"] = "affirmative_null"
        audit["inference"]["evidence_class"] = "affirmative_null"
        audit["influence"]["full_evidence_class"] = "affirmative_null"
    allowed, forbidden = claims_for_article_branch(
        "no_material_incremental_lead"
    )
    mutated["publication"] = {
        **mutated["publication"],
        "article_branch": "no_material_incremental_lead",
        "allowed_claims": list(allowed),
        "forbidden_claims": list(forbidden),
    }

    with pytest.raises(CrossPoolContractError, match="evidence class"):
        validate_article_manifest(mutated)


def test_manifest_reconciles_predictive_metric_arithmetic() -> None:
    manifest = build_article_manifest(_statistical_manifest_input())
    mutated = deepcopy(manifest)
    mutated["predictive"]["primary"]["inference"]["metrics"][
        "mae_improvement_bps"
    ] = 999.0

    with pytest.raises(CrossPoolContractError, match="MAE improvement"):
        validate_article_manifest(mutated)


def _blocked_provenance() -> RunProvenance:
    return RunProvenance(
        code_commit="a" * 40,
        source_diff_sha256="b" * 64,
        base_features=None,
        bsc_features=None,
        base_replay=None,
        bsc_replay=None,
        base_ledger=None,
        bsc_ledger=None,
        base_ledger_coverage=None,
        bsc_ledger_coverage=None,
        config=RunConfiguration(),
        runtime=RuntimeEnvironment(
            python_version="3.12.4",
            numpy_version="2.1.0",
            numpy_build_sha256="c" * 64,
            machine_architecture="arm64",
            byte_order="little",
            bit_generator="PCG64",
            draw_dtype="int64",
            matplotlib_version="3.9.2",
            matplotlib_backend="Agg",
            freetype_version="2.6.1",
            font_name="DejaVu Sans",
            font_file_sha256="d" * 64,
            figure_rcparams_sha256="e" * 64,
        ),
    )


def _statistical_manifest_input() -> StatisticalManifestInput:
    primary = _predictive_audit("bsc_to_base", day_count=28)
    reverse = _predictive_audit("base_to_bsc", day_count=28)
    return StatisticalManifestInput(
        provenance=_valid_provenance(),
        primary_predictive_audit=primary,
        reverse_predictive_audit=reverse,
        primary_dtw_stability=_dtw_stability(
            "bsc_to_base",
            lag_steps=1.0,
        ),
        reverse_dtw_stability=_dtw_stability(
            "base_to_bsc",
            lag_steps=1.0,
        ),
        predictive_sensitivity=PredictiveSensitivityInput(
            primary_15m=_predictive_audit(
                "bsc_to_base",
                day_count=28,
                horizon_ms=900_000,
            ).inference,
            reverse_15m=_predictive_audit(
                "base_to_bsc",
                day_count=28,
                horizon_ms=900_000,
            ).inference,
            primary_4h=_predictive_audit(
                "bsc_to_base",
                day_count=28,
                horizon_ms=14_400_000,
            ).inference,
            reverse_4h=_predictive_audit(
                "base_to_bsc",
                day_count=28,
                horizon_ms=14_400_000,
            ).inference,
        ),
        causal_audit_counts={
            "base_stream_rows": 1_508,
            "bsc_stream_rows": 3_105,
            "panel_15m_rows": 10_000,
            "panel_1h_rows": 2_547,
            "panel_4h_rows": 637,
        },
        event_study=_event_study_group(),
        dtw_null_summary={
            "primary_rotation_comparison_count": 60,
            "primary_observed_cost_better_share": 0.8,
            "reverse_rotation_comparison_count": 60,
            "reverse_observed_cost_better_share": 0.7,
        },
        market_structure=_market_structure_group(),
        figures=_statistical_figures(),
        artifacts=_statistical_artifacts(),
    )


def _valid_provenance() -> RunProvenance:
    base_end_block = POOL_ATTRIBUTION_ORIENTATIONS["uni-base"].inception_block + 1_999
    bsc_end_block = POOL_ATTRIBUTION_ORIENTATIONS["uni-bsc"].inception_block + 1_999
    base_replay = _replay_input_file("uni-base", last_block=base_end_block)
    bsc_replay = _replay_input_file("uni-bsc", last_block=bsc_end_block)
    base_ledger, base_coverage = _coverage_evidence(
        "uni-base",
        replay=base_replay,
        ledger_rows=36,
        covered_end_block=base_end_block,
        required_end_block=base_end_block,
    )
    bsc_ledger, bsc_coverage = _coverage_evidence(
        "uni-bsc",
        replay=bsc_replay,
        ledger_rows=13,
        covered_end_block=bsc_end_block,
        required_end_block=bsc_end_block,
    )
    return RunProvenance(
        code_commit="a" * 40,
        source_diff_sha256="b" * 64,
        base_features=_input_file("base-features"),
        bsc_features=_input_file("bsc-features"),
        base_replay=base_replay,
        bsc_replay=bsc_replay,
        base_ledger=base_ledger,
        bsc_ledger=bsc_ledger,
        base_ledger_coverage=base_coverage,
        bsc_ledger_coverage=bsc_coverage,
        config=RunConfiguration(),
        runtime=_runtime_environment(),
    )


def _patch_economic_execution_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provenance = _valid_provenance()
    monkeypatch.setattr(
        economic_cli,
        "capture_git_state",
        lambda _root: (provenance.code_commit, provenance.source_diff_sha256),
    )
    monkeypatch.setattr(
        economic_cli,
        "collect_runtime_environment",
        lambda: provenance.runtime,
    )


def _input_file(label: str) -> InputFileProvenance:
    return InputFileProvenance(
        sha256=_digest(label),
        rows=10,
        first_block=1,
        last_block=10,
        first_timestamp_ms=100,
        last_timestamp_ms=250,
    )


def _replay_input_file(pool: str, *, last_block: int) -> InputFileProvenance:
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[pool]
    return InputFileProvenance(
        sha256=_digest(f"{pool}-replay"),
        rows=10,
        first_block=orientation.inception_block,
        last_block=last_block,
        first_timestamp_ms=100,
        last_timestamp_ms=300,
    )


def _coverage_evidence(
    pool: str,
    *,
    replay: InputFileProvenance,
    ledger_rows: int,
    covered_end_block: int,
    required_end_block: int,
) -> tuple[InputFileProvenance, VerifiedLedgerCoverageEvidence]:
    orientation = POOL_ATTRIBUTION_ORIENTATIONS[pool]
    ledger_blocks = tuple(
        orientation.inception_block + index for index in range(ledger_rows)
    )
    ledger_bytes = (
        "chain,pool_id,block_number\n"
        + "".join(
            f"{orientation.chain},{orientation.pool_id},{block_number}\n"
            for block_number in ledger_blocks
        )
    ).encode()
    transaction_hash = "0x" + "3" * 64
    transaction_hashes = (transaction_hash,)
    transaction_digest = _json_digest(list(transaction_hashes))
    action_witnesses = WitnessSetCoverage(
        witness_count=ledger_rows,
        witnesses_sha256=_digest(f"{pool}-action-witnesses"),
        transaction_count=1,
        transactions_sha256=transaction_digest,
        transaction_hashes=transaction_hashes,
    )
    relevant_witnesses = WitnessSetCoverage(
        witness_count=1,
        witnesses_sha256=_digest(f"{pool}-transfer-witnesses"),
        transaction_count=1,
        transactions_sha256=transaction_digest,
        transaction_hashes=transaction_hashes,
    )
    transfer_chunk = TransferChunkCoverage(
        index=0,
        start_block=orientation.inception_block,
        end_block=covered_end_block,
        unfiltered_count=1,
        unfiltered_sha256=_digest(f"{pool}-transfer-chunk"),
    )
    bundle = BundleDigestCoverage(
        transaction_hash=transaction_hash,
        payload_sha256=_digest(f"{pool}-bundle"),
    )
    evidence = RpcLedgerEvidence(
        rpc_provider_origin="https://fixture-rpc.example",
        acquisition_policy=AcquisitionPolicyCoverage(
            max_blocks_per_log_query=2_000,
            retry_attempts=3,
            subdivision="sequential_binary",
            hidden_provider_retries="disabled",
            completeness="provider_conditioned",
        ),
        action_witnesses=action_witnesses,
        frozen_token_ids=FrozenTokenCoverage(
            token_count=1,
            token_ids_sha256=_json_digest(["1"]),
            token_ids=("1",),
        ),
        full_transfer_scan=FullTransferCoverage(
            position_manager=(
                "0x7c5f5a4bbd8fd63184577525326123b519429bdc"
                if pool == "uni-base"
                else "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b"
            ),
            transfer_topic=(
                "0xddf252ad1be2c89b69c2b068fc378daa"
                "952ba7f163c4a11628f55a4df523b3ef"
            ),
            start_block=orientation.inception_block,
            end_block=covered_end_block,
            log_count=1,
            chunks_sha256=_json_digest([asdict(transfer_chunk)]),
            chunks=(transfer_chunk,),
        ),
        relevant_transfer_witnesses=relevant_witnesses,
        eligible_bundles=EligibleBundleCoverage(
            transaction_count=1,
            transactions_sha256=transaction_digest,
            bundles_sha256=_json_digest([asdict(bundle)]),
            transaction_hashes=transaction_hashes,
            bundles=(bundle,),
        ),
        replay_input=ReplayCoverage(
            sha256=replay.sha256,
            byte_length=1,
            row_count=replay.rows,
            header_sha256=frozen_replay_header_sha256(),
            parser_version=FROZEN_REPLAY_PARSER_VERSION,
            parser_contract_sha256=frozen_replay_parser_contract_sha256(),
            price_semantics_sha256=frozen_replay_price_semantics_sha256(),
            chain=orientation.chain,
            pool_id=orientation.pool_id,
            first_block=replay.first_block,
            last_block=replay.last_block,
            first_timestamp_ms=replay.first_timestamp_ms,
            last_timestamp_ms=replay.last_timestamp_ms,
            price_event_count=5,
            price_events_sha256=_digest(f"{pool}-price-events"),
        ),
        action_reconciliation=ReconciliationCoverage(
            status="exact_success",
            count=ledger_rows,
            sha256=_digest(f"{pool}-action-reconciliation"),
        ),
        ownership_reconciliation=ReconciliationCoverage(
            status="exact_success",
            count=1,
            sha256=_digest(f"{pool}-ownership-reconciliation"),
        ),
    )
    coverage_bytes = build_rpc_ledger_coverage_bytes(
        pool,
        ledger_bytes,
        chain_id=orientation.chain_id,
        covered_start_block=orientation.inception_block,
        covered_start_block_hash="0x" + "1" * 64,
        covered_start_timestamp_ms=replay.first_timestamp_ms,
        covered_end_block=covered_end_block,
        covered_end_block_hash="0x" + "2" * 64,
        covered_end_timestamp_ms=replay.last_timestamp_ms,
        evidence=evidence,
    )
    payload = json.loads(coverage_bytes)
    assert isinstance(payload, dict)
    coverage = rpc_ledger_coverage_from_payload(pool, payload)
    ledger_provenance = InputFileProvenance(
        sha256=coverage.ledger_sha256,
        rows=coverage.ledger_rows,
        first_block=coverage.ledger_first_block,
        last_block=coverage.ledger_last_block,
        first_timestamp_ms=100,
        last_timestamp_ms=250,
    )
    verified = VerifiedLedgerCoverageEvidence(
        schema_version=coverage.schema_version,
        sidecar_sha256=hashlib.sha256(coverage_bytes).hexdigest(),
        ledger_sha256=coverage.ledger_sha256,
        pool=pool,
        chain=orientation.chain,
        chain_id=orientation.chain_id,
        pool_id=orientation.pool_id,
        covered_start_block=orientation.inception_block,
        covered_start_block_hash="0x" + "1" * 64,
        covered_start_timestamp_ms=100,
        covered_end_block=covered_end_block,
        covered_end_block_hash="0x" + "2" * 64,
        covered_end_timestamp_ms=replay.last_timestamp_ms,
        required_end_block=required_end_block,
        required_end_timestamp_ms=250,
        ledger_rows=coverage.ledger_rows,
        ledger_first_block=coverage.ledger_first_block,
        ledger_last_block=coverage.ledger_last_block,
        evidence=coverage.evidence,
        attestation_sha256=coverage.attestation_sha256,
    )
    return ledger_provenance, verified


def _json_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _event_study_group() -> dict[str, object]:
    return {
        "status": "complete",
        "primary": _event_direction_group("bsc_to_base"),
        "reverse": _event_direction_group("base_to_bsc"),
        "exclusion_counts": {
            "primary_missing_target_start_state": 0,
            "primary_missing_target_end_state": 0,
            "reverse_missing_target_start_state": 0,
            "reverse_missing_target_end_state": 0,
        },
    }


def _event_direction_group(direction: Direction) -> dict[str, object]:
    return {
        "direction": direction,
        "shock_count": 20,
        "response_count": 60,
        "exclusion_count": 0,
        "summaries": [
            {
                "horizon_ms": horizon_ms,
                "event_count": 20,
                "event_day_count": 10,
                "mean_response_bps": _interval(1.0, 0.5, 1.5),
                "median_response_bps": _interval(0.8, 0.2, 1.2),
                "direction_agreement": _interval(0.6, 0.5, 0.7),
            }
            for horizon_ms in (900_000, 3_600_000, 14_400_000)
        ],
    }


def _market_structure_group() -> dict[str, object]:
    return {
        "status": "complete",
        "venues": [
            _venue_structure("uni-base", fee_rate="0.0015"),
            _venue_structure("uni-bsc", fee_rate="0.0012"),
        ],
    }


def _venue_structure(pool: str, *, fee_rate: str) -> dict[str, object]:
    return {
        "pool": pool,
        "activity_start_timestamp_ms": 100,
        "activity_end_timestamp_ms": 250,
        "swap_count": 100,
        "meaningful_move_count": 25,
        "fee_rate_fraction": fee_rate,
        "median_update_gap_ms": "1000",
        "p95_update_gap_ms": "5000",
        "median_active_liquidity_units": "1000000",
        "total_volume_usd": "10000",
        "known_owner_count": 3,
        "exact_known_owner_capital_coverage_fraction": "0.9",
        "exact_opening_capital_top_owner_share_fraction": "0.5",
        "exact_opening_capital_top_three_share_fraction": "1",
        "exact_opening_capital_hhi_fraction": "0.4",
    }


def _statistical_figures() -> dict[str, object]:
    artifacts = _statistical_artifacts()
    return {
        "price_gap": _figure("price_gap.png", artifacts),
        "event_response": _figure("event_response.png", artifacts),
        "dtw_lag": _figure("dtw_lag.png", artifacts),
        "diagnostic_predictive_performance": _figure(
            "predictive_performance.png",
            artifacts,
        ),
        "lp_performance": None,
    }


def _figure(name: str, artifacts: dict[str, str]) -> dict[str, str]:
    return {"artifact": name, "sha256": artifacts[name]}


def _statistical_artifacts() -> dict[str, str]:
    names = (
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
    return {name: _digest(name) for name in names}


def _interval(point: float, lower: float, upper: float) -> dict[str, float]:
    return {"point": point, "lower": lower, "upper": upper}


def _strategy_summary(
    *,
    net: str,
    worst: str,
    drawdown: str,
    active_window_count: int = 4,
    positive_window_count: int = 1,
    positive_active_window_count: int | None = 1,
    total_fees: str = "1",
    total_transaction_cost: str = "2",
    rebalance_count: int = 1,
) -> StrategySummary:
    return StrategySummary(
        window_count=23,
        active_window_count=active_window_count,
        positive_window_count=positive_window_count,
        positive_active_window_count=positive_active_window_count,
        aggregate_net_return=Decimal(net),
        worst_window_return=Decimal(worst),
        worst_within_window_drawdown_magnitude=Decimal(drawdown),
        total_fees_usd=Decimal(total_fees),
        total_transaction_cost_usd=Decimal(total_transaction_cost),
        rebalance_count=rebalance_count,
    )


def _strategy_summary_payload() -> dict[str, object]:
    return {
        "window_count": 23,
        "active_window_count": 4,
        "positive_window_count": 1,
        "positive_active_window_count": 1,
        "aggregate_net_return_fraction": "0",
        "worst_window_return_fraction": "0",
        "worst_within_window_drawdown_magnitude_fraction": "0",
        "all_window_positive_rate_fraction": (
            "0.043478260869565217391304347826086956521739130434783"
        ),
        "active_window_positive_rate_fraction": "0.25",
        "total_fees_usd": "1",
        "total_transaction_cost_usd": "2",
        "fee_to_transaction_cost_ratio": "0.5",
        "rebalance_count": 1,
    }


def _panel_row() -> PanelRow:
    return PanelRow(
        timestamp_ms=200,
        horizon_ms=900_000,
        base_state_timestamp_ms=190,
        bsc_state_timestamp_ms=195,
        base_forward_state_timestamp_ms=290,
        bsc_forward_state_timestamp_ms=295,
        base_lag_block_number=1,
        bsc_lag_block_number=2,
        base_state_block_number=3,
        bsc_state_block_number=4,
        base_forward_block_number=5,
        bsc_forward_block_number=6,
        base_age_ms=10,
        bsc_age_ms=5,
        base_mid=0.00072,
        bsc_mid=0.00071,
        base_trailing_return_bps=1.25,
        bsc_trailing_return_bps=-2.5,
        base_minus_bsc_gap_bps=3.75,
        base_forward_return_bps=4.5,
        bsc_forward_return_bps=-5.5,
        base_regime="early",
        bsc_regime="early",
    )


def _runtime_environment() -> RuntimeEnvironment:
    return RuntimeEnvironment(
        python_version="3.12.4",
        numpy_version="2.1.0",
        numpy_build_sha256="c" * 64,
        machine_architecture="arm64",
        byte_order="little",
        bit_generator="PCG64",
        draw_dtype="int64",
        matplotlib_version="3.9.2",
        matplotlib_backend="Agg",
        freetype_version="2.6.1",
        font_name="DejaVu Sans",
        font_file_sha256="d" * 64,
        figure_rcparams_sha256="e" * 64,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@lru_cache(maxsize=None)
def _predictive_audit(
    direction: Direction,
    *,
    day_count: int,
    horizon_ms: int = _HOUR_MS,
) -> DirectionalPredictiveAudit:
    rows = tuple(
        PredictionRow(
            timestamp_ms=_FIRST_REFIT_MS + day * _DAY_MS,
            target_timestamp_ms=(
                _FIRST_REFIT_MS + day * _DAY_MS + horizon_ms
            ),
            horizon_ms=horizon_ms,
            refit_timestamp_ms=(
                _FIRST_REFIT_MS + (day // 7) * _WEEK_MS
            ),
            fold_index=day // 7,
            direction=direction,
            target_regime="early",
            source_regime="early",
            actual_bps=12.0 if day % 2 == 0 else -12.0,
            baseline_prediction_bps=0.0,
            cross_prediction_bps=12.0 if day % 2 == 0 else -12.0,
        )
        for day in range(day_count)
    )
    return audit_predictions(rows)


def _dtw_stability(
    direction: Direction,
    *,
    lag_steps: float,
) -> DtwStability:
    return DtwStability(
        direction=direction,
        aggregate_median_lag_by_band={
            1: lag_steps,
            4: lag_steps,
            16: lag_steps,
        },
        weekly_median_lags_by_band={
            1: ((_MONDAY_START_MS, lag_steps),),
            4: ((_MONDAY_START_MS, lag_steps),),
            16: ((_MONDAY_START_MS, lag_steps),),
        },
        primary_band_same_sign_week_share=(0.0 if lag_steps == 0.0 else 1.0),
        band_unstable=lag_steps == 0.0,
    )
