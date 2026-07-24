from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

import research.scripts.validate_parameter_portfolio_publication as validator
from research.backtester.pbo import compute_pbo
from research.backtester.portfolio_allocation import (
    TrainingMetrics,
    equal_config_weights,
    equal_family_weights,
    shrinkage_weights,
)
from research.backtester.portfolio_catalog import build_portfolio_catalog
from research.backtester.portfolio_evaluation import (
    ALLOCATION_RULE_IDS,
    COMPARATOR_IDS,
)
from research.backtester.portfolio_publication import (
    ARTIFACT_SCHEMA_VERSION,
    CSV_FIELDS,
    ECONOMIC_FIELDS,
    REQUIRED_ARTIFACTS,
)
from research.backtester.run import WindowSpec
from research.scripts.evaluate_frozen_family_lp import POOL_EXPERIMENTS
from research.scripts.evaluate_parameter_portfolio import (
    PROTOCOL_VERSION,
    SOURCE_CLOSURE,
    _run_identity,
)
from research.scripts.validate_parameter_portfolio_publication import (
    FROZEN_SOURCE_CLOSURE,
    FROZEN_SOURCE_COMMIT,
    FULL_DIMENSIONS,
    CsvEvidence,
    PoolDimensions,
    ValidationFailure,
    ValidationReport,
    WeightSummary,
    _validate_attribution_rows,
    _validate_candidate_rows,
    _validate_comparator_rows,
    _validate_method_stability,
    _validate_path_status,
    _validate_pbo_payload,
    _validate_reset_rows,
    _validate_training_rows,
    _validate_weight_rows,
    canonical_json_bytes,
    iter_csv_rows,
    load_json_strict,
    main,
    sha256_file,
    validate_artifact_metadata,
    validate_artifact_set,
    validate_economic_row,
    validate_identity_hash,
    validate_manifest_shape,
    validate_run_identity,
    validate_source_closure,
    validate_summary,
)


def _economic_row(**overrides: str | int | float) -> dict[str, str]:
    row = {
        "opening_capital_usd": "100",
        "closing_cash_usd": "101",
        "window_net_return": "0.01",
        "max_drawdown": "-0.02",
        "terminal_liquidation_cost_usd": "0.1",
        "total_fees_usd": "0.5",
        "total_fixed_cost_usd": "0.2",
        "total_variable_cost_usd": "0.3",
        "external_marked_notional_usd": "10",
        "external_input_value_usd": "5",
        "external_output_value_usd": "4.7",
        "internal_cross_notional_usd": "2",
        "entry_action_batch_count": "2",
        "scaled_entry_action_batch_count": "1",
        "minimum_entry_execution_scale": "0.5",
        "terminal_position_settlement_count": "1",
        "terminal_loose_cngn_settlement_count": "0",
        "terminal_zero_settlement_count": "0",
        "terminal_inventory_swap_count": "1",
        "terminal_fixed_cost_usd": "0.06",
        "terminal_variable_cost_usd": "0.04",
        "terminal_external_marked_notional_usd": "1",
        "value_sample_count": "3",
    }
    row.update({key: str(value) for key, value in overrides.items()})
    return row


def _zero_economic_row(capital: float) -> dict[str, str]:
    return _economic_row(
        opening_capital_usd=capital,
        closing_cash_usd=capital,
        window_net_return=0,
        max_drawdown=0,
        terminal_liquidation_cost_usd=0,
        total_fees_usd=0,
        total_fixed_cost_usd=0,
        total_variable_cost_usd=0,
        external_marked_notional_usd=0,
        external_input_value_usd=0,
        external_output_value_usd=0,
        internal_cross_notional_usd=0,
        entry_action_batch_count=0,
        scaled_entry_action_batch_count=0,
        minimum_entry_execution_scale=1,
        terminal_position_settlement_count=0,
        terminal_loose_cngn_settlement_count=0,
        terminal_zero_settlement_count=0,
        terminal_inventory_swap_count=0,
        terminal_fixed_cost_usd=0,
        terminal_variable_cost_usd=0,
        terminal_external_marked_notional_usd=0,
        value_sample_count=2,
    )


def test_strict_json_rejects_duplicate_keys_and_nonfinite_values(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"status":"valid","status":"invalid"}\n')
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value":NaN}\n')

    with pytest.raises(ValidationFailure, match=r"^FAIL code=JSON_DUPLICATE_KEY$"):
        load_json_strict(duplicate)
    with pytest.raises(ValidationFailure, match=r"^FAIL code=JSON_NONFINITE$"):
        load_json_strict(nonfinite)


def test_csv_reader_requires_exact_header_and_row_width(tmp_path: Path) -> None:
    valid = tmp_path / "valid.csv"
    with valid.open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("schema_version", "pool"))
        writer.writerow(("v2", "uni-base"))

    assert list(iter_csv_rows(valid, ("schema_version", "pool"))) == [
        {"schema_version": "v2", "pool": "uni-base"}
    ]

    duplicate_header = tmp_path / "duplicate-header.csv"
    duplicate_header.write_text("pool,pool\nuni-base,uni-base\n")
    with pytest.raises(ValidationFailure, match=r"^FAIL code=CSV_SCHEMA$"):
        list(iter_csv_rows(duplicate_header, ("pool", "other")))

    ragged = tmp_path / "ragged.csv"
    ragged.write_text("schema_version,pool\nv2\n")
    with pytest.raises(ValidationFailure, match=r"^FAIL code=CSV_SCHEMA$"):
        list(iter_csv_rows(ragged, ("schema_version", "pool")))


