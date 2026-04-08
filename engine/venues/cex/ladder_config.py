"""Helpers for CEX ladder parameter compatibility and serialization."""

from __future__ import annotations

from typing import Any, Literal


def offsets_form_uniform_ladder(offsets: list[int]) -> bool:
    """Return whether the offsets describe a strictly increasing uniform ladder."""
    if len(offsets) <= 1:
        return True
    deltas = [curr - prev for prev, curr in zip(offsets, offsets[1:])]
    return bool(deltas) and len(set(deltas)) == 1 and deltas[0] > 0


def hydrate_ladder_fields_from_legacy_offsets(
    *,
    spread_offset_ngn: int,
    ladder_step_ngn: int,
    ladder_levels_per_side: int,
    legacy_offsets: list[int] | None,
    provided_fields: set[str],
) -> tuple[int, int, int, list[int] | None]:
    """Hydrate new ladder fields from legacy offsets without losing custom ladders."""
    offsets = [int(offset) for offset in (legacy_offsets or [])]
    if not offsets:
        return spread_offset_ngn, ladder_step_ngn, ladder_levels_per_side, legacy_offsets

    new_fields = {"spread_offset_ngn", "ladder_step_ngn", "ladder_levels_per_side"}
    if provided_fields.intersection(new_fields):
        return spread_offset_ngn, ladder_step_ngn, ladder_levels_per_side, None

    hydrated_spread = offsets[0]
    hydrated_levels = len(offsets)
    hydrated_step = ladder_step_ngn
    preserved_offsets: list[int] | None = offsets

    if offsets_form_uniform_ladder(offsets):
        if len(offsets) > 1:
            hydrated_step = offsets[1] - offsets[0]
        preserved_offsets = None

    return hydrated_spread, hydrated_step, hydrated_levels, preserved_offsets


def resolve_ladder_offsets(
    *,
    spread_offset_ngn: int,
    ladder_step_ngn: int,
    ladder_levels_per_side: int,
    legacy_offsets: list[int] | None,
) -> list[int]:
    """Resolve the effective ladder offsets for runtime use."""
    offsets = [int(offset) for offset in (legacy_offsets or [])]
    if offsets:
        return offsets
    return [
        spread_offset_ngn + (index * ladder_step_ngn)
        for index in range(ladder_levels_per_side)
    ]


def cex_params_payload(
    *,
    base_payload: dict[str, Any],
    legacy_offsets: list[int] | None,
    mode: Literal["python", "json"] = "python",
) -> dict[str, Any]:
    """Serialize CEX params while preserving legacy custom ladders honestly."""
    del mode  # Serialization mode is already applied to the base payload.

    payload = dict(base_payload)
    offsets = [int(offset) for offset in (legacy_offsets or [])]
    if not offsets:
        return payload

    payload["ladder_offsets_ngn"] = offsets
    if not offsets_form_uniform_ladder(offsets):
        payload.pop("spread_offset_ngn", None)
        payload.pop("ladder_step_ngn", None)
        payload.pop("ladder_levels_per_side", None)
    return payload
