"""Explicit registry for non-account portfolio exposure sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PortfolioSourceKind = Literal["lp_position", "exchange"]


@dataclass(frozen=True, slots=True)
class PortfolioSourceDescriptor:
    """One additive non-account source in the global portfolio view."""

    venue: str
    source_kind: PortfolioSourceKind


DEFAULT_PORTFOLIO_SOURCE_REGISTRY: tuple[PortfolioSourceDescriptor, ...] = (
    PortfolioSourceDescriptor("uni-base", "lp_position"),
    PortfolioSourceDescriptor("uni-bsc", "lp_position"),
    PortfolioSourceDescriptor("quidax", "exchange"),
)
