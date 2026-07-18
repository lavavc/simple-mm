from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

from research.cross_pool.contracts import (
    CrossPoolContractError,
    DtwConfig,
    DtwNullResult,
    DtwPath,
    DtwStability,
    DtwWeekResult,
    PanelRow,
)
from research.cross_pool.dtw import (
    assess_dtw_stability,
    banded_dtw,
    build_day_rotation_nulls,
    evaluate_weekly_dtw,
)

GRID_MS = 900_000
DAY_STEPS = 96
WEEK_STEPS = 672
WEEK_MS = WEEK_STEPS * GRID_MS
MONDAY_START_MS = 1_767_571_200_000


def test_known_shifted_innovations_recover_a_positive_target_lag() -> None:
    source = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0)
    target = (0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0)

    path = banded_dtw(source, target, band_steps=1)

    assert path.matches == (
        (0, 0),
        (0, 1),
        *((index, index + 1) for index in range(1, 11)),
        (11, 11),
    )
    assert path.total_cost == 0.0
    assert path.normalized_cost == 0.0
    assert path.median_signed_lag_steps == 1.0


def test_zero_cost_ties_choose_diagonal_predecessors_first() -> None:
    path = banded_dtw((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), band_steps=1)

    assert path.matches == ((0, 0), (1, 1), (2, 2))
    assert path.total_cost == 0.0
    assert path.normalized_cost == 0.0
    assert path.median_signed_lag_steps == 0.0


def test_equal_nondiagonal_costs_choose_source_advance_before_target_advance() -> None:
    path = banded_dtw((0.0, 1.0, 0.0), (1.0, 0.0, 1.0), band_steps=1)

    assert path.matches == ((0, 0), (0, 1), (1, 2), (2, 2))
    assert path.total_cost == 2.0
    assert path.normalized_cost == 0.5
    assert path.median_signed_lag_steps == 0.5


def test_path_cost_is_normalized_by_stored_match_count() -> None:
    path = banded_dtw((0.0, 1.0), (0.0, 2.0), band_steps=0)

    assert path.matches == ((0, 0), (1, 1))
    assert path.total_cost == 1.0
    assert path.normalized_cost == 0.5
    assert path.median_signed_lag_steps == 0.0


def test_banded_dtw_never_escapes_requested_band_or_legal_steps() -> None:
    path = banded_dtw(
        (0.0, 1.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0, 0.0, 1.0),
        band_steps=1,
    )

    assert path.matches[0] == (0, 0)
    assert path.matches[-1] == (4, 4)
    assert all(abs(source_i - target_i) <= 1 for source_i, target_i in path.matches)
    assert all(
        (next_source - source_i, next_target - target_i)
        in {(1, 1), (1, 0), (0, 1)}
        for (source_i, target_i), (next_source, next_target) in zip(
            path.matches,
            path.matches[1:],
            strict=False,
        )
    )


def test_dtw_config_is_the_frozen_analysis_configuration() -> None:
    assert DtwConfig() == DtwConfig(
        grid_ms=900_000,
        band_steps=(1, 4, 16),
        primary_band_steps=4,
    )


@pytest.mark.parametrize(
    "mutation",
    ("grid", "bands", "band_order", "primary", "bool_grid"),
)
def test_dtw_config_rejects_analysis_tuning(mutation: str) -> None:
    kwargs: dict[str, object] = {}
    if mutation == "grid":
        kwargs["grid_ms"] = 60_000
    elif mutation == "bands":
        kwargs["band_steps"] = (1, 4)
    elif mutation == "band_order":
        kwargs["band_steps"] = (4, 1, 16)
    elif mutation == "primary":
        kwargs["primary_band_steps"] = 1
    else:
        kwargs["grid_ms"] = True

    with pytest.raises(CrossPoolContractError):
        DtwConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("source", "target", "band_steps", "match"),
    (
        ((), (0.0,), 0, "nonempty"),
        ((0.0,), (), 0, "nonempty"),
        ((0.0, float("nan")), (0.0, 1.0), 1, "finite"),
        ((0.0,), (0.0,), -1, "nonnegative integer"),
        ((0.0,), (0.0,), True, "nonnegative integer"),
        ((0.0, 1.0, 2.0), (0.0, 1.0, 2.0, 3.0, 4.0), 1, "unreachable"),
    ),
)
def test_banded_dtw_fails_closed_on_invalid_inputs(
    source: tuple[float, ...],
    target: tuple[float, ...],
    band_steps: int,
    match: str,
) -> None:
    with pytest.raises(CrossPoolContractError, match=match):
        banded_dtw(source, target, band_steps=band_steps)