def test_identity_hash_uses_canonical_newline_terminated_json() -> None:
    identity = {"pool": "uni-base", "total_windows": 26}
    digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()

    validate_identity_hash({**identity, "identity_sha256": digest})

    with pytest.raises(ValidationFailure, match=r"^FAIL code=IDENTITY_HASH$"):
        validate_identity_hash({**identity, "identity_sha256": "0" * 64})


def test_economic_rows_are_complete_finite_and_reconciled() -> None:
    row = _economic_row()

    validate_economic_row(row, status="valid")

    malformed = {**row, "window_net_return": "0.02"}
    with pytest.raises(ValidationFailure, match=r"^FAIL code=ECONOMIC_RECONCILIATION$"):
        validate_economic_row(malformed, status="valid")

    nonfinite = {**row, "total_fees_usd": "inf"}
    with pytest.raises(ValidationFailure, match=r"^FAIL code=ECONOMIC_VALUE$"):
        validate_economic_row(nonfinite, status="valid")

    blank = {field: "" for field in row}
    validate_economic_row(blank, status="invalid_terminal_liquidation")

    with pytest.raises(ValidationFailure, match=r"^FAIL code=INVALID_ECONOMICS$"):
        validate_economic_row(row, status="invalid_terminal_liquidation")

    excessive_terminal_cost = {
        **row,
        "terminal_liquidation_cost_usd": "0.6",
    }
    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=ECONOMIC_RECONCILIATION$",
    ):
        validate_economic_row(excessive_terminal_cost, status="valid")

    inconsistent_terminal_breakdown = {
        **row,
        "terminal_variable_cost_usd": "0.05",
    }
    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=ECONOMIC_RECONCILIATION$",
    ):
        validate_economic_row(inconsistent_terminal_breakdown, status="valid")

    invalid_scale_counts = {
        **row,
        "entry_action_batch_count": "1",
        "scaled_entry_action_batch_count": "2",
    }
    with pytest.raises(ValidationFailure, match=r"^FAIL code=ECONOMIC_VALUE$"):
        validate_economic_row(invalid_scale_counts, status="valid")

    invalid_zero_entry_scale = {
        **row,
        "entry_action_batch_count": "0",
        "scaled_entry_action_batch_count": "0",
        "minimum_entry_execution_scale": "0.5",
    }
    with pytest.raises(ValidationFailure, match=r"^FAIL code=ECONOMIC_VALUE$"):
        validate_economic_row(invalid_zero_entry_scale, status="valid")


def test_validation_failure_output_never_includes_private_detail() -> None:
    failure = ValidationFailure(
        "CSV_SCHEMA",
        pool="uni-base",
        artifact="window_weights.csv",
    )

    assert str(failure) == ("FAIL code=CSV_SCHEMA pool=uni-base artifact=window_weights.csv")


def test_artifact_set_and_manifest_metadata_bind_every_published_byte(
    tmp_path: Path,
) -> None:
    publication = tmp_path / "uni_base"
    publication.mkdir()
    artifacts: dict[str, dict[str, object]] = {}
    for name in REQUIRED_ARTIFACTS:
        path = publication / name
        if name in CSV_FIELDS:
            path.write_text(",".join(CSV_FIELDS[name]) + "\n")
        elif name == "run_manifest.json":
            path.write_text("{}\n")
        else:
            path.write_text("placeholder\n")
        if name != "run_manifest.json":
            artifacts[name] = {
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "row_count": 0 if name in CSV_FIELDS else None,
            }

    validate_artifact_set(publication, pool="uni-base")
    validate_artifact_metadata(
        publication,
        {"artifacts": artifacts},
        pool="uni-base",
    )

    (publication / "summary.md").write_text("changed\n")
    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=ARTIFACT_HASH pool=uni-base artifact=summary.md$",
    ):
        validate_artifact_metadata(
            publication,
            {"artifacts": artifacts},
            pool="uni-base",
        )


def test_artifact_set_rejects_extra_files_and_symlinks(tmp_path: Path) -> None:
    publication = tmp_path / "uni_base"
    publication.mkdir()
    for name in REQUIRED_ARTIFACTS:
        (publication / name).write_text("placeholder\n")
    (publication / "extra.txt").write_text("extra\n")

    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=ARTIFACT_SET pool=uni-base$",
    ):
        validate_artifact_set(publication, pool="uni-base")

    (publication / "extra.txt").unlink()
    (publication / "summary.md").unlink()
    (publication / "summary.md").symlink_to(publication / "pbo_allocation_rules.json")
    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=ARTIFACT_SET pool=uni-base$",
    ):
        validate_artifact_set(publication, pool="uni-base")


def test_manifest_shape_accepts_only_compatible_full_run_gates() -> None:
    identity = {
        "protocol_version": PROTOCOL_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "phase": "primary",
        "pool": "uni-base",
        "run_kind": "full",
        "total_windows": 26,
    }
    identity["identity_sha256"] = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "run_kind": "completed_amended_protocol_run",
        "publication_status": "completed",
        "pool": "uni-base",
        "completed_windows": 26,
        "canonical_economic_units": 4_895,
        "retained_declarations": 4_935,
        "primary_run_identity": identity,
        "primary_phase": {
            "status": "completed",
            "completed_windows": 26,
            "expected_windows": 26,
        },
        "candidate_reset_matrix": {
            "status": "complete_valid",
            "expected_rows": 127_270,
            "observed_rows": 127_270,
            "valid_rows": 127_270,
            "invalid_rows": 0,
            "invalid_status_counts": {},
        },
        "removal_phase": {
            "status": "completed",
            "completed_windows": 26,
            "expected_windows": 26,
            "removed_economic_id": "private",
        },
        "best_sleeve_removal_economic_id": "private",
        "pbo_status": {
            "candidate_sleeves": "computed",
            "allocation_rules": "computed",
            "carried_paths": "not_applicable_path_dependent_carried_bankroll",
        },
        "artifacts": {},
    }

    validate_manifest_shape(manifest, pool="uni-base")

    invalid = {
        **manifest,
        "publication_status": "completed_with_invalid_candidate_reset_matrix",
    }
    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=MANIFEST_GATE pool=uni-base$",
    ):
        validate_manifest_shape(invalid, pool="uni-base")

    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=MANIFEST_SCHEMA pool=uni-base$",
    ):
        validate_manifest_shape({**manifest, "unexpected": True}, pool="uni-base")


