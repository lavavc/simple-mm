"""Immutable bytes for one challenger evidence artifact."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from research.cross_pool.contracts import CrossPoolContractError

CHALLENGER_ARTIFACT_NAMES = (
    "challenger_predictions.csv",
    "challenger_fold_audits.csv",
    "challenger_metrics.csv",
    "challenger_contrasts.csv",
    "challenger_diagnostics.json",
    "challenger_report.md",
    "model_error_comparison.png",
    "source_price_contrasts.png",
    "freshness_sensitivity.png",
    "two_part_calibration.png",
    "loss_dependence.png",
)


@dataclass(frozen=True)
class ChallengerArtifact:
    relative_name: str
    content: bytes

    def __post_init__(self) -> None:
        if (
            not self.relative_name
            or self.relative_name.startswith(("/", "\\"))
            or "/" in self.relative_name
            or "\\" in self.relative_name
        ):
            raise CrossPoolContractError("challenger artifact name must be one filename")
        if not isinstance(self.content, bytes) or not self.content:
            raise CrossPoolContractError("challenger artifact bytes must be nonempty")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()