def test_dtw_path_contract_rejects_stale_derived_fields() -> None:
    path = banded_dtw((0.0, 1.0), (0.0, 2.0), band_steps=0)

    with pytest.raises(CrossPoolContractError, match="normalized"):
        replace(path, normalized_cost=0.25)
    with pytest.raises(CrossPoolContractError, match="median"):
        replace(path, median_signed_lag_steps=1.0)
    with pytest.raises(CrossPoolContractError, match="terminal"):
        replace(path, target_length=3)
    with pytest.raises(CrossPoolContractError, match="requested band"):
        replace(path, band_steps=0, matches=((0, 0), (0, 1), (1, 1)))


def test_dtw_path_contract_rejects_illegal_steps() -> None:
    with pytest.raises(CrossPoolContractError, match="legal steps"):
        DtwPath(
            source_length=3,
            target_length=3,
            band_steps=2,
            matches=((0, 0), (2, 2)),
            total_cost=0.0,
            normalized_cost=0.0,
            median_signed_lag_steps=0.0,
        )


def test_complete_week_recovers_direction_relative_lead_for_every_band() -> None:
    panel = _complete_lead_week(MONDAY_START_MS)

    primary = evaluate_weekly_dtw(
        panel,
        DtwConfig(),
        direction="bsc_to_base",
    )
    reverse = evaluate_weekly_dtw(
        panel,
        DtwConfig(),
        direction="base_to_bsc",
    )

    assert [row.band_steps for row in primary] == [1, 4, 16]
    assert [row.band_steps for row in reverse] == [1, 4, 16]
    for row in primary:
        assert row.week_start_timestamp_ms == MONDAY_START_MS
        assert row.direction == "bsc_to_base"
        assert row.path_length == WEEK_STEPS + 1
        assert row.total_cost == 0.0
        assert row.normalized_cost == 0.0
        assert row.median_signed_lag_steps == 1.0
        assert row.matches[0] == (0, 0)
        assert row.matches[-1] == (WEEK_STEPS - 1, WEEK_STEPS - 1)
        assert all(
            abs(source_i - target_i) <= row.band_steps
            for source_i, target_i in row.matches
        )
    for row in reverse:
        assert row.direction == "base_to_bsc"
        assert row.total_cost == 0.0
        assert row.median_signed_lag_steps == -1.0


def test_weekly_results_are_ordered_by_week_then_frozen_band() -> None:
    panel = _complete_lead_week(MONDAY_START_MS) + _complete_lead_week(
        MONDAY_START_MS + WEEK_MS
    )

    rows = evaluate_weekly_dtw(
        panel,
        DtwConfig(),
        direction="bsc_to_base",
    )

    assert [
        (row.week_start_timestamp_ms, row.band_steps) for row in rows
    ] == [
        (MONDAY_START_MS, 1),
        (MONDAY_START_MS, 4),
        (MONDAY_START_MS, 16),
        (MONDAY_START_MS + WEEK_MS, 1),
        (MONDAY_START_MS + WEEK_MS, 4),
        (MONDAY_START_MS + WEEK_MS, 16),
    ]


