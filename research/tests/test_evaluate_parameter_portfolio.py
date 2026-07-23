from __future__ import annotations

import json
from dataclasses import asdict, replace

import pytest

import research.scripts.evaluate_parameter_portfolio as orchestration
from research.backtester.entry_eligibility import AlwaysEligibleOverlay
from research.backtester.params import BacktestParams
from research.backtester.portfolio_allocation import Allocation
from research.backtester.portfolio_catalog import (
    DirectionalPolicyDefinition,
    PortfolioCatalog,
    SleeveAlias,
    SleeveDefinition,
    build_portfolio_catalog,
    parameter_fingerprint,
)
from research.backtester.portfolio_evaluation import route_directional_catalog
from research.backtester.portfolio_publication import REQUIRED_ARTIFACTS
from research.backtester.portfolio_simulator import simulate_portfolio
from research.backtester.simulator import UNISWAP_BASE_POOL
from research.scripts.evaluate_parameter_portfolio import (
    build_parser,
    validate_cli_args,
)


def _sleeve(sleeve_id: str, family: str, config_name: str) -> SleeveDefinition:
    params = BacktestParams(initial_capital_usd=100.0)
    return SleeveDefinition(
        sleeve_id=sleeve_id,
        family=family,  # type: ignore[arg-type]
        config_name=config_name,
        parameter_payload=json.dumps(
            asdict(params),
            separators=(",", ":"),
            sort_keys=True,
        ),
        parameter_fingerprint=parameter_fingerprint(params),
        source_constructor="test",
        aliases=(
            SleeveAlias(
                "paper" if family == "paper_exclusive" else family,  # type: ignore[arg-type]
                config_name,
                "test",
            ),
        ),
    )


@pytest.fixture
def catalog() -> PortfolioCatalog:
    static = _sleeve("uni-base:static:a", "static", "static_a")
    upside = _sleeve(
        "uni-base:directional:raw-up",
        "directional",
        "upside_capture_tight",
    )
    policy = DirectionalPolicyDefinition(
        sleeve_id="uni-base:directional:upside_tight_v1",
        family="directional",
        profile="upside_tight_v1",
        archetype_membership=(
            ("upside_capture", upside),
            (
                "dip_accumulator",
                replace(
                    upside,
                    sleeve_id="uni-base:directional:raw-dip",
                    config_name="dip_accumulator_tight",
                ),
            ),
            (
                "fee_box",
                replace(
                    upside,
                    sleeve_id="uni-base:directional:raw-fee",
                    config_name="fee_box_tight",
                ),
            ),
        ),
    )
    return PortfolioCatalog("uni-base", (static,), (policy,))


def test_directional_policy_funds_only_the_causally_routed_archetype(
    catalog: PortfolioCatalog,
) -> None:
    routed = route_directional_catalog(
        catalog,
        {
            "gate_strict_sign_cone": "1",
            "entry_predicted_markout_20_25": "0.01",
        },
    )
    by_id = {sleeve.sleeve_id: sleeve for sleeve in routed.sleeves}

    assert by_id[
        "uni-base:directional:upside_tight_v1"
    ].config_name.startswith("upside_capture_")
    assert "uni-base:directional:raw-up" not in by_id
    assert routed.directional_policies == ()

    with pytest.raises(ValueError, match="entry overlays reference unknown sleeves"):
        simulate_portfolio(
            events=[],
            sleeves=routed.sleeves,
            allocation=Allocation("equal_config", {}, 1.0),
            pool_config=UNISWAP_BASE_POOL,
            bankroll_usd=100.0,
            settle_to_cash=False,
            entry_overlays_by_sleeve={
                "uni-base:directional:raw-up": AlwaysEligibleOverlay()
            },
        )


def test_no_position_omits_directional_policy(catalog: PortfolioCatalog) -> None:
    routed = route_directional_catalog(catalog, {})

    assert [sleeve.sleeve_id for sleeve in routed.sleeves] == [
        "uni-base:static:a"
    ]


def test_full_run_rejects_all_truncation_flags() -> None:
    parser = build_parser()
    for flag in (("--max-windows", "2"), ("--catalog-limit-per-family", "2")):
        args = parser.parse_args(("--full-run", *flag))
        with pytest.raises(ValueError, match="full run forbids truncation"):
            validate_cli_args(args)


def test_smoke_catalog_limit_retains_the_frozen_static_comparator() -> None:
    limited = orchestration._limit_catalog(
        build_portfolio_catalog("uni-base", 100.0),
        1,
    )

    static = [unit for unit in limited.sleeves if unit.family == "static"]
    assert [unit.config_name for unit in static] == ["static_spot_w0025"]


def test_publication_contract_has_fourteen_artifacts() -> None:
    assert len(REQUIRED_ARTIFACTS) == 14
    assert REQUIRED_ARTIFACTS[0] == "run_manifest.json"
    assert REQUIRED_ARTIFACTS[-1] == "summary.md"
