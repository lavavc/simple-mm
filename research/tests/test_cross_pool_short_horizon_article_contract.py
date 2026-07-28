from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

PUBLICATION_DIR = Path("research/results/reports/cross_pool_short_horizon_v1")
PUBLICATION_MANIFEST = PUBLICATION_DIR / "short_horizon_manifest.json"
PARENT_PUBLICATION_DIR = Path("research/results/reports/cross_pool_lead_lag")

ARTICLE = Path("research/articles/02-backtesting-the-market-layer.md")
EVIDENCE_PACK = Path(
    "research/articles/evidence-pack-2026-07-cngn-market-making.md"
)
README = Path("research/articles/README.md")
CTO_BRIEF = Path("research/articles/cto-final-research-brief-2026-07.md")

MANIFEST_SHA256 = (
    "9145738bb6dd4aa84512b3f62625d779e6e4ef223f5b614e1f9facc502ab7509"
)
PARENT_MANIFEST_SHA256 = (
    "adf4fd71fd33604cee70fcba7aa90d2b7763715d3cba68a868d26347c599abfe"
)
EVIDENCE_PACK_SHA256 = (
    "50737e3c513b100f6d1907777f2da6fa71df485793fdaf1694ed3f61b55fe8bf"
)
PARENT_SOURCE_MARKER = (
    "CPL_SOURCE_MANIFEST: "
    "research/results/cross_pool_lead_lag/article_manifest.json"
)
HORIZONS_MS = (30_000, 60_000, 120_000, 180_000, 300_000, 600_000, 900_000)

FIGURE_HASHES = {
    "short_horizon_response.png": (
        "2ef9ec2b7da282dccc2bd0ac4dc6f706b393298389620deca3b2f9ef26d5f99a"
    ),
    "short_horizon_updates.png": (
        "7efeae4b654f55137de04186640b7d3b5674f0663fbd9d3205526bbc59172433"
    ),
    "short_horizon_fee_gap.png": (
        "ff2fe442578bd04bfbd6cb099cb2d01d19f6c118cba1e921c65e64be57b2e09c"
    ),
}

PARENT_FIGURE_HASHES = {
    "predictive_performance.png": (
        "a8badb8f809545597d9c2d712971bf2d8fa352cfc51205d21fa652ea936c812f"
    ),
    "dtw_lag.png": (
        "3c9d79a12983f4eee6e7c083cf2e76af5701dbf9c6e0025a9c7298ab9a0f81ac"
    ),
    "lp_performance.png": (
        "a18468cb4e4016dfbb72853d047a6e0e748bfcc1392e69ec86a2cd24354785ab"
    ),
}