def test_only_contiguous_boundary_partial_weeks_are_excluded() -> None:
    leading = tuple(
        _panel_row(
            MONDAY_START_MS - (4 - index) * GRID_MS,
            base_return=float(index % 2),
            bsc_return=float((index + 1) % 2),
        )
        for index in range(4)
    )
    complete = _complete_lead_week(MONDAY_START_MS)
    trailing = tuple(
        _panel_row(
            MONDAY_START_MS + WEEK_MS + index * GRID_MS,
            base_return=float(index % 2),
            bsc_return=float((index + 1) % 2),
        )
        for index in range(4)
    )

    rows = evaluate_weekly_dtw(
        leading + complete + trailing,
        DtwConfig(),
        direction="bsc_to_base",
    )

    assert len(rows) == 3
    assert {row.week_start_timestamp_ms for row in rows} == {MONDAY_START_MS}
    assert (
        evaluate_weekly_dtw(
            complete[:-1],
            DtwConfig(),
            direction="bsc_to_base",
        )
        == ()
    )


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("gap", "contiguous"),
        ("duplicate", "strictly increasing"),
        ("horizon", "15-minute horizon"),
        ("alignment", "epoch-aligned"),
        ("nonfinite", "finite trailing innovations"),
        ("future_state", "causal state timestamps"),
        ("age", "state ages"),
        ("zero_variance", "zero variance"),
    ),
)
def test_weekly_evaluation_fails_closed_on_invalid_panel_rows(
    mutation: str,
    match: str,
) -> None:
    panel = list(_complete_lead_week(MONDAY_START_MS))
    if mutation == "gap":
        del panel[100]
    elif mutation == "duplicate":
        panel[100] = panel[99]
    elif mutation == "horizon":
        panel[100] = replace(panel[100], horizon_ms=3_600_000)
    elif mutation == "alignment":
        panel[100] = replace(panel[100], timestamp_ms=panel[100].timestamp_ms + 1)
    elif mutation == "nonfinite":
        panel[100] = replace(panel[100], base_trailing_return_bps=float("nan"))
    elif mutation == "future_state":
        panel[100] = replace(
            panel[100],
            base_state_timestamp_ms=panel[100].timestamp_ms + 1,
        )
    elif mutation == "age":
        panel[100] = replace(panel[100], bsc_age_ms=1)
    elif mutation == "zero_variance":
        panel = [replace(row, base_trailing_return_bps=0.0) for row in panel]
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(CrossPoolContractError, match=match):
        evaluate_weekly_dtw(
            tuple(panel),
            DtwConfig(),
            direction="bsc_to_base",
        )


def test_week_result_contract_rejects_stale_path_fields() -> None:
    row = evaluate_weekly_dtw(
        _complete_lead_week(MONDAY_START_MS),
        DtwConfig(),
        direction="bsc_to_base",
    )[0]

    with pytest.raises(CrossPoolContractError, match="path_length"):
        replace(row, path_length=row.path_length - 1)
    with pytest.raises(CrossPoolContractError, match="normalized"):
        replace(row, normalized_cost=1.0)
    with pytest.raises(CrossPoolContractError, match="median"):
        replace(row, median_signed_lag_steps=0.0)
    with pytest.raises(CrossPoolContractError, match="Monday"):
        replace(row, week_start_timestamp_ms=row.week_start_timestamp_ms + DAY_STEPS * GRID_MS)


def test_week_result_rejects_noncanonical_direction_and_band() -> None:
    row = evaluate_weekly_dtw(
        _complete_lead_week(MONDAY_START_MS),
        DtwConfig(),
        direction="bsc_to_base",
    )[0]

    with pytest.raises(CrossPoolContractError, match="direction"):
        DtwWeekResult(
            week_start_timestamp_ms=row.week_start_timestamp_ms,
            direction="invalid",  # type: ignore[arg-type]
            band_steps=row.band_steps,
            path_length=row.path_length,
            total_cost=row.total_cost,
            normalized_cost=row.normalized_cost,
            median_signed_lag_steps=row.median_signed_lag_steps,
            matches=row.matches,
        )
    with pytest.raises(CrossPoolContractError, match="frozen band"):
        replace(row, band_steps=2)


def test_weekly_nulls_use_every_nonzero_target_day_rotation() -> None:
    rows = build_day_rotation_nulls(
        _rotation_week(MONDAY_START_MS),
        DtwConfig(),
        direction="bsc_to_base",
    )

    assert len(rows) == 18
    for band_steps in (1, 4, 16):
        assert {
            row.rotation_days for row in rows if row.band_steps == band_steps
        } == {1, 2, 3, 4, 5, 6}
    assert [
        (row.band_steps, row.rotation_days) for row in rows
    ] == [
        (band_steps, rotation_days)
        for band_steps in (1, 4, 16)
        for rotation_days in range(1, 7)
    ]


def test_day_rotation_nulls_pin_left_rotated_target_costs_and_lags() -> None:
    rows = build_day_rotation_nulls(
        _rotation_week(MONDAY_START_MS),
        DtwConfig(),
        direction="bsc_to_base",
    )
    primary = tuple(row for row in rows if row.band_steps == 4)

    assert [row.rotation_days for row in primary] == [1, 2, 3, 4, 5, 6]
    assert [row.path_length for row in primary] == [676] * 6
    assert [row.total_cost for row in primary] == pytest.approx(
        [1003.0, 1669.0, 2005.0, 2005.0, 1669.0, 1003.0]
    )
    assert [row.normalized_cost for row in primary] == pytest.approx(
        [
            1003.0 / 676.0,
            1669.0 / 676.0,
            2005.0 / 676.0,
            2005.0 / 676.0,
            1669.0 / 676.0,
            1003.0 / 676.0,
        ]
    )
    assert [row.observed_normalized_cost for row in primary] == [0.0] * 6
    assert [row.observed_cost_improvement for row in primary] == pytest.approx(
        [row.normalized_cost for row in primary]
    )
    assert [row.median_signed_lag_steps for row in primary] == [
        -4.0,
        -4.0,
        -4.0,
        4.0,
        4.0,
        4.0,
    ]
    assert [row.observed_median_signed_lag_steps for row in primary] == [0.0] * 6
    assert [row.observed_signed_lag_difference_steps for row in primary] == [
        -4.0,
        -4.0,
        -4.0,
        4.0,
        4.0,
        4.0,
    ]


