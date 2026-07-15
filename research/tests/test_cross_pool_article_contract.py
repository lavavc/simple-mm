from __future__ import annotations

import re
from pathlib import Path

import pytest

DESIGN_PATH = Path("docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md")

TERMINOLOGY_PATHS = (
    Path("research/articles/02-backtesting-the-market-layer.md"),
    Path("research/articles/evidence-pack-2026-07-cngn-market-making.md"),
    Path("research/autoresearch/lp.md"),
    Path("research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md"),
)

CANONICAL_POLICY_TRANSFER = (
    "The Base strict-QTS 20/25 directional LP policy did not transfer to BSC: "
    "it returned -1.272% across seven BSC windows, versus +1.039% across four "
    "Base windows."
)

CANONICAL_INFORMATION_TRANSFER = (
    "That result concerns policy transferability. It does not test whether "
    "lagged BSC pool prices contain incremental information about future Base "
    "price changes."
)

STALE_POLICY_TRANSFER_PHRASES = (
    "BSC rejects cross-pool generalization",
    "BSC rejects transferability",
    "BSC rejection",
    "BSC rejects the same strict QTS gates",
    "strict QTS gates reject BSC",
)

LP_RESULT_ROWS = (
    "| Base | `gate_strict_qts_20_25` | 4 | +1.039% | +0.118% | 100.0% | +0.228 pp | +0.796% |",
    "| BSC | `gate_strict_qts_20_25` | 7 | -1.272% | -0.842% | 14.3% | -1.602 pp | +0.400% |",
)

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


@pytest.mark.parametrize("path", TERMINOLOGY_PATHS)
def test_policy_transfer_language_does_not_prejudge_information_transfer(
    path: Path,
) -> None:
    text = path.read_text()
    normalized = " ".join(text.split())

    for stale_phrase in STALE_POLICY_TRANSFER_PHRASES:
        assert stale_phrase not in text
    assert CANONICAL_POLICY_TRANSFER in normalized
    assert CANONICAL_INFORMATION_TRANSFER in normalized


@pytest.mark.parametrize(
    "path",
    (
        Path("research/articles/02-backtesting-the-market-layer.md"),
        Path("research/articles/evidence-pack-2026-07-cngn-market-making.md"),
    ),
)
def test_policy_transfer_edit_preserves_result_tables(path: Path) -> None:
    text = path.read_text()

    for row in LP_RESULT_ROWS:
        assert row in text


def test_policy_transfer_edit_preserves_lp_metrics_and_promotion_boundary() -> None:
    lp_text = Path("research/autoresearch/lp.md").read_text()
    normalized_lp = " ".join(lp_text.split())
    article_text = Path("research/articles/02-backtesting-the-market-layer.md").read_text()
    normalized_article = " ".join(article_text.split())
    evidence_text = Path(
        "research/articles/evidence-pack-2026-07-cngn-market-making.md"
    ).read_text()
    normalized_evidence = " ".join(evidence_text.split())

    assert (
        "worst -0.842%, 14.3% positive, and -1.602 percentage points versus hold."
    ) in normalized_lp
    assert "The Base slice is not live LP alpha." in lp_text
    assert "no live LP promotion is justified" in normalized_article
    assert (
        "No live LP promotion is justified without a non-pool inventory comparator."
        in normalized_evidence
    )
    assert "Closeout decision: DEX LP remains diagnostic, not deployable." in lp_text


def _section_between(text: str, start: str, end: str) -> str:
    return text.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]


def _backticked_bullets(section: str) -> tuple[str, ...]:
    return tuple(re.findall(r"^- `([^`]+)`[.;]$", section, flags=re.MULTILINE))
