from __future__ import annotations

import math
from decimal import Decimal

import pytest

from research.cross_pool.contracts import (
    CrossPoolContractError,
    PanelConfig,
    PoolEvent,
    PoolName,
)
from research.cross_pool.panel import build_causal_panel


def test_panel_config_pins_known_first_sqrt_mid_blocks() -> None:
    config = PanelConfig(horizon_ms=3_600_000)

    assert config.base_transition_block == 45_848_255
    assert config.bsc_transition_block == 97_799_490


def test_panel_uses_causal_as_of_states_on_an_epoch_aligned_clock() -> None:
    base, bsc = _asynchronous_fixture()

    panel = build_causal_panel(
        base,
        bsc,
        PanelConfig(
            horizon_ms=1_000,
            base_transition_block=10,
            bsc_transition_block=20,
        ),
    )

    assert panel.common_interval_start_ms == 300
    assert panel.common_interval_end_ms == 7_000
    assert panel.horizon_ms == 1_000
    assert [row.timestamp_ms for row in panel.rows] == [2_000, 3_000, 4_000, 5_000, 6_000]
    assert all(row.timestamp_ms % row.horizon_ms == 0 for row in panel.rows)

    first = panel.rows[0]
    assert first.base_state_timestamp_ms == 1_500
    assert first.bsc_state_timestamp_ms == 1_900
    assert first.base_forward_state_timestamp_ms == 1_500
    assert first.bsc_forward_state_timestamp_ms == 1_900
    assert first.base_state_timestamp_ms <= first.timestamp_ms
    assert first.bsc_state_timestamp_ms <= first.timestamp_ms
    assert first.base_forward_state_timestamp_ms <= first.timestamp_ms + first.horizon_ms
    assert first.bsc_forward_state_timestamp_ms <= first.timestamp_ms + first.horizon_ms
    assert first.base_lag_block_number == 8
    assert first.bsc_lag_block_number == 18
    assert first.base_age_ms == 500
    assert first.bsc_age_ms == 100
    assert first.base_mid == 1.01
    assert first.bsc_mid == 0.99
    assert first.base_trailing_return_bps == pytest.approx(10_000 * math.log(1.01 / 1.00))
    assert first.bsc_trailing_return_bps == pytest.approx(10_000 * math.log(0.99 / 1.00))
    assert first.base_minus_bsc_gap_bps == pytest.approx(10_000 * math.log(1.01 / 0.99))
    assert first.base_forward_return_bps == 0.0
    assert first.bsc_forward_return_bps == 0.0


def test_panel_includes_events_exactly_on_lag_current_and_forward_boundaries() -> None:
    base = _boundary_fixture("uni-base", block_offset=0)
    bsc = _boundary_fixture("uni-bsc", block_offset=100)

    panel = build_causal_panel(
        base,
        bsc,
        PanelConfig(
            horizon_ms=1_000,
            base_transition_block=30,
            bsc_transition_block=130,
        ),
    )

    first = panel.rows[0]
    assert first.timestamp_ms == 2_000
    assert first.base_lag_block_number == 10
    assert first.base_state_block_number == 20
    assert first.base_forward_block_number == 30
    assert first.bsc_lag_block_number == 110
    assert first.bsc_state_block_number == 120
    assert first.bsc_forward_block_number == 130
    assert first.base_state_timestamp_ms == 2_000
    assert first.base_forward_state_timestamp_ms == 3_000
    assert first.base_regime == "mixed"
    assert first.bsc_regime == "mixed"


def test_event_inside_forward_window_changes_only_the_forward_label() -> None:
    base, bsc = _asynchronous_fixture()
    config = PanelConfig(
        horizon_ms=1_000,
        base_transition_block=10,
        bsc_transition_block=20,
    )
    before = build_causal_panel(base, bsc, config)
    inserted = base[:2] + (_event("uni-base", 2_500, 9, "1.03"),) + base[2:]

    after = build_causal_panel(inserted, bsc, config)

    before_row = before.rows[0]
    after_row = after.rows[0]
    assert after_row.base_state_timestamp_ms == before_row.base_state_timestamp_ms
    assert after_row.base_mid == before_row.base_mid
    assert after_row.base_trailing_return_bps == before_row.base_trailing_return_bps
    assert after_row.base_forward_state_timestamp_ms == 2_500
    assert after_row.base_forward_return_bps != before_row.base_forward_return_bps


def test_future_event_cannot_change_an_earlier_panel_row() -> None:
    base, bsc = _asynchronous_fixture()
    config = PanelConfig(
        horizon_ms=1_000,
        base_transition_block=10,
        bsc_transition_block=20,
    )
    before = build_causal_panel(base, bsc, config)
    future = _event("uni-base", 5_500, 12, "1.07")

    after = build_causal_panel(base[:-1] + (future, base[-1]), bsc, config)

    assert [
        row for row in before.rows if row.timestamp_ms + row.horizon_ms < future.timestamp_ms
    ] == [row for row in after.rows if row.timestamp_ms + row.horizon_ms < future.timestamp_ms]
    before_affected = next(row for row in before.rows if row.timestamp_ms == 5_000)
    after_affected = next(row for row in after.rows if row.timestamp_ms == 5_000)
    assert before_affected.base_forward_state_timestamp_ms == 4_500
    assert after_affected.base_forward_state_timestamp_ms == future.timestamp_ms
    assert after_affected.base_forward_return_bps != before_affected.base_forward_return_bps