@pytest.mark.parametrize(
    ("band_steps", "path_length", "total_costs", "signed_lags"),
    (
        (
            1,
            673,
            (1006.75, 1677.25, 2013.25, 2013.25, 1677.25, 1006.75),
            (-1.0, -1.0, -1.0, 1.0, 1.0, 1.0),
        ),
        (
            16,
            688,
            (988.0, 1636.0, 1972.0, 1972.0, 1636.0, 988.0),
            (-16.0, -16.0, -16.0, 16.0, 16.0, 16.0),
        ),
    ),
)
def test_day_rotation_nulls_pin_sensitivity_band_outputs(
    band_steps: int,
    path_length: int,
    total_costs: tuple[float, ...],
    signed_lags: tuple[float, ...],
) -> None:
    rows = build_day_rotation_nulls(
        _rotation_week(MONDAY_START_MS),
        DtwConfig(),
        direction="bsc_to_base",
    )
    sensitivity = tuple(row for row in rows if row.band_steps == band_steps)

    assert [row.rotation_days for row in sensitivity] == [1, 2, 3, 4, 5, 6]
    assert [row.path_length for row in sensitivity] == [path_length] * 6
    assert [row.total_cost for row in sensitivity] == pytest.approx(total_costs)
    assert [row.normalized_cost for row in sensitivity] == pytest.approx(
        [total_cost / path_length for total_cost in total_costs]
    )
    assert [row.median_signed_lag_steps for row in sensitivity] == list(
        signed_lags
    )


def test_rotation_nulls_are_direction_relative_and_exclude_partial_weeks() -> None:
    complete = _rotation_week(MONDAY_START_MS)
    reverse = build_day_rotation_nulls(
        complete,
        DtwConfig(),
        direction="base_to_bsc",
    )

    assert len(reverse) == 18
    assert all(row.direction == "base_to_bsc" for row in reverse)
    assert (
        build_day_rotation_nulls(
            complete[:-1],
            DtwConfig(),
            direction="bsc_to_base",
        )
        == ()
    )


def test_null_result_contract_rejects_stale_derived_fields() -> None:
    row = build_day_rotation_nulls(
        _rotation_week(MONDAY_START_MS),
        DtwConfig(),
        direction="bsc_to_base",
    )[0]

    with pytest.raises(CrossPoolContractError, match="path_length"):
        replace(row, path_length=0)
    with pytest.raises(CrossPoolContractError, match="normalized"):
        replace(row, normalized_cost=row.normalized_cost + 1.0)
    with pytest.raises(CrossPoolContractError, match="cost improvement"):
        replace(row, observed_cost_improvement=0.0)
    with pytest.raises(CrossPoolContractError, match="lag difference"):
        replace(row, observed_signed_lag_difference_steps=0.0)
    with pytest.raises(CrossPoolContractError, match="rotation_days"):
        replace(row, rotation_days=0)


def test_null_result_contract_requires_a_frozen_week_band() -> None:
    with pytest.raises(CrossPoolContractError, match="frozen band"):
        DtwNullResult(
            week_start_timestamp_ms=MONDAY_START_MS,
            direction="bsc_to_base",
            band_steps=2,
            rotation_days=1,
            path_length=2,
            total_cost=0.0,
            normalized_cost=0.0,
            observed_normalized_cost=0.0,
            observed_cost_improvement=0.0,
            median_signed_lag_steps=0.0,
            observed_median_signed_lag_steps=0.0,
            observed_signed_lag_difference_steps=0.0,
        )


