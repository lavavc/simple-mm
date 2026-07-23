from __future__ import annotations

import re
from pathlib import Path

import pytest

DESIGN_PATH = Path("docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md")

PUBLIC_TERMINOLOGY_PATHS = (
    Path("research/articles/02-backtesting-the-market-layer.md"),
    Path("research/articles/evidence-pack-2026-07-cngn-market-making.md"),
)

INTERNAL_TERMINOLOGY_PATHS = (
    Path("research/autoresearch/lp.md"),
    Path("research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md"),
)

TERMINOLOGY_PATHS = PUBLIC_TERMINOLOGY_PATHS + INTERNAL_TERMINOLOGY_PATHS

PUBLIC_POLICY_TRANSFER = (
    "The pre-specified diagnostic directional LP policy did not transfer across "
    "pools; this is a pool-separated research result, not a live recommendation."
)

PUBLIC_INFORMATION_TRANSFER = (
    "That transfer finding does not answer whether either pool contributes "
    "stable incremental information about the other."
)

INTERNAL_POLICY_TRANSFER = (
    "The Base strict-QTS 20/25 directional LP policy did not transfer to BSC: "
    "it returned -1.272% across seven BSC windows, versus +1.039% across four "
    "Base windows."
)

INTERNAL_INFORMATION_TRANSFER = (
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

REVIEWED_BLOCK = """CPL_EDITORIAL_STATUS: EVIDENCE_REVIEWED
CPL_PRIMARY_CLASS: inconclusive
CPL_REVERSE_CLASS: inconclusive
CPL_ARTICLE_BRANCH: leadership_unresolved
CPL_ECONOMIC_CLASS: no_net_return_improvement
CPL_ROBUSTNESS_STATUS: complete
CPL_ROBUSTNESS_FLAGS: dtw_band_unstable
CPL_SOURCE_MANIFEST: research/results/cross_pool_lead_lag/article_manifest.json"""

REVIEWED_ARTICLE_PATHS = (
    Path("research/articles/README.md"),
    Path("research/articles/02-backtesting-the-market-layer.md"),
    Path("research/articles/evidence-pack-2026-07-cngn-market-making.md"),
)

CLOSED_ACQUISITION_PATHS = (
    Path("research/autoresearch/README.md"),
    Path("research/autoresearch/fair-price.md"),
    Path("research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md"),
)

CLOSED_ACQUISITION = (
    "The July 2026 branch no longer includes fintech quote APIs, "
    "CBN/FMDQ/NAFEM rates, or renewed Bybit historical searches. "
    "External-reference hooks remain available only for genuinely new "
    "timestamped overlapping data supplied later; acquiring that data is not "
    "an open task."
)

PUBLIC_LP_RESULT_ROWS = (
    "| Base | 4 | +1.039% | +0.118% | 100.0% | +0.228 pp | +0.796% |",
    "| BSC | 7 | -1.272% | -0.842% | 14.3% | -1.602 pp | +0.400% |",
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
    assert "schema version `2.0.0`" in design
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
def test_policy_transfer_language_omits_stale_overclaims(
    path: Path,
) -> None:
    text = path.read_text()

    for stale_phrase in STALE_POLICY_TRANSFER_PHRASES:
        assert stale_phrase not in text


@pytest.mark.parametrize("path", PUBLIC_TERMINOLOGY_PATHS)
def test_public_policy_transfer_language_is_aggregate_and_non_operational(
    path: Path,
) -> None:
    normalized = " ".join(path.read_text().split())

    assert PUBLIC_POLICY_TRANSFER in normalized
    assert PUBLIC_INFORMATION_TRANSFER in normalized
    assert "strict-QTS" not in normalized
    assert "gate_strict_qts_20_25" not in normalized


@pytest.mark.parametrize("path", INTERNAL_TERMINOLOGY_PATHS)
def test_internal_policy_transfer_language_preserves_audit_detail(path: Path) -> None:
    normalized = " ".join(path.read_text().split())

    assert INTERNAL_POLICY_TRANSFER in normalized
    assert INTERNAL_INFORMATION_TRANSFER in normalized


@pytest.mark.parametrize(
    "path",
    (
        Path("research/articles/02-backtesting-the-market-layer.md"),
        Path("research/articles/evidence-pack-2026-07-cngn-market-making.md"),
    ),
)
def test_public_policy_transfer_preserves_pool_separated_aggregate_results(
    path: Path,
) -> None:
    text = path.read_text()

    for row in PUBLIC_LP_RESULT_ROWS:
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


@pytest.mark.parametrize("path", REVIEWED_ARTICLE_PATHS)
def test_reviewed_article_files_share_one_status_contract(path: Path) -> None:
    text = path.read_text()

    assert text.count(REVIEWED_BLOCK) == 1


def test_reviewed_block_stays_out_of_historical_closeout_documents() -> None:
    for path in (
        Path("research/autoresearch/lp.md"),
        Path("research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md"),
    ):
        assert REVIEWED_BLOCK not in path.read_text()


def test_reviewed_blocks_use_the_approved_document_anchors() -> None:
    readme = Path("research/articles/README.md").read_text()
    article = Path("research/articles/02-backtesting-the-market-layer.md").read_text()
    evidence = Path("research/articles/evidence-pack-2026-07-cngn-market-making.md").read_text()

    assert readme.index(REVIEWED_BLOCK) < readme.index("Active July 2026 sequence:")
    assert article.index("## Why This Fits Lava") < article.index(REVIEWED_BLOCK)
    assert article.index(REVIEWED_BLOCK) < article.index("## Narrative Progression")
    assert evidence.index("Purpose:") < evidence.index(REVIEWED_BLOCK)
    assert evidence.index(REVIEWED_BLOCK) < evidence.index("## Source Map")


def test_handoff_addendum_records_reviewed_cross_pool_result() -> None:
    handoff = Path(
        "research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md"
    ).read_text()
    addendum = _section_between(
        handoff,
        "## July 15, 2026 Addendum",
        "## Current Research State",
    )
    normalized = " ".join(addendum.split())

    assert handoff.count("## July 15, 2026 Addendum") == 1
    assert "docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md" in handoff
    assert "results remain pending" not in normalized
    assert "`leadership_unresolved`" in addendum
    assert "`no_net_return_improvement`" in addendum
    assert "`dtw_band_unstable`" in addendum
    assert "research/results/cross_pool_lead_lag/article_manifest.json" in addendum
    assert handoff.index("## July 15, 2026 Addendum") < handoff.index("## Current Research State")


@pytest.mark.parametrize("path", CLOSED_ACQUISITION_PATHS)
def test_external_reference_acquisition_is_explicitly_closed(path: Path) -> None:
    normalized = " ".join(path.read_text().split())

    assert CLOSED_ACQUISITION in normalized


def test_closeout_docs_do_not_reopen_rejected_external_search() -> None:
    handoff = Path(
        "research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md"
    ).read_text()
    fair_price = Path("research/autoresearch/fair-price.md").read_text()
    lp = Path("research/autoresearch/lp.md").read_text()

    assert "Try, in order:" not in handoff
    assert "should be expanded with forward capture" not in fair_price
    assert "Bybit P2P remains the best forward external anchor" not in fair_price
    assert "Deferred until new comparator data exists:" not in lp
    assert "Deferred only if genuinely new comparator data is supplied:" in lp


def test_article_two_contains_reviewed_cross_pool_result_and_boundaries() -> None:
    article = Path("research/articles/02-backtesting-the-market-layer.md").read_text()
    normalized = " ".join(article.split())

    for phrase in (
        "### 7. The separate cross-pool information-transfer experiment",
        "`raw_sqrt_mid`",
        "early/late",
        "causal as-of",
        "one-hour primary",
        "15-minute and four-hour sensitivities",
        "compares nested models",
        "adds BSC features to a Base-only baseline",
        "Base-only baseline",
        "cross-pool model",
        "14-day warmup",
        "weekly walk-forward",
        "reverse Base-to-BSC falsification",
        "unconditional frozen-policy evaluation",
        "Both one-hour directional tests were `inconclusive`",
        "`leadership_unresolved`",
        "`no_net_return_improvement`",
        "USDC/USDT parity",
        "below 10 basis points",
        "causal price discovery",
        "toxic flow",
        "external-LP profitability",
        "deployable alpha",
    ):
        assert phrase in normalized
    assert article.index(
        "### 7. The separate cross-pool information-transfer experiment"
    ) < article.index("### 8. Why the failures are the point")


def test_article_two_scaffolds_the_final_portfolio_test_without_a_result_claim() -> None:
    article = Path("research/articles/02-backtesting-the-market-layer.md").read_text()
    normalized = " ".join(article.split())
    pending_marker = "<!-- PORTFOLIO_RESULT: PENDING_VALIDATION -->"

    for phrase in (
        "#### The final portfolio test",
        "canonical economic candidates",
        "joint portfolio accounting",
        "carried capital paths",
        "reset-only diagnostics",
        "Portfolio results — publication gated.",
        "separately for each pool",
        "missing non-pool inventory comparator",
    ):
        assert phrase in normalized
    assert article.index("#### The final portfolio test") < article.index(
        "### 7. The separate cross-pool information-transfer experiment"
    )
    assert article.count(pending_marker) == 1
    assert (
        article.index("#### The final portfolio test")
        < article.index(pending_marker)
        < article.index("### 7. The separate cross-pool information-transfer experiment")
    )


@pytest.mark.parametrize("path", REVIEWED_ARTICLE_PATHS)
def test_reviewed_article_files_publish_only_approved_aggregate_outcomes(
    path: Path,
) -> None:
    text = path.read_text()

    assert "leadership_unresolved" in text
    assert "no_net_return_improvement" in text
    assert "deployable alpha" in text or path.name == "README.md"


def test_durable_evidence_pack_records_complete_review_provenance_keys() -> None:
    evidence = Path(
        "research/articles/evidence-pack-2026-07-cngn-market-making.md"
    ).read_text()
    for key in (
        "CPL_MANIFEST_SHA256",
        "CPL_REVIEWED_BY",
        "CPL_REVIEWED_AT_UTC",
        "CPL_CODE_COMMIT",
        "CPL_SCHEMA_VERSION",
        "CPL_SOURCE_DIFF_SHA256",
        "CPL_INPUT_SHA256_BASE_FEATURES",
        "CPL_INPUT_SHA256_BSC_FEATURES",
        "CPL_INPUT_SHA256_BASE_REPLAY",
        "CPL_INPUT_SHA256_BSC_REPLAY",
        "CPL_INPUT_SHA256_BASE_LEDGER",
        "CPL_INPUT_SHA256_BSC_LEDGER",
    ):
        assert len(re.findall(rf"^{key}: \S+$", evidence, flags=re.MULTILINE)) == 1


def test_reviewed_article_files_link_the_approved_design() -> None:
    design_path = "docs/superpowers/specs/2026-07-15-cross-pool-price-leadership-design.md"

    assert design_path in Path("research/articles/README.md").read_text()
    assert (
        design_path
        in Path("research/articles/evidence-pack-2026-07-cngn-market-making.md").read_text()
    )


def test_reviewed_article_preserves_thesis_and_historical_closeout_boundaries() -> None:
    article = Path("research/articles/02-backtesting-the-market-layer.md").read_text()
    normalized_article = " ".join(article.split())
    handoff = Path(
        "research/autoresearch/research-closeout-and-article-handoff-2026-07-10.md"
    ).read_text()

    assert (
        "The honest way to build market infrastructure for local stablecoins "
        "is to turn every trading belief into a timestamped hypothesis, then "
        "publish the tests that survive contact with venue-specific data."
    ) in normalized_article
    assert (
        "Do not merge Base and BSC into one market unless transferability is the claim."
        in normalized_article
    )
    assert "Date: 2026-07-10" in handoff
    assert "Do not search again without genuinely new data." in handoff


def _section_between(text: str, start: str, end: str) -> str:
    return text.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]


def _backticked_bullets(section: str) -> tuple[str, ...]:
    return tuple(re.findall(r"^- `([^`]+)`[.;]$", section, flags=re.MULTILINE))
