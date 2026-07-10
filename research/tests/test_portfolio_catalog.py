from __future__ import annotations

from dataclasses import asdict, replace
from types import MappingProxyType

import pytest

from research.backtester.params import BacktestParams, EntryFilters, TransactionCostModel
from research.backtester.portfolio_catalog import build_portfolio_catalog, parameter_fingerprint


def test_only_directional_profiles_are_allocatable_directional_units() -> None:
    catalog = build_portfolio_catalog("uni-base", bankroll_usd=1200.0)

    assert set(catalog.family_names) == {"ewma", "paper", "static", "frozen", "directional"}
    assert all(unit.family != "directional" for unit in catalog.sleeves)
    assert tuple(catalog.allocation_units) == catalog.sleeves + catalog.directional_policies
    assert {
        unit.sleeve_id for unit in catalog.allocation_units if unit.family == "directional"
    } == {
        "directional:balanced_v1",
        "directional:upside_wide_v1",
        "directional:upside_tight_v1",
        "directional:dip_wide_v1",
        "directional:fee_tight_v1",
    }
    assert {policy.profile for policy in catalog.directional_policies} == {
        "balanced_v1",
        "upside_wide_v1",
        "upside_tight_v1",
        "dip_wide_v1",
        "fee_tight_v1",
    }


def test_parameter_fingerprint_is_stable_for_exact_copies() -> None:
    params = BacktestParams(strategy_mode="static", fixed_width_pct=0.01)

    fingerprint = parameter_fingerprint(params)

    assert fingerprint == parameter_fingerprint(replace(params))


def test_directional_catalog_deduplicates_components_by_reference() -> None:
    catalog = build_portfolio_catalog("uni-base", bankroll_usd=1200.0)
    components = [
        sleeve
        for policy in catalog.directional_policies
        for sleeve in policy.archetypes.values()
    ]
    by_fingerprint: dict[str, list[object]] = {}
    for component in components:
        by_fingerprint.setdefault(component.parameter_fingerprint, []).append(component)

    assert len(components) == 15
    assert len(by_fingerprint) == 7
    assert all(component not in catalog.allocation_units for component in components)
    assert all(
        all(component is references[0] for component in references)
        for references in by_fingerprint.values()
    )


def test_catalog_records_do_not_retain_mutable_parameter_or_route_state() -> None:
    catalog = build_portfolio_catalog("uni-base", bankroll_usd=1200.0)
    sleeve = catalog.sleeves[0]
    first = sleeve.params
    second = sleeve.params

    assert first is not second
    assert first.entry_filters is not second.entry_filters
    assert first.transaction_costs is not second.transaction_costs
    first.entry_filters.min_swap_volume_usd = 123.0
    first.transaction_costs.close_position_on_end = True
    assert sleeve.params.entry_filters.min_swap_volume_usd is None
    assert sleeve.params.transaction_costs.close_position_on_end is False

    policy = catalog.directional_policies[0]
    assert isinstance(policy.archetypes, MappingProxyType)
    with pytest.raises(TypeError):
        policy.archetypes["replacement"] = sleeve  # type: ignore[index]


def test_fingerprint_covers_nested_parameter_contracts() -> None:
    params = BacktestParams(
        entry_filters=EntryFilters(min_swap_volume_usd=100.0),
        transaction_costs=TransactionCostModel(close_position_on_end=True),
    )

    assert parameter_fingerprint(params) != parameter_fingerprint(
        replace(params, entry_filters=replace(params.entry_filters, min_swap_volume_usd=200.0))
    )
    assert parameter_fingerprint(params) != parameter_fingerprint(
        replace(
            params,
            transaction_costs=replace(params.transaction_costs, close_position_on_end=False),
        )
    )


def test_catalog_counts_and_output_are_deterministic() -> None:
    first = build_portfolio_catalog("uni-base", bankroll_usd=1200.0)
    second = build_portfolio_catalog("uni-base", bankroll_usd=1200.0)

    assert len(first.sleeves) == 4930
    assert len(first.allocation_units) == 4935
    assert len(first.directional_policies) == 5
    assert asdict(first) == asdict(second)
    assert all(
        set(policy.archetypes) == {"upside_capture", "dip_accumulator", "fee_box"}
        for policy in first.directional_policies
    )
    assert all(
        sleeve.family == "directional"
        for policy in first.directional_policies
        for sleeve in policy.archetypes.values()
    )
