from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.short_horizon_manifest import (
    PARENT_MANIFEST_SHA256,
    SHORT_HORIZON_SOURCE_DEPENDENCIES,
    QaBlockedManifestInput,
    QaReasonCode,
    ShortHorizonProvenance,
    build_generated_short_horizon_manifest,
    build_qa_blocked_short_horizon_manifest,
    canonical_short_horizon_manifest_bytes,
    capture_short_horizon_source,
    load_and_validate_short_horizon_manifest,
    reviewed_short_horizon_manifest,
    validate_short_horizon_manifest,
)
from research.tests.short_horizon_manifest_fixtures import (
    ARTIFACT_NAMES,
    generated_manifest,
    generated_manifest_input,
    manifest_provenance,
)


def test_generated_manifest_is_canonical_complete_and_parent_bound(tmp_path: Path) -> None:
    manifest = generated_manifest()
    raw = canonical_short_horizon_manifest_bytes(manifest)
    path = tmp_path / "short_horizon_manifest.json"
    path.write_bytes(raw)

    assert load_and_validate_short_horizon_manifest(path) == manifest
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["artifact_status"] == "generated_unreviewed"
    parent = cast(dict[str, object], manifest["parent"])
    assert parent == {
        "manifest_sha256": PARENT_MANIFEST_SHA256,
        "schema_version": "2.0.0",
        "artifact_status": "reviewed",
        "article_branch": "leadership_unresolved",
        "economic_class": "no_net_return_improvement",
        "allowed_claim": "aggregate_leadership_unresolved",
        "base_features_sha256": (
            "47bae897266c00d92823fcae8d004debf9e50d22639ab52c326b121c9b7da23a"
        ),
        "bsc_features_sha256": ("aba2dbef0548d4bca4882c26f220da14f963c1727bff855cd7532784c2cfaa5c"),
    }
    assert tuple(cast(dict[str, str], manifest["artifacts"])) == ARTIFACT_NAMES
    results = cast(dict[str, object], manifest["results"])
    cohorts = cast(list[dict[str, object]], results["cohorts"])
    assert tuple((row["direction"], row["cohort"]) for row in cohorts) == (
        ("bsc_to_base", "primary"),
        ("bsc_to_base", "exclude_same_timestamp"),
        ("base_to_bsc", "primary"),
        ("base_to_bsc", "exclude_same_timestamp"),
    )


def test_generated_manifest_rejects_wrong_frozen_input_and_parent_anchor() -> None:
    inputs = generated_manifest_input()
    bad_parent = replace(
        inputs.provenance.parent_manifest,
        sha256="d" * 64,
    )
    with pytest.raises(CrossPoolContractError, match="parent manifest"):
        build_generated_short_horizon_manifest(
            replace(inputs, provenance=replace(inputs.provenance, parent_manifest=bad_parent))
        )
    with pytest.raises(CrossPoolContractError, match="parent anchor"):
        build_generated_short_horizon_manifest(replace(inputs, parent_anchor_reconciled=False))


def test_manifest_requires_the_exact_source_dependency_closure() -> None:
    assert SHORT_HORIZON_SOURCE_DEPENDENCIES == (
        "engine/web3_utils.py",
        "research/cross_pool/bootstrap.py",
        "research/cross_pool/contracts.py",
        "research/cross_pool/event_study.py",
        "research/cross_pool/io.py",
        "research/cross_pool/short_horizon.py",
        "research/cross_pool/short_horizon_artifacts.py",
        "research/cross_pool/short_horizon_inference.py",
        "research/cross_pool/short_horizon_manifest.py",
        "research/cross_pool/short_horizon_manifest.schema.json",
        "research/cross_pool/short_horizon_parent.py",
        "research/cross_pool/short_horizon_publication.py",
        "research/cross_pool/short_horizon_reporting.py",
        "research/scripts/review_cross_pool_short_horizon.py",
        "research/scripts/run_cross_pool_short_horizon.py",
    )
    inputs = generated_manifest_input()
    with pytest.raises(CrossPoolContractError, match="exact source dependency closure"):
        ShortHorizonProvenance(
            parent_manifest=inputs.provenance.parent_manifest,
            base_features=inputs.provenance.base_features,
            bsc_features=inputs.provenance.bsc_features,
            girum_note=inputs.provenance.girum_note,
            code_commit=inputs.provenance.code_commit,
            source_dependencies=inputs.provenance.source_dependencies[:-1],
            runtime=inputs.provenance.runtime,
        )


def test_manifest_loader_rejects_duplicate_nonfinite_noncanonical_and_unknown_fields(
    tmp_path: Path,
) -> None:
    manifest = generated_manifest()
    path = tmp_path / "short_horizon_manifest.json"

    path.write_text('{"schema_version":"1.0.0","schema_version":"1.0.0"}\n')
    with pytest.raises(CrossPoolContractError, match="duplicate"):
        load_and_validate_short_horizon_manifest(path)

    path.write_text('{"value":NaN}\n')
    with pytest.raises(CrossPoolContractError, match="non-finite"):
        load_and_validate_short_horizon_manifest(path)

    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CrossPoolContractError, match="canonical"):
        load_and_validate_short_horizon_manifest(path)

    unknown = deepcopy(manifest)
    cast(dict[str, object], unknown["contract"])["unknown"] = True
    with pytest.raises(CrossPoolContractError, match="schema"):
        validate_short_horizon_manifest(unknown)


