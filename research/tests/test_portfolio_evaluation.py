from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research.backtester.clmm_math import tick_to_sqrt_price_x96
from research.backtester.data import V4Event
from research.backtester.params import BacktestParams
from research.backtester.portfolio_allocation import Allocation
from research.backtester.portfolio_catalog import (
    DirectionalPolicyDefinition,
    PortfolioCatalog,
    SleeveAlias,
    SleeveDefinition,
    parameter_fingerprint,
)
from research.backtester.portfolio_errors import ExecutionAccountingError
from research.backtester.portfolio_evaluation import (
    ALLOCATION_RULE_IDS,
    COMPARATOR_IDS,
    _reset_outcome,
    assess_candidate_reset_matrix,
    create_primary_path_states,
    create_removal_path_states,
    evaluate_primary_window,
    evaluate_removal_window,
    primary_window_from_payload,
    primary_window_to_payload,
    removal_window_from_payload,
    removal_window_to_payload,
    restore_primary_path_states,
    restore_removal_path_states,
    select_best_reset_sleeve,
)
from research.backtester.portfolio_path import FailureDiagnostic
from research.backtester.portfolio_publication import (
    CSV_FIELDS,
    build_artifact_rows,
    build_pbo_payload,
    publish_artifacts,
)
from research.backtester.run import Window, WindowCounts, WindowSlice
from research.backtester.simulator import UNISWAP_BASE_POOL


