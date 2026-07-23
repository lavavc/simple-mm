"""Stable economic identities for the LP parameter portfolio."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

from research.backtester.params import BacktestParams, EntryFilters, TransactionCostModel

DeclaredFamilyName = Literal["ewma", "paper", "static", "frozen", "directional"]
AllocationFamilyName = Literal[
    "ewma", "paper_exclusive", "static", "frozen", "directional"
]
DeclarationKind = Literal["parameter", "directional_policy"]

DECLARED_FAMILY_ORDER: tuple[DeclaredFamilyName, ...] = (
    "ewma",
    "paper",
    "static",
    "frozen",
    "directional",
)
ALLOCATION_FAMILY_ORDER: tuple[AllocationFamilyName, ...] = (
    "ewma",
    "paper_exclusive",
    "static",
    "frozen",
    "directional",
)


@dataclass(frozen=True)
class ParameterDeclaration:
    family: DeclaredFamilyName
    config_name: str
    params: BacktestParams
    source_constructor: str


@dataclass(frozen=True, order=True)
class SleeveAlias:
    family: DeclaredFamilyName
    config_name: str
    source_constructor: str


@dataclass(frozen=True)
class CatalogDeclaration:
    economic_id: str
    kind: DeclarationKind
    family: DeclaredFamilyName
    config_name: str
    source_constructor: str
    behavioral_fingerprint: str


@dataclass(frozen=True)
class SleeveDefinition:
    sleeve_id: str
    family: AllocationFamilyName
    config_name: str
    parameter_payload: str
    parameter_fingerprint: str
    source_constructor: str
    aliases: tuple[SleeveAlias, ...]

    def __post_init__(self) -> None:
        if not self.aliases:
            raise ValueError("canonical sleeve requires declaration aliases")
        if tuple(sorted(set(self.aliases), key=_alias_sort_key)) != self.aliases:
            raise ValueError("sleeve aliases must be unique and deterministically sorted")

    @property
    def params(self) -> BacktestParams:
        """Materialize an independent mutable parameter object for simulation."""
        payload = json.loads(self.parameter_payload)
        payload["entry_filters"] = EntryFilters(**payload["entry_filters"])
        payload["transaction_costs"] = TransactionCostModel(**payload["transaction_costs"])
        return BacktestParams(**payload)


@dataclass(frozen=True)
class DirectionalPolicyDefinition:
    sleeve_id: str
    family: Literal["directional"]
    profile: str
    archetype_membership: tuple[tuple[str, SleeveDefinition], ...]

    @property
    def archetypes(self) -> Mapping[str, SleeveDefinition]:
        return MappingProxyType(dict(self.archetype_membership))

    @property
    def behavioral_fingerprint(self) -> str:
        payload = {
            "profile": self.profile,
            "archetypes": {
                name: sleeve.parameter_fingerprint
                for name, sleeve in self.archetype_membership
            },
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class PortfolioCatalog:
    pool: str
    sleeves: tuple[SleeveDefinition, ...]
    directional_policies: tuple[DirectionalPolicyDefinition, ...]

    def __post_init__(self) -> None:
        if not self.pool:
            raise ValueError("portfolio catalog requires a pool")
        sleeve_ids = [sleeve.sleeve_id for sleeve in self.sleeves]
        policy_ids = [policy.sleeve_id for policy in self.directional_policies]
        identities = sleeve_ids + policy_ids
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate economic ID in portfolio catalog")
        if tuple(sorted(self.sleeves, key=_sleeve_sort_key)) != self.sleeves:
            raise ValueError("portfolio sleeves must be deterministically sorted")
        if tuple(sorted(self.directional_policies, key=lambda item: item.profile)) != (
            self.directional_policies
        ):
            raise ValueError("directional policies must be deterministically sorted")
        prefix = f"{self.pool}:"
        if any(not identity.startswith(prefix) for identity in identities):
            raise ValueError("economic IDs must be scoped to the catalog pool")

    @property
    def family_names(self) -> tuple[AllocationFamilyName, ...]:
        observed = {unit.family for unit in self.allocation_units}
        return tuple(family for family in ALLOCATION_FAMILY_ORDER if family in observed)

    @property
    def allocation_units(
        self,
    ) -> tuple[SleeveDefinition | DirectionalPolicyDefinition, ...]:
        """Return the complete and exclusive set of allocator-facing units."""
        return self.sleeves + self.directional_policies

    @property
    def declarations(self) -> tuple[CatalogDeclaration, ...]:
        rows = [
            CatalogDeclaration(
                economic_id=sleeve.sleeve_id,
                kind="parameter",
                family=alias.family,
                config_name=alias.config_name,
                source_constructor=alias.source_constructor,
                behavioral_fingerprint=sleeve.parameter_fingerprint,
            )
            for sleeve in self.sleeves
            for alias in sleeve.aliases
        ]
        rows.extend(
            CatalogDeclaration(
                economic_id=policy.sleeve_id,
                kind="directional_policy",
                family="directional",
                config_name=policy.profile,
                source_constructor="directional_policy_profiles",
                behavioral_fingerprint=policy.behavioral_fingerprint,
            )
            for policy in self.directional_policies
        )
        return tuple(sorted(rows, key=_declaration_sort_key))


def _canonical_parameter_payload(params: BacktestParams) -> str:
    return json.dumps(
        asdict(params),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def parameter_fingerprint(params: BacktestParams) -> str:
    """Return a stable SHA-256 identity for the complete parameter contract."""
    serialized = _canonical_parameter_payload(params).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _alias_sort_key(alias: SleeveAlias) -> tuple[int, str, str]:
    return (
        DECLARED_FAMILY_ORDER.index(alias.family),
        alias.source_constructor,
        alias.config_name,
    )


def _sleeve_sort_key(sleeve: SleeveDefinition) -> tuple[int, str]:
    return ALLOCATION_FAMILY_ORDER.index(sleeve.family), sleeve.sleeve_id


def _declaration_sort_key(
    declaration: CatalogDeclaration,
) -> tuple[int, str, str, str]:
    return (
        DECLARED_FAMILY_ORDER.index(declaration.family),
        declaration.source_constructor,
        declaration.config_name,
        declaration.economic_id,
    )


def _allocation_owner(
    aliases: tuple[SleeveAlias, ...],
) -> tuple[AllocationFamilyName, DeclaredFamilyName]:
    families = {alias.family for alias in aliases}
    if len(families) == 1:
        declared_family = next(iter(families))
        if declared_family == "paper":
            return "paper_exclusive", "paper"
        if declared_family == "directional":
            return "directional", "directional"
        return declared_family, declared_family
    if families == {"paper", "frozen"}:
        return "frozen", "frozen"
    raise ValueError(
        "unapproved cross-family duplicate: "
        + ", ".join(sorted(families))
    )


def canonicalize_parameter_declarations(
    pool: str,
    declarations: Sequence[ParameterDeclaration],
) -> tuple[SleeveDefinition, ...]:
    """Collapse exact parameter aliases into pool-scoped economic sleeves."""
    if not pool:
        raise ValueError("canonical economic IDs require a pool")
    grouped: dict[str, list[tuple[ParameterDeclaration, str]]] = {}
    for declaration in declarations:
        payload = _canonical_parameter_payload(declaration.params)
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        grouped.setdefault(fingerprint, []).append((declaration, payload))

    sleeves: list[SleeveDefinition] = []
    for fingerprint, members in grouped.items():
        payloads = {payload for _, payload in members}
        if len(payloads) != 1:
            raise ValueError("parameter fingerprint collision")
        aliases = tuple(
            sorted(
                {
                    SleeveAlias(
                        declaration.family,
                        declaration.config_name,
                        declaration.source_constructor,
                    )
                    for declaration, _ in members
                },
                key=_alias_sort_key,
            )
        )
        allocation_family, owner_family = _allocation_owner(aliases)
        owner_aliases = [alias for alias in aliases if alias.family == owner_family]
        if not owner_aliases:
            raise ValueError("canonical sleeve owner is missing its declaration")
        owner = owner_aliases[0]
        sleeves.append(
            SleeveDefinition(
                sleeve_id=f"{pool}:parameter:{fingerprint}",
                family=allocation_family,
                config_name=owner.config_name,
                parameter_payload=next(iter(payloads)),
                parameter_fingerprint=fingerprint,
                source_constructor=owner.source_constructor,
                aliases=aliases,
            )
        )
    return tuple(sorted(sleeves, key=_sleeve_sort_key))


def _directional_component_definitions(
    pool: str,
    profiles: Mapping[str, Mapping[str, object]],
) -> dict[str, SleeveDefinition]:
    by_fingerprint: dict[str, tuple[str, BacktestParams, set[SleeveAlias]]] = {}
    for archetype_configs in profiles.values():
        for config in archetype_configs.values():
            name = str(getattr(config, "name"))
            params = getattr(config, "params")
            if not isinstance(params, BacktestParams):
                raise TypeError("directional archetype config requires BacktestParams")
            payload = _canonical_parameter_payload(params)
            fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            alias = SleeveAlias(
                "directional", name, "directional_archetype_configs"
            )
            existing = by_fingerprint.get(fingerprint)
            if existing is None:
                by_fingerprint[fingerprint] = (payload, params, {alias})
            else:
                existing[2].add(alias)

    definitions: dict[str, SleeveDefinition] = {}
    for fingerprint, (payload, _params, aliases) in by_fingerprint.items():
        ordered_aliases = tuple(sorted(aliases, key=_alias_sort_key))
        owner = ordered_aliases[0]
        definitions[fingerprint] = SleeveDefinition(
            sleeve_id=f"{pool}:directional-component:{fingerprint}",
            family="directional",
            config_name=owner.config_name,
            parameter_payload=payload,
            parameter_fingerprint=fingerprint,
            source_constructor=owner.source_constructor,
            aliases=ordered_aliases,
        )
    return definitions


def build_portfolio_catalog(pool: str, bankroll_usd: float) -> PortfolioCatalog:
    """Build the frozen canonical catalog from authoritative constructors."""
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

    declarations: list[ParameterDeclaration] = []
    declarations.extend(
        ParameterDeclaration("ewma", f"ewma_{index:04d}", params, "generate_grid")
        for index, params in enumerate(generate_grid(initial_capital_usd=bankroll_usd))
    )
    declarations.extend(
        ParameterDeclaration(
            "paper", f"paper_{index:04d}", params, "generate_paper_grid"
        )
        for index, params in enumerate(
            generate_paper_grid(initial_capital_usd=bankroll_usd)
        )
    )
    declarations.extend(
        ParameterDeclaration("static", name, params, "static_lp_configs")
        for name, params in static_lp_configs(
            initial_capital_usd=bankroll_usd,
            mint_gas_usd=None,
            remove_gas_usd=None,
        )
    )
    declarations.extend(
        ParameterDeclaration("static", name, params, "static_lp_closed_configs")
        for name, params in static_lp_closed_configs(
            initial_capital_usd=bankroll_usd,
            mint_gas_usd=None,
            remove_gas_usd=None,
        )
    )
    declarations.extend(
        ParameterDeclaration("frozen", name, params, "frozen_paper_configs")
        for name, params in frozen_paper_configs(
            initial_capital_usd=bankroll_usd,
            mint_gas_usd=None,
            remove_gas_usd=None,
        )
    )
    sleeves = canonicalize_parameter_declarations(pool, declarations)

    directional_configs = directional_archetype_configs(
        initial_capital_usd=bankroll_usd,
        mint_gas_usd=None,
        remove_gas_usd=None,
    )
    profiles = directional_policy_profiles(directional_configs)
    components = _directional_component_definitions(pool, profiles)
    directional_policies: list[DirectionalPolicyDefinition] = []
    for profile in sorted(profiles):
        archetype_configs = profiles[profile]
        archetype_membership = []
        for archetype in sorted(archetype_configs):
            config = archetype_configs[archetype]
            fingerprint = parameter_fingerprint(config.params)
            archetype_membership.append((archetype, components[fingerprint]))
        directional_policies.append(
            DirectionalPolicyDefinition(
                sleeve_id=f"{pool}:directional:{profile}",
                family="directional",
                profile=profile,
                archetype_membership=tuple(archetype_membership),
            )
        )

    return PortfolioCatalog(
        pool=pool,
        sleeves=sleeves,
        directional_policies=tuple(directional_policies),
    )