def test_summary_is_an_exact_projection_of_manifest_status(tmp_path: Path) -> None:
    publication = tmp_path / "uni_base"
    publication.mkdir()
    summary = publication / "summary.md"
    summary.write_text(
        "\n".join(
            (
                "# Corrected weighted portfolio: uni-base",
                "",
                "- run kind: `completed_amended_protocol_run`",
                "- completed windows: 26",
                "- canonical economic units: 4895",
                "- retained declarations: 4935",
                "- candidate reset PBO status: `computed`",
                "- allocation reset PBO status: `computed`",
                "- carried paths: sequential boundary-settled USD capital",
                "- conclusions: diagnostic only; no live-promotion authorization",
                "",
            )
        )
    )
    manifest = {
        "run_kind": "completed_amended_protocol_run",
        "completed_windows": 26,
        "canonical_economic_units": 4_895,
        "retained_declarations": 4_935,
        "pbo_status": {
            "candidate_sleeves": "computed",
            "allocation_rules": "computed",
        },
    }

    validate_summary(publication, manifest, pool="uni-base")

    summary.write_text(summary.read_text().replace("diagnostic only", "validated"))
    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=SUMMARY pool=uni-base artifact=summary.md$",
    ):
        validate_summary(publication, manifest, pool="uni-base")


def test_source_closure_is_bound_to_the_frozen_commit() -> None:
    repo_root = Path.cwd()
    source_hashes = {
        relative: hashlib.sha256(
            subprocess.run(
                ["git", "show", f"{FROZEN_SOURCE_COMMIT}:{relative}"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
        ).hexdigest()
        for relative in FROZEN_SOURCE_CLOSURE
    }

    validate_source_closure(
        repo_root,
        source_hashes,
        pool="uni-base",
    )

    mutated = {**source_hashes, FROZEN_SOURCE_CLOSURE[0]: "0" * 64}
    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=SOURCE_CLOSURE pool=uni-base$",
    ):
        validate_source_closure(repo_root, mutated, pool="uni-base")


def test_runner_and_validator_bind_the_same_source_closure() -> None:
    assert FROZEN_SOURCE_CLOSURE == SOURCE_CLOSURE


def test_run_identity_rebuilds_inputs_catalog_config_and_constants() -> None:
    experiment = POOL_EXPERIMENTS["uni-base"]
    catalog = build_portfolio_catalog(
        experiment.pool,
        experiment.initial_capital_usd,
    )
    spec = WindowSpec(
        mode="swap_count",
        train_swaps=experiment.train_swaps,
        val_swaps=experiment.val_swaps,
        stride_swaps=experiment.val_swaps,
        min_train_swaps=experiment.train_swaps,
        min_train_liquidity_events=0,
        min_val_swaps=experiment.val_swaps,
    )
    identity = json.loads(
        canonical_json_bytes(
            _run_identity(
                experiment,
                catalog,
                spec,
                total_windows=26,
                full_run=True,
            )
        )
    )
    identity["identity_sha256"] = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()

    validate_run_identity(identity, repo_root=Path.cwd(), pool="uni-base")

    inputs = dict(identity["inputs"])
    inputs["history_csv"] = "0" * 64
    invalid = {**identity, "inputs": inputs}
    del invalid["identity_sha256"]
    invalid["identity_sha256"] = hashlib.sha256(canonical_json_bytes(invalid)).hexdigest()
    with pytest.raises(
        ValidationFailure,
        match=r"^FAIL code=INPUT_IDENTITY pool=uni-base$",
    ):
        validate_run_identity(invalid, repo_root=Path.cwd(), pool="uni-base")


def test_training_eligibility_is_recomputed_from_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        FULL_DIMENSIONS,
        "uni-base",
        PoolDimensions(windows=1, canonical_units=1, declarations=1),
    )
    artifact = "training_eligibility.csv"
    with (tmp_path / artifact).open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS[artifact],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(
            {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "pool": "uni-base",
                "window_index": 0,
                "window_start": "2026-01-01T00:00:00+00:00",
                "window_end": "2026-01-02T00:00:00+00:00",
                "economic_id": "unit-a",
                "family": "static",
                "routed_config_name": "static-a",
                "status": "valid",
                "failure_type": "",
                "failure_reason": "",
                "eligible": "false",
                "net_return": 0.01,
                "episode_count": 1,
                "fee_to_transaction_cost_ratio": 1.0,
                "max_drawdown": -0.01,
            }
        )

    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=TRAINING_ELIGIBILITY pool=uni-base "
            r"artifact=training_eligibility.csv$"
        ),
    ):
        _validate_training_rows(
            tmp_path,
            pool="uni-base",
            catalog_ids=("unit-a",),
            families={"unit-a": "static"},
            bounds={},
        )


