from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

import research.scripts.evaluate_parameter_portfolio as orchestration
from research.backtester.entry_eligibility import AlwaysEligibleOverlay
from research.backtester.params import BacktestParams
from research.backtester.portfolio_allocation import Allocation, TrainingMetrics
from research.backtester.portfolio_catalog import (
    DirectionalPolicyDefinition,
    PortfolioCatalog,
    SleeveDefinition,
    parameter_fingerprint,
)
from research.backtester.portfolio_simulator import PortfolioResult, simulate_portfolio
from research.backtester.run import Window, WindowCounts, WindowSlice
from research.backtester.simulator import UNISWAP_BASE_POOL
from research.scripts.evaluate_parameter_portfolio import (
    ARTIFACT_NAMES,
    WindowEvaluation,
    build_parser,
    compute_matrix_pbo,
    remove_sleeve_from_allocation,
    route_directional_catalog,
    validate_cli_args,
)


def _sleeve(sleeve_id: str, family: str, config_name: str) -> SleeveDefinition:
    params = BacktestParams(initial_capital_usd=100.0)
    fingerprint = parameter_fingerprint(params)
    return SleeveDefinition(
        sleeve_id=sleeve_id,
        family=family,  # type: ignore[arg-type]
        config_name=config_name,
        parameter_payload=json.dumps(asdict(params), separators=(",", ":"), sort_keys=True),
        parameter_fingerprint=fingerprint,
        source_constructor="test",
    )


@pytest.fixture
def catalog() -> PortfolioCatalog:
    static = _sleeve("static:a", "static", "static_a")
    upside = _sleeve("directional:raw-up", "directional", "upside_capture_tight")
    dip = replace(upside, sleeve_id="directional:raw-dip", config_name="dip_accumulator_tight")
    fee = replace(upside, sleeve_id="directional:raw-fee", config_name="fee_box_tight")
    policy = DirectionalPolicyDefinition(
        sleeve_id="directional:upside_tight_v1",
        family="directional",
        profile="upside_tight_v1",
        archetype_membership=(
            ("upside_capture", upside),
            ("dip_accumulator", dip),
            ("fee_box", fee),
        ),
    )
    return PortfolioCatalog("uni-base", (static,), (policy,))


def test_directional_policy_funds_only_the_causally_routed_archetype(
    catalog: PortfolioCatalog,
) -> None:
    routed = route_directional_catalog(
        catalog,
        {"gate_strict_sign_cone": "1", "entry_predicted_markout_20_25": "0.01"},
    )
    by_id = {sleeve.sleeve_id: sleeve for sleeve in routed.sleeves}
    assert by_id["directional:upside_tight_v1"].config_name.startswith("upside_capture_")
    assert "directional:raw-up" not in by_id
    assert all("dip_accumulator" not in item.config_name for item in routed.sleeves)
    assert routed.directional_policies == ()

    accepted = simulate_portfolio(
        events=[],
        sleeves=routed.sleeves,
        allocation=Allocation("equal_config", {}, 1.0),
        pool_config=UNISWAP_BASE_POOL,
        bankroll_usd=100.0,
        entry_overlays_by_sleeve={
            "directional:upside_tight_v1": AlwaysEligibleOverlay()
        },
    )
    assert accepted.attribution == {}

    with pytest.raises(ValueError, match="entry overlays reference unknown sleeves"):
        simulate_portfolio(
            events=[],
            sleeves=routed.sleeves,
            allocation=Allocation("equal_config", {}, 1.0),
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=100.0,
            entry_overlays_by_sleeve={
                "directional:raw-up": AlwaysEligibleOverlay()
            },
        )


def test_no_position_omits_directional_policy(catalog: PortfolioCatalog) -> None:
    routed = route_directional_catalog(catalog, {})
    assert [sleeve.sleeve_id for sleeve in routed.sleeves] == ["static:a"]

    with pytest.raises(ValueError, match="entry overlays reference unknown sleeves"):
        simulate_portfolio(
            events=[],
            sleeves=routed.sleeves,
            allocation=Allocation("equal_config", {}, 1.0),
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=100.0,
            entry_overlays_by_sleeve={
                "directional:upside_tight_v1": AlwaysEligibleOverlay()
            },
        )


