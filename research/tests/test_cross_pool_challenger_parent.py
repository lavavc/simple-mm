from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from research.cross_pool.challenger_parent import (
    PARENT_ARTIFACT_SHA256,
    _parse_panel_bytes,
    load_challenger_parent,
    project_parent_row,
)
from research.cross_pool.contracts import CrossPoolContractError

PARENT_DIR = Path("research/results/cross_pool_lead_lag")


def test_loads_exact_reviewed_parent_and_shared_oos_calendar() -> None:
    parent = load_challenger_parent(PARENT_DIR)

    assert parent.anchor.artifact_sha256 == tuple(PARENT_ARTIFACT_SHA256.items())
    assert tuple(panel.horizon_ms for panel in parent.panels) == (
        900_000,
        3_600_000,
        14_400_000,
    )
    assert tuple(len(panel.rows) for panel in parent.panels) == (10_193, 2_547, 635)
    assert len(parent.predictions) == 22_148
    assert len(parent.target_days_utc) == 88
    assert parent.target_days_utc == tuple(sorted(parent.target_days_utc))
    assert len(set(parent.target_days_utc)) == 88


def test_every_prediction_joins_one_panel_row_and_observable_fold() -> None:
    parent = load_challenger_parent(PARENT_DIR)
    panels = {
        (row.timestamp_ms, row.horizon_ms): row
        for panel in parent.panels
        for row in panel.rows
    }

    for prediction in parent.predictions:
        panel_row = panels[(prediction.timestamp_ms, prediction.horizon_ms)]
        assert prediction.target_timestamp_ms == prediction.timestamp_ms + prediction.horizon_ms
        assert prediction.actual_bps == (
            panel_row.base_forward_return_bps
            if prediction.direction == "bsc_to_base"
            else panel_row.bsc_forward_return_bps
        )
        assert prediction.refit_timestamp_ms <= prediction.timestamp_ms


def test_projection_uses_state_timestamp_for_update_not_nonzero_return() -> None:
    parent = load_challenger_parent(PARENT_DIR)
    row = parent.panels[1].rows[100]
    price_neutral_update = replace(
        row,
        base_forward_state_timestamp_ms=row.base_state_timestamp_ms + 1,
        base_forward_return_bps=0.0,
    )

    projected = project_parent_row(price_neutral_update, "bsc_to_base", predecessor=None)

    assert projected.actual_bps == 0.0
    assert projected.updated is True


def test_projection_normalizes_reverse_gap_age_regime_and_target() -> None:
    parent = load_challenger_parent(PARENT_DIR)
    row = parent.panels[1].rows[100]
    predecessor = parent.panels[1].rows[99]

    projected = project_parent_row(row, "base_to_bsc", predecessor=predecessor)

    assert projected.target_return_bps == row.bsc_trailing_return_bps
    assert projected.previous_target_return_bps == predecessor.bsc_trailing_return_bps
    assert projected.source_return_bps == row.base_trailing_return_bps
    assert projected.gap_bps == -row.base_minus_bsc_gap_bps
    assert projected.target_age_ms == row.bsc_age_ms
    assert projected.source_age_ms == row.base_age_ms
    assert projected.target_regime == row.bsc_regime
    assert projected.source_regime == row.base_regime
    assert projected.actual_bps == row.bsc_forward_return_bps


def test_panel_parser_rejects_noncanonical_newlines() -> None:
    raw = (PARENT_DIR / "panel_1h.csv").read_bytes().replace(b"\n", b"\r\n")

    with pytest.raises(CrossPoolContractError, match="newlines"):
        _parse_panel_bytes(raw, expected_horizon_ms=3_600_000)


def test_panel_parser_rejects_duplicate_timestamp() -> None:
    raw = (PARENT_DIR / "panel_4h.csv").read_bytes()
    header, first, *rest = raw.splitlines(keepends=True)
    duplicated = b"".join((header, first, first, *rest))

    with pytest.raises(CrossPoolContractError, match="canonical order"):
        _parse_panel_bytes(duplicated, expected_horizon_ms=14_400_000)
