from __future__ import annotations

import re
from pathlib import Path

DESIGN_PATH = Path("docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md")

MANIFEST_GROUPS = (
    "schema_version",
    "artifact_status",
    "provenance",
    "qa",
    "robustness",
    "predictive",
    "event_study",
    "dtw",
    "market_structure",
    "economics",
    "publication",
    "figures",
    "artifacts",
    "review",
)

PUBLICATION_BRANCHES = (
    "bsc_to_base_incremental",
    "base_to_bsc_incremental",
    "bidirectional_incremental_no_unique_leader",
    "no_material_incremental_lead",
    "leadership_unresolved",
    "not_adjudicable_qa",
)

ECONOMIC_BRANCHES = (
    "pareto_improvement",
    "return_risk_tradeoff",
    "no_net_return_improvement",
    "not_adjudicable_qa",
)


def test_cross_pool_design_pins_writer_manifest_and_branches() -> None:
    design = DESIGN_PATH.read_text()
    normalized = " ".join(design.split())

    assert "article_manifest.json" in design
    assert "schema version `1.0.0`" in design
    manifest_section = _section_between(
        design,
        "The top-level manifest groups are:",
        "The manifest distinguishes three concepts.",
    )
    publication_section = _section_between(
        design,
        "Publication branches are:",
        "Select exactly one branch with this frozen decision table:",
    )
    economics_section = _section_between(
        design,
        "Economic classification is independent:",
        "Compare the gated policy with the original policy",
    )
    assert _backticked_bullets(manifest_section) == MANIFEST_GROUPS
    assert _backticked_bullets(publication_section) == PUBLICATION_BRANCHES
    assert tuple(re.findall(r"`([^`]+)`", economics_section)) == ECONOMIC_BRANCHES
    assert (
        "Generated code may set only `generated_unreviewed` or `qa_blocked`; "
        "it cannot mark its own evidence `reviewed`."
    ) in normalized
    assert (
        "A reviewed manifest requires reviewer identity and UTC time plus a "
        "schema-valid publication branch."
    ) in normalized
    assert (
        "Data-valid reviewed evidence requires complete aggregate fields, "
        "exactly one publication branch, and one independent economic branch."
    ) in normalized
    assert (
        'A human-reviewed QA failure requires `qa.status == "blocked"`, '
        "nonempty reasons, both branches set to `not_adjudicable_qa`, "
        "unavailable result groups, and no allowed performance claims."
    ) in normalized
    assert (
        "Serialization rejects NaN and Infinity and uses sorted keys, stable "
        "arrays, and unit-bearing field names."
    ) in normalized
    assert (
        "The bundled Draft 2020-12 JSON Schema and "
        "`validate_article_manifest()` are the enforcement boundary."
    ) in normalized
    assert (
        "is a policy-transfer finding; it does not prejudge this information-transfer experiment"
    ) in normalized


def _section_between(text: str, start: str, end: str) -> str:
    return text.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]


def _backticked_bullets(section: str) -> tuple[str, ...]:
    return tuple(re.findall(r"^- `([^`]+)`[.;]$", section, flags=re.MULTILINE))