def test_candidate_routed_status_requires_a_concrete_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        FULL_DIMENSIONS,
        "uni-base",
        PoolDimensions(windows=1, canonical_units=1, declarations=1),
    )
    artifact = "sleeve_validation_matrix.csv"
    with (tmp_path / artifact).open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS[artifact],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(
            {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "pool": "uni-base",
                "window_index": 0,
                "window_start": "2026-01-01T00:00:00+00:00",
                "window_end": "2026-01-02T00:00:00+00:00",
                "economic_id": "unit-a",
                "family": "directional",
                "routed_config_name": "no_position",
                "route_status": "routed",
                "status": "invalid_terminal_liquidation",
                "failure_type": "TerminalLiquidationError",
                "failure_reason": "terminal settlement failed",
                "episode_count": "",
                **{field: "" for field in ECONOMIC_FIELDS},
            }
        )

    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=CANDIDATE_ROWS pool=uni-base "
            r"artifact=sleeve_validation_matrix.csv$"
        ),
    ):
        _validate_candidate_rows(
            tmp_path,
            pool="uni-base",
            catalog_ids=("unit-a",),
            families={"unit-a": "directional"},
            routed_names={(0, "unit-a"): "no_position"},
            bounds={},
        )


def test_reset_liquidity_failure_binds_the_frozen_aggregate_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        FULL_DIMENSIONS,
        "uni-base",
        PoolDimensions(windows=1, canonical_units=1, declarations=1),
    )
    artifact = "reset_portfolio_validation_matrix.csv"
    with (tmp_path / artifact).open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS[artifact],
            lineterminator="\n",
        )
        writer.writeheader()
        for rule in ALLOCATION_RULE_IDS:
            writer.writerow(
                {
                    "schema_version": ARTIFACT_SCHEMA_VERSION,
                    "pool": "uni-base",
                    "window_index": 0,
                    "window_start": "2026-01-01T00:00:00+00:00",
                    "window_end": "2026-01-02T00:00:00+00:00",
                    "allocation_rule": rule,
                    "status": "invalid_liquidity_cap",
                    "failure_type": "LiquidityShareExceeded",
                    "failure_reason": "aggregate synthetic liquidity share exceeded cap",
                    "observed_share": "0.21",
                    "cap": "0.20",
                    "deployed_weight": "0.1",
                    "cash_weight": "0.9",
                    **{field: "" for field in ECONOMIC_FIELDS},
                }
            )
    weights = {
        (0, rule): WeightSummary(
            deployed_weight=0.1,
            cash_weight=0.9,
            herfindahl=0.01,
            largest_weight=0.1,
            effective_sleeve_count=100.0,
            removed_weight=None,
            weights={"unit-a": 0.1},
        )
        for rule in ALLOCATION_RULE_IDS
    }

    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=RESET_STATUS pool=uni-base "
            r"artifact=reset_portfolio_validation_matrix.csv$"
        ),
    ):
        _validate_reset_rows(
            tmp_path,
            pool="uni-base",
            weights=weights,
            bounds={},
        )


def test_failure_diagnostics_flow_from_csv_into_invalid_pbo_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        FULL_DIMENSIONS,
        "uni-base",
        PoolDimensions(windows=1, canonical_units=1, declarations=1),
    )
    common = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "pool": "uni-base",
        "window_index": 0,
        "window_start": "2026-01-01T00:00:00+00:00",
        "window_end": "2026-01-02T00:00:00+00:00",
    }
    candidate_artifact = "sleeve_validation_matrix.csv"
    with (tmp_path / candidate_artifact).open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS[candidate_artifact],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(
            {
                **common,
                "economic_id": "unit-a",
                "family": "static",
                "routed_config_name": "static-a",
                "route_status": "routed",
                "status": "invalid_terminal_liquidation",
                "failure_type": "TerminalLiquidationError",
                "failure_reason": "terminal inventory swap exceeded value",
                "episode_count": "",
                **{field: "" for field in ECONOMIC_FIELDS},
            }
        )
    _, candidate_failures = _validate_candidate_rows(
        tmp_path,
        pool="uni-base",
        catalog_ids=("unit-a",),
        families={"unit-a": "static"},
        routed_names={(0, "unit-a"): "static-a"},
        bounds={},
    )

    reset_artifact = "reset_portfolio_validation_matrix.csv"
    with (tmp_path / reset_artifact).open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS[reset_artifact],
            lineterminator="\n",
        )
        writer.writeheader()
        for rule in ALLOCATION_RULE_IDS:
            writer.writerow(
                {
                    **common,
                    "allocation_rule": rule,
                    "status": "invalid_execution_accounting",
                    "failure_type": "ExecutionAccountingError",
                    "failure_reason": "joint action value did not reconcile",
                    "observed_share": "",
                    "cap": "",
                    "deployed_weight": "0.1",
                    "cash_weight": "0.9",
                    **{field: "" for field in ECONOMIC_FIELDS},
                }
            )
    weights = {
        (0, rule): WeightSummary(
            deployed_weight=0.1,
            cash_weight=0.9,
            herfindahl=0.01,
            largest_weight=0.1,
            effective_sleeve_count=100.0,
            removed_weight=None,
            weights={"unit-a": 0.1},
        )
        for rule in ALLOCATION_RULE_IDS
    }
    _, reset_failures = _validate_reset_rows(
        tmp_path,
        pool="uni-base",
        weights=weights,
        bounds={},
    )
    payload = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "pool": "uni-base",
        "candidate_sleeves": {
            "status": "invalid_incomplete_matrix",
            "expected_rows": 1,
            "observed_rows": 1,
            "valid_rows": 0,
            "invalid_rows": 1,
            "invalid_status_counts": {"invalid_terminal_liquidation": 1},
            "invalid_observations": candidate_failures,
        },
        "allocation_rules": {
            "status": "invalid_incomplete_matrix",
            "invalid_observations": reset_failures,
        },
        "carried_paths": {"status": "not_applicable_path_dependent_carried_bankroll"},
    }
    (tmp_path / "pbo_allocation_rules.json").write_bytes(canonical_json_bytes(payload))
    manifest = {
        "candidate_reset_matrix": {
            "status": "invalid_incomplete_matrix",
            "valid_rows": 0,
            "invalid_rows": 1,
            "invalid_status_counts": {"invalid_terminal_liquidation": 1},
        },
        "pbo_status": {
            "candidate_sleeves": "invalid_incomplete_matrix",
            "allocation_rules": "invalid_incomplete_matrix",
        },
        "best_sleeve_removal_economic_id": None,
    }
    evidence = CsvEvidence(
        catalog_ids=("unit-a",),
        window_bounds={},
        candidate_returns={"unit-a": []},
        candidate_failures=candidate_failures,
        reset_returns={rule: [] for rule in ALLOCATION_RULE_IDS},
        reset_failures=reset_failures,
        carried_rows={},
        comparator_rows={},
        weight_summaries={},
        removal_all_valid=False,
    )

    _validate_pbo_payload(tmp_path, manifest, evidence, pool="uni-base")