def test_stability_uses_equal_week_weight_and_accepts_exact_two_thirds() -> None:
    rows = _stability_rows(
        primary_lags=(1.0, 1.0, -1.0),
        band_1_lags=(1.0, 1.0, 1.0),
        band_16_lags=(1.0, 1.0, 1.0),
    )

    stability = assess_dtw_stability(rows, DtwConfig())

    assert dict(stability.aggregate_median_lag_by_band) == {
        1: 1.0,
        4: 1.0,
        16: 1.0,
    }
    assert stability.primary_band_same_sign_week_share == 2.0 / 3.0
    assert stability.band_unstable is False
    assert isinstance(stability.aggregate_median_lag_by_band, MappingProxyType)
    assert isinstance(stability.weekly_median_lags_by_band, MappingProxyType)


def test_stability_flags_below_two_thirds_primary_week_agreement() -> None:
    rows = _stability_rows(
        primary_lags=(1.0, 1.0, 1.0, -1.0, -1.0),
        band_1_lags=(1.0, 1.0, 1.0, 1.0, 1.0),
        band_16_lags=(1.0, 1.0, 1.0, 1.0, 1.0),
    )

    stability = assess_dtw_stability(rows, DtwConfig())

    assert stability.aggregate_median_lag_by_band[4] == 1.0
    assert stability.primary_band_same_sign_week_share == 3.0 / 5.0
    assert stability.band_unstable is True


def test_stability_flags_opposite_band_signs() -> None:
    rows = _stability_rows(
        primary_lags=(1.0,),
        band_1_lags=(1.0,),
        band_16_lags=(-1.0,),
    )

    stability = assess_dtw_stability(rows, DtwConfig())

    assert dict(stability.aggregate_median_lag_by_band) == {
        1: 1.0,
        4: 1.0,
        16: -1.0,
    }
    assert stability.primary_band_same_sign_week_share == 1.0
    assert stability.band_unstable is True


def test_zero_primary_aggregate_is_unstable_without_manufactured_sign() -> None:
    rows = _stability_rows(
        primary_lags=(0.0,),
        band_1_lags=(0.0,),
        band_16_lags=(0.0,),
    )

    stability = assess_dtw_stability(rows, DtwConfig())

    assert stability.aggregate_median_lag_by_band[4] == 0.0
    assert stability.primary_band_same_sign_week_share == 0.0
    assert stability.band_unstable is True


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("empty", "at least one"),
        ("missing_band", "complete frozen band matrix"),
        ("duplicate", "unique"),
        ("unsorted", "canonical ordering"),
        ("mixed_direction", "one direction"),
        ("week_gap", "consecutive complete weeks"),
    ),
)
def test_stability_rejects_incomplete_or_ambiguous_week_matrices(
    mutation: str,
    match: str,
) -> None:
    rows = list(
        _stability_rows(
            primary_lags=(1.0, 1.0),
            band_1_lags=(1.0, 1.0),
            band_16_lags=(1.0, 1.0),
        )
    )
    if mutation == "empty":
        rows = []
    elif mutation == "missing_band":
        del rows[2]
    elif mutation == "duplicate":
        rows.insert(1, rows[0])
    elif mutation == "unsorted":
        rows[0], rows[1] = rows[1], rows[0]
    elif mutation == "mixed_direction":
        rows[0] = replace(rows[0], direction="base_to_bsc")
    elif mutation == "week_gap":
        rows[3:] = [
            replace(
                row,
                week_start_timestamp_ms=row.week_start_timestamp_ms + WEEK_MS,
            )
            for row in rows[3:]
        ]
    else:  # pragma: no cover - the parameter table is exhaustive.
        raise AssertionError(mutation)

    with pytest.raises(CrossPoolContractError, match=match):
        assess_dtw_stability(tuple(rows), DtwConfig())


def test_stability_contract_rejects_stale_aggregate_and_flags() -> None:
    stability = assess_dtw_stability(
        _stability_rows(
            primary_lags=(1.0, 1.0, -1.0),
            band_1_lags=(1.0, 1.0, 1.0),
            band_16_lags=(1.0, 1.0, 1.0),
        ),
        DtwConfig(),
    )

    with pytest.raises(CrossPoolContractError, match="aggregate"):
        replace(
            stability,
            aggregate_median_lag_by_band={1: 1.0, 4: -1.0, 16: 1.0},
        )
    with pytest.raises(CrossPoolContractError, match="same-sign share"):
        replace(stability, primary_band_same_sign_week_share=0.5)
    with pytest.raises(CrossPoolContractError, match="band_unstable"):
        replace(stability, band_unstable=True)


