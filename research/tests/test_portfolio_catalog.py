from __future__ import annotations

from dataclasses import asdict, replace

from research.backtester.params import BacktestParams, EntryFilters, TransactionCostModel
from research.backtester.portfolio_catalog import build_portfolio_catalog, parameter_fingerprint


def test_catalog_uses_existing_families_without_directional_component_funding() -> None:
    catalog = build_portfolio_catalog("uni-base", bankroll_usd=1200.0)

    assert set(catalog.family_names) == {"ewma", "paper", "static", "frozen", "directional"}
    assert len({s.sleeve_id for s in catalog.sleeves}) == len(catalog.sleeves)
    assert all(s.family != "directional_component" for s in catalog.sleeves)
    assert {p.profile for p in catalog.directional_policies} == {
        "balanced_v1",
        "upside_wide_v1",
        "upside_tight_v1",
        "dip_wide_v1",
        "fee_tight_v1",
    }


def test_exact_duplicates_share_one_underlying_sleeve() -> None:
    params = BacktestParams(strategy_mode="static", fixed_width_pct=0.01)

    fingerprint = parameter_fingerprint(params)

    assert fingerprint == parameter_fingerprint(replace(params))


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

    assert len(first.sleeves) == 4937
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