def test_attribution_opening_values_are_bound_to_frozen_weights(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        FULL_DIMENSIONS,
        "uni-base",
        PoolDimensions(windows=1, canonical_units=1, declarations=1),
    )
    artifact = "joint_attribution.csv"
    common = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "pool": "uni-base",
        "window_index": 0,
        "window_start": "2026-01-01T00:00:00+00:00",
        "window_end": "2026-01-02T00:00:00+00:00",
        "allocation_rule": "equal_config",
    }

    def values(
        opening: float,
        closing: float,
        terminal_transfer: float = 0.0,
    ) -> dict[str, float]:
        return {
            "opening_value_usd": opening,
            "closing_value_usd": closing,
            "pnl_usd": closing - opening,
            "portfolio_return_contribution": (closing - opening) / 100.0,
            "total_fees_usd": 0.0,
            "total_fixed_cost_usd": 0.0,
            "total_variable_cost_usd": 0.0,
            "external_marked_notional_usd": 0.0,
            "external_input_value_usd": 0.0,
            "external_output_value_usd": 0.0,
            "internal_cross_notional_usd": 0.0,
            "terminal_funding_transfer_usd": terminal_transfer,
        }

    def write_rows(*, mutate: bool) -> None:
        sleeve_opening = 11.0 if mutate else 10.0
        with (tmp_path / artifact).open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=CSV_FIELDS[artifact],
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerow(
                {
                    **common,
                    "row_type": "sleeve",
                    "economic_id": "unit-a",
                    "family": "directional",
                    **values(sleeve_opening, sleeve_opening),
                }
            )
            writer.writerow(
                {
                    **common,
                    "row_type": "family",
                    "economic_id": "family:directional",
                    "family": "directional",
                    **values(sleeve_opening, sleeve_opening),
                }
            )
            writer.writerow(
                {
                    **common,
                    "row_type": "cash",
                    "economic_id": "cash",
                    "family": "cash",
                    **values(90.0, 90.0),
                }
            )

    carried = {
        ("equal_config", 0): {
            "status": "valid",
            "opening_capital_usd": "100",
            "closing_cash_usd": "100",
            "window_net_return": "0",
            "total_fees_usd": "0",
            "total_fixed_cost_usd": "0",
            "total_variable_cost_usd": "0",
            "external_marked_notional_usd": "0",
            "external_input_value_usd": "0",
            "external_output_value_usd": "0",
            "internal_cross_notional_usd": "0",
        },
        ("equal_family", 0): {"status": "blocked_prior_invalid"},
        ("shrinkage", 0): {"status": "blocked_prior_invalid"},
    }
    weights = {
        (0, "equal_config"): WeightSummary(
            deployed_weight=0.1,
            cash_weight=0.9,
            herfindahl=0.01,
            largest_weight=0.1,
            effective_sleeve_count=100.0,
            removed_weight=None,
            weights={"unit-a": 0.1},
        )
    }

    write_rows(mutate=False)
    _validate_attribution_rows(
        tmp_path,
        pool="uni-base",
        catalog_ids=("unit-a",),
        families={"unit-a": "directional"},
        carried=carried,
        weights=weights,
        bounds={},
    )

    funding_rows = list(csv.DictReader((tmp_path / artifact).read_text().splitlines()))
    funding_rows[-1]["terminal_funding_transfer_usd"] = "-0.4"
    with (tmp_path / artifact).open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS[artifact],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(funding_rows)

    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=ATTRIBUTION_ALLOCATION pool=uni-base "
            r"artifact=joint_attribution.csv$"
        ),
    ):
        _validate_attribution_rows(
            tmp_path,
            pool="uni-base",
            catalog_ids=("unit-a",),
            families={"unit-a": "directional"},
            carried=carried,
            weights=weights,
            bounds={},
        )

    write_rows(mutate=True)
    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=ATTRIBUTION_ALLOCATION pool=uni-base "
            r"artifact=joint_attribution.csv$"
        ),
    ):
        _validate_attribution_rows(
            tmp_path,
            pool="uni-base",
            catalog_ids=("unit-a",),
            families={"unit-a": "directional"},
            carried=carried,
            weights=weights,
            bounds={},
        )

    with (tmp_path / artifact).open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS[artifact],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(
            {
                **common,
                "row_type": "sleeve",
                "economic_id": "unit-a",
                "family": "directional",
                **values(10.0, 10.5, 0.5),
            }
        )
        writer.writerow(
            {
                **common,
                "row_type": "family",
                "economic_id": "family:directional",
                "family": "directional",
                **values(10.0, 10.5, 0.5),
            }
        )
        writer.writerow(
            {
                **common,
                "row_type": "cash",
                "economic_id": "cash",
                "family": "cash",
                **values(90.0, 89.5, -0.5),
            }
        )

    _validate_attribution_rows(
        tmp_path,
        pool="uni-base",
        catalog_ids=("unit-a",),
        families={"unit-a": "directional"},
        carried=carried,
        weights=weights,
        bounds={},
    )