def test_stability_contract_requires_exact_frozen_band_support() -> None:
    with pytest.raises(CrossPoolContractError, match="frozen bands"):
        DtwStability(
            direction="bsc_to_base",
            aggregate_median_lag_by_band={1: 1.0, 4: 1.0},
            weekly_median_lags_by_band={
                1: ((MONDAY_START_MS, 1.0),),
                4: ((MONDAY_START_MS, 1.0),),
            },
            primary_band_same_sign_week_share=1.0,
            band_unstable=False,
        )


def _complete_lead_week(week_start_ms: int) -> tuple[PanelRow, ...]:
    source = tuple(
        0.0 if index in (0, WEEK_STEPS - 2, WEEK_STEPS - 1) else float(index % 2)
        for index in range(WEEK_STEPS)
    )
    target = (0.0,) + source[:-1]
    return tuple(
        _panel_row(
            week_start_ms + index * GRID_MS,
            base_return=target[index],
            bsc_return=source[index],
        )
        for index in range(WEEK_STEPS)
    )


def _rotation_week(week_start_ms: int) -> tuple[PanelRow, ...]:
    values = tuple(float(day - 3) for day in range(7) for _ in range(DAY_STEPS))
    return tuple(
        _panel_row(
            week_start_ms + index * GRID_MS,
            base_return=value,
            bsc_return=value,
        )
        for index, value in enumerate(values)
    )


def _stability_rows(
    *,
    primary_lags: tuple[float, ...],
    band_1_lags: tuple[float, ...],
    band_16_lags: tuple[float, ...],
) -> tuple[DtwWeekResult, ...]:
    if not (
        len(primary_lags) == len(band_1_lags) == len(band_16_lags)
    ):
        raise ValueError("stability fixtures require equal week support")
    rows: list[DtwWeekResult] = []
    for week_index in range(len(primary_lags)):
        week_start_ms = MONDAY_START_MS + week_index * WEEK_MS
        for band_steps, lags in (
            (1, band_1_lags),
            (4, primary_lags),
            (16, band_16_lags),
        ):
            rows.append(
                _week_result_with_lag(
                    week_start_ms,
                    band_steps=band_steps,
                    lag=lags[week_index],
                )
            )
    return tuple(rows)


def _week_result_with_lag(
    week_start_ms: int,
    *,
    band_steps: int,
    lag: float,
) -> DtwWeekResult:
    if lag == 0.0:
        matches = tuple((index, index) for index in range(WEEK_STEPS))
    elif lag == 1.0:
        matches = (
            (0, 0),
            (0, 1),
            *((index, index + 1) for index in range(1, WEEK_STEPS - 1)),
            (WEEK_STEPS - 1, WEEK_STEPS - 1),
        )
    elif lag == -1.0:
        matches = (
            (0, 0),
            (1, 0),
            *((index + 1, index) for index in range(1, WEEK_STEPS - 1)),
            (WEEK_STEPS - 1, WEEK_STEPS - 1),
        )
    else:
        raise ValueError("fixture lag must be -1, 0, or 1")
    return DtwWeekResult(
        week_start_timestamp_ms=week_start_ms,
        direction="bsc_to_base",
        band_steps=band_steps,
        path_length=len(matches),
        total_cost=0.0,
        normalized_cost=0.0,
        median_signed_lag_steps=lag,
        matches=matches,
    )


def _panel_row(
    timestamp_ms: int,
    *,
    base_return: float,
    bsc_return: float,
) -> PanelRow:
    block_number = timestamp_ms // GRID_MS + 1
    return PanelRow(
        timestamp_ms=timestamp_ms,
        horizon_ms=GRID_MS,
        base_state_timestamp_ms=timestamp_ms,
        bsc_state_timestamp_ms=timestamp_ms,
        base_forward_state_timestamp_ms=timestamp_ms + GRID_MS,
        bsc_forward_state_timestamp_ms=timestamp_ms + GRID_MS,
        base_lag_block_number=block_number - 1,
        bsc_lag_block_number=block_number - 1,
        base_state_block_number=block_number,
        bsc_state_block_number=block_number,
        base_forward_block_number=block_number + 1,
        bsc_forward_block_number=block_number + 1,
        base_age_ms=0,
        bsc_age_ms=0,
        base_mid=1.0,
        bsc_mid=1.0,
        base_trailing_return_bps=base_return,
        bsc_trailing_return_bps=bsc_return,
        base_minus_bsc_gap_bps=0.0,
        base_forward_return_bps=0.0,
        bsc_forward_return_bps=0.0,
        base_regime="late",
        bsc_regime="late",
    )
