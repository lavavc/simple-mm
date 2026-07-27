from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

PUBLICATION_DIR = Path("research/results/reports/cross_pool_short_horizon_v1")
PUBLICATION_MANIFEST = PUBLICATION_DIR / "short_horizon_manifest.json"

ARTICLE = Path("research/articles/02-backtesting-the-market-layer.md")
EVIDENCE_PACK = Path(
    "research/articles/evidence-pack-2026-07-cngn-market-making.md"
)
README = Path("research/articles/README.md")

MANIFEST_SHA256 = (
    "9145738bb6dd4aa84512b3f62625d779e6e4ef223f5b614e1f9facc502ab7509"
)
PARENT_MANIFEST_SHA256 = (
    "adf4fd71fd33604cee70fcba7aa90d2b7763715d3cba68a868d26347c599abfe"
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