def test_weight_vectors_must_equal_the_frozen_allocation_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        FULL_DIMENSIONS,
        "uni-base",
        PoolDimensions(windows=1, canonical_units=2, declarations=2),
    )
    initial_capital = POOL_EXPERIMENTS["uni-base"].initial_capital_usd
    catalog = build_portfolio_catalog("uni-base", initial_capital)
    units = catalog.allocation_units[:2]
    catalog_ids = tuple(unit.sleeve_id for unit in units)
    families = {unit.sleeve_id: unit.family for unit in units}
    metrics = {
        units[0].sleeve_id: TrainingMetrics(0.02, 2, 2.0, -0.02),
        units[1].sleeve_id: TrainingMetrics(0.01, 1, 1.5, -0.03),
    }
    constructors = {
        "equal_config": equal_config_weights,
        "equal_family": equal_family_weights,
        "shrinkage": shrinkage_weights,
    }
    artifact = "window_weights.csv"

    def write_rows(*, mutate: bool) -> None:
        with (tmp_path / artifact).open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=CSV_FIELDS[artifact],
                lineterminator="\n",
            )
            writer.writeheader()
            for rule in ALLOCATION_RULE_IDS:
                allocation = constructors[rule](catalog.allocation_units, metrics)
                weights = {
                    economic_id: allocation.weights.get(economic_id, 0.0)
                    for economic_id in catalog_ids
                }
                deployed = sum(weights.values())
                cash = 1.0 - deployed
                if mutate and rule == "equal_config":
                    weights[catalog_ids[0]] -= 0.01
                    deployed -= 0.01
                    cash += 0.01
                for economic_id in catalog_ids:
                    writer.writerow(
                        {
                            "schema_version": ARTIFACT_SCHEMA_VERSION,
                            "pool": "uni-base",
                            "window_index": 0,
                            "window_start": "2026-01-01T00:00:00+00:00",
                            "window_end": "2026-01-02T00:00:00+00:00",
                            "allocation_rule": rule,
                            "economic_id": economic_id,
                            "family": families[economic_id],
                            "weight": weights[economic_id],
                            "deployed_weight": deployed,
                            "cash_weight": cash,
                        }
                    )

    write_rows(mutate=False)
    _validate_weight_rows(
        tmp_path,
        pool="uni-base",
        catalog=catalog,
        catalog_ids=catalog_ids,
        families=families,
        eligible={0: set(catalog_ids)},
        training_metrics={0: metrics},
        removed_economic_id=None,
        bounds={},
    )

    write_rows(mutate=True)
    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=WEIGHT_CONSTRAINT pool=uni-base "
            r"artifact=window_weights.csv$"
        ),
    ):
        _validate_weight_rows(
            tmp_path,
            pool="uni-base",
            catalog=catalog,
            catalog_ids=catalog_ids,
            families=families,
            eligible={0: set(catalog_ids)},
            training_metrics={0: metrics},
            removed_economic_id=None,
            bounds={},
        )


def test_comparator_plans_are_rebuilt_from_catalog_and_training(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        FULL_DIMENSIONS,
        "uni-base",
        PoolDimensions(windows=1, canonical_units=4_895, declarations=4_935),
    )
    initial_capital = POOL_EXPERIMENTS["uni-base"].initial_capital_usd
    catalog = build_portfolio_catalog("uni-base", initial_capital)
    catalog_ids = tuple(unit.sleeve_id for unit in catalog.allocation_units)
    static = next(
        unit.sleeve_id
        for unit in catalog.allocation_units
        if unit.family == "static" and getattr(unit, "config_name", None) == "static_spot_w0025"
    )
    wrong_static = next(economic_id for economic_id in catalog_ids if economic_id != static)
    artifact = "comparators.csv"

    def write_rows(*, mutate: bool) -> None:
        with (tmp_path / artifact).open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=CSV_FIELDS[artifact],
                lineterminator="\n",
            )
            writer.writeheader()
            for comparator in COMPARATOR_IDS:
                if comparator in {
                    "cash",
                    "hold_cngn_mark",
                    "hold_cngn_pool_routed",
                }:
                    selection = "not_applicable"
                    selected = ""
                elif comparator == "static_spot_w0025":
                    selection = "predeclared"
                    selected = wrong_static if mutate else static
                else:
                    selection = "no_eligible"
                    selected = ""
                writer.writerow(
                    {
                        "schema_version": ARTIFACT_SCHEMA_VERSION,
                        "pool": "uni-base",
                        "window_index": 0,
                        "window_start": "2026-01-01T00:00:00+00:00",
                        "window_end": "2026-01-02T00:00:00+00:00",
                        "comparator_id": comparator,
                        "status": "valid",
                        "blocking_status": "",
                        "blocking_window_index": "",
                        "failure_type": "",
                        "failure_reason": "",
                        "selection_status": selection,
                        "selected_economic_id": selected,
                        **_zero_economic_row(initial_capital),
                    }
                )

    write_rows(mutate=False)
    _validate_comparator_rows(
        tmp_path,
        pool="uni-base",
        catalog=catalog,
        catalog_ids=catalog_ids,
        training_metrics={0: {}},
        bounds={},
    )

    write_rows(mutate=True)
    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=COMPARATOR_ROWS pool=uni-base "
            r"artifact=comparators.csv$"
        ),
    ):
        _validate_comparator_rows(
            tmp_path,
            pool="uni-base",
            catalog=catalog,
            catalog_ids=catalog_ids,
            training_metrics={0: {}},
            bounds={},
        )


