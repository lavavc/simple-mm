from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from research.cross_pool.challenger_inference import infer_challengers
from research.cross_pool.challenger_parent import load_challenger_parent
from research.cross_pool.challenger_reporting import (
    CHALLENGER_ARTIFACT_NAMES,
    _freshness_figure,
    render_challenger_artifacts,
    validate_rendered_challenger_artifacts,
)
from research.cross_pool.challengers import run_challengers

PARENT_DIR = Path("research/results/cross_pool_lead_lag")


@pytest.fixture(scope="module")
def rendered():
    study = run_challengers(load_challenger_parent(PARENT_DIR))
    inference = infer_challengers(study)
    artifacts = render_challenger_artifacts(study, inference)
    return study, inference, artifacts


def test_artifact_set_headers_and_semantics_are_exact(rendered) -> None:
    study, inference, artifacts = rendered
    by_name = {artifact.relative_name: artifact for artifact in artifacts}

    assert tuple(by_name) == CHALLENGER_ARTIFACT_NAMES
    assert by_name["challenger_predictions.csv"].content.startswith(
        b"timestamp_ms,target_timestamp_ms,horizon_ms,refit_timestamp_ms,fold_index,"
    )
    assert by_name["challenger_fold_audits.csv"].content.startswith(
        b"family,variant,direction,horizon_ms,fold_index,"
    )
    assert by_name["challenger_metrics.csv"].content.startswith(
        b"family,variant,direction,horizon_ms,support,status,"
    )
    assert by_name["challenger_contrasts.csv"].content.startswith(
        b"family,direction,horizon_ms,support,endpoint,contrast,"
    )
    assert all(
        artifact.content.endswith(b"\n")
        for artifact in artifacts
        if artifact.relative_name.endswith((".csv", ".json", ".md"))
    )
    validate_rendered_challenger_artifacts(artifacts, study, inference)


def test_report_explains_models_failures_inference_and_claim_boundary(rendered) -> None:
    _, _, artifacts = rendered
    report = next(
        artifact.content.decode("utf-8")
        for artifact in artifacts
        if artifact.relative_name == "challenger_report.md"
    )

    for phrase in (
        "ARX(2)",
        "restricted additive",
        "Huber",
        "two-part",
        "Update incidence",
        "seven-day circular block bootstrap",
        "not adjudicable",
        "leadership_unresolved",
        "fresh-data replication",
    ):
        assert phrase in report


def test_pngs_are_nonempty_deterministic_and_have_distinct_subjects(rendered) -> None:
    study, inference, artifacts = rendered
    by_name = {artifact.relative_name: artifact for artifact in artifacts}
    png_names = tuple(name for name in CHALLENGER_ARTIFACT_NAMES if name.endswith(".png"))

    assert len(png_names) == 5
    assert all(by_name[name].content.startswith(b"\x89PNG\r\n\x1a\n") for name in png_names)
    assert all(len(by_name[name].content) > 10_000 for name in png_names)
    assert len({by_name[name].sha256 for name in png_names}) == 5

    repeated = {
        artifact.relative_name: artifact.sha256
        for artifact in render_challenger_artifacts(study, inference)
    }
    assert repeated == {name: artifact.sha256 for name, artifact in by_name.items()}


def test_render_validation_rejects_same_length_semantic_table_tampering(rendered) -> None:
    study, inference, artifacts = rendered
    tampered = tuple(
        replace(
            artifact,
            content=artifact.content.replace(b",ols,", b",gam,", 1),
        )
        if artifact.relative_name == "challenger_predictions.csv"
        else artifact
        for artifact in artifacts
    )

    with pytest.raises(ValueError, match="challenger prediction"):
        validate_rendered_challenger_artifacts(tampered, study, inference)


def test_freshness_figure_preserves_missing_support_positions(
    rendered,
    monkeypatch,
) -> None:
    class FakeAxis:
        def __init__(self) -> None:
            self.x_values: list[list[int]] = []

        def plot(self, x, _y, **_kwargs) -> None:
            self.x_values.append(list(x))

        def axhline(self, *_args, **_kwargs) -> None: pass
        def set_xticks(self, *_args, **_kwargs) -> None: pass
        def set_ylabel(self, *_args, **_kwargs) -> None: pass
        def set_title(self, *_args, **_kwargs) -> None: pass
        def grid(self, *_args, **_kwargs) -> None: pass
        def legend(self, *_args, **_kwargs) -> None: pass

    class FakeFigure:
        def tight_layout(self) -> None: pass

    _, inference, _ = rendered
    source = next(
        row
        for row in inference.contrasts
        if row.family == "ols"
        and row.direction == "bsc_to_base"
        and row.horizon_ms == 3_600_000
        and row.endpoint == "mae"
        and row.contrast == "source_price"
        and row.support == "all"
    )
    contrasts = (
        source,
        replace(source, support="both_age_le_1h"),
    )
    axis = FakeAxis()
    monkeypatch.setattr(
        "research.cross_pool.challenger_reporting.plt.subplots",
        lambda **_kwargs: (FakeFigure(), axis),
    )
    monkeypatch.setattr(
        "research.cross_pool.challenger_reporting._png_bytes",
        lambda _figure: b"png",
    )

    assert _freshness_figure(contrasts) == b"png"
    assert axis.x_values == [[0, 2]]