SHORT_HORIZON_SOURCE_BLOCK = """CSH_EDITORIAL_STATUS: EVIDENCE_REVIEWED
CSH_RESEARCH_ROLE: post_hoc_exploratory
CSH_PARENT_DECISION: leadership_unresolved
CSH_PARENT_DECISION_UNCHANGED: true
CSH_SOURCE_MANIFEST: research/results/cross_pool_short_horizon_v1/short_horizon_manifest.json
CSH_MANIFEST_SHA256: 9145738bb6dd4aa84512b3f62625d779e6e4ef223f5b614e1f9facc502ab7509"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    assert isinstance(payload, dict)
    return payload


def test_reviewed_extension_identity_and_parent_boundary() -> None:
    manifest = _manifest(PUBLICATION_MANIFEST)

    assert _sha256(PUBLICATION_MANIFEST) == MANIFEST_SHA256
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["artifact_status"] == "reviewed"
    assert manifest["review"] == {
        "reviewed_at_utc": "2026-07-27T23:15:46Z",
        "reviewed_by": "sol_ultra",
        "status": "reviewed",
    }
    assert manifest["contract"]["research_role"] == "post_hoc_exploratory"
    assert tuple(manifest["contract"]["horizons_ms"]) == HORIZONS_MS
    assert manifest["contract"]["bootstrap"]["resamples"] == 2_000
    assert manifest["contract"]["bootstrap"]["unit"] == "shock_utc_day"
    assert manifest["contract"]["bootstrap"]["simultaneous_method"] == (
        "within_direction_max_z"
    )
    assert manifest["parent"]["manifest_sha256"] == PARENT_MANIFEST_SHA256
    assert manifest["parent"]["article_branch"] == "leadership_unresolved"
    assert manifest["parent"]["economic_class"] == "no_net_return_improvement"
    assert manifest["publication"]["parent_decision_unchanged"] is True


def test_reviewed_extension_passes_common_support_qa() -> None:
    manifest = _manifest(PUBLICATION_MANIFEST)

    assert manifest["qa"]["status"] == "pass"
    assert manifest["qa"]["parent_anchor_reconciled"] is True
    assert manifest["qa"]["reasons"] == []
    assert manifest["qa"]["counts"] == [
        {
            "detected_shock_count": 123,
            "direction": "bsc_to_base",
            "eligible_shock_count": 123,
            "exclusion_count": 0,
            "same_timestamp_shock_count": 0,
        },
        {
            "detected_shock_count": 197,
            "direction": "base_to_bsc",
            "eligible_shock_count": 197,
            "exclusion_count": 0,
            "same_timestamp_shock_count": 0,
        },
    ]


def test_publication_figures_are_hash_bound_by_the_reviewed_manifest() -> None:
    manifest = _manifest(PUBLICATION_MANIFEST)

    for filename, expected_hash in FIGURE_HASHES.items():
        publication_path = PUBLICATION_DIR / filename
        assert _sha256(publication_path) == expected_hash
        assert manifest["artifacts"][filename] == expected_hash


def test_article_files_preserve_parent_and_add_extension_source_contract() -> None:
    for path in (README, ARTICLE, EVIDENCE_PACK):
        text = path.read_text()
        assert text.count(PARENT_SOURCE_MARKER) == 1
        assert text.count(SHORT_HORIZON_SOURCE_BLOCK) == 1


def test_article_and_evidence_embed_every_reviewed_figure_once() -> None:
    for path in (ARTICLE, EVIDENCE_PACK):
        text = path.read_text()
        for filename in FIGURE_HASHES:
            link = f"../results/reports/cross_pool_short_horizon_v1/{filename}"
            assert text.count(f"]({link})") == 1


def test_publication_explains_response_update_and_fee_gap_estimands() -> None:
    for path in (ARTICLE, EVIDENCE_PACK):
        normalized = " ".join(path.read_text().split())
        for phrase in (
            "unconditional response",
            "every eligible shock",
            "valid zero response",
            "conditional response",
            "observed target update",
            "selected subset",
            "update incidence",
            "right-censored",
            "not a survival estimate",
            "simultaneous max-z",
            "all seven horizons",
            "point estimates only",
            "not net executable profit",
            "USDC/USDT parity",
            "gas, slippage, latency, inventory",
            "does not mean the gap closed toward zero",
        ):
            assert phrase in normalized


def test_evidence_pack_records_exact_reviewed_provenance_and_results() -> None:
    evidence = EVIDENCE_PACK.read_text()
    normalized = " ".join(evidence.split())

    for line in (
        "CSH_SCHEMA_VERSION: 1.0.0",
        "CSH_ARTIFACT_STATUS: reviewed",
        "CSH_QA_STATUS: pass",
        "CSH_REVIEWED_BY: sol_ultra",
        "CSH_REVIEWED_AT_UTC: 2026-07-27T23:15:46Z",
        "CSH_CODE_COMMIT: f4d43e773e4f24923226db86df9c64c9cd616cf3",
        f"CSH_PARENT_MANIFEST_SHA256: {PARENT_MANIFEST_SHA256}",
    ):
        assert evidence.count(line) == 1
    for filename, digest in FIGURE_HASHES.items():
        key = filename.removeprefix("short_horizon_").removesuffix(".png").upper()
        assert evidence.count(f"CSH_{key}_FIGURE_SHA256: {digest}") == 1
    for phrase in (
        "Girum de-clustered",
        "This reviewed extension",
        "does not reproduce Girum's directional magnitude ordering",
    ):
        assert phrase in normalized


def test_evidence_pack_pins_all_horizon_and_caption_values() -> None:
    evidence = EVIDENCE_PACK.read_text()
    normalized = " ".join(evidence.split())
    manifest = _manifest(PUBLICATION_MANIFEST)
    horizon_labels = {
        30_000: "30s",
        60_000: "1m",
        120_000: "2m",
        180_000: "3m",
        300_000: "5m",
        600_000: "10m",
        900_000: "15m",
    }
    direction_labels = {
        "bsc_to_base": "BSC to Base",
        "base_to_bsc": "Base to BSC",
    }

    primary_cohorts = [
        cohort
        for cohort in manifest["results"]["cohorts"]
        if cohort["cohort"] == "primary"
    ]
    for cohort in primary_cohorts:
        for summary in cohort["summaries"]:
            interval = summary["unconditional_mean_response_bps"]
            delay_seconds = summary["conditional_first_update_median_delay_ms"] / 1_000
            row = (
                f"| {direction_labels[cohort['direction']]} | "
                f"{horizon_labels[summary['horizon_ms']]} | "
                f"{interval['point']:+.6f} | "
                f"[{interval['lower']:+.6f}, {interval['upper']:+.6f}] | "
                f"{summary['update_count']}/{summary['eligible_event_count']} "
                f"({summary['update_incidence']:.1%}) | {delay_seconds:g}s |"
            )
            assert evidence.count(row) == 1

    for phrase in (
        "84/123 BSC-to-Base shocks and 134/197 Base-to-BSC shocks remain censored",
        "Conditional means are +2.382769 and -1.237692 bps",
        "unconditional means of +0.878230 and +0.039554 bps",
        "Median target-state ages are 23.5 and 35.6 minutes",
        "p95 ages of 11.6 and 33.7 hours",
        "[-2.308442, +4.064901] bps for BSC to Base",
        "[-2.627305, +2.706414] bps for Base to BSC",
        "[-6.662079, +11.427616] bps",
        "BSC-to-Base moves from -14.774555 to -19.003638 bps",
        "Base-to-BSC from -18.949015 to -20.881117 bps",
        "difference, +4.229084 and +1.932103 bps",
    ):
        assert phrase in normalized


def test_extension_claim_boundaries_remain_explicit() -> None:
    manifest = _manifest(PUBLICATION_MANIFEST)
    expected_forbidden = {
        "causal_price_discovery",
        "unique_directional_leader",
        "deployable_alpha",
        "toxic_flow_attribution",
        "external_lp_profitability",
        "net_executable_profit",
    }

    assert set(manifest["publication"]["forbidden_claims"]) == expected_forbidden
    for path in (ARTICLE, EVIDENCE_PACK):
        normalized = " ".join(path.read_text().split())
        assert "does not change the reviewed parent conclusion" in normalized
        assert "does not establish causal price discovery" in normalized
        assert "does not establish a unique directional leader" in normalized
        assert "does not establish deployable alpha" in normalized
        assert "does not establish net executable profit" in normalized


def test_cto_brief_is_concise_visual_and_evidence_bound() -> None:
    text = CTO_BRIEF.read_text()
    normalized = " ".join(text.split())

    assert len(text.split()) <= 4_300
    for filename in FIGURE_HASHES:
        link = f"../results/reports/cross_pool_short_horizon_v1/{filename}"
        assert text.count(f"]({link})") == 1
    for digest in FIGURE_HASHES.values():
        assert text.count(digest) == 1
    for filename, digest in PARENT_FIGURE_HASHES.items():
        figure_path = PARENT_PUBLICATION_DIR / filename
        link = f"../results/reports/cross_pool_lead_lag/{filename}"
        assert _sha256(figure_path) == digest
        assert text.count(f"]({link})") == 1
        assert text.count(digest) == 1
    for phrase in (
        "Decision headline",
        "Pre-specified result",
        "Post-hoc exploratory extension",
        "Weighted-portfolio result",
        "unconditional response",
        "every eligible shock",
        "valid zero response",
        "conditional response",
        "selected subset",
        "update incidence",
        "right-censored",
        "not a survival estimate",
        "point estimates only",
        "not net executable profit",
        "does not mean the gap closed toward zero",
        "Recommended decisions",
        "No deployment decision",
        "Fund measurement",
        "Keep portfolio research separate from live decisions",
        "Finish the article from the sealed evidence",
        "One basis point is 0.01%",
        "as-of price",
        "markout",
        "mean absolute error",
        "target-only MAE minus cross-pool MAE",
        "block bootstrap",
        "confidence interval",
        "dynamic time warping",
        "reset-capital window",
        "Probability of backtest overfitting",
        "Failed or unfinished hypotheses",
    ):
        assert phrase in normalized


def test_cto_brief_covers_every_finished_hypothesis() -> None:
    normalized = " ".join(CTO_BRIEF.read_text().split())

    for heading in (
        "Hypothesis 1: An independent fair-price label exists",
        "Hypothesis 2: Quidax top book predicts its own next move",
        "Hypothesis 3: DEX or blended anchors improve Quidax quoting",
        "Hypothesis 4: Quidax and the DEX pools broadly agree",
        "Hypothesis 5: Full-grid DEX LP selection produces a deployable policy",
        "Hypothesis 6: A directional LP policy transfers across pools",
        "Hypothesis 7: One pool improves forecasts of the other",
        "Hypothesis 8: Event responses and time alignment identify a leader",
        "Hypothesis 9: A cross-pool forecast gate improves LP returns",
        "Hypothesis 10: Short-horizon reactions reveal a leader or fee opportunity",
        "Hypothesis 11: Girum's magnitude pattern replicates",
        "Hypothesis 12: Joint portfolio allocation supports a performance claim",
    ):
        assert normalized.count(heading) == 1

    for phrase in (
        "255,846 Quidax rows and zero overlapping Binance observations",
        "2024-03-07T02:59:00+00:00",
        "99 complete quote states across 23 UTC days",
        "every midpoint confidence interval included zero",
        "+54.5 bps [37.0, 73.7]",
        "83 states across 17 UTC days",
        "714 matched Base rows",
        "2,934 matched BSC rows",
        "Base paper -0.460%",
        "Base EWMA -1.256%",
        "BSC paper -2.086%",
        "BSC EWMA -1.755%",
        "+1.039% across four active Base windows",
        "-1.272% across seven BSC windows",
        "Both reviewed evidence classes are `inconclusive`",
        "zero aggregate signed minutes in both directions",
        "band-unstable",
        "+0.515% for the original policy and +0.310% for the gated policy",
        "All unconditional simultaneous max-z bands include zero",
        "magnitude ordering reverses",
        "allocation-rule reset matrices were invalid and incomplete",
        "no weighted-portfolio performance result is reportable",
    ):
        assert phrase in normalized


def test_cto_brief_explains_lp_policy_mechanics_and_search() -> None:
    normalized = " ".join(CTO_BRIEF.read_text().split())

    for phrase in (
        "Liquidity provision in CLMMs: evidence from transactions data",
        "https://arxiv.org/abs/2604.22069",
        "not a replication of the paper's population study",
        "earns fees only while the current tick is inside its range",
        "2,640 EWMA combinations",
        "2,240 paper-style combinations",
        "40 reduced paper-style configurations",
        "five profiles crossed with three archetypes",
        "20, 50, or 100 swaps",
        "10, 25, or 50 swaps",
        "Base used 200 training swaps followed by 50 validation swaps",
        "BSC used 300 followed by 75",
        "net return - maximum drawdown",
        "top 100",
        "upside_tight_v1",
        "gate_strict_qts_20_25",
        "+0.125%",
        "0.25% below and 0.75% above",
        "4% of its range",
        "+0.5% fee return",
        "-0.25% stop",
        "four Base windows",
        "not a deployable winner",
    ):
        assert phrase in normalized


def test_cto_brief_lists_failed_or_unfinished_work_without_reopening_it() -> None:
    normalized = " ".join(CTO_BRIEF.read_text().split())

    for phrase in (
        "Promotion-grade Fair Price and executable CEX PnL",
        "Depth-walk, imbalance, OWA, microprice, and fill-probability tests",
        "LP capacity, dynamic sizing, and live promotion",
        "Portfolio-level allocation-rule PBO and performance",
        "Causal price discovery, toxic-flow attribution, and external-LP profitability",
        "require genuinely new data or a corrected analysis contract locked before results are inspected",
    ):
        assert phrase in normalized


def test_cto_brief_passes_no_ai_slop_word_and_pattern_gate() -> None:
    text = CTO_BRIEF.read_text().lower()

    for banned in (
        "delve",
        "leverage",
        "utilize",
        "facilitate",
        "robust",
        "game changer",
        "paradigm shift",
        "at the end of the day",
        "it's worth noting",
        "in conclusion",
        "overall,",
        "let's dive in",
        "stands as a testament",
    ):
        assert banned not in text


def test_cto_brief_records_exact_outcomes_and_source_hashes() -> None:
    text = CTO_BRIEF.read_text()
    normalized = " ".join(text.split())

    for phrase in (
        "BSC-to-Base MAE difference was -0.241675 bps (95% [-0.289590, -0.198156])",
        "Base-to-BSC was -0.587238 bps (95% [-0.734881, -0.466305])",
        "26 Base windows and 37 BSC windows",
        "4,895 canonical economic units",
        "candidate reset matrices were complete",
        "allocation-rule reset matrices were invalid and incomplete",
        "integrity passed for both packages, but the default claim gate failed for both",
        "no weighted-portfolio performance result is reportable",
        f"{MANIFEST_SHA256}",
        f"{PARENT_MANIFEST_SHA256}",
        f"{EVIDENCE_PACK_SHA256}",
        "6ce68fa8c0a05cb6339d223e9558aa9f5a425a5205de6d5b8bdddce252241ac4",
        "81c3f01c496f137dcaf1eb7c25b425b05eb5c93adc08fd20f5397cedcf893dc7",
    ):
        assert phrase in normalized

    manifest = _manifest(PUBLICATION_MANIFEST)
    primary = {
        cohort["direction"]: cohort
        for cohort in manifest["results"]["cohorts"]
        if cohort["cohort"] == "primary"
    }
    for direction in ("bsc_to_base", "base_to_bsc"):
        summaries = {row["horizon_ms"]: row for row in primary[direction]["summaries"]}
        for horizon_ms in (180_000, 900_000):
            interval = summaries[horizon_ms]["unconditional_mean_response_bps"]
            phrase = (
                f"{interval['point']:+.6f} bps "
                f"[{interval['lower']:+.6f}, {interval['upper']:+.6f}]"
            ).replace("-", "−")
            assert phrase in normalized

    bsc_to_base = primary["bsc_to_base"]["summaries"][-1]
    base_to_bsc = primary["base_to_bsc"]["summaries"][-1]
    for summary in (bsc_to_base, base_to_bsc):
        incidence = (
            f"{summary['update_count']}/{summary['eligible_event_count']} "
            f"({summary['update_incidence']:.1%})"
        )
        assert incidence in normalized
    assert (
        f"{bsc_to_base['conditional_first_update_mean_response_bps']:+.6f} bps and "
        f"{base_to_bsc['conditional_first_update_mean_response_bps']:+.6f} bps"
    ) in normalized
    assert (
        f"{bsc_to_base['conditional_first_update_median_delay_ms'] / 1_000:g} and "
        f"{base_to_bsc['conditional_first_update_median_delay_ms'] / 1_000:g} seconds"
    ) in normalized
    assert (
        f"{bsc_to_base['mean_fee_gap_start_bps']:.6f} to "
        f"{bsc_to_base['mean_fee_gap_end_bps']:.6f} bps"
    ) in normalized
    assert (
        f"{base_to_bsc['mean_fee_gap_start_bps']:.6f} to "
        f"{base_to_bsc['mean_fee_gap_end_bps']:.6f} bps"
    ) in normalized
    assert (
        f"{bsc_to_base['mean_fee_gap_closure_bps']:+.6f} and "
        f"{base_to_bsc['mean_fee_gap_closure_bps']:+.6f} bps"
    ) in normalized


def test_cto_brief_binds_economics_and_girum_to_the_sealed_evidence_pack() -> None:
    brief = " ".join(CTO_BRIEF.read_text().split())
    evidence = " ".join(EVIDENCE_PACK.read_text().split())

    assert _sha256(EVIDENCE_PACK) == EVIDENCE_PACK_SHA256
    for phrase in (
        "+0.515% for the original policy and +0.310% for the gated policy",
        "| Girum de-clustered | +3.4 bps | +0.4 bps |",
        "| This reviewed extension | +0.035202 bps | +0.204359 bps |",
    ):
        assert phrase in evidence
    for phrase in (
        "+0.515% for the original policy and +0.310% for the gated policy",
        "+3.4 bps Base to BSC and +0.4 bps BSC to Base",
        "+0.035202 and +0.204359 bps",
    ):
        assert phrase in brief


def test_cto_brief_preserves_prohibited_inferences_and_credentials_boundary() -> None:
    text = CTO_BRIEF.read_text()
    normalized = " ".join(text.split())

    assert (
        "The evidence does not support claims of causal price discovery, a unique "
        "directional leader, deployable alpha, toxic-flow attribution, external-LP "
        "profitability, net executable profit, or a definitive no-lead finding."
    ) in normalized
    for forbidden in ("ALCHEMY", "API_KEY", "PRIVATE_KEY", "SECRET_KEY"):
        assert forbidden not in text
