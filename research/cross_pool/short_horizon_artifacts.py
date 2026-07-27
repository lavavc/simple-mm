"""Narrow immutable artifact bytes for the short-horizon evidence package."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from research.cross_pool.contracts import CrossPoolContractError

SHORT_HORIZON_ARTIFACT_NAMES = (
    "short_horizon_events.csv",
    "short_horizon_summaries.csv",
    "short_horizon_qa.json",
    "short_horizon_report.md",
    "short_horizon_response.png",
    "short_horizon_updates.png",
    "short_horizon_fee_gap.png",
)


@dataclass(frozen=True)
class ShortHorizonArtifact:
    relative_name: str
    content: bytes

    def __post_init__(self) -> None:
        if (
            not self.relative_name
            or self.relative_name.startswith(("/", "\\"))
            or "/" in self.relative_name
            or "\\" in self.relative_name
        ):
            raise CrossPoolContractError("artifact name must be one relative filename")
        if not isinstance(self.content, bytes) or not self.content:
            raise CrossPoolContractError("artifact content must be nonempty bytes")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()
