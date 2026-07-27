from __future__ import annotations

import csv
import io
from dataclasses import replace

import pytest

from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.short_horizon_manifest import DirectionQaCounts
from research.tests.short_horizon_reporting_fixtures import (
    small_inferences,
    small_studies,
)


def test_rendered_package_is_complete_deterministic_and_claim_safe() -> None:
    from research.cross_pool.short_horizon_reporting import (
        ShortHorizonEvidence,
        render_short_horizon_artifacts,
    )

    evidence = ShortHorizonEvidence(
        studies=small_studies(),
        inferences=small_inferences(),
        parent_anchor_reconciled=True,
    )
    first = render_short_horizon_artifacts(evidence)
    second = render_short_horizon_artifacts(evidence)

    assert first == second
    assert tuple(artifact.relative_name for artifact in first) == (
        "short_horizon_events.csv",
        "short_horizon_summaries.csv",
        "short_horizon_qa.json",
        "short_horizon_report.md",
        "short_horizon_response.png",
        "short_horizon_updates.png",
        "short_horizon_fee_gap.png",
    )
    summaries = next(
        artifact.content
        for artifact in first
        if artifact.relative_name == "short_horizon_summaries.csv"
    )
    rows = tuple(csv.DictReader(io.StringIO(summaries.decode("utf-8"), newline="")))
    assert len(rows) == 28
    assert tuple(int(row["horizon_ms"]) for row in rows[:7]) == (
        30_000,
        60_000,
        120_000,
        180_000,
        300_000,
        600_000,
        900_000,
    )
    report = next(
        artifact.content.decode("utf-8")
        for artifact in first
        if artifact.relative_name == "short_horizon_report.md"
    )
    assert "post-hoc exploratory" in report
    assert "pointwise" in report
    assert "simultaneous" in report
    assert "right-censored" in report
    assert "fee-only" in report
    assert "not net executable profit" in report
    assert "Girum" in report
    assert "does not reproduce Girum's directional magnitude ordering" in report
    assert "descriptive, not corroborating evidence of Base leadership" in report
    assert "Primary mean response" in report
    assert "Tie-excluded mean response" in report
    assert "Primary update incidence" in report
    assert "Tie-excluded update incidence" in report
    assert "10-minute de-clustering sensitivity" in report
    assert "point estimates; inference is reported in short_horizon_summaries.csv" in report
    assert "does not change the reviewed parent conclusion" in report
    tie_section = report.split("## Timestamp-tie sensitivity", 1)[1].split(
        "## Fee-only gap",
        1,
    )[0]
    assert tie_section.count("| BSC to Base |") == 7
    assert tie_section.count("| Base to BSC |") == 7
    for artifact in first[-3:]:
        assert artifact.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_event_csv_round_trips_losslessly_and_recomputes_inference() -> None:
    from research.cross_pool.short_horizon_reporting import (
        ShortHorizonEvidence,
        parse_short_horizon_events_csv,
        render_short_horizon_artifacts,
        validate_rendered_short_horizon_artifacts,
    )

    evidence = ShortHorizonEvidence(
        studies=small_studies(),
        inferences=small_inferences(),
        parent_anchor_reconciled=True,
    )
    artifacts = render_short_horizon_artifacts(evidence)
    event_bytes = artifacts[0].content

    assert parse_short_horizon_events_csv(event_bytes) == evidence.studies
    validate_rendered_short_horizon_artifacts(artifacts, evidence)

    tampered = list(artifacts)
    tampered[1] = replace(tampered[1], content=tampered[1].content.replace(b"0.0", b"0.1", 1))
    with pytest.raises(CrossPoolContractError, match="deterministic artifact"):
        validate_rendered_short_horizon_artifacts(tuple(tampered), evidence)


def test_event_parser_rejects_derived_field_tampering_and_noncanonical_rows() -> None:
    from research.cross_pool.short_horizon_reporting import (
        ShortHorizonEvidence,
        parse_short_horizon_events_csv,
        render_short_horizon_artifacts,
    )

    evidence = ShortHorizonEvidence(
        studies=small_studies(),
        inferences=small_inferences(),
        parent_anchor_reconciled=True,
    )
    events = render_short_horizon_artifacts(evidence)[0].content
    changed = events.replace(b",true,", b",false,", 1)
    with pytest.raises(CrossPoolContractError):
        parse_short_horizon_events_csv(changed)

    with pytest.raises(CrossPoolContractError, match="canonical"):
        parse_short_horizon_events_csv(events.replace(b"\n", b"\r\n"))


def test_evidence_rejects_wrong_profile_order_and_qa_count_projection() -> None:
    from research.cross_pool.short_horizon_reporting import ShortHorizonEvidence

    studies = small_studies()
    inferences = small_inferences()
    with pytest.raises(CrossPoolContractError, match="canonical order"):
        ShortHorizonEvidence(
            studies=tuple(reversed(studies)),
            inferences=inferences,
            parent_anchor_reconciled=True,
        )
    evidence = ShortHorizonEvidence(
        studies=studies,
        inferences=inferences,
        parent_anchor_reconciled=True,
    )
    assert evidence.qa_counts == (
        DirectionQaCounts(
            direction="bsc_to_base",
            detected_shock_count=1,
            eligible_shock_count=1,
            exclusion_count=0,
            same_timestamp_shock_count=0,
        ),
        DirectionQaCounts(
            direction="base_to_bsc",
            detected_shock_count=1,
            eligible_shock_count=1,
            exclusion_count=0,
            same_timestamp_shock_count=0,
        ),
    )