def test_validation_metrics_cannot_change_frozen_weights(
    monkeypatch: pytest.MonkeyPatch, catalog: PortfolioCatalog
) -> None:
    metric = TrainingMetrics(0.1, 1, 2.0, -0.1)
    monkeypatch.setattr(
        orchestration,
        "_training_metrics",
        lambda *args: {"static:a": metric},
    )

    def fake_portfolio(
        *, events: list[float], bankroll_usd: float, **kwargs: object
    ) -> PortfolioResult:
        final_value = bankroll_usd * (1.0 + events[0])
        return PortfolioResult(
            bankroll_usd=bankroll_usd,
            final_value=final_value,
            cash_value=0.0,
            total_fees=0.0,
            total_transaction_cost=0.0,
            total_price_impact_cost=0.0,
            external_swap_notional_usd=0.0,
            internal_netting_notional_usd=0.0,
            max_aggregate_liquidity_share=0.0,
            value_samples=[],
            attribution={},
        )

    monkeypatch.setattr(orchestration, "simulate_portfolio", fake_portfolio)
    monkeypatch.setattr(orchestration, "simulate_pool", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        orchestration,
        "_compute_metrics",
        lambda *args: {"net_return": 0.0},
    )
    monkeypatch.setattr(orchestration, "_build_pool_state", lambda events: None)
    now = datetime.now(UTC)
    window = Window(0, now, now + timedelta(hours=1), now, now + timedelta(hours=1))
    counts = WindowCounts(1, 1, 0)

    def evaluate(validation_return: float):
        window_slice = WindowSlice(window, [], [validation_return], counts, counts, None)
        return orchestration.evaluate_window(
            pool="uni-base",
            catalog=catalog,
            window_slice=window_slice,
            entry_state={},
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=100.0,
        )

    first = evaluate(0.90)
    second = evaluate(-0.90)
    assert first.allocations == second.allocations
    assert first.portfolio_results != second.portfolio_results


def test_remove_sleeve_keeps_removed_weight_as_cash() -> None:
    allocation = Allocation("equal_config", {"a": 0.4, "b": 0.3}, 0.3)
    removed = remove_sleeve_from_allocation(allocation, "a")
    assert removed.weights == {"b": 0.3}
    assert removed.cash_weight == pytest.approx(0.7)


def test_compute_matrix_pbo_rejects_ragged_windows() -> None:
    with pytest.raises(ValueError, match="ragged matrix"):
        compute_matrix_pbo({"a": {0: 0.1, 1: 0.2}, "b": {0: 0.3}})


def test_full_run_rejects_all_truncation_flags() -> None:
    parser = build_parser()
    for flag in (("--max-windows", "2"), ("--catalog-limit-per-family", "2")):
        args = parser.parse_args(("--full-run", *flag))
        with pytest.raises(ValueError, match="full run forbids truncation"):
            validate_cli_args(args)


def test_artifact_contract_is_complete() -> None:
    assert ARTIFACT_NAMES == (
        "configuration_catalog.csv",
        "training_eligibility.csv",
        "window_weights.csv",
        "sleeve_validation_matrix.csv",
        "family_validation_matrix.csv",
        "portfolio_validation_matrix.csv",
        "comparators.csv",
        "concentration_and_contribution.csv",
        "pbo_allocation_rules.json",
        "summary.md",
    )


def test_portfolio_validation_rows_include_accounting_fields(
    monkeypatch: pytest.MonkeyPatch, catalog: PortfolioCatalog,
) -> None:
    evaluation = _artifact_evaluation(
        catalog,
        window_index=0,
        comparator_metrics={"static:a": {"net_return": 0.01}},
        routed_catalog=route_directional_catalog(catalog, {}),
    )
    weights = {
        "static:a": 0.2,
        "directional:upside_tight_v1": 0.55,
    }
    allocation = Allocation("test", weights, 0.25)
    portfolio_result = replace(
        _portfolio_result(0.0), max_aggregate_liquidity_share=0.073
    )
    evaluation = replace(
        evaluation,
        allocations={rule: replace(allocation, rule=rule) for rule in evaluation.allocations},
        portfolio_results={rule: portfolio_result for rule in evaluation.portfolio_results},
    )
    monkeypatch.setattr(orchestration, "simulate_portfolio", lambda **kwargs: portfolio_result)

    rows, _ = orchestration._artifact_rows(
        catalog, (evaluation,), {0: _artifact_slices()[0]}, UNISWAP_BASE_POOL, 100.0
    )

    for row in rows["portfolio_validation_matrix.csv"]:
        assert row["deployed_weight"] == pytest.approx(sum(weights.values()))
        assert row["cash_weight"] == pytest.approx(allocation.cash_weight)
        assert row["max_aggregate_liquidity_share"] == pytest.approx(
            portfolio_result.max_aggregate_liquidity_share
        )