def test_path_status_enforces_continuity_and_irreversible_blocking() -> None:
    valid = {
        **_economic_row(),
        "status": "valid",
        "blocking_status": "",
        "blocking_window_index": "",
        "failure_type": "",
        "failure_reason": "",
    }
    next_capital = {"rule": 100.0}
    blocked: dict[str, tuple[str, int, str, str]] = {}

    _validate_path_status(
        valid,
        method_id="rule",
        index=0,
        next_capital=next_capital,
        blocked=blocked,
        pool="uni-base",
        artifact="carried_portfolio_path.csv",
    )
    assert next_capital == {"rule": 101.0}

    invalid = {
        **{field: "" for field in ECONOMIC_FIELDS},
        "status": "invalid_terminal_liquidation",
        "blocking_status": "invalid_terminal_liquidation",
        "blocking_window_index": "1",
        "failure_type": "TerminalLiquidationError",
        "failure_reason": "terminal inventory swap exceeded available value",
    }
    _validate_path_status(
        invalid,
        method_id="rule",
        index=1,
        next_capital=next_capital,
        blocked=blocked,
        pool="uni-base",
        artifact="carried_portfolio_path.csv",
    )
    assert blocked == {
        "rule": (
            "invalid_terminal_liquidation",
            1,
            "TerminalLiquidationError",
            "terminal inventory swap exceeded available value",
        )
    }

    blocked_row = {
        **{field: "" for field in ECONOMIC_FIELDS},
        "status": "blocked_prior_invalid",
        "blocking_status": "invalid_terminal_liquidation",
        "blocking_window_index": "1",
        "failure_type": "TerminalLiquidationError",
        "failure_reason": "terminal inventory swap exceeded available value",
    }
    _validate_path_status(
        blocked_row,
        method_id="rule",
        index=2,
        next_capital=next_capital,
        blocked=blocked,
        pool="uni-base",
        artifact="carried_portfolio_path.csv",
    )

    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=PATH_STATUS pool=uni-base "
            r"artifact=carried_portfolio_path.csv$"
        ),
    ):
        _validate_path_status(
            {
                **blocked_row,
                "failure_reason": "a different terminal failure",
            },
            method_id="rule",
            index=3,
            next_capital=next_capital,
            blocked=blocked,
            pool="uni-base",
            artifact="carried_portfolio_path.csv",
        )

    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=PATH_STATUS pool=uni-base "
            r"artifact=carried_portfolio_path.csv$"
        ),
    ):
        _validate_path_status(
            {
                **{field: "" for field in ECONOMIC_FIELDS},
                "status": "invalid_terminal_liquidation",
                "blocking_status": "invalid_terminal_liquidation",
                "blocking_window_index": "0",
                "failure_type": "TerminalLiquidationError",
                "failure_reason": "x" * 513,
            },
            method_id="bounded",
            index=0,
            next_capital={"bounded": 100.0},
            blocked={},
            pool="uni-base",
            artifact="carried_portfolio_path.csv",
        )

    drifted = {
        **valid,
        "opening_capital_usd": "99",
        "closing_cash_usd": "99.99",
    }
    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=PATH_CONTINUITY pool=uni-base "
            r"artifact=carried_portfolio_path.csv$"
        ),
    ):
        _validate_path_status(
            drifted,
            method_id="other",
            index=0,
            next_capital={"other": 100.0},
            blocked={},
            pool="uni-base",
            artifact="carried_portfolio_path.csv",
        )


def test_pbo_payload_is_recomputed_from_candidate_and_reset_matrices(
    tmp_path: Path,
) -> None:
    windows = 26
    candidate_returns = {
        "candidate-a": [0.001 * ((index % 5) - 2) for index in range(windows)],
        "candidate-b": [0.0005 * ((index % 7) - 3) for index in range(windows)],
    }
    reset_returns = {
        "equal_config": [0.0002 * ((index % 3) - 1) for index in range(windows)],
        "equal_family": [0.0003 * ((index % 4) - 2) for index in range(windows)],
        "shrinkage": [0.0001 * ((index % 6) - 2) for index in range(windows)],
    }
    candidate_matrix = list(candidate_returns.values())
    reset_matrix = [reset_returns[rule] for rule in ALLOCATION_RULE_IDS]
    payload = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "pool": "uni-base",
        "candidate_sleeves": {
            "status": "computed",
            **asdict(compute_pbo(candidate_matrix, partitions=8)),
        },
        "allocation_rules": {
            "status": "computed",
            **asdict(compute_pbo(reset_matrix, partitions=8)),
        },
        "carried_paths": {"status": "not_applicable_path_dependent_carried_bankroll"},
    }
    (tmp_path / "pbo_allocation_rules.json").write_bytes(canonical_json_bytes(payload))
    selected = min(
        candidate_returns,
        key=lambda economic_id: (
            -sum(candidate_returns[economic_id]) / windows,
            economic_id,
        ),
    )
    manifest = {
        "candidate_reset_matrix": {
            "status": "complete_valid",
            "valid_rows": 127_270,
            "invalid_rows": 0,
            "invalid_status_counts": {},
        },
        "pbo_status": {
            "candidate_sleeves": "computed",
            "allocation_rules": "computed",
        },
        "best_sleeve_removal_economic_id": selected,
    }
    evidence = CsvEvidence(
        catalog_ids=tuple(candidate_returns),
        window_bounds={},
        candidate_returns=candidate_returns,
        candidate_failures=[],
        reset_returns=reset_returns,
        reset_failures=[],
        carried_rows={},
        comparator_rows={},
        weight_summaries={},
        removal_all_valid=True,
    )

    _validate_pbo_payload(tmp_path, manifest, evidence, pool="uni-base")

    payload["candidate_sleeves"]["pbo"] = 0.123
    (tmp_path / "pbo_allocation_rules.json").write_bytes(canonical_json_bytes(payload))
    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=PBO_RECOMPUTE pool=uni-base "
            r"artifact=pbo_allocation_rules.json$"
        ),
    ):
        _validate_pbo_payload(tmp_path, manifest, evidence, pool="uni-base")


