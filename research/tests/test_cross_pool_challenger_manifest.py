from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from research.cross_pool.challenger_inference import infer_challengers
from research.cross_pool.challenger_manifest import (
    CHALLENGER_SOURCE_PATHS,
    build_generated_challenger_manifest,
    canonical_challenger_manifest_bytes,
    reviewed_challenger_manifest,
    validate_challenger_manifest,
)
from research.cross_pool.challenger_parent import load_challenger_parent
from research.cross_pool.challenger_reporting import render_challenger_artifacts
from research.cross_pool.challengers import run_challengers
from research.cross_pool.contracts import CrossPoolContractError

PARENT_DIR = Path("research/results/cross_pool_lead_lag")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def generated_manifest():
    parent = load_challenger_parent(PARENT_DIR)
    study = run_challengers(parent)
    inference = infer_challengers(study)
    artifacts = render_challenger_artifacts(study, inference)
    return build_generated_challenger_manifest(
        parent=parent,
        study=study,
        inference=inference,
        artifacts=artifacts,
        code_commit="a" * 40,
        source_diff_sha256="b" * 64,
    )


def test_generated_manifest_is_canonical_strict_and_complete(generated_manifest) -> None:
    validate_challenger_manifest(generated_manifest)
    raw = canonical_challenger_manifest_bytes(generated_manifest)

    assert raw.endswith(b"\n")
    assert generated_manifest["artifact_status"] == "generated_unreviewed"
    assert generated_manifest["research_role"] == "post_hoc_exploratory"
    assert generated_manifest["parent_decision_unchanged"] is True
    assert generated_manifest["registry"]["metric_cells"] == 270
    assert generated_manifest["registry"]["contrast_cells"] == 378
    assert generated_manifest["registry"]["source_price_cells"] == 126
    assert len(generated_manifest["artifacts"]) == 11
    configuration = generated_manifest["configuration"]
    assert configuration["arx2"]["predecessor"] == "prior_regular_panel_row"
    assert configuration["gam"]["age_transform"] == "log1p_age_ms_over_60000"
    assert configuration["gam"]["quantile_method"] == "linear"
    assert configuration["two_part"]["maximum_line_search_halvings"] == 50
    assert configuration["two_part"]["log_loss_clipping"] == "finfo_eps_only"
    assert configuration["bootstrap"]["generator"] == "PCG64"
    assert configuration["bootstrap"]["raw_interval"] == "central_nearest_rank"
    assert configuration["bootstrap"]["max_t"] == "single_step_centered_absolute"
    assert configuration["bootstrap"]["adjusted_p_value"] == "plus_one_two_sided"
    assert configuration["adequacy"]["minimum_complete_blocks"] == 6


def test_source_closure_contains_every_static_local_import() -> None:
    closure = set(CHALLENGER_SOURCE_PATHS)
    assert len(closure) == len(CHALLENGER_SOURCE_PATHS)
    assert {
        "research/cross_pool/article_manifest.schema.json",
        "research/cross_pool/challenger_manifest.schema.json",
    } <= closure

    for relative_name in CHALLENGER_SOURCE_PATHS:
        source_path = REPOSITORY_ROOT / relative_name
        assert source_path.is_file(), relative_name
        if source_path.suffix != ".py":
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=relative_name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == "__import__"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            ):
                raise AssertionError(f"dynamic import is not source-bound: {relative_name}")
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
            else:
                continue
            for module in modules:
                if not module.startswith(("engine", "research")):
                    continue
                parts = module.split(".")
                candidates = [
                    "/".join(parts) + ".py",
                    "/".join((*parts, "__init__.py")),
                ]
                candidates.extend(
                    "/".join((*parts[:index], "__init__.py"))
                    for index in range(1, len(parts))
                )
                for candidate in candidates:
                    if (REPOSITORY_ROOT / candidate).is_file():
                        assert candidate in closure, (
                            f"{relative_name} imports unbound local source {candidate}"
                        )


def test_manifest_rejects_changed_configuration_or_claim_boundary(generated_manifest) -> None:
    changed = deepcopy(generated_manifest)
    changed["configuration"]["bootstrap"]["block_days"] = 1
    with pytest.raises(CrossPoolContractError):
        validate_challenger_manifest(changed)

    changed = deepcopy(generated_manifest)
    changed["parent_decision_unchanged"] = False
    with pytest.raises(CrossPoolContractError):
        validate_challenger_manifest(changed)


def test_review_transition_changes_only_status_and_review(generated_manifest) -> None:
    reviewed = reviewed_challenger_manifest(
        generated_manifest,
        reviewed_by="sol_ultra",
        reviewed_at_utc="2026-07-27T23:59:00Z",
    )

    assert reviewed["artifact_status"] == "reviewed"
    assert reviewed["review"] == {
        "status": "reviewed",
        "reviewed_by": "sol_ultra",
        "reviewed_at_utc": "2026-07-27T23:59:00Z",
    }
    projection = deepcopy(reviewed)
    projection["artifact_status"] = "generated_unreviewed"
    projection["review"] = generated_manifest["review"]
    assert projection == generated_manifest
    validate_challenger_manifest(reviewed)