def _sleeve(sleeve_id: str, family: str, config_name: str) -> SleeveDefinition:
    params = BacktestParams(initial_capital_usd=100.0)
    return SleeveDefinition(
        sleeve_id=sleeve_id,
        family=family,  # type: ignore[arg-type]
        config_name=config_name,
        parameter_payload=json.dumps(
            asdict(params),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        parameter_fingerprint=parameter_fingerprint(params),
        source_constructor="test",
        aliases=(
            SleeveAlias(
                "paper" if family == "paper_exclusive" else family,  # type: ignore[arg-type]
                config_name,
                "test",
            ),
        ),
    )


def _catalog() -> PortfolioCatalog:
    static = _sleeve(
        "uni-base:static:spot-w0025",
        "static",
        "static_spot_w0025",
    )
    directional = _sleeve(
        "uni-base:directional-component:upside",
        "directional",
        "upside_capture_tight",
    )
    policy = DirectionalPolicyDefinition(
        sleeve_id="uni-base:directional:upside-tight-v1",
        family="directional",
        profile="upside_tight_v1",
        archetype_membership=(
            ("dip_accumulator", replace(directional, config_name="dip_accumulator_tight")),
            ("fee_box", replace(directional, config_name="fee_box_tight")),
            ("upside_capture", directional),
        ),
    )
    return PortfolioCatalog("uni-base", (static,), (policy,))


def _swap(index: int) -> V4Event:
    return V4Event(
        block_time=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        chain="base",
        pool_id=UNISWAP_BASE_POOL.pool_address,
        event_type="swap",
        tx_hash=f"0x{index:064x}",
        log_index=0,
        block_number=index + 1,
        sqrt_price_x96=tick_to_sqrt_price_x96(index),
        tick=index,
        active_liquidity=10**15,
        fee_rate=UNISWAP_BASE_POOL.fee_rate,
        amount0=-1_000.0,
        amount1=1_000.0,
        amount_usd=1_000.0,
        cngn_usd_price=1.0001**index,
        token0_symbol="cNGN",
        token1_symbol="USDC",
    )


def _window(index: int, offset: int = 0) -> WindowSlice:
    train = [_swap(offset + value) for value in range(3)]
    validation = [_swap(offset + value) for value in range(3, 6)]
    counts = WindowCounts(3, 3, 0)
    return WindowSlice(
        Window(
            index,
            train[0].block_time,
            train[-1].block_time,
            validation[0].block_time,
            validation[-1].block_time,
        ),
        train,
        validation,
        counts,
        counts,
        None,
    )


def test_invalid_reset_retains_exception_type_and_reason() -> None:
    def fail() -> object:
        raise ExecutionAccountingError("joint entry scale lattice has no feasible point")

    outcome = _reset_outcome("equal_config", fail)  # type: ignore[arg-type]

    assert outcome.failure == FailureDiagnostic(
        "ExecutionAccountingError",
        "joint entry scale lattice has no feasible point",
    )


def test_primary_window_has_complete_reset_and_independent_carried_methods() -> None:
    catalog = _catalog()
    states = create_primary_path_states(100.0)

    first = evaluate_primary_window(
        catalog=catalog,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=states,
    )
    second = evaluate_primary_window(
        catalog=catalog,
        window_slice=_window(1, 10),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=states,
    )

    assert tuple(first.reset_rules) == ALLOCATION_RULE_IDS
    assert tuple(first.carried_rules) == ALLOCATION_RULE_IDS
    assert tuple(first.comparators) == COMPARATOR_IDS
    assert len(first.candidates) == len(catalog.allocation_units)
    directional = next(row for row in first.candidates if row.family == "directional")
    assert directional.route_status == "no_position"
    assert directional.status == "valid"
    assert directional.economics is not None
    assert directional.economics.window_net_return == 0.0
    for method_id in (*ALLOCATION_RULE_IDS, *COMPARATOR_IDS):
        assert second.path_snapshots[method_id].next_opening_capital_usd == (
            second.carried_rules[method_id].closing_cash_usd
            if method_id in second.carried_rules
            else second.comparators[method_id].closing_cash_usd
        )
    assert second.carried_rules["equal_config"].opening_capital_usd == (
        first.carried_rules["equal_config"].closing_cash_usd
    )


def test_primary_window_reports_bounded_unit_progress() -> None:
    catalog = _catalog()
    progress: list[tuple[str, int, int]] = []

    evaluate_primary_window(
        catalog=catalog,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
        progress_callback=lambda phase, completed, total: progress.append(
            (phase, completed, total)
        ),
    )

    assert progress == [
        ("training", 2, 2),
        ("candidate_reset", 2, 2),
    ]


def test_primary_window_checkpoint_payload_round_trips_exactly() -> None:
    catalog = _catalog()
    record = evaluate_primary_window(
        catalog=catalog,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
    )

    payload = json.loads(json.dumps(primary_window_to_payload(record), sort_keys=True))
    restored = primary_window_from_payload(payload)

    assert restored == record
    assert payload["window_index"] == 0
    assert set(payload["path_snapshots"]) == {
        *ALLOCATION_RULE_IDS,
        *COMPARATOR_IDS,
    }
    assert {
        method_id: state.snapshot()
        for method_id, state in restore_primary_path_states(
            (restored,),
            100.0,
        ).items()
    } == record.path_snapshots


def test_restore_rejects_snapshot_closing_cash_mismatch() -> None:
    catalog = _catalog()
    record = evaluate_primary_window(
        catalog=catalog,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
    )
    cash_snapshot = record.path_snapshots["cash"]
    tampered = replace(
        record,
        path_snapshots={
            **record.path_snapshots,
            "cash": replace(
                cash_snapshot,
                next_opening_capital_usd=(cash_snapshot.next_opening_capital_usd + 1.0),
            ),
        },
    )

    with pytest.raises(ValueError, match="snapshot closing capital"):
        restore_primary_path_states((tampered,), 100.0)


def test_economic_summary_rejects_return_capital_mismatch() -> None:
    primary = evaluate_primary_window(
        catalog=_catalog(),
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
    )
    summary = primary.candidates[0].economics
    assert summary is not None

    with pytest.raises(ValueError, match="return does not reconcile"):
        replace(summary, window_net_return=summary.window_net_return + 0.5)


def test_candidate_record_rejects_valid_row_without_economics() -> None:
    primary = evaluate_primary_window(
        catalog=_catalog(),
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
    )

    with pytest.raises(ValueError, match="valid candidate"):
        replace(
            primary.candidates[0],
            status="valid",
            episode_count=None,
            economics=None,
        )


def test_primary_record_rejects_miskeyed_comparator_plan() -> None:
    primary = evaluate_primary_window(
        catalog=_catalog(),
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
    )
    cash = primary.comparator_plans["cash"]

    with pytest.raises(ValueError, match="comparator plan identity"):
        replace(
            primary,
            comparator_plans={
                **primary.comparator_plans,
                "cash": replace(cash, comparator_id="hold_cngn_mark"),
            },
        )


def test_best_sleeve_removal_is_a_separate_carried_checkpoint_phase() -> None:
    catalog = _catalog()
    primary_states = create_primary_path_states(100.0)
    primary = tuple(
        evaluate_primary_window(
            catalog=catalog,
            window_slice=_window(index, index * 10),
            entry_state={},
            pool_config=UNISWAP_BASE_POOL,
            reference_bankroll_usd=100.0,
            path_states=primary_states,
        )
        for index in range(2)
    )
    selected = select_best_reset_sleeve(primary)
    removal_states = create_removal_path_states(100.0)

    record = evaluate_removal_window(
        catalog=catalog,
        primary_record=primary[0],
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        removed_economic_id=selected,
        path_states=removal_states,
    )
    payload = removal_window_to_payload(record)

    assert tuple(record.outcomes) == ALLOCATION_RULE_IDS
    assert payload["phase"] == "best_sleeve_removal"
    assert payload["removed_economic_id"] == selected
    restored = removal_window_from_payload(payload)
    assert restored == record
    assert {
        rule: state.snapshot()
        for rule, state in restore_removal_path_states((restored,), 100.0).items()
    } == record.path_snapshots


def test_publication_rows_have_frozen_cardinality_and_no_legacy_family_pbo() -> None:
    catalog = _catalog()
    primary_states = create_primary_path_states(100.0)
    primary = tuple(
        evaluate_primary_window(
            catalog=catalog,
            window_slice=_window(index, index * 10),
            entry_state={},
            pool_config=UNISWAP_BASE_POOL,
            reference_bankroll_usd=100.0,
            path_states=primary_states,
        )
        for index in range(2)
    )
    removed_id = select_best_reset_sleeve(primary)
    removal_states = create_removal_path_states(100.0)
    removal = tuple(
        evaluate_removal_window(
            catalog=catalog,
            primary_record=primary[index],
            window_slice=_window(index, index * 10),
            entry_state={},
            pool_config=UNISWAP_BASE_POOL,
            removed_economic_id=removed_id,
            path_states=removal_states,
        )
        for index in range(2)
    )

    rows = build_artifact_rows(catalog, primary, removal)
    pbo = build_pbo_payload(primary)

    unit_count = len(catalog.allocation_units)
    assert len(rows["configuration_catalog.csv"]) == len(catalog.declarations)
    assert len(rows["training_eligibility.csv"]) == 2 * unit_count
    assert len(rows["window_weights.csv"]) == 2 * 3 * unit_count
    assert len(rows["sleeve_validation_matrix.csv"]) == 2 * unit_count
    assert len(rows["reset_portfolio_validation_matrix.csv"]) == 2 * 3
    assert len(rows["carried_portfolio_path.csv"]) == 2 * 3
    assert len(rows["comparators.csv"]) == 2 * 8
    assert len(rows["method_stability.csv"]) == 11
    assert len(rows["comparator_conclusions.csv"]) == 3 * 8
    assert set(rows) == set(CSV_FIELDS)
    assert set(pbo) == {
        "schema_version",
        "pool",
        "candidate_sleeves",
        "allocation_rules",
        "carried_paths",
    }
    assert "families" not in pbo
    assert pbo["carried_paths"] == {"status": "not_applicable_path_dependent_carried_bankroll"}


def test_publication_bytes_are_resume_history_independent(tmp_path: Path) -> None:
    catalog = _catalog()
    primary_states = create_primary_path_states(100.0)
    primary = tuple(
        evaluate_primary_window(
            catalog=catalog,
            window_slice=_window(index, index * 10),
            entry_state={},
            pool_config=UNISWAP_BASE_POOL,
            reference_bankroll_usd=100.0,
            path_states=primary_states,
        )
        for index in range(2)
    )
    removed_id = select_best_reset_sleeve(primary)
    removal_states = create_removal_path_states(100.0)
    removal = tuple(
        evaluate_removal_window(
            catalog=catalog,
            primary_record=primary[index],
            window_slice=_window(index, index * 10),
            entry_state={},
            pool_config=UNISWAP_BASE_POOL,
            removed_economic_id=removed_id,
            path_states=removal_states,
        )
        for index in range(2)
    )
    identity = {
        "protocol_version": "2026-07-24",
        "pool": "uni-base",
        "identity_sha256": "a" * 64,
        "total_windows": 2,
    }

    publish_artifacts(
        tmp_path / "uninterrupted",
        catalog=catalog,
        records=primary,
        removal_records=removal,
        run_identity=identity,
        run_kind="smoke",
    )
    restored_primary = tuple(
        primary_window_from_payload(primary_window_to_payload(record)) for record in primary
    )
    restored_removal = tuple(
        removal_window_from_payload(removal_window_to_payload(record)) for record in removal
    )
    publish_artifacts(
        tmp_path / "resumed",
        catalog=catalog,
        records=restored_primary,
        removal_records=restored_removal,
        run_identity=identity,
        run_kind="smoke",
    )

    names = sorted(path.name for path in (tmp_path / "uninterrupted").iterdir())
    assert names == sorted(path.name for path in (tmp_path / "resumed").iterdir())
    for name in names:
        assert (tmp_path / "uninterrupted" / name).read_bytes() == (
            tmp_path / "resumed" / name
        ).read_bytes()


def test_invalid_candidate_matrix_publishes_primary_only_fail_closed_artifacts(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    primary = evaluate_primary_window(
        catalog=catalog,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
    )
    failed = replace(
        primary.candidates[0],
        status="invalid_terminal_liquidation",
        episode_count=None,
        economics=None,
        failure=FailureDiagnostic(
            "TerminalLiquidationError",
            "terminal liquidation failed",
        ),
    )
    invalid_primary = replace(
        primary,
        candidates=(failed, *primary.candidates[1:]),
    )
    expected_ids = tuple(unit.sleeve_id for unit in catalog.allocation_units)

    assessment = assess_candidate_reset_matrix((invalid_primary,), expected_ids)
    rows = build_artifact_rows(catalog, (invalid_primary,), ())
    pbo = build_pbo_payload(
        (invalid_primary,),
        expected_economic_ids=expected_ids,
    )
    manifest = publish_artifacts(
        tmp_path / "primary-only",
        catalog=catalog,
        records=(invalid_primary,),
        removal_records=(),
        run_identity={"pool": catalog.pool, "total_windows": 1},
        run_kind="completed_primary_invalid_candidate_matrix",
    )

    assert assessment.status == "invalid_incomplete_matrix"
    assert assessment.selected_economic_id is None
    assert assessment.invalid_rows == 1
    candidate_row = rows["sleeve_validation_matrix.csv"][0]
    assert candidate_row["status"] == "invalid_terminal_liquidation"
    assert candidate_row["episode_count"] == ""
    assert candidate_row["closing_cash_usd"] == ""
    for row in rows["concentration_and_contribution.csv"]:
        assert row["removal_status"] == ("not_run_incomplete_candidate_reset_matrix")
        assert row["best_sleeve_removed_economic_id"] == ""
        assert row["removal_closing_cash_usd"] == ""
    assert pbo["candidate_sleeves"] == {
        "status": "invalid_incomplete_matrix",
        "expected_rows": 2,
        "observed_rows": 2,
        "valid_rows": 1,
        "invalid_rows": 1,
        "invalid_status_counts": {"invalid_terminal_liquidation": 1},
        "invalid_observations": [
            {
                "economic_id": failed.economic_id,
                "window_index": 0,
                "status": "invalid_terminal_liquidation",
                "failure": {
                    "exception_type": "TerminalLiquidationError",
                    "reason": "terminal liquidation failed",
                },
            }
        ],
    }
    assert manifest["publication_status"] == ("completed_with_invalid_candidate_reset_matrix")
    assert manifest["best_sleeve_removal_economic_id"] is None
    assert manifest["removal_phase"] == {
        "status": "not_run_incomplete_candidate_reset_matrix",
        "completed_windows": 0,
        "expected_windows": 1,
        "removed_economic_id": None,
    }


def test_missing_candidate_row_blocks_publication(tmp_path: Path) -> None:
    catalog = _catalog()
    primary = evaluate_primary_window(
        catalog=catalog,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
    )
    incomplete = replace(primary, candidates=primary.candidates[:-1])

    with pytest.raises(ValueError, match="candidate matrix identities"):
        publish_artifacts(
            tmp_path / "incomplete",
            catalog=catalog,
            records=(incomplete,),
            removal_records=(),
            run_identity={"pool": catalog.pool, "total_windows": 1},
            run_kind="completed_primary_invalid_candidate_matrix",
        )


def test_publication_rejects_misaligned_removal_windows() -> None:
    catalog = _catalog()
    primary_states = create_primary_path_states(100.0)
    primary = tuple(
        evaluate_primary_window(
            catalog=catalog,
            window_slice=_window(index, index * 10),
            entry_state={},
            pool_config=UNISWAP_BASE_POOL,
            reference_bankroll_usd=100.0,
            path_states=primary_states,
        )
        for index in range(2)
    )
    removed_id = select_best_reset_sleeve(primary)
    removal_states = create_removal_path_states(100.0)
    removal = tuple(
        evaluate_removal_window(
            catalog=catalog,
            primary_record=primary[index],
            window_slice=_window(index, index * 10),
            entry_state={},
            pool_config=UNISWAP_BASE_POOL,
            removed_economic_id=removed_id,
            path_states=removal_states,
        )
        for index in range(2)
    )
    malformed_second = replace(
        removal[1],
        window_index=2,
        outcomes={
            rule: replace(outcome, window_index=2) for rule, outcome in removal[1].outcomes.items()
        },
    )
    malformed = (removal[0], malformed_second)

    with pytest.raises(ValueError, match="removal windows"):
        build_artifact_rows(catalog, primary, malformed)


def test_publication_rejects_removal_allocation_that_retains_selected_sleeve() -> None:
    catalog = _catalog()
    primary = evaluate_primary_window(
        catalog=catalog,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
    )
    removed_id = select_best_reset_sleeve((primary,))
    removal = evaluate_removal_window(
        catalog=catalog,
        primary_record=primary,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        removed_economic_id=removed_id,
        path_states=create_removal_path_states(100.0),
    )
    malformed = replace(
        removal,
        removed_allocations={
            **removal.removed_allocations,
            "equal_config": Allocation(
                "equal_config",
                {removed_id: 0.10},
                0.90,
            ),
        },
    )

    with pytest.raises(ValueError, match="removed allocation"):
        build_artifact_rows(catalog, (primary,), (malformed,))


def test_publication_rejects_incomplete_identity_window_count(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    primary = evaluate_primary_window(
        catalog=catalog,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        reference_bankroll_usd=100.0,
        path_states=create_primary_path_states(100.0),
    )
    removed_id = select_best_reset_sleeve((primary,))
    removal = evaluate_removal_window(
        catalog=catalog,
        primary_record=primary,
        window_slice=_window(0),
        entry_state={},
        pool_config=UNISWAP_BASE_POOL,
        removed_economic_id=removed_id,
        path_states=create_removal_path_states(100.0),
    )

    with pytest.raises(ValueError, match="identity window count"):
        publish_artifacts(
            tmp_path / "incomplete-identity",
            catalog=catalog,
            records=(primary,),
            removal_records=(removal,),
            run_identity={"pool": catalog.pool, "total_windows": 2},
            run_kind="completed_amended_protocol_run",
        )