def test_qa_blocked_manifest_is_manifest_only_and_cannot_be_reviewed() -> None:
    blocked = build_qa_blocked_short_horizon_manifest(
        QaBlockedManifestInput(
            provenance=replace(manifest_provenance(), base_features=None),
            reason_codes=("missing_base_features",),
        )
    )

    assert blocked["artifact_status"] == "qa_blocked"
    assert blocked["artifacts"] == {}
    assert blocked["results"] == {"status": "unavailable", "cohorts": None}
    with pytest.raises(CrossPoolContractError, match="QA-pass generated"):
        reviewed_short_horizon_manifest(
            blocked,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T12:00:00Z",
        )


def test_qa_blocked_input_hash_reason_must_match_observed_provenance() -> None:
    wrong_base = replace(
        manifest_provenance().base_features,
        sha256="d" * 64,
    )
    assert wrong_base is not None
    blocked = build_qa_blocked_short_horizon_manifest(
        QaBlockedManifestInput(
            provenance=replace(manifest_provenance(), base_features=wrong_base),
            reason_codes=("base_features_hash_mismatch",),
        )
    )
    assert blocked["artifact_status"] == "qa_blocked"

    with pytest.raises(CrossPoolContractError, match="input reasons"):
        build_qa_blocked_short_horizon_manifest(
            QaBlockedManifestInput(
                provenance=replace(manifest_provenance(), base_features=None),
                reason_codes=("measurement_failure",),
            )
        )

    with pytest.raises(CrossPoolContractError, match="unsupported QA reason"):
        build_qa_blocked_short_horizon_manifest(
            QaBlockedManifestInput(
                provenance=manifest_provenance(),
                reason_codes=cast(
                    tuple[QaReasonCode, ...],
                    ("source_dependency_failure",),
                ),
            )
        )


def test_loaded_results_reconstruct_typed_count_and_band_contracts() -> None:
    manifest = generated_manifest()
    cohorts = cast(
        list[dict[str, object]],
        cast(dict[str, object], manifest["results"])["cohorts"],
    )
    summaries = cast(list[dict[str, object]], cohorts[0]["summaries"])
    summaries[0]["update_incidence"] = 0.99

    with pytest.raises(CrossPoolContractError, match="incidence"):
        validate_short_horizon_manifest(manifest)


def test_review_transition_changes_only_status_and_review_fields() -> None:
    generated = generated_manifest()
    reviewed = reviewed_short_horizon_manifest(
        generated,
        reviewed_by="sol_ultra",
        reviewed_at_utc="2026-07-27T12:00:00Z",
    )
    expected = deepcopy(generated)
    expected["artifact_status"] = "reviewed"
    expected["review"] = {
        "status": "reviewed",
        "reviewed_by": "sol_ultra",
        "reviewed_at_utc": "2026-07-27T12:00:00Z",
    }

    assert reviewed == expected
    validate_short_horizon_manifest(reviewed)
    with pytest.raises(CrossPoolContractError, match="already"):
        reviewed_short_horizon_manifest(
            reviewed,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27T12:00:00Z",
        )
    with pytest.raises(CrossPoolContractError, match="UTC Z"):
        reviewed_short_horizon_manifest(
            generated,
            reviewed_by="sol_ultra",
            reviewed_at_utc="2026-07-27 12:00:00",
        )


def test_scoped_source_capture_ignores_unrelated_dirt_but_rejects_dependency_dirt(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    for relative_path in SHORT_HORIZON_SOURCE_DEPENDENCIES:
        dependency = repo / relative_path
        dependency.parent.mkdir(parents=True, exist_ok=True)
        dependency.write_text(f"fixture:{relative_path}\n")
    dependency = repo / SHORT_HORIZON_SOURCE_DEPENDENCIES[0]
    unrelated = repo / "research" / "backtester" / "portfolio_simulator.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("original\n")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    unrelated.write_text("dirty\n")

    capture = capture_short_horizon_source(repo)

    assert tuple(item.relative_path for item in capture.dependencies) == (
        SHORT_HORIZON_SOURCE_DEPENDENCIES
    )
    dependency.write_text("dirty dependency\n")
    with pytest.raises(CrossPoolContractError, match="dependency closure is dirty"):
        capture_short_horizon_source(repo)

    dependency.write_text(f"fixture:{SHORT_HORIZON_SOURCE_DEPENDENCIES[0]}\n")
    _git(
        repo,
        "update-index",
        "--assume-unchanged",
        SHORT_HORIZON_SOURCE_DEPENDENCIES[0],
    )
    dependency.write_text("hidden dirty dependency\n")
    with pytest.raises(CrossPoolContractError, match="dependency closure is dirty"):
        capture_short_horizon_source(repo)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
