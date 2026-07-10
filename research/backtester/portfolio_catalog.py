"""Stable identities for the LP parameter sleeves used by portfolio research."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, Mapping

from research.backtester.params import BacktestParams

FamilyName = Literal["ewma", "paper", "static", "frozen", "directional"]


@dataclass(frozen=True)
class SleeveDefinition:
    sleeve_id: str
    family: FamilyName
    config_name: str
    params: BacktestParams
    parameter_fingerprint: str
    source_constructor: str


@dataclass(frozen=True)
class DirectionalPolicyDefinition:
    sleeve_id: str
    family: Literal["directional"]
    profile: str
    archetypes: Mapping[str, SleeveDefinition]


@dataclass(frozen=True)
class PortfolioCatalog:
    pool: str
    sleeves: tuple[SleeveDefinition, ...]
    directional_policies: tuple[DirectionalPolicyDefinition, ...]

    def __post_init__(self) -> None:
        sleeve_ids = [sleeve.sleeve_id for sleeve in self.sleeves]
        policy_ids = [policy.sleeve_id for policy in self.directional_policies]
        identities = sleeve_ids + policy_ids
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate sleeve_id in portfolio catalog")

    @property
    def family_names(self) -> tuple[str, ...]:
        return tuple(sorted({sleeve.family for sleeve in self.sleeves}))


def parameter_fingerprint(params: BacktestParams) -> str:
    """Return a stable SHA-256 identity for the complete parameter contract."""
    serialized = json.dumps(
        asdict(params),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def build_portfolio_catalog(pool: str, bankroll_usd: float) -> PortfolioCatalog:
    """Build a deterministic catalog from the authoritative family constructors."""
    from research.backtester.params import generate_grid, generate_paper_grid
    from research.scripts.evaluate_directional_paper_lp import (
        directional_archetype_configs,
        directional_policy_profiles,
    )
    from research.scripts.evaluate_frozen_family_lp import (
        frozen_paper_configs,
        static_lp_closed_configs,
        static_lp_configs,
    )

    sleeves: list[SleeveDefinition] = []
    by_family_fingerprint: dict[tuple[FamilyName, str], SleeveDefinition] = {}

    def add_sleeve(
        family: FamilyName,
        config_name: str,
        params: BacktestParams,
        source_constructor: str,
    ) -> SleeveDefinition:
        fingerprint = parameter_fingerprint(params)
        key = (family, fingerprint)
        existing = by_family_fingerprint.get(key)
        if existing is not None:
            return existing
        sleeve = SleeveDefinition(
            sleeve_id=f"{family}:{fingerprint}",
            family=family,
            config_name=config_name,
            params=params,
            parameter_fingerprint=fingerprint,
            source_constructor=source_constructor,
        )
        by_family_fingerprint[key] = sleeve
        sleeves.append(sleeve)
        return sleeve

    for index, params in enumerate(generate_grid(initial_capital_usd=bankroll_usd)):
        add_sleeve("ewma", f"ewma_{index:04d}", params, "generate_grid")
    for index, params in enumerate(generate_paper_grid(initial_capital_usd=bankroll_usd)):
        add_sleeve("paper", f"paper_{index:04d}", params, "generate_paper_grid")

    for name, params in static_lp_configs(
        initial_capital_usd=bankroll_usd,
        mint_gas_usd=None,
        remove_gas_usd=None,
    ):
        add_sleeve("static", name, params, "static_lp_configs")
    for name, params in static_lp_closed_configs(
        initial_capital_usd=bankroll_usd,
        mint_gas_usd=None,
        remove_gas_usd=None,
    ):
        add_sleeve("static", name, params, "static_lp_closed_configs")
    for name, params in frozen_paper_configs(
        initial_capital_usd=bankroll_usd,
        mint_gas_usd=None,
        remove_gas_usd=None,
    ):
        add_sleeve("frozen", name, params, "frozen_paper_configs")

    directional_configs = directional_archetype_configs(
        initial_capital_usd=bankroll_usd,
        mint_gas_usd=None,
        remove_gas_usd=None,
    )
    profiles = directional_policy_profiles(directional_configs)
    directional_policies: list[DirectionalPolicyDefinition] = []
    for profile, archetype_configs in profiles.items():
        archetypes = {
            archetype: add_sleeve(
                "directional",
                config.name,
                config.params,
                "directional_archetype_configs",
            )
            for archetype, config in archetype_configs.items()
        }
        directional_policies.append(
            DirectionalPolicyDefinition(
                sleeve_id=f"directional-policy:{profile}",
                family="directional",
                profile=profile,
                archetypes=archetypes,
            )
        )

    return PortfolioCatalog(
        pool=pool,
        sleeves=tuple(sleeves),
        directional_policies=tuple(directional_policies),
    )