def test_method_stability_repeats_the_first_failure_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        FULL_DIMENSIONS,
        "uni-base",
        PoolDimensions(windows=1),
    )
    diagnostic = {
        "status": "invalid_terminal_liquidation",
        "blocking_status": "invalid_terminal_liquidation",
        "blocking_window_index": "0",
        "failure_type": "TerminalLiquidationError",
        "failure_reason": "terminal inventory swap exceeded value",
    }
    evidence = CsvEvidence(
        catalog_ids=(),
        window_bounds={},
        candidate_returns={},
        candidate_failures=[],
        reset_returns={},
        reset_failures=[],
        carried_rows={(method_id, 0): dict(diagnostic) for method_id in ALLOCATION_RULE_IDS},
        comparator_rows={(method_id, 0): dict(diagnostic) for method_id in COMPARATOR_IDS},
        weight_summaries={},
        removal_all_valid=False,
    )
    artifact = "method_stability.csv"

    def write_rows(*, mutate: bool) -> None:
        with (tmp_path / artifact).open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=CSV_FIELDS[artifact],
                lineterminator="\n",
            )
            writer.writeheader()
            for method_id in (*ALLOCATION_RULE_IDS, *COMPARATOR_IDS):
                writer.writerow(
                    {
                        "schema_version": ARTIFACT_SCHEMA_VERSION,
                        "pool": "uni-base",
                        "method_id": method_id,
                        "method_kind": (
                            "allocation_rule" if method_id in ALLOCATION_RULE_IDS else "comparator"
                        ),
                        "completed_windows": 1,
                        "valid_windows": 0,
                        "invalid_windows": 1,
                        "first_invalid_status": "invalid_terminal_liquidation",
                        "first_invalid_window_index": 0,
                        "first_failure_type": "TerminalLiquidationError",
                        "first_failure_reason": (
                            "a different reason"
                            if mutate and method_id == ALLOCATION_RULE_IDS[0]
                            else diagnostic["failure_reason"]
                        ),
                        "cumulative_return": "",
                        "mean_window_return": "",
                        "median_window_return": "",
                        "positive_window_rate": "",
                        "worst_window_return": "",
                        "continuous_max_drawdown": "",
                        "total_fees_usd": "",
                        "total_fixed_cost_usd": "",
                        "total_variable_cost_usd": "",
                    }
                )

    write_rows(mutate=False)
    _validate_method_stability(tmp_path, evidence, pool="uni-base")

    write_rows(mutate=True)
    with pytest.raises(
        ValidationFailure,
        match=(
            r"^FAIL code=STABILITY_STATUS pool=uni-base "
            r"artifact=method_stability.csv$"
        ),
    ):
        _validate_method_stability(tmp_path, evidence, pool="uni-base")


def test_cli_failure_is_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        (
            "--publication-root",
            str(tmp_path),
            "--repo-root",
            str(Path.cwd()),
            "--pool",
            "uni-base",
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "FAIL code=ARTIFACT_SET pool=uni-base\n"


def test_cli_fails_closed_when_evidence_is_valid_but_claim_gate_is_blocked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validator,
        "validate_pool_publication",
        lambda *_args, **_kwargs: ValidationReport(
            pool="uni-base",
            windows=26,
            artifacts=14,
            manifest_sha256="0" * 64,
            evidence_status="not_publishable",
        ),
    )

    exit_code = main(
        (
            "--publication-root",
            str(tmp_path),
            "--repo-root",
            str(Path.cwd()),
            "--pool",
            "uni-base",
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "FAIL code=CLAIM_GATE pool=uni-base\n"


def test_integrity_only_mode_attests_a_fail_closed_diagnostic_package(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validator,
        "validate_pool_publication",
        lambda *_args, **_kwargs: ValidationReport(
            pool="uni-base",
            windows=26,
            artifacts=14,
            manifest_sha256="0" * 64,
            evidence_status="not_publishable",
        ),
    )

    exit_code = main(
        (
            "--publication-root",
            str(tmp_path),
            "--repo-root",
            str(Path.cwd()),
            "--pool",
            "uni-base",
            "--integrity-only",
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == (
        "PASS pool=uni-base windows=26 artifacts=14 "
        f"manifest_sha256={'0' * 64} evidence_status=not_publishable\n"
    )
