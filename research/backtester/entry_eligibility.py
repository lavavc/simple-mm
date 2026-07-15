"""Generic entry-eligibility decisions for research backtests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from research.backtester.sizing import EntryContext


@dataclass(frozen=True)
class EntryEligibilityDecision:
    eligible: bool
    reason: Literal["agreement", "disagreement", "unconditional"]


class EntryEligibilityOverlay(Protocol):
    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision: ...


@dataclass(frozen=True)
class AlwaysEligibleOverlay:
    def evaluate(self, context: EntryContext) -> EntryEligibilityDecision:
        return EntryEligibilityDecision(eligible=True, reason="unconditional")
