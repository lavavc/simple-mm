from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.cross_pool.contracts import CrossPoolContractError
from research.cross_pool.short_horizon_manifest import (
    BASE_FEATURES_SHA256,
    BSC_FEATURES_SHA256,
    PARENT_MANIFEST_SHA256,
)
from research.cross_pool.short_horizon_parent import (
    PARENT_15M_PROJECTION_SHA256,
    PARENT_EVENT_STUDY_SHA256,
    validate_frozen_parent_anchor,
)
from research.tests.short_horizon_reporting_fixtures import small_studies


def test_fixed_parent_anchor_fixture_pins_every_external_identity() -> None:
    path = Path(__file__).with_name("fixtures") / "cross_pool_short_horizon_parent_anchor.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["manifest_sha256"] == PARENT_MANIFEST_SHA256
    assert payload["event_study_sha256"] == PARENT_EVENT_STUDY_SHA256
    assert payload["projection_15m_sha256"] == PARENT_15M_PROJECTION_SHA256
    assert payload["base_features_sha256"] == BASE_FEATURES_SHA256
    assert payload["bsc_features_sha256"] == BSC_FEATURES_SHA256
    assert payload["summaries"]["bsc_to_base"] == {
        "agreement_share": 0.1951219512195122,
        "event_count": 123,
        "mean_response_bps": 0.8782298255072702,
        "median_response_bps": 0.0,
        "shock_day_count": 59,
    }
    assert payload["summaries"]["base_to_bsc"] == {
        "agreement_share": 0.24873096446700507,
        "event_count": 197,
        "mean_response_bps": 0.03955408492685122,
        "median_response_bps": 0.0,
        "shock_day_count": 70,
    }


def test_parent_anchor_rejects_any_nonfrozen_manifest_before_row_comparison() -> None:
    with pytest.raises(CrossPoolContractError, match="frozen identity"):
        validate_frozen_parent_anchor(
            b"{}\n",
            b"not parent events\n",
            small_studies(),
        )