def _portfolio_result(net_return: float) -> PortfolioResult:
    return PortfolioResult(
        bankroll_usd=100.0,
        final_value=100.0 * (1.0 + net_return),
        cash_value=100.0,
        total_fees=0.0,
        total_transaction_cost=0.0,
        total_price_impact_cost=0.0,
        external_swap_notional_usd=0.0,
        internal_netting_notional_usd=0.0,
        max_aggregate_liquidity_share=0.0,
        value_samples=[],
        attribution={},
    )


def _artifact_evaluation(
    catalog: PortfolioCatalog,
    *,
    window_index: int,
    comparator_metrics: dict[str, dict[str, float]],
    routed_catalog: PortfolioCatalog,
) -> WindowEvaluation:
    allocations = {
        rule: Allocation(rule, {}, 1.0)
        for rule in ("equal_config", "equal_family", "shrinkage")
    }
    return WindowEvaluation(
        pool=catalog.pool,
        window_index=window_index,
        window_start=f"2026-01-0{window_index + 1}",
        window_end=f"2026-01-0{window_index + 2}",
        training_metrics={},
        allocations=allocations,
        portfolio_results={rule: _portfolio_result(0.0) for rule in allocations},
        comparator_metrics=comparator_metrics,
        routed_catalog=routed_catalog,
    )


def _artifact_slices() -> dict[int, WindowSlice]:
    now = datetime.now(UTC)
    counts = WindowCounts(1, 1, 0)
    return {
        index: WindowSlice(
            Window(index, now, now, now, now), [], [], counts, counts, None
        )
        for index in (0, 1)
    }


def test_directional_no_position_materializes_complete_zero_return_series(
    monkeypatch: pytest.MonkeyPatch, catalog: PortfolioCatalog
) -> None:
    policy_id = catalog.directional_policies[0].sleeve_id
    active = route_directional_catalog(
        catalog,
        {"gate_strict_sign_cone": "1", "entry_predicted_markout_20_25": "0.01"},
    )
    cash = route_directional_catalog(catalog, {})
    evaluations = (
        _artifact_evaluation(
            catalog,
            window_index=0,
            comparator_metrics={
                "static:a": {"net_return": -0.02},
                policy_id: {"net_return": 0.10},
            },
            routed_catalog=active,
        ),
        _artifact_evaluation(
            catalog,
            window_index=1,
            comparator_metrics={"static:a": {"net_return": -0.02}},
            routed_catalog=cash,
        ),
    )
    monkeypatch.setattr(orchestration, "simulate_portfolio", lambda **kwargs: _portfolio_result(0))
    monkeypatch.setattr(orchestration, "_build_pool_state", lambda events: None)
    rows, _ = orchestration._artifact_rows(
        catalog, evaluations, _artifact_slices(), UNISWAP_BASE_POOL, 100.0
    )
    policy_rows = [
        row for row in rows["sleeve_validation_matrix.csv"] if row["sleeve_id"] == policy_id
    ]
    assert [row["net_return"] for row in policy_rows] == [0.10, 0.0]
    assert policy_rows[1]["episode_count"] == 0
    assert {
        row["best_sleeve_removed"] for row in rows["concentration_and_contribution.csv"]
    } == {policy_id}


def test_artifact_path_rejects_missing_non_directional_window(
    catalog: PortfolioCatalog,
) -> None:
    routed = route_directional_catalog(catalog, {})
    evaluations = (
        _artifact_evaluation(
            catalog,
            window_index=0,
            comparator_metrics={"static:a": {"net_return": 0.01}},
            routed_catalog=routed,
        ),
        _artifact_evaluation(
            catalog,
            window_index=1,
            comparator_metrics={},
            routed_catalog=routed,
        ),
    )
    with pytest.raises(ValueError, match="ragged sleeve matrix.*static:a"):
        orchestration._artifact_rows(
            catalog, evaluations, {}, UNISWAP_BASE_POOL, 100.0
        )