def test_panel_keeps_long_flat_states_and_exposes_their_ages() -> None:
    base, bsc = _asynchronous_fixture()

    panel = build_causal_panel(
        base,
        bsc,
        PanelConfig(
            horizon_ms=1_000,
            base_transition_block=10,
            bsc_transition_block=20,
        ),
    )

    last = panel.rows[-1]
    assert last.timestamp_ms == 6_000
    assert last.base_state_timestamp_ms == 4_500
    assert last.bsc_state_timestamp_ms == 4_900
    assert last.base_age_ms == 1_500
    assert last.bsc_age_ms == 1_100
    assert last.base_trailing_return_bps == 0.0
    assert last.base_forward_return_bps == 0.0


def test_panel_classifies_early_mixed_and_late_regimes_across_all_states() -> None:
    base, bsc = _asynchronous_fixture()

    panel = build_causal_panel(
        base,
        bsc,
        PanelConfig(
            horizon_ms=1_000,
            base_transition_block=10,
            bsc_transition_block=20,
        ),
    )

    regimes = [(row.base_regime, row.bsc_regime) for row in panel.rows]
    assert regimes[0] == ("early", "early")
    assert regimes[1] == ("mixed", "mixed")
    assert regimes[3] == ("late", "late")


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("empty_base", "at least one uni-base event"),
        ("wrong_pool", "uni-base stream contains"),
        ("non_monotonic", "uni-base timestamps must be strictly increasing"),
        ("zero_horizon", "horizon_ms must be positive"),
        ("zero_boundary", "transition blocks must be positive"),
        ("insufficient_overlap", "no complete causal panel row"),
    ],
)
def test_panel_fails_closed_on_invalid_inputs(mutation: str, match: str) -> None:
    base, bsc = _asynchronous_fixture()
    config = PanelConfig(
        horizon_ms=1_000,
        base_transition_block=10,
        bsc_transition_block=20,
    )
    if mutation == "empty_base":
        base = ()
    elif mutation == "wrong_pool":
        base = (_event("uni-bsc", 100, 8, "1.00"),) + base[1:]
    elif mutation == "non_monotonic":
        base = (base[1], base[0]) + base[2:]
    elif mutation == "zero_horizon":
        config = PanelConfig(
            horizon_ms=0,
            base_transition_block=10,
            bsc_transition_block=20,
        )
    elif mutation == "zero_boundary":
        config = PanelConfig(
            horizon_ms=1_000,
            base_transition_block=0,
            bsc_transition_block=20,
        )
    elif mutation == "insufficient_overlap":
        base = (
            _event("uni-base", 1_000, 8, "1.00"),
            _event("uni-base", 2_500, 9, "1.01"),
        )
        bsc = (
            _event("uni-bsc", 1_100, 18, "1.00"),
            _event("uni-bsc", 2_400, 19, "1.01"),
        )
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(CrossPoolContractError, match=match):
        build_causal_panel(base, bsc, config)


def _asynchronous_fixture() -> tuple[tuple[PoolEvent, ...], tuple[PoolEvent, ...]]:
    base = (
        _event("uni-base", 100, 8, "1.00"),
        _event("uni-base", 1_500, 9, "1.01"),
        _event("uni-base", 3_500, 10, "1.02"),
        _event("uni-base", 4_500, 11, "1.04"),
        _event("uni-base", 7_600, 13, "1.05"),
    )
    bsc = (
        _event("uni-bsc", 300, 18, "1.00"),
        _event("uni-bsc", 1_900, 19, "0.99"),
        _event("uni-bsc", 3_100, 20, "1.01"),
        _event("uni-bsc", 4_900, 21, "1.02"),
        _event("uni-bsc", 7_000, 22, "1.03"),
    )
    return base, bsc


def _boundary_fixture(pool: PoolName, *, block_offset: int) -> tuple[PoolEvent, ...]:
    return tuple(
        _event(pool, timestamp_ms, block_offset + block_number, str(block_number))
        for timestamp_ms, block_number in (
            (1_000, 10),
            (2_000, 20),
            (3_000, 30),
            (4_000, 40),
            (5_000, 50),
        )
    )


def _event(
    pool: PoolName,
    timestamp_ms: int,
    block_number: int,
    price: str,
) -> PoolEvent:
    mid = Decimal(price)
    return PoolEvent(
        pool=pool,
        timestamp_ms=timestamp_ms,
        block_number=block_number,
        tx_hash=f"0x{pool}-{block_number}",
        log_index=0,
        raw_mid=mid,
        fee_adjusted_bid=mid * Decimal("0.999"),
        fee_adjusted_ask=mid * Decimal("1.001"),
        stored_cngn_usd_price=mid,
        stored_price_model="sqrt_mid",
    )
